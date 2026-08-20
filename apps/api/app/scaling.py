"""Phase 16A rebuildable history projections.

These rows are strictly derived accelerators.  They are deliberately kept out
of formal-history validation and can always be discarded and rebuilt from the
current Scene, checkpoint, and TimelineEvent authority.
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .character_mind import ActiveCharacterCognitionReader
from .execution_trace import stable_fingerprint
from .historical import CurrentSceneCheckpointResolver
from .models import (
    Character, CurrentStateChangeHead, EntityType, HistoryProjectionStatus,
    Project, ProjectHistoryProjection, RevealConstraint, RevealStatus, Scene,
    SceneExecutionBinding, SceneHistoryFeature, ScenePerformance, SceneProposal,
    SceneStateCheckpoint, SceneStatus, StoryArc, StoryThread, ThreadStatus,
    TimelineEvent, TimelineEventType, WorldEntity,
)


RECENT_SCENE_LIMIT = 10
PROJECTION_PROTOCOL = "project-history-projection-v1"
THREAD_STATS_META_KEY = "__projection_meta__"


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set)):
        return sorted((_canonical(item) for item in value), key=lambda item: stable_fingerprint(item, "projection-canonical-v1"))
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class HistoryProjectionFingerprintBuilder:
    """Deterministic append chain; timestamps never participate."""
    initial = stable_fingerprint({"protocol": PROJECTION_PROTOCOL}, PROJECTION_PROTOCOL)

    def extend(self, previous: str | None, feature_fingerprint: str) -> str:
        return stable_fingerprint(
            {"previous": previous or self.initial, "feature": feature_fingerprint},
            PROJECTION_PROTOCOL,
        )

    def build(self, features: list[SceneHistoryFeature]) -> str:
        result = self.initial
        for feature in sorted(features, key=lambda row: (row.sequence, row.scene_id)):
            result = self.extend(result, feature.feature_fingerprint)
        return result


class SceneHistoryFeatureBuilder:
    """Single structured feature definition shared by rebuild and append."""
    def build(self, db: Session, project_id: str, scene: Scene) -> dict[str, Any]:
        if scene.project_id != project_id or _value(scene.status) != SceneStatus.OCCURRED.value or scene.history_status != "ACTIVE":
            raise ValueError("SCALING_PROJECTION_INTEGRITY_INVALID")
        checkpoint = CurrentSceneCheckpointResolver().current(db, project_id, scene.id)
        binding = db.scalar(select(SceneExecutionBinding).where(
            SceneExecutionBinding.project_id == project_id,
            SceneExecutionBinding.scene_id == scene.id,
            SceneExecutionBinding.active.is_(True),
        ))
        performance = db.get(ScenePerformance, binding.performance_id) if binding else None
        proposal = db.get(SceneProposal, performance.scene_proposal_id) if performance else None
        events = db.scalars(select(TimelineEvent).where(
            TimelineEvent.project_id == project_id,
            TimelineEvent.scene_id == scene.id,
            TimelineEvent.active.is_(True),
            TimelineEvent.event_type == TimelineEventType.STATE_CHANGE,
        ).order_by(TimelineEvent.sequence, TimelineEvent.ordinal, TimelineEvent.id)).all()
        payload = {
            "scene_id": scene.id,
            "sequence": scene.sequence,
            "world_time": scene.world_time,
            # This row is the formal Scene projection.  Proposal metadata has
            # distinct legacy semantics and is retained only in the bounded
            # recent-signature payload below.
            "location_id": scene.location,
            "participant_ids": sorted(str(value) for value in (scene.participants or [])),
            "thread_ids": sorted(str(value) for value in (scene.story_threads or [])),
            "proposal_type": _value(proposal.proposal_type) if proposal else None,
            "primary_thread_id": proposal.primary_thread_id if proposal else None,
            "checkpoint_id": checkpoint.id,
            "checkpoint_fingerprint": checkpoint.checkpoint_fingerprint,
            "state_change_events": [
                {"id": event.id, "target_type": event.target_type, "target_id": event.target_id,
                 "path": event.path, "fingerprint": event.event_fingerprint}
                for event in events
            ],
            "execution_binding_id": binding.id if binding else None,
        }
        payload["feature_fingerprint"] = stable_fingerprint(payload, "scene-history-feature-v1")
        return payload

    @staticmethod
    def signature(scene: Scene, proposal: SceneProposal | None) -> dict[str, Any]:
        """The frozen legacy ``_signature`` contract, not formal Scene data."""
        return {
            "scene_id": scene.id, "sequence": scene.sequence,
            "proposal_type": _value(proposal.proposal_type) if proposal else None,
            "primary_thread_id": proposal.primary_thread_id if proposal else None,
            "participants": sorted(str(value) for value in (proposal.participants if proposal else scene.participants or [])),
            # Intentionally no fallback for a proposal with no location_id.
            "location_id": proposal.location_id if proposal else scene.location,
        }


class ProjectHistoryProjectionService:
    feature_builder = SceneHistoryFeatureBuilder()
    fingerprint_builder = HistoryProjectionFingerprintBuilder()

    def _project_lock(self, db: Session, project_id: str) -> Project:
        project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
        if not project:
            raise ValueError("SCALING_PROJECTION_PROJECT_NOT_FOUND")
        return project

    def _projection(self, db: Session, project_id: str) -> ProjectHistoryProjection | None:
        return db.scalar(select(ProjectHistoryProjection).where(ProjectHistoryProjection.project_id == project_id))

    def current_source_fingerprint(self, db: Session, project_id: str) -> str:
        """Read only the current Gravity inputs, never the full event/scene history."""
        project = db.get(Project, project_id)
        if not project:
            raise ValueError("SCALING_PROJECTION_PROJECT_NOT_FOUND")
        latest = db.scalar(select(Scene).where(
            Scene.project_id == project_id, Scene.status == SceneStatus.OCCURRED,
            Scene.history_status == "ACTIVE",
        ).order_by(Scene.sequence.desc(), Scene.id.desc()).limit(1))
        checkpoint_fingerprint = None
        if latest:
            checkpoint = db.scalar(select(SceneStateCheckpoint).where(
                SceneStateCheckpoint.project_id == project_id,
                SceneStateCheckpoint.scene_id == latest.id,
                SceneStateCheckpoint.active.is_(True),
            ).order_by(SceneStateCheckpoint.version.desc()).limit(1))
            checkpoint_fingerprint = checkpoint.checkpoint_fingerprint if checkpoint else None
        heads = db.scalars(select(CurrentStateChangeHead).where(
            CurrentStateChangeHead.project_id == project_id,
        ).order_by(CurrentStateChangeHead.target_type, CurrentStateChangeHead.target_id, CurrentStateChangeHead.path)).all()
        head_events = {}
        if heads:
            head_events = {row.id: row for row in db.scalars(select(TimelineEvent).where(
                TimelineEvent.id.in_([head.timeline_event_id for head in heads]),
            )).all()}
        active_character_ids = list(db.scalars(select(Character.id).where(
            Character.project_id == project_id, Character.active.is_(True),
        ).order_by(Character.id)).all())
        return stable_fingerprint(_canonical({
            "protocol": PROJECTION_PROTOCOL,
            "latest": {"id": latest.id if latest else None, "sequence": latest.sequence if latest else 0,
                       "checkpoint": checkpoint_fingerprint,
                       "location": latest.location if latest else None,
                       "participants": sorted(latest.participants or []) if latest else [],
                       "story_threads": sorted(latest.story_threads or []) if latest else []},
            "state_heads": [{"target_type": head.target_type, "target_id": head.target_id, "path": head.path,
                             "timeline_event_id": head.timeline_event_id,
                             "active": bool(head_events.get(head.timeline_event_id) and head_events[head.timeline_event_id].active),
                             "event_fingerprint": head_events.get(head.timeline_event_id).event_fingerprint if head_events.get(head.timeline_event_id) else None}
                            for head in heads],
            # Thread alignment is projected against the *current* active set.
            # Other Gravity inputs are live reads and must not stale history.
            "active_character_ids": active_character_ids,
        }), "project-history-source-v1")

    def rebuild(self, db: Session, project_id: str, *, project_locked: bool = False) -> ProjectHistoryProjection:
        if project_locked:
            if not db.get(Project, project_id):
                raise ValueError("SCALING_PROJECTION_PROJECT_NOT_FOUND")
        else:
            self._project_lock(db, project_id)
        projection = self._projection(db, project_id)
        if projection is None:
            projection = ProjectHistoryProjection(project_id=project_id, protocol_version=PROJECTION_PROTOCOL,
                                                  status=HistoryProjectionStatus.REBUILDING,
                                                  recent_scene_signatures=[], thread_stats={}, character_stats={})
            db.add(projection)
            db.flush()
        else:
            projection.status = HistoryProjectionStatus.REBUILDING
        scenes = db.scalars(select(Scene).where(
            Scene.project_id == project_id, Scene.status == SceneStatus.OCCURRED,
            Scene.history_status == "ACTIVE",
        ).order_by(Scene.sequence, Scene.id)).all()
        features: list[SceneHistoryFeature] = []
        active_ids = set()
        for scene in scenes:
            values = self.feature_builder.build(db, project_id, scene)
            feature = db.scalar(select(SceneHistoryFeature).where(
                SceneHistoryFeature.project_id == project_id, SceneHistoryFeature.scene_id == scene.id,
            ))
            if feature is None:
                feature = SceneHistoryFeature(project_id=project_id, scene_id=scene.id, **self._feature_columns(values))
                db.add(feature)
            else:
                self._assign_feature(feature, values)
            active_ids.add(scene.id)
            features.append(feature)
        for feature in db.scalars(select(SceneHistoryFeature).where(SceneHistoryFeature.project_id == project_id)).all():
            if feature.scene_id not in active_ids:
                feature.active = False
        db.flush()
        self._rebuild_heads(db, project_id)
        active_character_ids = self._active_character_ids(db, project_id)
        self._assign_projection(db, projection, features, active_character_ids)
        projection.status = HistoryProjectionStatus.READY
        projection.dirty_from_sequence = None
        projection.last_rebuilt_at = datetime.utcnow()
        projection.source_history_fingerprint = self.current_source_fingerprint(db, project_id)
        db.flush()
        return projection

    def rebuild_from_sequence(self, db: Session, project_id: str, from_sequence: int, *, project_locked: bool = False) -> ProjectHistoryProjection:
        """A safe full derivation; feature identities preserve unchanged prefixes."""
        self.mark_dirty(db, project_id, from_sequence)
        return self.rebuild(db, project_id, project_locked=project_locked)

    def sync_after_scene_commit(self, db: Session, project_or_commit: Any, scene_id: str | None = None) -> None:
        """Best-effort derived append.  Formal commit correctness remains independent."""
        if scene_id is None:
            project_id = project_or_commit.project_id
            scene_id = project_or_commit.scene_id
        else:
            project_id = project_or_commit
        try:
            with db.begin_nested():
                self._sync_after_scene_commit(db, project_id, scene_id)
        except Exception:
            self.mark_dirty(db, project_id, None)

    def _sync_after_scene_commit(self, db: Session, project_id: str, scene_id: str) -> None:
        self._project_lock(db, project_id)
        scene = db.get(Scene, scene_id)
        if not scene or scene.project_id != project_id:
            raise ValueError("SCALING_PROJECTION_INTEGRITY_INVALID")
        projection = self._projection(db, project_id)
        if projection is None:
            projection = ProjectHistoryProjection(project_id=project_id, protocol_version=PROJECTION_PROTOCOL,
                                                  status=HistoryProjectionStatus.DIRTY,
                                                  recent_scene_signatures=[], thread_stats={}, character_stats={})
            db.add(projection)
            db.flush()
            if scene.sequence != 1:
                projection.dirty_from_sequence = 1
                return
            projection.status = HistoryProjectionStatus.READY
        if _value(projection.status) != HistoryProjectionStatus.READY.value or projection.built_through_sequence != scene.sequence - 1:
            self._mark_dirty_row(projection, scene.sequence)
            return
        active_character_ids = self._active_character_ids(db, project_id)
        stored_active_ids = set((projection.thread_stats or {}).get(THREAD_STATS_META_KEY, {}).get("active_character_ids", []))
        if stored_active_ids != active_character_ids:
            self._mark_dirty_row(projection, scene.sequence)
            return
        existing = db.scalar(select(SceneHistoryFeature).where(
            SceneHistoryFeature.project_id == project_id, SceneHistoryFeature.scene_id == scene.id,
        ))
        if existing and existing.active:
            return
        values = self.feature_builder.build(db, project_id, scene)
        feature = existing or SceneHistoryFeature(project_id=project_id, scene_id=scene.id, **self._feature_columns(values))
        if existing is None:
            db.add(feature)
        else:
            self._assign_feature(feature, values)
        db.flush()
        self._upsert_heads_for_scene(db, project_id, scene.id)
        self._append_projection(db, projection, feature, active_character_ids)
        projection.source_history_fingerprint = self.current_source_fingerprint(db, project_id)
        projection.status = HistoryProjectionStatus.READY
        projection.dirty_from_sequence = None
        db.flush()

    def sync_after_replay_commit(self, db: Session, project_or_session: Any, from_sequence: int | None = None) -> None:
        if from_sequence is None:
            project_id = project_or_session.project_id
            from_sequence = 1
        else:
            project_id = project_or_session
        try:
            with db.begin_nested():
                # Replay already owns the Project row lock.  Avoid taking a
                # second lock while preserving the same serialized boundary.
                self.rebuild_from_sequence(db, project_id, from_sequence, project_locked=True)
        except Exception:
            self.mark_dirty(db, project_id, from_sequence)

    def mark_dirty(self, db: Session, project_id: str, from_sequence: int | None) -> ProjectHistoryProjection:
        projection = self._projection(db, project_id)
        if projection is None:
            projection = ProjectHistoryProjection(project_id=project_id, protocol_version=PROJECTION_PROTOCOL,
                                                  status=HistoryProjectionStatus.DIRTY,
                                                  recent_scene_signatures=[], thread_stats={}, character_stats={})
            db.add(projection)
        self._mark_dirty_row(projection, from_sequence)
        return projection

    @staticmethod
    def _mark_dirty_row(projection: ProjectHistoryProjection, from_sequence: int | None) -> None:
        projection.status = HistoryProjectionStatus.DIRTY
        if from_sequence is not None:
            projection.dirty_from_sequence = min(projection.dirty_from_sequence, from_sequence) if projection.dirty_from_sequence is not None else from_sequence

    def status(self, db: Session, project_id: str) -> dict[str, Any]:
        projection = self._projection(db, project_id)
        feature_count = db.scalar(select(func.count(SceneHistoryFeature.id)).where(
            SceneHistoryFeature.project_id == project_id, SceneHistoryFeature.active.is_(True))) or 0
        head_count = db.scalar(select(func.count(CurrentStateChangeHead.id)).where(
            CurrentStateChangeHead.project_id == project_id)) or 0
        return {
            "history_projection_status": _value(projection.status) if projection else "MISSING",
            "built_through_sequence": projection.built_through_sequence if projection else 0,
            "active_scene_count": projection.active_scene_count if projection else 0,
            "scene_feature_count": feature_count, "current_state_head_count": head_count,
            "dirty_from_sequence": projection.dirty_from_sequence if projection else None,
            "projection_fingerprint": projection.projection_fingerprint if projection else None,
            "fast_path_available": bool(projection and _value(projection.status) == HistoryProjectionStatus.READY.value and projection.source_history_fingerprint == self.current_source_fingerprint(db, project_id)),
            "protocol_version": projection.protocol_version if projection else PROJECTION_PROTOCOL,
        }

    def fast_context(self, db: Session, project_id: str, builder: Any) -> dict[str, Any] | None:
        projection = self._projection(db, project_id)
        if not projection or _value(projection.status) != HistoryProjectionStatus.READY.value:
            return None
        if projection.source_history_fingerprint != self.current_source_fingerprint(db, project_id):
            return None
        project = db.get(Project, project_id)
        if not project:
            return None
        characters = db.scalars(select(Character).where(Character.project_id == project_id, Character.active.is_(True)).order_by(Character.id)).all()
        threads = db.scalars(select(StoryThread).where(StoryThread.project_id == project_id, StoryThread.status.in_((ThreadStatus.OPEN, ThreadStatus.PAUSED))).order_by(StoryThread.id)).all()
        arc = db.scalar(select(StoryArc).where(StoryArc.project_id == project_id, StoryArc.status == "ACTIVE").order_by(StoryArc.id).limit(1))
        locations = db.scalars(select(WorldEntity).where(WorldEntity.project_id == project_id, WorldEntity.active.is_(True), WorldEntity.entity_type == EntityType.LOCATION).order_by(WorldEntity.id)).all()
        knowledge, knowledge_rows_by_character, memories = [], {}, []
        reader = ActiveCharacterCognitionReader()
        for character in characters:
            rows = reader.knowledge(db, project_id, character.id)
            knowledge_rows_by_character[character.id] = rows
            knowledge.extend({"knowledge_id": row.id, "character_id": character.id, "status": _value(row.status), "confidence": row.confidence, "proposition": row.proposition, "fact_identity": row.proposition} for row in rows)
            memories.extend({"memory_id": row.id, "character_id": character.id, "importance": row.importance, "emotional_weight": row.emotional_weight} for row in reader.memories(db, project_id, character.id))
        reveals = [{"canon_fact_id": row.canon_fact_id, "status": RevealStatus.AVAILABLE.value,
                    "allowed_character_ids": sorted(row.allowed_character_ids or [])}
                   for row in db.scalars(select(RevealConstraint).where(RevealConstraint.project_id == project_id).order_by(RevealConstraint.id)).all()
                   if _value(row.status) == RevealStatus.AVAILABLE.value]
        features = db.scalars(select(SceneHistoryFeature).where(
            SceneHistoryFeature.project_id == project_id, SceneHistoryFeature.active.is_(True),
        ).order_by(SceneHistoryFeature.sequence.desc(), SceneHistoryFeature.scene_id.desc()).limit(RECENT_SCENE_LIMIT)).all()
        features.reverse()
        heads = db.scalars(select(CurrentStateChangeHead).where(CurrentStateChangeHead.project_id == project_id).order_by(
            CurrentStateChangeHead.sequence, CurrentStateChangeHead.ordinal, CurrentStateChangeHead.target_type,
            CurrentStateChangeHead.target_id, CurrentStateChangeHead.path)).all()
        head_scene_ids = sorted({head.scene_id for head in heads if head.scene_id})
        feature_by_scene = {}
        if head_scene_ids:
            feature_by_scene = {row.scene_id: row for row in db.scalars(select(SceneHistoryFeature).where(
                SceneHistoryFeature.project_id == project_id, SceneHistoryFeature.active.is_(True),
                SceneHistoryFeature.scene_id.in_(head_scene_ids),
            )).all()}
        events = {}
        if heads:
            events = {row.id: row for row in db.scalars(select(TimelineEvent).where(
                TimelineEvent.project_id == project_id,
                TimelineEvent.id.in_([head.timeline_event_id for head in heads]),
            )).all()}
        changes = []
        for head in heads:
            event = events.get(head.timeline_event_id)
            if not event or not event.active:
                return None
            feature = feature_by_scene.get(event.scene_id)
            changes.append({"id": event.id, "sequence": event.sequence, "ordinal": event.ordinal,
                            "scene_id": event.scene_id, "target_type": event.target_type,
                            "target_id": event.target_id, "path": event.path,
                            "before_value": event.before_value, "after_value": event.after_value,
                            "event_fingerprint": event.event_fingerprint,
                            "thread_ids": list(feature.thread_ids or []) if feature else []})
        scenes = [{"id": row.scene_id, "sequence": row.sequence, "location_id": row.location_id,
                   "participants": sorted(row.participant_ids or []), "story_threads": sorted(row.thread_ids or [])}
                  for row in features]
        return {
            "protocol_version": "story-gravity-context-v2", "project": {"id": project.id, "story_seed": project.story_seed, "autonomy_settings": project.autonomy_settings},
            "current_sequence": projection.built_through_sequence, "scenes": scenes,
            "recent_scene_signatures": copy.deepcopy(projection.recent_scene_signatures or []),
            "history_stats": {"thread_stats": copy.deepcopy(projection.thread_stats or {}), "character_stats": copy.deepcopy(projection.character_stats or {}), "projection_fingerprint": projection.projection_fingerprint},
            "characters": [builder._character(row, knowledge_rows_by_character.get(row.id, [])) for row in characters],
            "story_threads": [builder._thread(row) for row in threads], "story_arc": builder._arc(arc),
            "locations": [{"id": row.id, "name": row.name} for row in locations],
            "knowledge": sorted(knowledge, key=lambda item: (item["character_id"], item["knowledge_id"])),
            "memories": sorted(memories, key=lambda item: (item["character_id"], item["memory_id"])),
            "state_changes": changes, "causal_links": [], "reveals": reveals,
        }

    @staticmethod
    def _feature_columns(values: dict[str, Any]) -> dict[str, Any]:
        events = values["state_change_events"]
        return {"sequence": values["sequence"], "active": True, "world_time": values["world_time"],
                "location_id": values["location_id"], "participant_ids": values["participant_ids"],
                "thread_ids": values["thread_ids"], "proposal_type": values["proposal_type"],
                "primary_thread_id": values["primary_thread_id"], "checkpoint_id": values["checkpoint_id"],
                "checkpoint_fingerprint": values["checkpoint_fingerprint"], "state_change_count": len(events),
                "state_change_targets": sorted({f"{item['target_type']}:{item['target_id']}" for item in events}),
                "state_change_paths": sorted({item["path"] for item in events if item["path"]}),
                "thread_state_event_ids": sorted(item["id"] for item in events if item["target_type"] == "STORY_THREAD"),
                "feature_fingerprint": values["feature_fingerprint"]}

    def _assign_feature(self, feature: SceneHistoryFeature, values: dict[str, Any]) -> None:
        for key, value in self._feature_columns(values).items():
            setattr(feature, key, copy.deepcopy(value))

    @staticmethod
    def _active_character_ids(db: Session, project_id: str) -> set[str]:
        return set(db.scalars(select(Character.id).where(
            Character.project_id == project_id, Character.active.is_(True),
        )).all())

    @staticmethod
    def _proposal_for_scene(db: Session, project_id: str, scene_id: str) -> SceneProposal | None:
        binding = db.scalar(select(SceneExecutionBinding).where(
            SceneExecutionBinding.project_id == project_id,
            SceneExecutionBinding.scene_id == scene_id,
            SceneExecutionBinding.active.is_(True),
        ))
        performance = db.get(ScenePerformance, binding.performance_id) if binding else None
        return db.get(SceneProposal, performance.scene_proposal_id) if performance else None

    def _signature(self, db: Session, project_id: str, feature: SceneHistoryFeature) -> dict[str, Any]:
        scene = db.get(Scene, feature.scene_id)
        if not scene:
            raise ValueError("SCALING_PROJECTION_INTEGRITY_INVALID")
        return self.feature_builder.signature(scene, self._proposal_for_scene(db, project_id, scene.id))

    def _assign_projection(self, db: Session, projection: ProjectHistoryProjection,
                           features: list[SceneHistoryFeature], active_character_ids: set[str]) -> None:
        features = sorted(features, key=lambda row: (row.sequence, row.scene_id))
        projection.protocol_version = PROJECTION_PROTOCOL
        projection.built_through_sequence = features[-1].sequence if features else 0
        projection.active_scene_count = len(features)
        projection.last_scene_id = features[-1].scene_id if features else None
        projection.recent_scene_signatures = [self._signature(db, projection.project_id, row) for row in features[-RECENT_SCENE_LIMIT:]]
        projection.thread_stats = self._thread_stats(features, active_character_ids)
        projection.character_stats = self._character_stats(features)
        projection.projection_fingerprint = self.fingerprint_builder.build(features)

    def _append_projection(self, db: Session, projection: ProjectHistoryProjection,
                           feature: SceneHistoryFeature, active_character_ids: set[str]) -> None:
        projection.protocol_version = PROJECTION_PROTOCOL
        projection.built_through_sequence = feature.sequence
        projection.active_scene_count += 1
        projection.last_scene_id = feature.scene_id
        signatures = list(projection.recent_scene_signatures or []) + [self._signature(db, projection.project_id, feature)]
        projection.recent_scene_signatures = signatures[-RECENT_SCENE_LIMIT:]
        thread_stats = copy.deepcopy(projection.thread_stats or {})
        for thread_id in feature.thread_ids or []:
            row = thread_stats.setdefault(thread_id, {"last_touched_sequence": None, "scene_count": 0, "aligned_participant_ids": [], "scene_alignment_count": 0})
            row["last_touched_sequence"] = feature.sequence
            row["scene_count"] += 1
            row["aligned_participant_ids"] = sorted(set(row["aligned_participant_ids"]) | set(feature.participant_ids or []))
            row["scene_alignment_count"] += 1 if set(feature.participant_ids or []).intersection(active_character_ids) else 0
        character_stats = copy.deepcopy(projection.character_stats or {})
        for character_id in feature.participant_ids or []:
            row = character_stats.setdefault(character_id, {"last_participation_sequence": None, "scene_count": 0})
            row["last_participation_sequence"] = feature.sequence
            row["scene_count"] += 1
        projection.thread_stats, projection.character_stats = thread_stats, character_stats
        projection.projection_fingerprint = self.fingerprint_builder.extend(projection.projection_fingerprint, feature.feature_fingerprint)

    @staticmethod
    def _thread_stats(features: list[SceneHistoryFeature], active_character_ids: set[str]) -> dict[str, Any]:
        stats: dict[str, Any] = {
            THREAD_STATS_META_KEY: {"active_character_ids": sorted(active_character_ids)},
        }
        for feature in features:
            for thread_id in feature.thread_ids or []:
                row = stats.setdefault(thread_id, {"last_touched_sequence": None, "scene_count": 0, "aligned_participant_ids": [], "scene_alignment_count": 0})
                row["last_touched_sequence"] = feature.sequence
                row["scene_count"] += 1
                row["aligned_participant_ids"] = sorted(set(row["aligned_participant_ids"]) | set(feature.participant_ids or []))
                row["scene_alignment_count"] += 1 if set(feature.participant_ids or []).intersection(active_character_ids) else 0
        return stats

    @staticmethod
    def _character_stats(features: list[SceneHistoryFeature]) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        for feature in features:
            for character_id in feature.participant_ids or []:
                row = stats.setdefault(character_id, {"last_participation_sequence": None, "scene_count": 0})
                row["last_participation_sequence"] = feature.sequence
                row["scene_count"] += 1
        return stats

    def _rebuild_heads(self, db: Session, project_id: str) -> None:
        db.execute(delete(CurrentStateChangeHead).where(CurrentStateChangeHead.project_id == project_id))
        events = db.scalars(select(TimelineEvent).where(
            TimelineEvent.project_id == project_id, TimelineEvent.active.is_(True),
            TimelineEvent.event_type == TimelineEventType.STATE_CHANGE,
        ).order_by(TimelineEvent.sequence, TimelineEvent.ordinal, TimelineEvent.id)).all()
        current: dict[tuple[str, str, str], TimelineEvent] = {}
        for event in events:
            if event.target_type and event.target_id and event.path:
                current[(event.target_type, event.target_id, event.path)] = event
        for event in current.values():
            db.add(self._head(event, project_id))

    def _upsert_heads_for_scene(self, db: Session, project_id: str, scene_id: str) -> None:
        events = db.scalars(select(TimelineEvent).where(
            TimelineEvent.project_id == project_id, TimelineEvent.scene_id == scene_id,
            TimelineEvent.active.is_(True), TimelineEvent.event_type == TimelineEventType.STATE_CHANGE,
        ).order_by(TimelineEvent.sequence, TimelineEvent.ordinal, TimelineEvent.id)).all()
        for event in events:
            if not event.target_type or not event.target_id or not event.path:
                continue
            head = db.scalar(select(CurrentStateChangeHead).where(
                CurrentStateChangeHead.project_id == project_id,
                CurrentStateChangeHead.target_type == event.target_type,
                CurrentStateChangeHead.target_id == event.target_id,
                CurrentStateChangeHead.path == event.path,
            ))
            if head is None:
                db.add(self._head(event, project_id))
            elif (head.sequence or -1, head.ordinal or -1, head.timeline_event_id) <= (event.sequence or -1, event.ordinal or -1, event.id):
                self._assign_head(head, event)

    @staticmethod
    def _head(event: TimelineEvent, project_id: str) -> CurrentStateChangeHead:
        return CurrentStateChangeHead(project_id=project_id, timeline_event_id=event.id, scene_id=event.scene_id,
                                      sequence=event.sequence, ordinal=event.ordinal, target_type=event.target_type,
                                      target_id=event.target_id, path=event.path, event_fingerprint=event.event_fingerprint)

    @staticmethod
    def _assign_head(head: CurrentStateChangeHead, event: TimelineEvent) -> None:
        head.timeline_event_id, head.scene_id = event.id, event.scene_id
        head.sequence, head.ordinal = event.sequence, event.ordinal
        head.event_fingerprint = event.event_fingerprint


class SceneHistoryFeatureAudit:
    def audit(self, db: Session, project_id: str) -> None:
        service = ProjectHistoryProjectionService()
        for feature in db.scalars(select(SceneHistoryFeature).where(
            SceneHistoryFeature.project_id == project_id, SceneHistoryFeature.active.is_(True),
        ).order_by(SceneHistoryFeature.sequence, SceneHistoryFeature.scene_id)).all():
            scene = db.get(Scene, feature.scene_id)
            if not scene or scene.project_id != project_id:
                raise ValueError("SCALING_PROJECTION_INTEGRITY_INVALID")
            expected = service.feature_builder.build(db, project_id, scene)
            if feature.feature_fingerprint != expected["feature_fingerprint"] or feature.checkpoint_id != expected["checkpoint_id"]:
                raise ValueError("SCALING_PROJECTION_INTEGRITY_INVALID")


class ProjectHistoryProjectionAudit:
    def audit(self, db: Session, project_id: str) -> None:
        service = ProjectHistoryProjectionService()
        projection = service._projection(db, project_id)
        if not projection or _value(projection.status) != HistoryProjectionStatus.READY.value:
            raise ValueError("SCALING_PROJECTION_UNAVAILABLE")
        SceneHistoryFeatureAudit().audit(db, project_id)
        features = db.scalars(select(SceneHistoryFeature).where(
            SceneHistoryFeature.project_id == project_id, SceneHistoryFeature.active.is_(True),
        ).order_by(SceneHistoryFeature.sequence, SceneHistoryFeature.scene_id)).all()
        expected_fingerprint = service.fingerprint_builder.build(features)
        active_character_ids = service._active_character_ids(db, project_id)
        expected_recent = [service._signature(db, project_id, row) for row in features[-RECENT_SCENE_LIMIT:]]
        if (
            projection.projection_fingerprint != expected_fingerprint
            or projection.thread_stats != service._thread_stats(features, active_character_ids)
            or projection.character_stats != service._character_stats(features)
            or projection.recent_scene_signatures != expected_recent
            or projection.active_scene_count != len(features)
            or projection.built_through_sequence != (features[-1].sequence if features else 0)
            or projection.last_scene_id != (features[-1].scene_id if features else None)
        ):
            raise ValueError("SCALING_PROJECTION_INTEGRITY_INVALID")
        if projection.source_history_fingerprint != service.current_source_fingerprint(db, project_id):
            raise ValueError("SCALING_PROJECTION_INTEGRITY_INVALID")
        events = db.scalars(select(TimelineEvent).where(
            TimelineEvent.project_id == project_id, TimelineEvent.active.is_(True),
            TimelineEvent.event_type == TimelineEventType.STATE_CHANGE,
        ).order_by(TimelineEvent.sequence, TimelineEvent.ordinal, TimelineEvent.id)).all()
        expected: dict[tuple[str, str, str], TimelineEvent] = {}
        for event in events:
            if event.target_type and event.target_id and event.path:
                expected[(event.target_type, event.target_id, event.path)] = event
        heads = db.scalars(select(CurrentStateChangeHead).where(CurrentStateChangeHead.project_id == project_id)).all()
        actual = {(row.target_type, row.target_id, row.path): row.timeline_event_id for row in heads}
        if actual != {key: event.id for key, event in expected.items()}:
            raise ValueError("SCALING_PROJECTION_INTEGRITY_INVALID")
        for head in heads:
            event = expected.get((head.target_type, head.target_id, head.path))
            if not event or head.event_fingerprint != event.event_fingerprint or head.sequence != event.sequence or head.ordinal != event.ordinal:
                raise ValueError("SCALING_PROJECTION_INTEGRITY_INVALID")
