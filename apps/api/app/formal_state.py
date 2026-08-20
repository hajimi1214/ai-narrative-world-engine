"""Versioned, incrementally maintained identity for the formal world.

Formal tables remain authoritative.  The v2 identity is a bounded accelerator;
the explicit rebuild/audit path always serializes the formal rows again.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, select, event
from sqlalchemy.orm import Session

from .execution_trace import stable_fingerprint
from .models import (
    CanonFact, Character, CharacterKnowledge, CharacterMemory, Chapter,
    FormalStateIdentityStatus, FormalStateLeaf, Project, ProjectFormalStateIdentity,
    RevealConstraint, Scene, StoryArc, StoryThread, WorldEntity,
)
from .revision import _record

FORMAL_WORLD_STATE_PROTOCOL = "formal-world-state-v2"
WORLD_SNAPSHOT_V1_PROTOCOL = "world-snapshot-v1"
SCENE_CHECKPOINT_V5_PROTOCOL = "scene-checkpoint-v5"
COLLECTION_MODELS = {
    "canon_facts": CanonFact, "world_entities": WorldEntity, "characters": Character,
    "character_knowledge": CharacterKnowledge, "character_memories": CharacterMemory,
    "reveal_constraints": RevealConstraint, "story_threads": StoryThread,
    "story_arcs": StoryArc, "scenes": Scene, "chapters": Chapter,
}
COLLECTIONS = ("project", *COLLECTION_MODELS.keys())
MODULUS = 1 << 256


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonical(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (datetime, date)):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    return value


def _row_payload(row: Any) -> dict[str, Any]:
    value = dict(_record(row))
    if isinstance(row, Project):
        value = {key: value.get(key) for key in {"id", "status", "creation_mode", "story_seed", "current_world_time"}}
    value.pop("created_at", None)
    value.pop("updated_at", None)
    if isinstance(row, Chapter):
        value.pop("content", None)
    return _canonical(value)


def _leaf_fingerprint(collection: str, resource_id: str, row: dict[str, Any]) -> str:
    return stable_fingerprint({"collection": collection, "resource_id": resource_id, "row": _canonical(row)}, "formal-world-state-leaf-v2")


def _digest_int(fingerprint: str) -> int:
    return int(fingerprint.split(":", 1)[-1], 16)


def _empty_state() -> dict[str, dict[str, int]]:
    return {collection: {"count": 0, "xor": 0, "sum": 0} for collection in COLLECTIONS}


def _root(state: dict[str, dict[str, int]]) -> str:
    normalized = {collection: {key: int(value.get(key, 0)) for key in ("count", "xor", "sum")} for collection, value in sorted(state.items())}
    return FORMAL_WORLD_STATE_PROTOCOL + ":" + hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def formal_world_state_v2_fingerprint(payload: dict[str, Any]) -> str:
    """Recompute identity from a complete legacy-compatible payload."""
    state = _empty_state()
    project = payload.get("project") or {}
    project_id = str(project.get("id", ""))
    if project_id:
        fp = _leaf_fingerprint("project", project_id, project)
        state["project"] = {"count": 1, "xor": _digest_int(fp), "sum": _digest_int(fp) % MODULUS}
    for collection in COLLECTION_MODELS:
        for row in payload.get(collection) or []:
            resource_id = str(row.get("id", ""))
            if not resource_id:
                continue
            fp = _leaf_fingerprint(collection, resource_id, row)
            value = _digest_int(fp)
            current = state[collection]
            current["count"] += 1
            current["xor"] ^= value
            current["sum"] = (current["sum"] + value) % MODULUS
    return _root(state)


class FormalSnapshotRowSerializer:
    collections = COLLECTIONS

    def row_payload(self, row: Any) -> dict[str, Any]:
        return _row_payload(row)

    def payload_rows(self, payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        project = payload.get("project") or {}
        if project.get("id"):
            rows[("project", str(project["id"]))] = _canonical(project)
        for collection in COLLECTION_MODELS:
            for row in payload.get(collection) or []:
                if row.get("id"):
                    rows[(collection, str(row["id"]))] = _canonical(row)
        return rows

    def database_rows(self, db: Session, project_id: str) -> dict[tuple[str, str], dict[str, Any]]:
        from .versioning import WorldSnapshotBuilder
        payload, _ = WorldSnapshotBuilder().build(db, project_id)
        return self.payload_rows(payload)


class FormalStateIdentityService:
    protocol = FORMAL_WORLD_STATE_PROTOCOL

    def _identity(self, db: Session, project_id: str) -> ProjectFormalStateIdentity | None:
        return db.scalar(select(ProjectFormalStateIdentity).where(ProjectFormalStateIdentity.project_id == project_id))

    def _owns(self, db: Session, row: Any, project_id: str) -> bool:
        owner = getattr(row, "project_id", None)
        if owner is None and isinstance(row, (CharacterKnowledge, CharacterMemory)):
            owner = db.scalar(select(Character.project_id).where(Character.id == row.character_id))
        return owner == project_id

    def _set_state(self, identity: ProjectFormalStateIdentity, state: dict[str, dict[str, int]], count: int, sequence: int | None):
        identity.collection_state = state
        identity.resource_count = count
        identity.state_fingerprint = _root(state)
        identity.protocol_version = self.protocol
        identity.status = FormalStateIdentityStatus.READY
        identity.built_through_sequence = sequence
        identity.dirty_reason = None
        identity.last_rebuilt_at = datetime.utcnow()

    def rebuild(self, db: Session, project_id: str, *, built_through_sequence: int | None = None, project_locked: bool = False) -> ProjectFormalStateIdentity:
        from .versioning import WorldSnapshotBuilder
        # The Project row is the existing serialization boundary for formal
        # world writes.  Rebuilds take it first so concurrent PG rebuilds and
        # SceneCommit cannot publish competing current roots.
        if not project_locked and not db.scalar(select(Project).where(Project.id == project_id).with_for_update()):
            raise ValueError("FORMAL_STATE_PROJECT_NOT_FOUND")
        payload, _ = WorldSnapshotBuilder().build(db, project_id)
        rows = FormalSnapshotRowSerializer().payload_rows(payload)
        db.execute(delete(FormalStateLeaf).where(FormalStateLeaf.project_id == project_id))
        state = _empty_state()
        for (collection, resource_id), row in rows.items():
            fp = _leaf_fingerprint(collection, resource_id, row)
            db.add(FormalStateLeaf(project_id=project_id, collection_name=collection, resource_id=resource_id, leaf_fingerprint=fp))
            value = _digest_int(fp)
            bucket = state[collection]
            bucket["count"] += 1; bucket["xor"] ^= value; bucket["sum"] = (bucket["sum"] + value) % MODULUS
        identity = self._identity(db, project_id)
        if identity is None:
            identity = ProjectFormalStateIdentity(project_id=project_id)
            db.add(identity)
        self._set_state(identity, state, len(rows), built_through_sequence)
        db.flush()
        return identity

    def mark_dirty(self, db: Session, project_id: str, reason: str = "FORMAL_STATE_MUTATION") -> ProjectFormalStateIdentity:
        identity = self._identity(db, project_id)
        if identity is None:
            identity = ProjectFormalStateIdentity(project_id=project_id, protocol_version=self.protocol, status=FormalStateIdentityStatus.DIRTY, dirty_reason=reason)
            db.add(identity)
        else:
            identity.status = FormalStateIdentityStatus.DIRTY; identity.dirty_reason = reason
        db.flush()
        return identity

    def current(self, db: Session, project_id: str, *, sequence: int | None = None) -> tuple[str, str]:
        identity = self._identity(db, project_id)
        if identity and identity.status == FormalStateIdentityStatus.READY and identity.protocol_version == self.protocol and identity.state_fingerprint:
            if sequence is None or identity.built_through_sequence is None or identity.built_through_sequence <= sequence:
                return identity.state_fingerprint, self.protocol
        if identity and identity.status == FormalStateIdentityStatus.DIRTY:
            # A dirty accelerator may not certify v2.  The legacy value keeps
            # correctness explicit; callers will reject v2-derived batches or
            # perform a deliberate rebuild/transition.
            from .versioning import WorldSnapshotBuilder
            payload, fingerprint = WorldSnapshotBuilder().build(db, project_id)
            return fingerprint, WORLD_SNAPSHOT_V1_PROTOCOL
        # Missing/dirty state is a correctness-first transition.  It is the
        # only normal path that performs the explicit O(N) initialization.
        identity = self.rebuild(db, project_id, built_through_sequence=sequence)
        return identity.state_fingerprint, self.protocol

    def sync_manifest(self, db: Session, project_id: str, manifest: dict[str, Any], *, sequence: int | None = None) -> ProjectFormalStateIdentity:
        previous_sync_flag = db.info.get("formal_state_sync_in_progress")
        db.info["formal_state_sync_in_progress"] = True
        try:
            return self._sync_manifest(db, project_id, manifest, sequence=sequence)
        finally:
            if previous_sync_flag is None:
                db.info.pop("formal_state_sync_in_progress", None)
            else:
                db.info["formal_state_sync_in_progress"] = previous_sync_flag

    def _sync_manifest(self, db: Session, project_id: str, manifest: dict[str, Any], *, sequence: int | None = None) -> ProjectFormalStateIdentity:
        identity = self._identity(db, project_id)
        if not identity or identity.status != FormalStateIdentityStatus.READY or identity.protocol_version != self.protocol:
            return self.rebuild(db, project_id, built_through_sequence=sequence)
        state = _canonical(identity.collection_state or _empty_state())
        for collection, ids in (manifest.get("collections") or {}).items():
            model = COLLECTION_MODELS.get(collection)
            if not model:
                raise ValueError("FORMAL_STATE_UNKNOWN_COLLECTION")
            for resource_id in ids or []:
                old = db.scalar(select(FormalStateLeaf).where(FormalStateLeaf.project_id == project_id, FormalStateLeaf.collection_name == collection, FormalStateLeaf.resource_id == resource_id).with_for_update())
                if old:
                    old_value = _digest_int(old.leaf_fingerprint); bucket = state[collection]; bucket["xor"] ^= old_value; bucket["sum"] = (bucket["sum"] - old_value) % MODULUS; bucket["count"] -= 1
                row = db.get(model, resource_id)
                if row is not None and self._owns(db, row, project_id):
                    payload = _row_payload(row); fp = _leaf_fingerprint(collection, resource_id, payload); value = _digest_int(fp)
                    if old is None: old = FormalStateLeaf(project_id=project_id, collection_name=collection, resource_id=resource_id, leaf_fingerprint=fp); db.add(old)
                    else: old.leaf_fingerprint = fp
                    bucket = state[collection]; bucket["xor"] ^= value; bucket["sum"] = (bucket["sum"] + value) % MODULUS; bucket["count"] += 1
                elif old:
                    db.delete(old)
        if manifest.get("project"):
            project = db.get(Project, project_id); old = db.scalar(select(FormalStateLeaf).where(FormalStateLeaf.project_id == project_id, FormalStateLeaf.collection_name == "project", FormalStateLeaf.resource_id == project_id).with_for_update())
            if old:
                value = _digest_int(old.leaf_fingerprint); state["project"]["xor"] ^= value; state["project"]["sum"] = (state["project"]["sum"] - value) % MODULUS; state["project"]["count"] -= 1
            payload = _row_payload(project); fp = _leaf_fingerprint("project", project_id, payload); value = _digest_int(fp)
            if old is None: db.add(FormalStateLeaf(project_id=project_id, collection_name="project", resource_id=project_id, leaf_fingerprint=fp))
            else: old.leaf_fingerprint = fp
            state["project"]["xor"] ^= value; state["project"]["sum"] = (state["project"]["sum"] + value) % MODULUS; state["project"]["count"] += 1
        count = sum(bucket["count"] for bucket in state.values())
        self._set_state(identity, state, count, sequence)
        db.flush()
        return identity

    def audit(self, db: Session, project_id: str) -> None:
        identity = self._identity(db, project_id)
        if not identity or identity.status != FormalStateIdentityStatus.READY:
            raise ValueError("FORMAL_STATE_IDENTITY_INTEGRITY_INVALID")
        from .versioning import WorldSnapshotBuilder
        payload, _ = WorldSnapshotBuilder().build(db, project_id)
        rows = FormalSnapshotRowSerializer().payload_rows(payload)
        state = _empty_state()
        expected_fps = set()
        for (collection, resource_id), row in rows.items():
            fp = _leaf_fingerprint(collection, resource_id, row); expected_fps.add((collection, resource_id, fp)); value = _digest_int(fp)
            bucket = state[collection]; bucket["count"] += 1; bucket["xor"] ^= value; bucket["sum"] = (bucket["sum"] + value) % MODULUS
        actual_fps = {(leaf.collection_name, leaf.resource_id, leaf.leaf_fingerprint) for leaf in db.scalars(select(FormalStateLeaf).where(FormalStateLeaf.project_id == project_id)).all()}
        if _root(state) != identity.state_fingerprint or state != _canonical(identity.collection_state or {}) or len(rows) != identity.resource_count or expected_fps != actual_fps:
            raise ValueError("FORMAL_STATE_IDENTITY_INTEGRITY_INVALID")

    def status(self, db: Session, project_id: str) -> dict[str, Any]:
        identity = self._identity(db, project_id)
        return {"status": getattr(identity.status, "value", identity.status) if identity else "DIRTY", "protocol": identity.protocol_version if identity else self.protocol, "resource_count": identity.resource_count if identity else 0, "built_through_sequence": identity.built_through_sequence if identity else None, "state_fingerprint": identity.state_fingerprint if identity else None, "fast_path_available": bool(identity and identity.status == FormalStateIdentityStatus.READY and identity.protocol_version == self.protocol)}

    def rebuild_and_anchor(self, db: Session, project_id: str, *, source_type: str, source_id: str | None = None, project_locked: bool = False):
        identity = self.rebuild(db, project_id, project_locked=project_locked)
        from .versioning import WorldSnapshotBuilder
        from .snapshot_storage import CompactSnapshotService
        payload, _ = WorldSnapshotBuilder().build(db, project_id)
        anchor = CompactSnapshotService().anchor(db, project_id, "BASELINE", payload, identity.state_fingerprint)
        CompactSnapshotService().heads.update(db, project_id, anchor, source_type=source_type, source_id=source_id)
        return identity, anchor

    def manifest_delta(self, db: Session, project_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Build a compact upsert delta from only touched formal rows."""
        collections: dict[str, Any] = {}
        for collection, ids in (manifest.get("collections") or {}).items():
            model = COLLECTION_MODELS.get(collection)
            if not model: continue
            rows = []
            for resource_id in sorted(ids or []):
                row = db.get(model, resource_id)
                if row is not None and self._owns(db, row, project_id):
                    rows.append(_row_payload(row))
            if rows: collections[collection] = {"upsert": rows, "delete": []}
        project = None
        if manifest.get("project"):
            project = _row_payload(db.get(Project, project_id))
        return {"protocol": "compact-world-snapshot-v1", "project": project, "collections": collections}


class FormalStateIdentityAudit:
    def audit(self, db: Session, project_id: str) -> None:
        return FormalStateIdentityService().audit(db, project_id)


CurrentFormalStateIdentityService = FormalStateIdentityService


@event.listens_for(Session, "before_flush")
def _mark_formal_identity_dirty(session: Session, flush_context, instances=None) -> None:
    """Direct formal CRUD cannot silently leave a READY accelerator stale."""
    if session.info.get("formal_state_sync_in_progress"):
        return
    touched: set[str] = set()
    tracked = tuple(COLLECTION_MODELS.values()) + (Project,)
    for row in set(session.new).union(session.dirty).union(session.deleted):
        if type(row) in tracked and getattr(row, "project_id", None):
            touched.add(row.project_id)
    for project_id in touched:
        identity = session.identity_map.get((ProjectFormalStateIdentity, (project_id,)))
        if identity is None:
            identity = session.scalar(select(ProjectFormalStateIdentity).where(ProjectFormalStateIdentity.project_id == project_id))
        if identity:
            identity.status = FormalStateIdentityStatus.DIRTY
            identity.dirty_reason = "FORMAL_STATE_MUTATION"
