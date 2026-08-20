"""Compact, deterministic storage for exact formal WorldSnapshot boundaries."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    CanonFact, Chapter, Character, CharacterKnowledge, CharacterMemory, Project,
    ProjectWorldSnapshotHead, RevealConstraint, Scene, SnapshotStorageMode,
    SnapshotType, StoryArc, StoryThread, WorldEntity, WorldSnapshot,
)


COMPACT_PROTOCOL = "compact-world-snapshot-v1"
COLLECTIONS = (
    "canon_facts", "world_entities", "characters", "character_knowledge",
    "character_memories", "reveal_constraints", "story_threads", "story_arcs",
    "scenes", "chapters",
)


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def snapshot_fingerprint(payload: dict[str, Any]) -> str:
    return "world-snapshot-v1:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


class SnapshotDeltaCodec:
    """Whole-row JSON UPSERT/DELETE deltas over legacy-compatible payloads."""

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(payload)
        result["project"] = _canonical(result.get("project") or {})
        for collection in COLLECTIONS:
            rows = result.get(collection) or []
            result[collection] = sorted((_canonical(row) for row in rows), key=lambda row: str(row.get("id", "")))
        return result

    def diff(self, pre_payload: dict[str, Any], post_payload: dict[str, Any]) -> dict[str, Any]:
        pre, post = self.normalize(pre_payload), self.normalize(post_payload)
        collections: dict[str, Any] = {}
        for collection in COLLECTIONS:
            before = {row["id"]: row for row in pre.get(collection, []) if row.get("id")}
            after = {row["id"]: row for row in post.get(collection, []) if row.get("id")}
            upsert = [after[key] for key in sorted(after) if before.get(key) != after[key]]
            delete = sorted(set(before) - set(after))
            if upsert or delete:
                collections[collection] = {"upsert": upsert, "delete": delete}
        return {
            "protocol": COMPACT_PROTOCOL,
            "project": post["project"] if pre.get("project") != post.get("project") else None,
            "collections": collections,
        }

    def apply(self, base_payload: dict[str, Any], delta_payload: dict[str, Any]) -> dict[str, Any]:
        if delta_payload.get("protocol") != COMPACT_PROTOCOL:
            raise ValueError("SNAPSHOT_CHAIN_INVALID")
        result = self.normalize(base_payload)
        if delta_payload.get("project") is not None:
            result["project"] = _canonical(delta_payload["project"])
        for collection, change in (delta_payload.get("collections") or {}).items():
            if collection not in COLLECTIONS or not isinstance(change, dict):
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            rows = {row["id"]: row for row in result.get(collection, []) if row.get("id")}
            for row_id in change.get("delete") or []:
                rows.pop(row_id, None)
            for row in change.get("upsert") or []:
                if not isinstance(row, dict) or not row.get("id"):
                    raise ValueError("SNAPSHOT_CHAIN_INVALID")
                rows[row["id"]] = _canonical(row)
            result[collection] = [rows[row_id] for row_id in sorted(rows)]
        return self.normalize(result)

    def fingerprint(self, *, project_id: str, mode: SnapshotStorageMode | str,
                    base_snapshot_id: str | None, base_state_fingerprint: str | None,
                    payload: dict[str, Any], schema_version: int) -> str:
        node = {
            "protocol": COMPACT_PROTOCOL, "project_id": project_id, "mode": _value(mode),
            "base_snapshot_id": base_snapshot_id, "base_state_fingerprint": base_state_fingerprint,
            "payload": _canonical(payload), "schema_version": schema_version,
        }
        return "compact-snapshot-storage-v1:" + hashlib.sha256(
            json.dumps(node, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()


class SnapshotPayloadResolver:
    codec = SnapshotDeltaCodec()

    def materialize(self, db: Session, snapshot_or_id: WorldSnapshot | str) -> dict[str, Any]:
        snapshot = db.get(WorldSnapshot, snapshot_or_id) if isinstance(snapshot_or_id, str) else snapshot_or_id
        if not snapshot:
            raise ValueError("SNAPSHOT_BASE_MISSING")
        nodes: list[WorldSnapshot] = []
        seen: set[str] = set()
        current = snapshot
        while True:
            if current.id in seen:
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            seen.add(current.id)
            self.validate_node_local(db, current)
            nodes.append(current)
            mode = _value(current.storage_mode)
            if mode == SnapshotStorageMode.LEGACY_FULL.value:
                # v1 is deliberately read as it was stored.  Existing legacy
                # rows are not rewritten merely because compact storage exists.
                result = copy.deepcopy(current.payload)
                break
            if mode == SnapshotStorageMode.COMPACT_ANCHOR.value:
                result = self.codec.normalize(current.payload)
                break
            if mode not in {SnapshotStorageMode.COMPACT_DELTA.value, SnapshotStorageMode.REFERENCE.value}:
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            if not current.base_snapshot_id:
                raise ValueError("SNAPSHOT_BASE_MISSING")
            base = db.get(WorldSnapshot, current.base_snapshot_id)
            if not base:
                raise ValueError("SNAPSHOT_BASE_MISSING")
            if base.project_id != current.project_id:
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            current = base
        for node in reversed(nodes[:-1]):
            mode = _value(node.storage_mode)
            if mode == SnapshotStorageMode.COMPACT_DELTA.value:
                result = self.codec.apply(result, node.payload)
            elif mode == SnapshotStorageMode.REFERENCE.value:
                pass
        result = self.codec.normalize(result) if _value(nodes[-1].storage_mode) != SnapshotStorageMode.LEGACY_FULL.value else result
        if getattr(snapshot, "state_fingerprint_protocol", "world-snapshot-v1") == "formal-world-state-v2":
            from .formal_state import formal_world_state_v2_fingerprint
            valid_fingerprint = formal_world_state_v2_fingerprint(result)
        else:
            valid_fingerprint = snapshot_fingerprint(result)
        if valid_fingerprint != snapshot.state_fingerprint:
            raise ValueError("SNAPSHOT_CHAIN_INVALID")
        return result

    def validate_chain(self, db: Session, snapshot_or_id: WorldSnapshot | str) -> None:
        snapshot = db.get(WorldSnapshot, snapshot_or_id) if isinstance(snapshot_or_id, str) else snapshot_or_id
        if not snapshot:
            raise ValueError("SNAPSHOT_BASE_MISSING")
        seen: set[str] = set()
        current = snapshot
        while True:
            if current.id in seen:
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            seen.add(current.id)
            self.validate_node_local(db, current)
            mode = _value(current.storage_mode)
            if mode in {SnapshotStorageMode.LEGACY_FULL.value, SnapshotStorageMode.COMPACT_ANCHOR.value}:
                return
            if not current.base_snapshot_id:
                raise ValueError("SNAPSHOT_BASE_MISSING")
            base = db.get(WorldSnapshot, current.base_snapshot_id)
            if not base:
                raise ValueError("SNAPSHOT_BASE_MISSING")
            if base.project_id != current.project_id:
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            if mode == SnapshotStorageMode.REFERENCE.value and current.state_fingerprint != base.state_fingerprint:
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            if current.materialization_depth != base.materialization_depth + 1:
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            current = base

    def validate_node_local(self, db: Session, snapshot_or_id: WorldSnapshot | str) -> WorldSnapshot:
        """Validate one compact node and, at most, its immediate base metadata.

        This is deliberately the normal-runtime contract.  It proves that the
        current pointer is structurally safe without walking historical state;
        callers needing historical integrity must use ``validate_chain``.
        """
        snapshot = db.get(WorldSnapshot, snapshot_or_id) if isinstance(snapshot_or_id, str) else snapshot_or_id
        if not snapshot:
            raise ValueError("SNAPSHOT_BASE_MISSING")
        mode = _value(snapshot.storage_mode)
        if mode == SnapshotStorageMode.LEGACY_FULL.value:
            if snapshot_fingerprint(snapshot.payload) != snapshot.state_fingerprint:
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            return snapshot
        if mode not in {SnapshotStorageMode.COMPACT_ANCHOR.value, SnapshotStorageMode.COMPACT_DELTA.value, SnapshotStorageMode.REFERENCE.value}:
            raise ValueError("SNAPSHOT_CHAIN_INVALID")
        base = db.get(WorldSnapshot, snapshot.base_snapshot_id) if snapshot.base_snapshot_id else None
        if mode == SnapshotStorageMode.COMPACT_ANCHOR.value:
            if snapshot.base_snapshot_id or snapshot.materialization_depth != 0:
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            if getattr(snapshot, "state_fingerprint_protocol", "world-snapshot-v1") == "formal-world-state-v2":
                from .formal_state import formal_world_state_v2_fingerprint
                anchor_fingerprint = formal_world_state_v2_fingerprint(snapshot.payload)
            else:
                anchor_fingerprint = snapshot_fingerprint(snapshot.payload)
            if anchor_fingerprint != snapshot.state_fingerprint:
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            base_fingerprint = None
        else:
            if not base or base.project_id != snapshot.project_id:
                raise ValueError("SNAPSHOT_BASE_MISSING" if not base else "SNAPSHOT_CHAIN_INVALID")
            if snapshot.materialization_depth != base.materialization_depth + 1:
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            base_fingerprint = base.state_fingerprint
            if mode == SnapshotStorageMode.COMPACT_DELTA.value and snapshot.payload.get("protocol") != COMPACT_PROTOCOL:
                raise ValueError("SNAPSHOT_CHAIN_INVALID")
            if mode == SnapshotStorageMode.REFERENCE.value:
                expected_manifest = {"protocol": COMPACT_PROTOCOL, "kind": "reference", "base_snapshot_id": snapshot.base_snapshot_id}
                if snapshot.payload != expected_manifest:
                    raise ValueError("SNAPSHOT_CHAIN_INVALID")
                if snapshot.state_fingerprint != base.state_fingerprint:
                    raise ValueError("SNAPSHOT_CHAIN_INVALID")
        expected = self.codec.fingerprint(
            project_id=snapshot.project_id, mode=mode, base_snapshot_id=snapshot.base_snapshot_id,
            base_state_fingerprint=base_fingerprint, payload=snapshot.payload,
            schema_version=snapshot.schema_version,
        )
        if snapshot.storage_fingerprint != expected:
            raise ValueError("SNAPSHOT_CHAIN_INVALID")
        return snapshot

    # Compatibility for callers that used the private helper in early 16B.
    _validate_node = validate_node_local


class ProjectWorldSnapshotHeadService:
    def _fingerprint(self, *, project_id: str, snapshot: WorldSnapshot, source_type: str,
                     source_id: str | None, sequence: int | None) -> str:
        value = {"project_id": project_id, "snapshot_id": snapshot.id,
                 "state_fingerprint": snapshot.state_fingerprint, "source_type": source_type,
                 "source_id": source_id, "sequence": sequence}
        return "project-world-snapshot-head-v1:" + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

    def current_fast(self, db: Session, project_id: str) -> WorldSnapshot | None:
        """Return a locally verified current head in bounded work."""
        head = db.scalar(select(ProjectWorldSnapshotHead).where(ProjectWorldSnapshotHead.project_id == project_id))
        if not head:
            return None
        snapshot = db.get(WorldSnapshot, head.snapshot_id)
        if not snapshot or snapshot.project_id != project_id or snapshot.state_fingerprint != head.state_fingerprint:
            raise ValueError("PROJECT_SNAPSHOT_HEAD_INVALID")
        if head.head_fingerprint != self._fingerprint(project_id=project_id, snapshot=snapshot,
                                                       source_type=head.source_type, source_id=head.source_id,
                                                       sequence=head.sequence):
            raise ValueError("PROJECT_SNAPSHOT_HEAD_INVALID")
        try:
            SnapshotPayloadResolver().validate_node_local(db, snapshot)
        except ValueError as exc:
            raise ValueError("PROJECT_SNAPSHOT_HEAD_INVALID") from exc
        return snapshot

    def current_audited(self, db: Session, project_id: str) -> WorldSnapshot | None:
        """Return the current head only after explicit full-chain validation."""
        snapshot = self.current_fast(db, project_id)
        if not snapshot:
            return None
        try:
            SnapshotPayloadResolver().validate_chain(db, snapshot)
        except ValueError as exc:
            raise ValueError("PROJECT_SNAPSHOT_HEAD_INVALID") from exc
        return snapshot

    # ``current`` intentionally remains the bounded runtime read API.
    current = current_fast

    def update(self, db: Session, project_id: str, snapshot: WorldSnapshot, *, source_type: str,
               source_id: str | None = None, sequence: int | None = None) -> ProjectWorldSnapshotHead:
        if snapshot.project_id != project_id:
            raise ValueError("SNAPSHOT_CHAIN_INVALID")
        # Scene commits, replay commits, and revision applies own the Project
        # serialization lock before changing the current-history boundary.
        head = db.scalar(select(ProjectWorldSnapshotHead).where(
            ProjectWorldSnapshotHead.project_id == project_id).with_for_update())
        if head is None:
            # A service caller may not already be inside a SceneCommit's
            # project lock.  Savepoint retry makes that first derived row safe
            # under PostgreSQL's unique constraint without leaking an error.
            try:
                with db.begin_nested():
                    candidate = ProjectWorldSnapshotHead(
                        project_id=project_id, snapshot_id=snapshot.id,
                        state_fingerprint=snapshot.state_fingerprint,
                        source_type=source_type, source_id=source_id, sequence=sequence,
                        head_fingerprint="",
                    )
                    db.add(candidate)
                    db.flush()
            except IntegrityError:
                pass
            head = db.scalar(select(ProjectWorldSnapshotHead).where(
                ProjectWorldSnapshotHead.project_id == project_id).with_for_update())
            if head is None:
                raise ValueError("PROJECT_SNAPSHOT_HEAD_INVALID")
            head.snapshot_id, head.state_fingerprint = snapshot.id, snapshot.state_fingerprint
            head.source_type, head.source_id, head.sequence = source_type, source_id, sequence
        else:
            head.snapshot_id, head.state_fingerprint = snapshot.id, snapshot.state_fingerprint
            head.source_type, head.source_id, head.sequence = source_type, source_id, sequence
        head.head_fingerprint = self._fingerprint(project_id=project_id, snapshot=snapshot,
                                                  source_type=source_type, source_id=source_id, sequence=sequence)
        db.flush()
        return head

    def audit(self, db: Session, project_id: str) -> None:
        head = db.scalar(select(ProjectWorldSnapshotHead).where(ProjectWorldSnapshotHead.project_id == project_id))
        if not head:
            raise ValueError("PROJECT_SNAPSHOT_HEAD_INVALID")
        self.current_audited(db, project_id)

    def rebuild(self, db: Session, project_id: str) -> ProjectWorldSnapshotHead:
        """Create a deliberately explicit full anchor for pointer repair."""
        from .versioning import WorldSnapshotBuilder
        payload, fingerprint = WorldSnapshotBuilder().build(db, project_id)
        anchor = CompactSnapshotService().anchor(db, project_id, SnapshotType.BASELINE, payload, fingerprint)
        return self.update(db, project_id, anchor, source_type="EXPLICIT_HEAD_REBUILD")


class CompactSnapshotService:
    codec = SnapshotDeltaCodec()
    resolver = SnapshotPayloadResolver()
    heads = ProjectWorldSnapshotHeadService()

    def _node(self, db: Session, *, project_id: str, kind: SnapshotType | str, mode: SnapshotStorageMode,
              state_fingerprint: str, payload: dict[str, Any], base: WorldSnapshot | None = None,
              source_revision_id: str | None = None) -> WorldSnapshot:
        if base and base.project_id != project_id:
            raise ValueError("SNAPSHOT_CHAIN_INVALID")
        schema = 2
        protocol = "formal-world-state-v2" if str(state_fingerprint).startswith("formal-world-state-v2:") else "world-snapshot-v1"
        node = WorldSnapshot(project_id=project_id, snapshot_type=kind, schema_version=schema,
                             state_fingerprint=state_fingerprint, payload=copy.deepcopy(payload),
                             state_fingerprint_protocol=protocol,
                             source_revision_id=source_revision_id, storage_mode=mode,
                             base_snapshot_id=base.id if base else None,
                             materialization_depth=(base.materialization_depth + 1) if base else 0)
        node.storage_fingerprint = self.codec.fingerprint(
            project_id=project_id, mode=mode, base_snapshot_id=node.base_snapshot_id,
            base_state_fingerprint=base.state_fingerprint if base else None, payload=node.payload,
            schema_version=schema,
        )
        db.add(node); db.flush()
        return node

    def anchor(self, db: Session, project_id: str, kind: SnapshotType | str, payload: dict[str, Any],
               state_fingerprint: str | None = None, source_revision_id: str | None = None) -> WorldSnapshot:
        full = self.codec.normalize(payload)
        return self._node(db, project_id=project_id, kind=kind, mode=SnapshotStorageMode.COMPACT_ANCHOR,
                          state_fingerprint=state_fingerprint or snapshot_fingerprint(full), payload=full,
                          source_revision_id=source_revision_id)

    def reference(self, db: Session, project_id: str, kind: SnapshotType | str, base: WorldSnapshot) -> WorldSnapshot:
        payload = {"protocol": COMPACT_PROTOCOL, "kind": "reference", "base_snapshot_id": base.id}
        return self._node(db, project_id=project_id, kind=kind, mode=SnapshotStorageMode.REFERENCE,
                          state_fingerprint=base.state_fingerprint, payload=payload, base=base)

    def delta(self, db: Session, project_id: str, kind: SnapshotType | str, base: WorldSnapshot,
              delta_payload: dict[str, Any], state_fingerprint: str) -> WorldSnapshot:
        return self._node(db, project_id=project_id, kind=kind, mode=SnapshotStorageMode.COMPACT_DELTA,
                          state_fingerprint=state_fingerprint, payload=delta_payload, base=base)

    def capture_pre(self, db: Session, project_id: str, kind: SnapshotType | str,
                    build_payload) -> WorldSnapshot:
        head = self.heads.current(db, project_id)
        if head:
            return self.reference(db, project_id, kind, head)
        payload, fingerprint = build_payload()
        anchor = self.anchor(db, project_id, kind, payload, fingerprint)
        self.heads.update(db, project_id, anchor, source_type="ANCHOR")
        return anchor

    def from_payloads(self, db: Session, project_id: str, *, pre_kind: SnapshotType | str,
                      post_kind: SnapshotType | str, pre_payload: dict[str, Any], post_payload: dict[str, Any],
                      source_type: str, source_id: str | None = None, sequence: int | None = None) -> tuple[WorldSnapshot, WorldSnapshot]:
        pre_full, post_full = self.codec.normalize(pre_payload), self.codec.normalize(post_payload)
        head = self.heads.current(db, project_id)
        pre_fingerprint = snapshot_fingerprint(pre_full)
        pre = self.reference(db, project_id, pre_kind, head) if head and head.state_fingerprint == pre_fingerprint else self.anchor(db, project_id, pre_kind, pre_full, pre_fingerprint)
        post_fingerprint = snapshot_fingerprint(post_full)
        post = self.delta(db, project_id, post_kind, pre, self.codec.diff(pre_full, post_full), post_fingerprint)
        self.heads.update(db, project_id, post, source_type=source_type, source_id=source_id, sequence=sequence)
        return pre, post


class SceneCommitFormalMutationGuard:
    """Fail closed for snapshot-covered writes made during one normal commit.

    The guard observes flushes instead of diffing a full PRE snapshot.  This
    keeps the normal path bounded while retaining an exact row-level manifest
    for the compact delta builder.
    """

    collection_by_model = {
        CanonFact: "canon_facts",
        WorldEntity: "world_entities",
        Character: "characters",
        CharacterKnowledge: "character_knowledge",
        CharacterMemory: "character_memories",
        RevealConstraint: "reveal_constraints",
        StoryThread: "story_threads",
        StoryArc: "story_arcs",
        Scene: "scenes",
        Chapter: "chapters",
        Project: "project",
    }

    def __init__(self, db: Session):
        self.db = db
        self._observed: dict[int, tuple[Any, bool, bool]] = {}

    def observe(self) -> None:
        """Capture current unit-of-work changes before a controlled flush."""
        for row in set(self.db.new).union(self.db.dirty).union(self.db.deleted):
            if type(row) not in self.collection_by_model:
                continue
            state = inspect(row)
            changed = row in self.db.new or row in self.db.deleted or state.modified
            if changed:
                self._observed[id(row)] = (row, row in self.db.new, row in self.db.deleted)

    def close(self) -> None:
        # Kept as a no-op lifecycle seam for callers that use a guard in a
        # failure path.  This guard intentionally owns no Session listeners.
        return None

    def assert_complete(self, *, project_id: str, items: list[Any], scene: Any,
                        knowledge: list[Any], memories: list[Any]) -> dict[str, Any]:
        target_ids: dict[str, set[str]] = {
            "PROJECT": set(), "WORLD_ENTITY": set(), "CHARACTER": set(), "STORY_THREAD": set(),
        }
        for item in items:
            target_type = _value(item.target_type)
            if target_type in target_ids:
                target_ids[target_type].add(item.target_id)
        allowed_new = {
            "scenes": {scene.id},
            "character_knowledge": {row.id for row in knowledge},
            "character_memories": {row.id for row in memories},
        }
        actual: dict[str, set[str]] = {}
        include_project = False
        try:
            for row, was_new, was_deleted in self._observed.values():
                collection = self.collection_by_model[type(row)]
                row_id = getattr(row, "id", None)
                if was_deleted or not row_id or getattr(row, "project_id", project_id) != project_id:
                    raise ValueError("COMPACT_SNAPSHOT_DELTA_UNEXPECTED_MUTATION")
                if collection == "project":
                    if row_id != project_id or project_id not in target_ids["PROJECT"]:
                        raise ValueError("COMPACT_SNAPSHOT_DELTA_UNEXPECTED_MUTATION")
                    include_project = True
                    continue
                expected_target_type = {
                    "world_entities": "WORLD_ENTITY",
                    "characters": "CHARACTER",
                    "story_threads": "STORY_THREAD",
                }.get(collection)
                if expected_target_type:
                    if row_id not in target_ids[expected_target_type]:
                        raise ValueError("COMPACT_SNAPSHOT_DELTA_UNEXPECTED_MUTATION")
                elif collection in allowed_new:
                    if not was_new or row_id not in allowed_new[collection]:
                        raise ValueError("COMPACT_SNAPSHOT_DELTA_UNEXPECTED_MUTATION")
                else:
                    # Canon, reveal constraints, arcs, chapters, and every
                    # other snapshot-covered row are outside normal authority.
                    raise ValueError("COMPACT_SNAPSHOT_DELTA_UNEXPECTED_MUTATION")
                actual.setdefault(collection, set()).add(row_id)
            expected = {
                "scenes": {scene.id},
                "character_knowledge": {row.id for row in knowledge},
                "character_memories": {row.id for row in memories},
            }
            for item in items:
                collection = SceneCommitSnapshotDeltaBuilder.collection_by_target.get(_value(item.target_type))
                if collection:
                    expected.setdefault(collection, set()).add(item.target_id)
            if {key: value for key, value in actual.items() if value} != {key: value for key, value in expected.items() if value}:
                raise ValueError("COMPACT_SNAPSHOT_DELTA_UNEXPECTED_MUTATION")
            if include_project != bool(target_ids["PROJECT"]):
                raise ValueError("COMPACT_SNAPSHOT_DELTA_UNEXPECTED_MUTATION")
            return {"project": include_project, "collections": actual}
        finally:
            self.close()


class SceneCommitSnapshotDeltaBuilder:
    """Bounded normal-commit delta from known formal mutations and final rows."""
    collection_by_target = {
        "CHARACTER": "characters", "WORLD_ENTITY": "world_entities", "STORY_THREAD": "story_threads",
    }

    def build(self, post_payload: dict[str, Any], items: list[Any], knowledge: list[Any],
              memories: list[Any], scene: Any, *, mutation_manifest: dict[str, Any] | None = None) -> dict[str, Any]:
        codec = SnapshotDeltaCodec()
        full = codec.normalize(post_payload)
        upsert_ids: dict[str, set[str]] = {
            "scenes": {scene.id},
            "character_knowledge": {row.id for row in knowledge},
            "character_memories": {row.id for row in memories},
        }
        include_project = False
        for item in items:
            target_type = _value(item.target_type)
            if target_type == "PROJECT":
                include_project = True
                continue
            collection = self.collection_by_target.get(target_type)
            if not collection:
                raise ValueError("COMPACT_SNAPSHOT_DELTA_INVALID")
            upsert_ids.setdefault(collection, set()).add(item.target_id)
        if mutation_manifest is not None:
            observed = {
                collection: set(ids)
                for collection, ids in (mutation_manifest.get("collections") or {}).items()
                if ids
            }
            expected = {collection: ids for collection, ids in upsert_ids.items() if ids}
            if observed != expected or bool(mutation_manifest.get("project")) != include_project:
                raise ValueError("COMPACT_SNAPSHOT_DELTA_UNEXPECTED_MUTATION")
        collections: dict[str, Any] = {}
        for collection, ids in upsert_ids.items():
            rows = {row.get("id"): row for row in full.get(collection, []) if row.get("id")}
            if any(row_id not in rows for row_id in ids):
                raise ValueError("COMPACT_SNAPSHOT_DELTA_INVALID")
            collections[collection] = {"upsert": [rows[row_id] for row_id in sorted(ids)], "delete": []}
        return {
            "protocol": COMPACT_PROTOCOL,
            # Project is tiny and carrying it avoids a special case for world time.
            "project": full["project"] if include_project else None,
            "collections": collections,
        }


class CompactSnapshotAudit:
    def audit_snapshot(self, db: Session, snapshot_or_id: WorldSnapshot | str) -> dict[str, Any]:
        snapshot = db.get(WorldSnapshot, snapshot_or_id) if isinstance(snapshot_or_id, str) else snapshot_or_id
        if not snapshot:
            raise ValueError("SNAPSHOT_BASE_MISSING")
        payload = SnapshotPayloadResolver().materialize(db, snapshot)
        if getattr(snapshot, "state_fingerprint_protocol", "world-snapshot-v1") == "formal-world-state-v2":
            from .formal_state import formal_world_state_v2_fingerprint
            actual_fingerprint = formal_world_state_v2_fingerprint(payload)
        else:
            actual_fingerprint = snapshot_fingerprint(payload)
        if actual_fingerprint != snapshot.state_fingerprint:
            raise ValueError("SNAPSHOT_CHAIN_INVALID")
        return payload

    def audit_project_head(self, db: Session, project_id: str) -> None:
        ProjectWorldSnapshotHeadService().audit(db, project_id)

    def audit_current_formal_state(self, db: Session, project_id: str) -> dict[str, Any]:
        """Expensive explicit audit: storage reconstruction must equal Formal DB."""
        head = ProjectWorldSnapshotHeadService().current_audited(db, project_id)
        if not head:
            raise ValueError("PROJECT_SNAPSHOT_HEAD_INVALID")
        materialized = self.audit_snapshot(db, head)
        from .versioning import WorldSnapshotBuilder
        current, fingerprint = WorldSnapshotBuilder().build(db, project_id)
        if getattr(head, "state_fingerprint_protocol", None) == "formal-world-state-v2":
            from .formal_state import formal_world_state_v2_fingerprint
            expected_fingerprint = formal_world_state_v2_fingerprint(current)
        else:
            expected_fingerprint = fingerprint
        if expected_fingerprint != head.state_fingerprint or materialized != current:
            raise ValueError("SNAPSHOT_CHAIN_INVALID")
        return materialized
