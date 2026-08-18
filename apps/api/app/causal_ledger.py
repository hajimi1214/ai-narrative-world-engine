"""Deterministic, derived timeline and causal-ledger indexing for Phase 8."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .execution_trace import stable_fingerprint
from .historical import CurrentSceneCheckpointResolver, SceneCheckpointIntegrityValidator
from .models import (
    CanonFact, CausalEdgeKind, CausalLink, CausalRelationType, CausalResourceType,
    Character, CharacterDecision, CharacterKnowledge, CharacterMemory, Project,
    ReplaySceneRun, RetconApplication, RetconApplicationStatus, RetconReplaySession,
    ReplaySessionStatus, Scene, SceneCheckpointOrigin, SceneCommit, SceneCommitStatus,
    SceneExecutionBinding, ScenePerformanceTurn, SceneStateCheckpoint, StateDeltaBatch,
    StateDeltaBatchStatus, StateDeltaItem, StoryThread, TimelineEvent,
    TimelineEventType, TimelineOrigin, WorldEntity, WorldResolution, WorldSnapshot,
)
from .state_delta_validation import normalize_world_time
from .state_delta import WorldResolutionStateDeltaTranslator, compute_state_delta_after


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _escape(part: str) -> str:
    return str(part).replace("~", "~0").replace("/", "~1")


def paths_overlap(left: str | None, right: str | None) -> bool:
    """RFC6901 ancestry overlap used by state provenance lookup."""
    return bool(left and right and (left == right or left.startswith(right.rstrip("/") + "/") or right.startswith(left.rstrip("/") + "/")))

def explicit_knowledge_id(reference: Any) -> str | None:
    """Only the structured Decision contract can name a Knowledge row."""
    value = reference.get("knowledge_id") if isinstance(reference, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None

def explicit_memory_id(reference: Any) -> str | None:
    if isinstance(reference, str) and reference.strip(): return reference.strip()
    value = reference.get("memory_id") if isinstance(reference, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def read_overlay_path(document: Any, path: str) -> tuple[bool, Any]:
    """Read an RFC6901 path without mutating the supplied snapshot document."""
    if path == "":
        return True, copy.deepcopy(document)
    if not isinstance(path, str) or not path.startswith("/"):
        return False, None
    current = document
    for token in path[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and key in current:
            current = current[key]
        elif isinstance(current, list) and key.isdigit() and 0 <= int(key) < len(current):
            current = current[int(key)]
        else:
            return False, None
    return True, copy.deepcopy(current)


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, datetime) or isinstance(right, datetime):
        left = left.isoformat() if isinstance(left, datetime) else left
        right = right.isoformat() if isinstance(right, datetime) else right
    return left == right


def _path_same(path: str, left: Any, right: Any) -> bool:
    if path == "/current_world_time":
        try:
            return normalize_world_time(left) == normalize_world_time(right)
        except (TypeError, ValueError):
            return False
    return _same(left, right)


@dataclass(frozen=True)
class SceneStateTransition:
    target_type: str
    target_id: str
    path: str
    before_value: Any
    after_value: Any


class SceneStateTransitionExtractor:
    """Pure supported-world-state PRE/POST extractor, never an ORM writer."""
    TABLES = {
        "WORLD_ENTITY": ("world_entities", ("profile", "active")),
        "CHARACTER": ("characters", ("current_state", "inventory", "relationships", "physical_state", "emotional_state")),
        "STORY_THREAD": ("story_threads", ("state", "status", "progress")),
    }

    def extract(self, pre: dict[str, Any], post: dict[str, Any]) -> list[SceneStateTransition]:
        if self._canon_changed(pre, post):
            raise ValueError("CAUSAL_LEDGER_UNEXPECTED_CANON_MUTATION")
        transitions: list[SceneStateTransition] = []
        pre_project, post_project = pre.get("project", {}), post.get("project", {})
        if pre_project.get("current_world_time") != post_project.get("current_world_time"):
            project_id = str(post_project.get("id") or pre_project.get("id") or "")
            if project_id:
                transitions.append(SceneStateTransition("PROJECT", project_id, "/current_world_time", pre_project.get("current_world_time"), post_project.get("current_world_time")))
        for target_type, (table, fields) in self.TABLES.items():
            before = {str(row.get("id")): row for row in pre.get(table, []) if row.get("id")}
            after = {str(row.get("id")): row for row in post.get(table, []) if row.get("id")}
            for target_id in sorted(set(before) | set(after)):
                left, right = before.get(target_id, {}), after.get(target_id, {})
                for field in fields:
                    self._diff(transitions, target_type, target_id, f"/{_escape(field)}", left.get(field), right.get(field))
        return sorted(transitions, key=lambda item: (item.target_type, item.target_id, item.path))

    def _diff(self, out: list[SceneStateTransition], target_type: str, target_id: str, path: str, before: Any, after: Any) -> None:
        if _same(before, after):
            return
        if isinstance(before, dict) or isinstance(after, dict):
            left, right = before if isinstance(before, dict) else {}, after if isinstance(after, dict) else {}
            for key in sorted(set(left) | set(right), key=str):
                self._diff(out, target_type, target_id, f"{path}/{_escape(str(key))}", left.get(key), right.get(key))
            return
        # Lists are intentionally a whole-path audit: Phase 8 does not infer ADD/REMOVE.
        out.append(SceneStateTransition(target_type, target_id, path, copy.deepcopy(before), copy.deepcopy(after)))

    def _canon_changed(self, pre: dict[str, Any], post: dict[str, Any]) -> bool:
        before = {row.get("id"): row for row in pre.get("canon_facts", []) if row.get("id")}
        after = {row.get("id"): row for row in post.get("canon_facts", []) if row.get("id")}
        return before != after


class CausalLedgerService:
    """Indexes structured formal history only.  It has no authority to change it."""
    failure_injector = None
    extractor = SceneStateTransitionExtractor()

    def sync_after_scene_commit(self, db: Session, commit: SceneCommit) -> None:
        if _value(commit.status) != SceneCommitStatus.COMMITTED.value or not commit.scene_id:
            raise ValueError("CAUSAL_LEDGER_SCENE_COMMIT_INVALID")
        self.index_scene(db, commit.project_id, commit.scene_id)
        self.index_retcon_and_replay(db, commit.project_id)
        self.rebuild_temporal_edges(db, commit.project_id)
        self._inject_failure()

    def sync_after_replay_commit(self, db: Session, session: RetconReplaySession) -> None:
        if _value(session.status) != ReplaySessionStatus.COMPLETED.value:
            raise ValueError("CAUSAL_LEDGER_REPLAY_NOT_COMPLETED")
        self.index_current_history(db, session.project_id)
        self.index_retcon_and_replay(db, session.project_id)
        self.rebuild_temporal_edges(db, session.project_id)
        self._inject_failure()

    def _inject_failure(self) -> None:
        if type(self).failure_injector:
            type(self).failure_injector("AFTER_CAUSAL_LEDGER_SYNC")

    def index_current_history(self, db: Session, project_id: str) -> None:
        active_ids = set()
        scenes = db.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.sequence, Scene.id)).all()
        for scene in scenes:
            if _value(scene.status) == "OCCURRED" and scene.history_status == "ACTIVE":
                active_ids.add(scene.id)
                self.index_scene(db, project_id, scene.id)
            elif scene.history_status == "SUPERSEDED":
                self.deactivate_scene_history(db, project_id, scene.id)
        db.execute(update(TimelineEvent).where(TimelineEvent.project_id == project_id, TimelineEvent.event_type == TimelineEventType.SCENE_OCCURRED, TimelineEvent.scene_id.not_in(active_ids) if active_ids else True).values(active=False))

    def index_scene(self, db: Session, project_id: str, scene_id: str) -> TimelineEvent:
        scene = db.get(Scene, scene_id)
        if not scene or scene.project_id != project_id:
            raise ValueError("CAUSAL_LEDGER_SCENE_NOT_FOUND")
        if _value(scene.status) != "OCCURRED" or scene.history_status != "ACTIVE":
            self.deactivate_scene_history(db, project_id, scene_id)
            raise ValueError("CAUSAL_LEDGER_SCENE_NOT_CURRENT")
        try:
            checkpoint = CurrentSceneCheckpointResolver().current(db, project_id, scene_id)
            if checkpoint.capture_protocol_version >= 3:
                SceneCheckpointIntegrityValidator().validate_integrity(db, checkpoint)
        except ValueError as exc:
            raise ValueError("CAUSAL_LEDGER_CHECKPOINT_INVALID") from exc
        binding = db.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == scene.id, SceneExecutionBinding.active.is_(True)))
        payload = {
            "scene_id": scene.id, "sequence": scene.sequence, "location": scene.location,
            "participant_ids": sorted(str(value) for value in (scene.participants or [])),
            "story_thread_ids": sorted(str(value) for value in (scene.story_threads or [])),
            "execution_binding_id": binding.id if binding else None,
            "performance_id": binding.performance_id if binding else None,
            "checkpoint_id": checkpoint.id,
        }
        scene_event = self._event(db, project_id=project_id, event_type=TimelineEventType.SCENE_OCCURRED, source_type="SCENE", source_id=scene.id, source_key=f"SCENE:{scene.id}", scene=scene, ordinal=0, checkpoint=checkpoint, origin=self._origin(checkpoint), structured_payload=payload)
        pre, post = db.get(WorldSnapshot, checkpoint.pre_snapshot_id), db.get(WorldSnapshot, checkpoint.post_snapshot_id)
        if not pre or not post:
            raise ValueError("CAUSAL_LEDGER_CHECKPOINT_INVALID")
        transitions = self.extractor.extract(pre.payload, post.payload)
        old_state_event_ids = list(db.scalars(
            select(TimelineEvent.id).where(
                TimelineEvent.project_id == project_id,
                TimelineEvent.scene_id == scene.id,
                TimelineEvent.event_type == TimelineEventType.STATE_CHANGE,
                TimelineEvent.active.is_(True),
                TimelineEvent.checkpoint_id != checkpoint.id,
            )
        ))
        if old_state_event_ids:
            db.execute(update(TimelineEvent).where(TimelineEvent.id.in_(old_state_event_ids)).values(active=False))
            # These links are derived from superseded checkpoint state, not durable
            # execution lineage.  They must not be traversable from current state.
            db.execute(update(CausalLink).where(
                CausalLink.project_id == project_id,
                CausalLink.cause_type == CausalResourceType.TIMELINE_EVENT,
                CausalLink.cause_id.in_(old_state_event_ids),
            ).values(active=False))
        state_events = self._index_state_changes(db, scene, checkpoint, transitions, pre.payload, post.payload)
        for event in state_events:
            self._link(db, project_id, CausalResourceType.TIMELINE_EVENT, event.id, CausalResourceType.SCENE, scene.id, CausalEdgeKind.PROVENANCE, CausalRelationType.STATE_CHANGE_COMMITTED_IN_SCENE, scene, {"checkpoint_id": checkpoint.id})
        self._index_execution_links(db, scene, binding, state_events)
        return scene_event

    def _index_state_changes(self, db: Session, scene: Scene, checkpoint: SceneStateCheckpoint, transitions: list[SceneStateTransition], pre: dict[str, Any], post: dict[str, Any]) -> list[TimelineEvent]:
        origin = self._origin(checkpoint)
        if origin == TimelineOrigin.NORMAL_COMMIT:
            return self._normal_state_events(db, scene, checkpoint, transitions, pre, post)
        if origin == TimelineOrigin.LEGACY_BACKFILL:
            return [
                self._state_event(
                    db, scene, checkpoint, transition,
                    {"checkpoint_id": checkpoint.id, "legacy_backfill": True}, index,
                )
                for index, transition in enumerate(transitions, 1)
            ]
        return self._replay_state_events(db, scene, checkpoint, transitions)

    def _normal_state_events(self, db: Session, scene: Scene, checkpoint: SceneStateCheckpoint, transitions: list[SceneStateTransition], pre: dict[str, Any], post: dict[str, Any]) -> list[TimelineEvent]:
        commit = db.get(SceneCommit, checkpoint.source_scene_commit_id) if checkpoint.source_scene_commit_id else None
        if not commit or _value(commit.status) != "COMMITTED" or commit.scene_id != scene.id:
            raise ValueError("CAUSAL_LEDGER_STATE_DELTA_MISMATCH")
        batches = db.scalars(select(StateDeltaBatch).where(StateDeltaBatch.applied_commit_id == commit.id, StateDeltaBatch.status == StateDeltaBatchStatus.APPLIED).order_by(StateDeltaBatch.id)).all()
        items = [item for batch in batches for item in db.scalars(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch.id).order_by(StateDeltaItem.ordinal, StateDeltaItem.id)).all()]
        by_key = {(item.target_type.value, item.target_id, item.path): item for item in items}
        if len(by_key) != len(items):
            raise ValueError("CAUSAL_LEDGER_STATE_DELTA_MISMATCH")
        events = []
        for transition in transitions:
            covered = [item for item in items if item.target_type.value == transition.target_type and item.target_id == transition.target_id and (item.path == transition.path or transition.path.startswith(item.path + "/"))]
            if len(covered) != 1:
                raise ValueError("CAUSAL_LEDGER_STATE_DELTA_MISMATCH")
        turn_sequences = {
            turn.id: turn.sequence
            for turn in db.scalars(
                select(ScenePerformanceTurn).where(
                    ScenePerformanceTurn.id.in_([item.source_turn_id for item in items if item.source_turn_id])
                )
            )
        }
        ordered = sorted(
            items,
            key=lambda item: (
                turn_sequences.get(item.source_turn_id, -1),
                item.ordinal,
                item.target_type.value,
                item.target_id,
                item.path,
                item.semantic_fingerprint,
            ),
        )
        for index, item in enumerate(ordered, 1):
            found_before, actual_before = self._snapshot_value(pre, item.target_type.value, item.target_id, item.path)
            found_after, actual_after = self._snapshot_value(post, item.target_type.value, item.target_id, item.path)
            # UPSERT may intentionally create the final leaf.  It is still
            # causally proven when the absent PRE and present POST agree.
            if (found_before and not _path_same(item.path, actual_before, item.before_value)) or (not found_before and item.before_value is not None) or (found_after and not _path_same(item.path, actual_after, item.after_value)) or (not found_after and item.after_value is not None):
                raise ValueError("CAUSAL_LEDGER_STATE_DELTA_MISMATCH")
            transition = SceneStateTransition(item.target_type.value, item.target_id, item.path, item.before_value, item.after_value)
            events.append(self._state_event(db, scene, checkpoint, transition, {"state_delta_batch_id": item.batch_id, "state_delta_item_id": item.id, "source_resolution_id": item.source_resolution_id, "source_turn_id": item.source_turn_id, "domain": _value(item.domain), "operation": _value(item.operation), "item_semantic_fingerprint": item.semantic_fingerprint}, index))
        return events

    def _replay_state_events(self, db: Session, scene: Scene, checkpoint: SceneStateCheckpoint, transitions: list[SceneStateTransition]) -> list[TimelineEvent]:
        session = db.get(RetconReplaySession, checkpoint.source_replay_session_id) if checkpoint.source_replay_session_id else None
        if not session or _value(session.status) != "COMPLETED":
            raise ValueError("CAUSAL_LEDGER_CHECKPOINT_INVALID")
        run = db.scalar(select(ReplaySceneRun).where(ReplaySceneRun.replay_session_id == session.id, ReplaySceneRun.replacement_scene_id == scene.id))
        original_scene_id = run.original_scene_id if run else scene.id
        result = []
        for index, transition in enumerate(transitions, 1):
            result.append(self._state_event(db, scene, checkpoint, transition, {"checkpoint_id": checkpoint.id, "replay_session_id": session.id, "replacement_scene_id": scene.id, "original_scene_id": original_scene_id, "replay_scene_run_id": run.id if run else None}, index))
        return result

    def _state_event(self, db: Session, scene: Scene, checkpoint: SceneStateCheckpoint, transition: SceneStateTransition, payload: dict[str, Any], ordinal: int = 1) -> TimelineEvent:
        prior = db.scalar(select(TimelineEvent).where(TimelineEvent.project_id == scene.project_id, TimelineEvent.scene_id == scene.id, TimelineEvent.event_type == TimelineEventType.STATE_CHANGE, TimelineEvent.target_type == transition.target_type, TimelineEvent.target_id == transition.target_id, TimelineEvent.path == transition.path, TimelineEvent.checkpoint_id != checkpoint.id).order_by(TimelineEvent.sequence.desc(), TimelineEvent.ordinal.desc(), TimelineEvent.id.desc()))
        row = self._event(db, project_id=scene.project_id, event_type=TimelineEventType.STATE_CHANGE, source_type="SCENE_CHECKPOINT", source_id=checkpoint.id, source_key=f"CHECKPOINT:{checkpoint.id}:{transition.target_type}:{transition.target_id}:{transition.path}", scene=scene, ordinal=ordinal, checkpoint=checkpoint, origin=self._origin(checkpoint), target_type=transition.target_type, target_id=transition.target_id, path=transition.path, before=transition.before_value, after=transition.after_value, structured_payload=payload)
        if prior and row.supersedes_event_id is None: row.supersedes_event_id = prior.id
        return row

    def _snapshot_value(self, payload: dict[str, Any], target_type: str, target_id: str, path: str) -> tuple[bool, Any]:
        if target_type == "PROJECT":
            return read_overlay_path(payload.get("project", {}), path)
        table = {"WORLD_ENTITY": "world_entities", "CHARACTER": "characters", "STORY_THREAD": "story_threads"}.get(target_type)
        row = next((value for value in payload.get(table or "", []) if value.get("id") == target_id), None)
        return read_overlay_path(row, path) if row else (False, None)

    def _index_execution_links(self, db: Session, scene: Scene, binding: SceneExecutionBinding | None, state_events: list[TimelineEvent]) -> None:
        if not binding:
            return
        turns = db.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == binding.performance_id).order_by(ScenePerformanceTurn.sequence, ScenePerformanceTurn.id)).all()
        state_by_item = {event.structured_payload.get("state_delta_item_id"): event for event in state_events if event.structured_payload.get("state_delta_item_id")}
        for turn in turns:
            decision = db.get(CharacterDecision, turn.character_decision_id)
            if decision and decision.project_id == scene.project_id:
                for reference in decision.knowledge_used or []:
                    knowledge_id = explicit_knowledge_id(reference); knowledge = db.get(CharacterKnowledge, knowledge_id) if knowledge_id else None
                    if knowledge and knowledge.character_id == decision.character_id:
                        self._link(db, scene.project_id, CausalResourceType.CHARACTER_KNOWLEDGE, knowledge.id, CausalResourceType.CHARACTER_DECISION, decision.id, CausalEdgeKind.CAUSAL, CausalRelationType.KNOWLEDGE_INFORMED_DECISION, scene, {"explicit_reference": True})
                for reference in decision.memory_refs or []:
                    memory_id = explicit_memory_id(reference); memory = db.get(CharacterMemory, memory_id) if memory_id else None
                    if memory and memory.character_id == decision.character_id:
                        self._link(db, scene.project_id, CausalResourceType.CHARACTER_MEMORY, memory.id, CausalResourceType.CHARACTER_DECISION, decision.id, CausalEdgeKind.CAUSAL, CausalRelationType.MEMORY_INFORMED_DECISION, scene, {"explicit_reference": True})
                self._link(db, scene.project_id, CausalResourceType.CHARACTER_DECISION, decision.id, CausalResourceType.SCENE_PERFORMANCE_TURN, turn.id, CausalEdgeKind.CAUSAL, CausalRelationType.DECISION_PRODUCED_TURN, scene, {"performance_id": binding.performance_id})
            resolution = db.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == turn.id))
            if not resolution:
                continue
            self._link(db, scene.project_id, CausalResourceType.SCENE_PERFORMANCE_TURN, turn.id, CausalResourceType.WORLD_RESOLUTION, resolution.id, CausalEdgeKind.CAUSAL, CausalRelationType.TURN_RESOLVED_BY, scene, {"performance_id": binding.performance_id})
            for canon_id in resolution.canon_fact_ids_used or []:
                canon = db.get(CanonFact, str(canon_id))
                if canon and canon.project_id == scene.project_id:
                    self._link(db, scene.project_id, CausalResourceType.CANON_FACT, canon.id, CausalResourceType.WORLD_RESOLUTION, resolution.id, CausalEdgeKind.PROVENANCE, CausalRelationType.CANON_CONSTRAINED_RESOLUTION, scene, {"explicit_reference": True})
            for entity_id in resolution.world_entity_ids_used or []:
                entity = db.get(WorldEntity, str(entity_id))
                if entity and entity.project_id == scene.project_id:
                    self._link(db, scene.project_id, CausalResourceType.WORLD_ENTITY, entity.id, CausalResourceType.WORLD_RESOLUTION, resolution.id, CausalEdgeKind.PROVENANCE, CausalRelationType.WORLD_ENTITY_CONTEXT_FOR_RESOLUTION, scene, {"entity_id": entity.id})
        for event in state_events:
            item_id = event.structured_payload.get("state_delta_item_id")
            if not item_id:
                self._link_replay_resolution_if_unique(db, scene, binding, event)
                continue
            item = db.get(StateDeltaItem, item_id)
            if item and item.source_resolution_id:
                self._link(db, scene.project_id, CausalResourceType.WORLD_RESOLUTION, item.source_resolution_id, CausalResourceType.STATE_DELTA_ITEM, item.id, CausalEdgeKind.CAUSAL, CausalRelationType.RESOLUTION_PRODUCED_STATE_CHANGE, scene, {"item_semantic_fingerprint": item.semantic_fingerprint})
                self._link(db, scene.project_id, CausalResourceType.STATE_DELTA_ITEM, item.id, CausalResourceType.TIMELINE_EVENT, event.id, CausalEdgeKind.PROVENANCE, CausalRelationType.RESOLUTION_PRODUCED_STATE_CHANGE, scene, {"checkpoint_id": event.checkpoint_id})
        for knowledge in db.scalars(select(CharacterKnowledge).join(Character, CharacterKnowledge.character_id == Character.id).where(Character.project_id == scene.project_id, CharacterKnowledge.source == scene.id)).all():
            self._link(db, scene.project_id, CausalResourceType.SCENE, scene.id, CausalResourceType.CHARACTER_KNOWLEDGE, knowledge.id, CausalEdgeKind.PROVENANCE, CausalRelationType.SCENE_PRODUCED_KNOWLEDGE, scene, {"source": scene.id})
        for memory in db.scalars(select(CharacterMemory).join(Character, CharacterMemory.character_id == Character.id).where(Character.project_id == scene.project_id, CharacterMemory.source_scene == scene.id)).all():
            self._link(db, scene.project_id, CausalResourceType.SCENE, scene.id, CausalResourceType.CHARACTER_MEMORY, memory.id, CausalEdgeKind.PROVENANCE, CausalRelationType.SCENE_PRODUCED_MEMORY, scene, {"source_scene": scene.id})

    def _link_replay_resolution_if_unique(self, db: Session, scene: Scene, binding: SceneExecutionBinding, event: TimelineEvent) -> None:
        candidates = []
        translator = WorldResolutionStateDeltaTranslator()
        for resolution in db.scalars(select(WorldResolution).join(ScenePerformanceTurn, WorldResolution.performance_turn_id == ScenePerformanceTurn.id).where(ScenePerformanceTurn.performance_id == binding.performance_id)).all():
            canonical_effects, _ = translator.translate(resolution)
            for effect in canonical_effects:
                try:
                    expected = compute_state_delta_after(
                        event.before_value,
                        event.before_value is not None,
                        effect,
                    )
                except ValueError:
                    continue
                if effect.target_type.value == event.target_type and effect.target_id == event.target_id and effect.path == event.path and _path_same(event.path, expected, event.after_value): candidates.append(resolution)
        if len({value.id for value in candidates}) == 1:
            resolution = candidates[0]
            self._link(db, scene.project_id, CausalResourceType.WORLD_RESOLUTION, resolution.id, CausalResourceType.TIMELINE_EVENT, event.id, CausalEdgeKind.CAUSAL, CausalRelationType.RESOLUTION_PRODUCED_STATE_CHANGE, scene, {"structured_effect_path": event.path})

    def index_retcon_and_replay(self, db: Session, project_id: str) -> None:
        applications = db.scalars(select(RetconApplication).where(RetconApplication.project_id == project_id, RetconApplication.status == RetconApplicationStatus.REPLAY_COMPLETED)).all()
        for app in applications:
            replay = db.scalar(select(RetconReplaySession).where(RetconReplaySession.retcon_application_id == app.id, RetconReplaySession.status == ReplaySessionStatus.COMPLETED))
            payload = {"application_id": app.id, "revision_id": app.source_revision_id, "plan_id": app.retcon_plan_id, "affected_sequence_range": (app.replay_summary or {}).get("affected_sequence_range"), "replay_session_id": replay.id if replay else None}
            retcon_event = self._event(db, project_id=project_id, event_type=TimelineEventType.RETCON_APPLIED, source_type="RETCON_APPLICATION", source_id=app.id, source_key=f"RETCON_APPLICATION:{app.id}", origin=TimelineOrigin.RETCON, structured_payload=payload)
            if replay:
                replay_event = self._event(db, project_id=project_id, event_type=TimelineEventType.REPLAY_COMMITTED, source_type="REPLAY_SESSION", source_id=replay.id, source_key=f"REPLAY_SESSION:{replay.id}", origin=TimelineOrigin.REPLAY_COMMIT, structured_payload={"replay_session_id": replay.id, "retcon_application_id": app.id})
                self._link(db, project_id, CausalResourceType.RETCON_APPLICATION, app.id, CausalResourceType.REPLAY_SESSION, replay.id, CausalEdgeKind.PROVENANCE, CausalRelationType.RETCON_TRIGGERED_REPLAY, None, {"retcon_event_id": retcon_event.id, "replay_event_id": replay_event.id}, replay.id)
                for run in db.scalars(select(ReplaySceneRun).where(ReplaySceneRun.replay_session_id == replay.id, ReplaySceneRun.replacement_scene_id.is_not(None))).all():
                    self._link(db, project_id, CausalResourceType.SCENE, run.original_scene_id, CausalResourceType.SCENE, run.replacement_scene_id, CausalEdgeKind.PROVENANCE, CausalRelationType.REPLAY_REPLACED_SCENE, db.get(Scene, run.replacement_scene_id), {"replay_scene_run_id": run.id}, replay.id)

    def deactivate_scene_history(self, db: Session, project_id: str, scene_id: str) -> None:
        db.execute(update(TimelineEvent).where(TimelineEvent.project_id == project_id, TimelineEvent.scene_id == scene_id).values(active=False))
        db.execute(update(CausalLink).where(CausalLink.project_id == project_id, CausalLink.scene_id == scene_id).values(active=False))

    def rebuild_temporal_edges(self, db: Session, project_id: str) -> None:
        db.execute(update(CausalLink).where(CausalLink.project_id == project_id, CausalLink.relation_type == CausalRelationType.SCENE_PRECEDES_SCENE).values(active=False))
        scenes = db.scalars(select(Scene).where(Scene.project_id == project_id, Scene.status == "OCCURRED", Scene.history_status == "ACTIVE").order_by(Scene.sequence, Scene.id)).all()
        for before, after in zip(scenes, scenes[1:]):
            self._link(db, project_id, CausalResourceType.SCENE, before.id, CausalResourceType.SCENE, after.id, CausalEdgeKind.TEMPORAL, CausalRelationType.SCENE_PRECEDES_SCENE, after, {"ordered_by": ["sequence", "id"]})

    def _event(self, db: Session, *, project_id: str, event_type: TimelineEventType, source_type: str, source_id: str, source_key: str, origin: TimelineOrigin, structured_payload: dict[str, Any], scene: Scene | None = None, ordinal: int | None = None, checkpoint: SceneStateCheckpoint | None = None, target_type: str | None = None, target_id: str | None = None, path: str | None = None, before: Any = None, after: Any = None) -> TimelineEvent:
        values = {
            "event_type": _value(event_type), "source_type": source_type,
            "source_id": source_id, "source_key": source_key,
            "scene_id": scene.id if scene else None,
            "sequence": scene.sequence if scene else None,
            "ordinal": ordinal, "world_time": scene.world_time if scene else None,
            "origin": _value(origin), "checkpoint_id": checkpoint.id if checkpoint else None,
            "checkpoint_fingerprint": checkpoint.checkpoint_fingerprint if checkpoint else None,
            "target_type": target_type, "target_id": target_id, "path": path,
            "before": before, "after": after, "payload": structured_payload,
        }
        fingerprint = stable_fingerprint(values, "timeline-event-v1")
        row = db.scalar(select(TimelineEvent).where(TimelineEvent.project_id == project_id, TimelineEvent.source_key == source_key))
        if row:
            row.event_type=event_type; row.source_type=source_type; row.source_id=source_id; row.scene_id=scene.id if scene else None; row.sequence=scene.sequence if scene else None; row.ordinal=ordinal; row.world_time=scene.world_time if scene else None; row.origin=origin; row.active=True; row.checkpoint_id=checkpoint.id if checkpoint else None; row.target_type=target_type; row.target_id=target_id; row.path=path; row.before_value=copy.deepcopy(before); row.after_value=copy.deepcopy(after); row.event_fingerprint=fingerprint; row.structured_payload=copy.deepcopy(structured_payload)
            return row
        row = TimelineEvent(project_id=project_id, event_type=event_type, source_type=source_type, source_id=source_id, source_key=source_key, scene_id=scene.id if scene else None, sequence=scene.sequence if scene else None, ordinal=ordinal, world_time=scene.world_time if scene else None, origin=origin, active=True, checkpoint_id=checkpoint.id if checkpoint else None, target_type=target_type, target_id=target_id, path=path, before_value=copy.deepcopy(before), after_value=copy.deepcopy(after), structured_payload=copy.deepcopy(structured_payload), event_fingerprint=fingerprint)
        db.add(row); db.flush(); return row

    def _link(self, db: Session, project_id: str, cause_type: CausalResourceType, cause_id: str, effect_type: CausalResourceType, effect_id: str, edge_kind: CausalEdgeKind, relation_type: CausalRelationType, scene: Scene | None, evidence: dict[str, Any], replay_session_id: str | None = None) -> CausalLink:
        key = f"{_value(relation_type)}:{_value(cause_type)}:{cause_id}:{_value(effect_type)}:{effect_id}"
        payload = {"cause_type": _value(cause_type), "cause_id": cause_id, "effect_type": _value(effect_type), "effect_id": effect_id, "edge_kind": _value(edge_kind), "relation_type": _value(relation_type), "evidence": evidence, "replay_session_id": replay_session_id}
        row = db.scalar(select(CausalLink).where(CausalLink.project_id == project_id, CausalLink.source_key == key))
        if row:
            row.active = True; row.evidence = copy.deepcopy(evidence); row.link_fingerprint = stable_fingerprint(payload, "causal-link-v1")
            return row
        row = CausalLink(project_id=project_id, cause_type=cause_type, cause_id=cause_id, effect_type=effect_type, effect_id=effect_id, edge_kind=edge_kind, relation_type=relation_type, scene_id=scene.id if scene else None, sequence=scene.sequence if scene else None, evidence=copy.deepcopy(evidence), active=True, source_key=key, link_fingerprint=stable_fingerprint(payload, "causal-link-v1"), replay_session_id=replay_session_id)
        db.add(row); db.flush(); return row

    def _origin(self, checkpoint: SceneStateCheckpoint) -> TimelineOrigin:
        origin = _value(checkpoint.origin)
        if origin == SceneCheckpointOrigin.REPLAY_COMMIT.value:
            return TimelineOrigin.REPLAY_COMMIT
        if origin == SceneCheckpointOrigin.NORMAL_COMMIT.value:
            return TimelineOrigin.NORMAL_COMMIT
        return TimelineOrigin.LEGACY_BACKFILL


class CausalLedgerBackfillService:
    def backfill(self, db: Session, project_id: str) -> None:
        if not db.get(Project, project_id):
            raise ValueError("CAUSAL_LEDGER_PROJECT_NOT_FOUND")
        CausalLedgerService().index_current_history(db, project_id)
        CausalLedgerService().index_retcon_and_replay(db, project_id)
        CausalLedgerService().rebuild_temporal_edges(db, project_id)


class CurrentCausalLedgerAudit:
    def audit(self, db: Session, project_id: str) -> None:
        scenes = db.scalars(select(Scene).where(Scene.project_id == project_id, Scene.status == "OCCURRED", Scene.history_status == "ACTIVE").order_by(Scene.sequence, Scene.id)).all()
        for scene in scenes:
            events = db.scalars(select(TimelineEvent).where(TimelineEvent.project_id == project_id, TimelineEvent.scene_id == scene.id, TimelineEvent.event_type == TimelineEventType.SCENE_OCCURRED, TimelineEvent.active.is_(True))).all()
            if len(events) != 1:
                raise ValueError("CAUSAL_LEDGER_SCENE_EVENT_INVALID")
            checkpoint = CurrentSceneCheckpointResolver().current(db, project_id, scene.id)
            if checkpoint.capture_protocol_version >= 3:
                SceneCheckpointIntegrityValidator().validate_integrity(db, checkpoint)
            binding = db.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == scene.id, SceneExecutionBinding.active.is_(True)))
            if not binding:
                raise ValueError("CAUSAL_LEDGER_EXECUTION_LINEAGE_INVALID")
            turns = db.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == binding.performance_id)).all()
            for turn in turns:
                if not db.get(CharacterDecision, turn.character_decision_id):
                    raise ValueError("CAUSAL_LEDGER_EXECUTION_LINEAGE_INVALID")
                if turn.requires_world_resolution and not db.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == turn.id)):
                    raise ValueError("CAUSAL_LEDGER_EXECUTION_LINEAGE_INVALID")
            if _value(checkpoint.origin) == "NORMAL_COMMIT":
                items = [item for batch in db.scalars(select(StateDeltaBatch).where(StateDeltaBatch.applied_commit_id == checkpoint.source_scene_commit_id, StateDeltaBatch.status == StateDeltaBatchStatus.APPLIED)).all() for item in db.scalars(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch.id)).all()]
                for item in items:
                    count = db.query(TimelineEvent).filter_by(project_id=project_id, event_type=TimelineEventType.STATE_CHANGE, active=True, source_key=f"CHECKPOINT:{checkpoint.id}:{item.target_type.value}:{item.target_id}:{item.path}").count()
                    if count != 1:
                        raise ValueError("CAUSAL_LEDGER_STATE_EVENT_INVALID")
        expected_temporal = max(0, len(scenes) - 1)
        actual_temporal = db.query(CausalLink).filter_by(project_id=project_id, relation_type=CausalRelationType.SCENE_PRECEDES_SCENE, active=True).count()
        if actual_temporal != expected_temporal:
            raise ValueError("CAUSAL_LEDGER_TEMPORAL_EDGE_INVALID")


class CausalProvenanceQuery:
    def why_state(self, db: Session, project_id: str, target_type: str, target_id: str, path: str, max_depth: int = 8) -> dict[str, Any]:
        candidates = db.scalars(select(TimelineEvent).where(TimelineEvent.project_id == project_id, TimelineEvent.active.is_(True), TimelineEvent.event_type == TimelineEventType.STATE_CHANGE, TimelineEvent.target_type == target_type, TimelineEvent.target_id == target_id).order_by(TimelineEvent.sequence.desc(), TimelineEvent.ordinal.desc(), TimelineEvent.id.desc())).all()
        event = next((row for row in candidates if paths_overlap(row.path, path)), None)
        if not event:
            raise LookupError("CAUSAL_LEDGER_STATE_HISTORY_NOT_FOUND")
        return {"event": self.event_payload(event), "upstream": self._upstream(db, project_id, CausalResourceType.TIMELINE_EVENT, event.id, max_depth)}

    def trace_decision(self, db: Session, project_id: str, decision_id: str) -> dict[str, Any]:
        decision = db.get(CharacterDecision, decision_id)
        if not decision or decision.project_id != project_id:
            raise LookupError("CAUSAL_LEDGER_DECISION_NOT_FOUND")
        return {"decision_id": decision.id, "upstream": self._upstream(db, project_id, CausalResourceType.CHARACTER_DECISION, decision.id, 8), "downstream": self._downstream(db, project_id, CausalResourceType.CHARACTER_DECISION, decision.id, 8)}

    def trace_knowledge(self, db: Session, project_id: str, knowledge_id: str) -> dict[str, Any]:
        knowledge = db.get(CharacterKnowledge, knowledge_id)
        character = db.get(Character, knowledge.character_id) if knowledge else None
        if not knowledge or not character or character.project_id != project_id:
            raise LookupError("CAUSAL_LEDGER_KNOWLEDGE_NOT_FOUND")
        source_scene = db.get(Scene, knowledge.source) if knowledge.source else None
        if source_scene and source_scene.project_id != project_id:
            source_scene = None
        return {"knowledge_id": knowledge.id, "character_id": knowledge.character_id, "source_scene_id": source_scene.id if source_scene else None, "source_scene_active": bool(source_scene and source_scene.history_status == "ACTIVE"), "upstream": self._upstream(db, project_id, CausalResourceType.CHARACTER_KNOWLEDGE, knowledge.id, 8)}

    def resource_links(self, db: Session, project_id: str, resource_type: str, resource_id: str) -> dict[str, Any]:
        try:
            kind = CausalResourceType(resource_type)
        except ValueError as exc:
            raise LookupError("CAUSAL_LEDGER_RESOURCE_NOT_FOUND") from exc
        return {"resource_type": kind.value, "resource_id": resource_id, "incoming": self._upstream(db, project_id, kind, resource_id, 8), "outgoing": self._downstream(db, project_id, kind, resource_id, 8)}

    def state_history(self, db: Session, project_id: str, target_type: str, target_id: str, path: str | None, include_superseded: bool) -> list[dict[str, Any]]:
        query = select(TimelineEvent).where(TimelineEvent.project_id == project_id, TimelineEvent.event_type == TimelineEventType.STATE_CHANGE, TimelineEvent.target_type == target_type, TimelineEvent.target_id == target_id)
        if path is not None: query = query.where(TimelineEvent.path == path)
        if not include_superseded: query = query.where(TimelineEvent.active.is_(True))
        return [self.event_payload(row) for row in db.scalars(query.order_by(TimelineEvent.sequence, TimelineEvent.ordinal, TimelineEvent.id)).all()]

    @staticmethod
    def event_payload(row: TimelineEvent) -> dict[str, Any]:
        return {"id": row.id, "event_type": _value(row.event_type), "source_type": row.source_type, "source_id": row.source_id, "scene_id": row.scene_id, "sequence": row.sequence, "ordinal": row.ordinal, "world_time": row.world_time, "origin": _value(row.origin), "active": row.active, "checkpoint_id": row.checkpoint_id, "target_type": row.target_type, "target_id": row.target_id, "path": row.path, "before_value": row.before_value, "after_value": row.after_value, "structured_payload": row.structured_payload, "event_fingerprint": row.event_fingerprint}

    def _upstream(self, db: Session, project_id: str, resource_type: CausalResourceType, resource_id: str, max_depth: int) -> list[dict[str, Any]]:
        return self._walk(db, project_id, resource_type, resource_id, max_depth, incoming=True)

    def _downstream(self, db: Session, project_id: str, resource_type: CausalResourceType, resource_id: str, max_depth: int) -> list[dict[str, Any]]:
        return self._walk(db, project_id, resource_type, resource_id, max_depth, incoming=False)

    def _walk(self, db: Session, project_id: str, resource_type: CausalResourceType, resource_id: str, max_depth: int, incoming: bool) -> list[dict[str, Any]]:
        result, queue, visited = [], [(resource_type, resource_id, 0)], {(resource_type.value, resource_id)}
        while queue:
            kind, identifier, depth = queue.pop(0)
            if depth >= max_depth: continue
            query = select(CausalLink).where(CausalLink.project_id == project_id, CausalLink.active.is_(True))
            query = query.where(CausalLink.effect_type == kind, CausalLink.effect_id == identifier) if incoming else query.where(CausalLink.cause_type == kind, CausalLink.cause_id == identifier)
            for link in db.scalars(query.order_by(CausalLink.sequence, CausalLink.source_key)).all():
                next_kind, next_id = (link.cause_type, link.cause_id) if incoming else (link.effect_type, link.effect_id)
                result.append({"id": link.id, "cause_type": _value(link.cause_type), "cause_id": link.cause_id, "effect_type": _value(link.effect_type), "effect_id": link.effect_id, "edge_kind": _value(link.edge_kind), "relation_type": _value(link.relation_type), "scene_id": link.scene_id, "sequence": link.sequence, "evidence": link.evidence})
                marker = (_value(next_kind), next_id)
                if marker not in visited:
                    visited.add(marker); queue.append((next_kind, next_id, depth + 1))
        return result
