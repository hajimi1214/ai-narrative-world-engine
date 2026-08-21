"""Bounded append projection for deterministic narrative structure formation.

The existing formation engines remain the semantic authority.  This module
stores their scene inputs and advances only the open Chapter/Arc/Volume tail;
full source reads remain available for explicit rebuild and audit.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import event, func, inspect, select
from sqlalchemy.orm import Session

from .execution_trace import stable_fingerprint
from .models import (
    Chapter, ChapterSceneBinding, ChapterStructureStatus, NarrativeArc,
    NarrativeArcChapterBinding, NarrativeArcStatus, NarrativeStructureProjectionStatus,
    NarrativeStructureRevision, NarrativeStructureSceneFeature, NarrativeVolume,
    NarrativeVolumeArcBinding, NarrativeVolumeStatus, Project,
    ProjectNarrativeStructureProjection, Scene, SceneExecutionBinding,
    ScenePerformance, SceneProposal,
)
from .narrative_structure import (
    ChapterBoundaryScorer, ChapterFormationEngine, NarrativeArcFormationEngine, NarrativeSceneFeatureBuilder,
    NarrativeStructureConfig, NarrativeStructureSourceFingerprintBuilder,
    NarrativeVolumeFormationEngine, _thread_ranking,
)


PROJECTION_PROTOCOL = "narrative-structure-projection-v1"
SOURCE_PROTOCOL = "narrative-structure-source-v2"
STRUCTURE_PROTOCOL = "narrative-structure-v2"
_MODULUS = (1 << 256) - 189


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _digest(value: str) -> int:
    return int(sha256(value.encode("utf-8")).hexdigest(), 16)


def _empty_accumulator() -> dict[str, int]:
    return {"count": 0, "xor": 0, "sum": 0}


def _accumulate(state: dict[str, Any] | None, fingerprint: str, multiplier: int) -> dict[str, int]:
    value = _digest(fingerprint)
    result = dict(state or _empty_accumulator())
    result["count"] = int(result.get("count", 0)) + multiplier
    result["xor"] = int(result.get("xor", 0)) ^ value
    result["sum"] = (int(result.get("sum", 0)) + (multiplier * value)) % _MODULUS
    return result


def _accumulator_fingerprint(state: dict[str, Any]) -> str:
    return stable_fingerprint(
        {"count": int(state.get("count", 0)), "xor": str(state.get("xor", 0)), "sum": str(state.get("sum", 0))},
        SOURCE_PROTOCOL,
    )


def _config_fingerprint(config: NarrativeStructureConfig) -> str:
    return stable_fingerprint(asdict(config), "narrative-structure-config-v1")


def _structure_fingerprint(source_fingerprint: str, config_fingerprint: str) -> str:
    return stable_fingerprint({"source_fingerprint": source_fingerprint, "config_fingerprint": config_fingerprint}, STRUCTURE_PROTOCOL)


class NarrativeStructureProjectionService:
    """Keeps an O(1) source identity and an O(open-tail) formal projection."""

    def _projection(self, db: Session, project_id: str) -> ProjectNarrativeStructureProjection | None:
        return db.scalar(select(ProjectNarrativeStructureProjection).where(ProjectNarrativeStructureProjection.project_id == project_id))

    @staticmethod
    def _feature_values(feature: dict[str, Any], source_fingerprint: str) -> dict[str, Any]:
        return {
            "sequence": feature["sequence"], "world_time": feature.get("world_time"),
            "location_id": feature.get("location_id"), "participant_ids": list(feature.get("participant_ids") or []),
            "thread_ids": list(feature.get("thread_ids") or []), "primary_thread_id": feature.get("primary_thread_id"),
            "proposal_type": feature.get("proposal_type"), "state_change_count": feature.get("state_change_count", 0),
            "state_change_targets": list(feature.get("state_change_targets") or []),
            "state_change_paths": list(feature.get("state_change_paths") or []),
            "thread_state_event_ids": list(feature.get("thread_state_event_ids") or []),
            "checkpoint_fingerprint": feature.get("checkpoint_fingerprint"),
            "source_fingerprint": source_fingerprint, "feature_fingerprint": feature["feature_fingerprint"],
        }

    @staticmethod
    def _feature_payload(row: NarrativeStructureSceneFeature) -> dict[str, Any]:
        return {
            "scene_id": row.scene_id, "sequence": row.sequence, "world_time": row.world_time,
            "location_id": row.location_id, "participant_ids": list(row.participant_ids or []),
            "thread_ids": list(row.thread_ids or []), "primary_thread_id": row.primary_thread_id,
            "proposal_type": row.proposal_type, "state_change_count": row.state_change_count,
            "state_change_targets": list(row.state_change_targets or []),
            "state_change_paths": list(row.state_change_paths or []),
            "thread_state_event_ids": list(row.thread_state_event_ids or []),
            "checkpoint_fingerprint": row.checkpoint_fingerprint, "feature_fingerprint": row.feature_fingerprint,
        }

    def _ensure_projection(self, db: Session, project_id: str) -> ProjectNarrativeStructureProjection:
        projection = self._projection(db, project_id)
        if projection:
            return projection
        projection = ProjectNarrativeStructureProjection(
            project_id=project_id, protocol_version=PROJECTION_PROTOCOL,
            status=NarrativeStructureProjectionStatus.DIRTY, feature_accumulator=_empty_accumulator(),
        )
        db.add(projection); db.flush()
        return projection

    @staticmethod
    def _provisional_source(db: Session, scene: Scene, items: list[Any]) -> dict[str, Any]:
        """Build the one-scene structure input available before its ledger exists.

        Boundary formation consumes only the state-change shape, not Timeline
        IDs or checkpoint fingerprints.  ``sync_after_scene_commit`` replaces
        this row with the canonical frozen source after the ledger is written.
        """
        binding = db.scalar(select(SceneExecutionBinding).where(
            SceneExecutionBinding.project_id == scene.project_id,
            SceneExecutionBinding.scene_id == scene.id,
            SceneExecutionBinding.active.is_(True),
        ))
        performance = db.get(ScenePerformance, binding.performance_id) if binding else None
        proposal = db.get(SceneProposal, performance.scene_proposal_id) if performance else None
        events = [{
            "id": f"pending:{item.id}", "event_type": "STATE_CHANGE",
            "event_fingerprint": item.semantic_fingerprint,
            "target_type": _value(item.target_type), "target_id": item.target_id,
            "path": item.path, "sequence": scene.sequence, "ordinal": item.ordinal,
        } for item in sorted(items, key=lambda row: (row.ordinal, row.id))]
        return {
            "scene_id": scene.id, "sequence": scene.sequence,
            "world_time": scene.world_time.isoformat() if scene.world_time else None,
            "location_id": scene.location, "participant_ids": sorted(set(scene.participants or [])),
            "thread_ids": sorted(set(scene.story_threads or [])), "status": _value(scene.status),
            "history_status": scene.history_status, "checkpoint_fingerprint": None,
            "checkpoint_id": None,
            "execution": {
                "binding_id": binding.id if binding else None,
                "proposal_type": _value(proposal.proposal_type) if proposal else None,
                "primary_thread_id": proposal.primary_thread_id if proposal else None,
                "participant_ids": sorted(set(proposal.participants or [])) if proposal else [],
                "location_id": (proposal.location_id or proposal.proposed_location) if proposal else None,
            },
            "timeline_events": events,
        }

    def _replace_feature(
        self,
        db: Session,
        projection: ProjectNarrativeStructureProjection,
        scene: Scene,
        source: dict[str, Any],
    ) -> NarrativeStructureSceneFeature:
        feature = NarrativeSceneFeatureBuilder().one(source)
        source_fp = stable_fingerprint(source, "narrative-structure-scene-source-v1")
        row = db.scalar(select(NarrativeStructureSceneFeature).where(
            NarrativeStructureSceneFeature.project_id == scene.project_id,
            NarrativeStructureSceneFeature.scene_id == scene.id,
        ))
        accumulator = projection.feature_accumulator or _empty_accumulator()
        if row and row.active:
            accumulator = _accumulate(accumulator, row.feature_fingerprint, -1)
        if row is None:
            row = NarrativeStructureSceneFeature(
                project_id=scene.project_id, scene_id=scene.id, active=True,
                **self._feature_values(feature, source_fp),
            )
            db.add(row)
        else:
            for key, value in self._feature_values(feature, source_fp).items():
                setattr(row, key, value)
            row.active = True
        projection.feature_accumulator = _accumulate(accumulator, feature["feature_fingerprint"], 1)
        projection.source_feature_fingerprint = _accumulator_fingerprint(projection.feature_accumulator)
        return row

    def mark_dirty(self, db: Session, project_id: str, from_sequence: int | None = None, reason: str = "SOURCE_CHANGED") -> ProjectNarrativeStructureProjection:
        projection = self._ensure_projection(db, project_id)
        projection.status = NarrativeStructureProjectionStatus.DIRTY
        projection.dirty_from_sequence = min(projection.dirty_from_sequence, from_sequence) if projection.dirty_from_sequence is not None and from_sequence is not None else (from_sequence if from_sequence is not None else projection.dirty_from_sequence)
        projection.dirty_reason = reason
        return projection

    def _replace_features_from_authority(self, db: Session, project_id: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
        source, _ = NarrativeStructureSourceFingerprintBuilder().build(db, project_id)
        features = NarrativeSceneFeatureBuilder().build(source)
        existing = {item.scene_id: item for item in db.scalars(select(NarrativeStructureSceneFeature).where(NarrativeStructureSceneFeature.project_id == project_id)).all()}
        seen: set[str] = set(); accumulator = _empty_accumulator()
        source_by_id = {item["scene_id"]: stable_fingerprint(item, "narrative-structure-scene-source-v1") for item in source}
        for feature in features:
            seen.add(feature["scene_id"]); row = existing.get(feature["scene_id"])
            values = self._feature_values(feature, source_by_id[feature["scene_id"]])
            if row is None:
                row = NarrativeStructureSceneFeature(project_id=project_id, scene_id=feature["scene_id"], active=True, **values); db.add(row)
            else:
                for key, value in values.items(): setattr(row, key, value)
                row.active = True
            accumulator = _accumulate(accumulator, feature["feature_fingerprint"], 1)
        for scene_id, row in existing.items():
            if scene_id not in seen: row.active = False
        db.flush()
        return features, accumulator

    def adopt_full_sync(self, db: Session, project_id: str, revision: NarrativeStructureRevision) -> ProjectNarrativeStructureProjection:
        """Record an explicit full sync as the trustworthy projection baseline."""
        project = db.get(Project, project_id)
        if not project:
            raise LookupError("PROJECT_NOT_FOUND")
        features, accumulator = self._replace_features_from_authority(db, project_id)
        config = NarrativeStructureConfig.resolve(project, revision.config)
        projection = self._ensure_projection(db, project_id)
        source_fingerprint = _accumulator_fingerprint(accumulator)
        projection.protocol_version = PROJECTION_PROTOCOL
        projection.status = NarrativeStructureProjectionStatus.READY
        projection.config_fingerprint = _config_fingerprint(config)
        projection.feature_accumulator = accumulator
        projection.source_feature_fingerprint = source_fingerprint
        projection.structure_fingerprint = _structure_fingerprint(source_fingerprint, projection.config_fingerprint)
        projection.active_revision_id = revision.id
        projection.built_through_sequence = max((item["sequence"] for item in features), default=0)
        projection.sealed_through_sequence = max((item.end_sequence or 0 for item in db.scalars(select(Chapter).where(Chapter.project_id == project_id, Chapter.active.is_(True), Chapter.structure_status == ChapterStructureStatus.SEALED)).all()), default=0)
        projection.tail_start_sequence = projection.sealed_through_sequence + 1
        projection.dirty_from_sequence = None; projection.dirty_reason = None; projection.last_rebuilt_at = datetime.utcnow()
        return projection

    def rebuild(self, db: Session, project_id: str, config_data: dict[str, Any] | None = None) -> ProjectNarrativeStructureProjection:
        """Explicit O(N) rebuild, delegated to the frozen full formation service."""
        from .narrative_structure import NarrativeStructureService
        # Normal append acquires this same project row before it advances the
        # open tail.  Keep the order Project lock -> structure reads/writes
        # identical for explicit rebuild to serialize both paths on PG.
        project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
        if not project:
            raise LookupError("PROJECT_NOT_FOUND")
        # An operator explicitly asked for authority re-derivation; do not
        # shortcut through an otherwise healthy open-tail projection.
        self.mark_dirty(db, project_id, 1, "EXPLICIT_REBUILD")
        revision, _ = NarrativeStructureService().sync(db, project_id, config_data)
        return self.adopt_full_sync(db, project_id, revision)

    def _chapter_features(self, db: Session, chapter: Chapter) -> list[dict[str, Any]]:
        ids = list(chapter.source_scene_ids or [])
        rows = db.scalars(select(NarrativeStructureSceneFeature).where(
            NarrativeStructureSceneFeature.project_id == chapter.project_id,
            NarrativeStructureSceneFeature.scene_id.in_(ids), NarrativeStructureSceneFeature.active.is_(True),
        ).order_by(NarrativeStructureSceneFeature.sequence)).all() if ids else []
        if [row.scene_id for row in rows] != ids:
            raise ValueError("NARRATIVE_STRUCTURE_PROJECTION_FEATURE_MISSING")
        return [self._feature_payload(row) for row in rows]

    @staticmethod
    def _chapter_plan(number: int, features: list[dict[str, Any]], status: str, boundary: dict[str, Any]) -> dict[str, Any]:
        value = {"number": number, "status": status, "start_sequence": features[0]["sequence"], "end_sequence": features[-1]["sequence"], "scene_ids": [item["scene_id"] for item in features], "boundary_metadata": boundary, "features": features}
        value["structure_fingerprint"] = stable_fingerprint({key: value[key] for key in ("number", "status", "start_sequence", "end_sequence", "scene_ids", "boundary_metadata")}, "narrative-chapter-v1")
        return value

    @staticmethod
    def _assign_chapter(chapter: Chapter, plan: dict[str, Any]) -> None:
        chapter.source_scene_ids = plan["scene_ids"]; chapter.structure_status = ChapterStructureStatus(plan["status"])
        chapter.start_sequence = plan["start_sequence"]; chapter.end_sequence = plan["end_sequence"]
        chapter.boundary_metadata = plan["boundary_metadata"]; chapter.structure_fingerprint = plan["structure_fingerprint"]

    def _open_arc_chapters(self, db: Session, arc: NarrativeArc) -> list[dict[str, Any]]:
        ids = db.scalars(select(NarrativeArcChapterBinding.chapter_id).where(NarrativeArcChapterBinding.narrative_arc_id == arc.id).order_by(NarrativeArcChapterBinding.ordinal)).all()
        chapters = {item.id: item for item in db.scalars(select(Chapter).where(Chapter.id.in_(ids))).all()}
        result = []
        for chapter_id in ids:
            chapter = chapters.get(chapter_id)
            if not chapter:
                raise ValueError("NARRATIVE_STRUCTURE_BINDING_INVALID")
            result.append(self._chapter_plan(chapter.number, self._chapter_features(db, chapter), _value(chapter.structure_status), chapter.boundary_metadata or {}))
        return result

    @staticmethod
    def _arc_plan(number: int, chapters: list[dict[str, Any]], status: str, config: NarrativeStructureConfig) -> dict[str, Any]:
        features = [feature for chapter in chapters for feature in chapter["features"]]
        dominant, supporting = _thread_ranking(features)
        value = {"number": number, "status": status, "start_sequence": chapters[0]["start_sequence"], "end_sequence": chapters[-1]["end_sequence"], "chapter_numbers": [item["number"] for item in chapters], "dominant_thread_ids": dominant, "supporting_thread_ids": supporting, "structure_metadata": {"reason_codes": ["HARD_MAX_CHAPTERS"] if len(chapters) >= config.arc_max_chapters else []}}
        value["structure_fingerprint"] = stable_fingerprint(value, "narrative-arc-v1")
        return value

    @staticmethod
    def _assign_arc(arc: NarrativeArc, plan: dict[str, Any]) -> None:
        arc.status = NarrativeArcStatus(plan["status"]); arc.start_sequence = plan["start_sequence"]; arc.end_sequence = plan["end_sequence"]
        arc.dominant_thread_ids = plan["dominant_thread_ids"]; arc.supporting_thread_ids = plan["supporting_thread_ids"]
        arc.structure_metadata = plan["structure_metadata"]; arc.structure_fingerprint = plan["structure_fingerprint"]

    def _open_volume_arcs(self, db: Session, volume: NarrativeVolume) -> list[NarrativeArc]:
        ids = db.scalars(select(NarrativeVolumeArcBinding.narrative_arc_id).where(NarrativeVolumeArcBinding.volume_id == volume.id).order_by(NarrativeVolumeArcBinding.ordinal)).all()
        arcs = {item.id: item for item in db.scalars(select(NarrativeArc).where(NarrativeArc.id.in_(ids))).all()}
        result = [arcs[item] for item in ids if item in arcs]
        if len(result) != len(ids): raise ValueError("NARRATIVE_STRUCTURE_VOLUME_COVERAGE_INVALID")
        return result

    def _arc_as_plan(self, db: Session, arc: NarrativeArc, config: NarrativeStructureConfig) -> dict[str, Any]:
        return self._arc_plan(arc.number, self._open_arc_chapters(db, arc), _value(arc.status), config)

    @staticmethod
    def _volume_plan(number: int, arcs: list[dict[str, Any]], status: str, config: NarrativeStructureConfig) -> dict[str, Any]:
        counts = Counter(thread for arc in arcs for thread in arc["dominant_thread_ids"])
        dominant = sorted(counts, key=lambda item: (-counts[item], item))
        value = {"number": number, "status": status, "start_sequence": arcs[0]["start_sequence"], "end_sequence": arcs[-1]["end_sequence"], "arc_numbers": [item["number"] for item in arcs], "dominant_thread_ids": dominant, "structure_metadata": {"reason_codes": ["HARD_MAX_ARCS"] if len(arcs) >= config.volume_max_arcs else []}}
        value["structure_fingerprint"] = stable_fingerprint(value, "narrative-volume-v1")
        return value

    @staticmethod
    def _assign_volume(volume: NarrativeVolume, plan: dict[str, Any]) -> None:
        volume.status = NarrativeVolumeStatus(plan["status"]); volume.start_sequence = plan["start_sequence"]; volume.end_sequence = plan["end_sequence"]
        volume.dominant_thread_ids = plan["dominant_thread_ids"]; volume.structure_metadata = plan["structure_metadata"]; volume.structure_fingerprint = plan["structure_fingerprint"]

    def _update_open_volume(self, db: Session, project_id: str, revision: NarrativeStructureRevision, current_arc: NarrativeArc, new_arc: NarrativeArc | None, config: NarrativeStructureConfig) -> None:
        volume = db.scalar(select(NarrativeVolume).where(NarrativeVolume.project_id == project_id, NarrativeVolume.active.is_(True), NarrativeVolume.status == NarrativeVolumeStatus.OPEN).order_by(NarrativeVolume.number.desc()).limit(1))
        if volume is None:
            plan = self._volume_plan(1, [self._arc_as_plan(db, current_arc, config)], "OPEN", config)
            volume = NarrativeVolume(project_id=project_id, structure_revision_id=revision.id, number=1, title=None, active=True, **{key: plan[key] for key in ("status", "start_sequence", "end_sequence", "dominant_thread_ids", "structure_metadata", "structure_fingerprint")})
            db.add(volume); db.flush(); db.add(NarrativeVolumeArcBinding(volume_id=volume.id, narrative_arc_id=current_arc.id, ordinal=1)); db.flush(); return
        arcs = self._open_volume_arcs(db, volume)
        if new_arc is None:
            plan = self._volume_plan(volume.number, [self._arc_as_plan(db, item, config) for item in arcs], "OPEN", config); self._assign_volume(volume, plan); return
        candidate = [self._arc_as_plan(db, item, config) for item in arcs] + [self._arc_as_plan(db, new_arc, config)]
        formed = NarrativeVolumeFormationEngine().form(candidate, config)
        if len(formed) == 1:
            plan = self._volume_plan(volume.number, candidate, "OPEN", config); self._assign_volume(volume, plan)
            db.add(NarrativeVolumeArcBinding(volume_id=volume.id, narrative_arc_id=new_arc.id, ordinal=len(arcs) + 1)); db.flush(); return
        volume.status = NarrativeVolumeStatus.SEALED
        self._assign_volume(volume, self._volume_plan(volume.number, [self._arc_as_plan(db, item, config) for item in arcs], "SEALED", config))
        number = (db.scalar(select(func.max(NarrativeVolume.number)).where(NarrativeVolume.project_id == project_id)) or 0) + 1
        plan = self._volume_plan(number, [self._arc_as_plan(db, new_arc, config)], "OPEN", config)
        next_volume = NarrativeVolume(project_id=project_id, structure_revision_id=revision.id, number=number, title=None, active=True, **{key: plan[key] for key in ("status", "start_sequence", "end_sequence", "dominant_thread_ids", "structure_metadata", "structure_fingerprint")})
        db.add(next_volume); db.flush(); db.add(NarrativeVolumeArcBinding(volume_id=next_volume.id, narrative_arc_id=new_arc.id, ordinal=1)); db.flush()

    def _update_open_arc(self, db: Session, project_id: str, revision: NarrativeStructureRevision, chapter: Chapter, new_chapter: Chapter | None, config: NarrativeStructureConfig) -> None:
        arc = db.scalar(select(NarrativeArc).where(NarrativeArc.project_id == project_id, NarrativeArc.active.is_(True), NarrativeArc.status == NarrativeArcStatus.OPEN).order_by(NarrativeArc.number.desc()).limit(1))
        if arc is None:
            plan = self._arc_plan(1, [self._chapter_plan(1, self._chapter_features(db, chapter), _value(chapter.structure_status), chapter.boundary_metadata or {})], "OPEN", config)
            arc = NarrativeArc(project_id=project_id, structure_revision_id=revision.id, number=1, active=True, **{key: plan[key] for key in ("status", "start_sequence", "end_sequence", "dominant_thread_ids", "supporting_thread_ids", "structure_metadata", "structure_fingerprint")})
            db.add(arc); db.flush(); db.add(NarrativeArcChapterBinding(narrative_arc_id=arc.id, chapter_id=chapter.id, ordinal=1)); db.flush(); self._update_open_volume(db, project_id, revision, arc, None, config); return
        chapters = self._open_arc_chapters(db, arc)
        if new_chapter is None:
            self._assign_arc(arc, self._arc_plan(arc.number, chapters, "OPEN", config)); self._update_open_volume(db, project_id, revision, arc, None, config); return
        candidate = chapters + [self._chapter_plan(new_chapter.number, self._chapter_features(db, new_chapter), _value(new_chapter.structure_status), new_chapter.boundary_metadata or {})]
        formed = NarrativeArcFormationEngine().form(candidate, config)
        if len(formed) == 1:
            self._assign_arc(arc, self._arc_plan(arc.number, candidate, "OPEN", config)); db.add(NarrativeArcChapterBinding(narrative_arc_id=arc.id, chapter_id=new_chapter.id, ordinal=len(chapters) + 1)); db.flush(); self._update_open_volume(db, project_id, revision, arc, None, config); return
        self._assign_arc(arc, self._arc_plan(arc.number, chapters, "SEALED", config))
        number = (db.scalar(select(func.max(NarrativeArc.number)).where(NarrativeArc.project_id == project_id)) or 0) + 1
        plan = self._arc_plan(number, [candidate[-1]], "OPEN", config)
        next_arc = NarrativeArc(project_id=project_id, structure_revision_id=revision.id, number=number, active=True, **{key: plan[key] for key in ("status", "start_sequence", "end_sequence", "dominant_thread_ids", "supporting_thread_ids", "structure_metadata", "structure_fingerprint")})
        db.add(next_arc); db.flush(); db.add(NarrativeArcChapterBinding(narrative_arc_id=next_arc.id, chapter_id=new_chapter.id, ordinal=1)); db.flush(); self._update_open_volume(db, project_id, revision, arc, next_arc, config)

    def _append_structure(self, db: Session, project_id: str, revision: NarrativeStructureRevision, feature: dict[str, Any], config: NarrativeStructureConfig) -> None:
        chapter = db.scalar(select(Chapter).where(Chapter.project_id == project_id, Chapter.active.is_(True), Chapter.structure_status == ChapterStructureStatus.PROVISIONAL).order_by(Chapter.number.desc()).limit(1))
        if chapter is None:
            plan = self._chapter_plan(1, [feature], "PROVISIONAL", {"reason_codes": ["HISTORY_START"], "score": 0.0, "components": {}})
            chapter = Chapter(project_id=project_id, number=1, title=None, source_scene_ids=plan["scene_ids"], content=None, word_count=0, quality_report={}, status="DRAFT", structure_revision_id=revision.id, active=True, structure_status=ChapterStructureStatus.PROVISIONAL, start_sequence=plan["start_sequence"], end_sequence=plan["end_sequence"], structure_fingerprint=plan["structure_fingerprint"], boundary_metadata=plan["boundary_metadata"])
            db.add(chapter); db.flush(); db.add(ChapterSceneBinding(chapter_id=chapter.id, scene_id=feature["scene_id"], ordinal=1, scene_sequence=feature["sequence"])); db.flush(); self._update_open_arc(db, project_id, revision, chapter, None, config); return
        features = self._chapter_features(db, chapter)
        boundary = ChapterBoundaryScorer().score(features[-1], feature, len(features), config)
        hard = len(features) >= config.chapter_max_scenes
        soft = len(features) >= config.chapter_min_scenes and boundary["score"] >= config.chapter_boundary_threshold
        if not hard and not soft:
            features.append(feature); self._assign_chapter(chapter, self._chapter_plan(chapter.number, features, "PROVISIONAL", chapter.boundary_metadata or {})); db.add(ChapterSceneBinding(chapter_id=chapter.id, scene_id=feature["scene_id"], ordinal=len(features), scene_sequence=feature["sequence"])); db.flush(); self._update_open_arc(db, project_id, revision, chapter, None, config); return
        if hard and "HARD_MAX_SCENES" not in boundary["reason_codes"]: boundary["reason_codes"] = sorted([*boundary["reason_codes"], "HARD_MAX_SCENES"])
        self._assign_chapter(chapter, self._chapter_plan(chapter.number, features, "SEALED", chapter.boundary_metadata or {}))
        number = (db.scalar(select(func.max(Chapter.number)).where(Chapter.project_id == project_id)) or 0) + 1
        plan = self._chapter_plan(number, [feature], "PROVISIONAL", boundary)
        next_chapter = Chapter(project_id=project_id, number=number, title=None, source_scene_ids=plan["scene_ids"], content=None, word_count=0, quality_report={}, status="DRAFT", structure_revision_id=revision.id, active=True, structure_status=ChapterStructureStatus.PROVISIONAL, start_sequence=plan["start_sequence"], end_sequence=plan["end_sequence"], structure_fingerprint=plan["structure_fingerprint"], boundary_metadata=plan["boundary_metadata"])
        db.add(next_chapter); db.flush(); db.add(ChapterSceneBinding(chapter_id=next_chapter.id, scene_id=feature["scene_id"], ordinal=1, scene_sequence=feature["sequence"])); db.flush(); self._update_open_arc(db, project_id, revision, chapter, next_chapter, config)

    def prepare_for_scene_checkpoint(
        self, db: Session, project_id: str, scene: Scene, items: list[Any],
    ) -> list[str]:
        """Advance the open formal tail before the Scene POST is captured.

        The provisional feature has the exact state-change shape available at
        this point.  The post-commit pass replaces it with the canonical
        Timeline/Checkpoint-backed feature, without changing formal rows.
        A contained failure only leaves this derived projection DIRTY.
        """
        try:
            with db.begin_nested():
                project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
                if not project or scene.project_id != project_id or scene.sequence < 1:
                    return []
                config = NarrativeStructureConfig.resolve(project)
                config_fp = _config_fingerprint(config)
                projection = self._projection(db, project_id)
                revision = db.scalar(select(NarrativeStructureRevision).where(
                    NarrativeStructureRevision.project_id == project_id,
                    NarrativeStructureRevision.active.is_(True),
                ))
                if projection is None:
                    if scene.sequence != 1:
                        self.mark_dirty(db, project_id, 1, "PROJECTION_BASELINE_MISSING")
                        return []
                    projection = self._ensure_projection(db, project_id)
                    projection.status = NarrativeStructureProjectionStatus.READY
                    projection.config_fingerprint = config_fp
                    projection.feature_accumulator = _empty_accumulator()
                    projection.built_through_sequence = 0
                    if revision is None:
                        revision = NarrativeStructureRevision(
                            project_id=project_id, active=True, protocol_version=2,
                            source_history_fingerprint="", source_max_sequence=0,
                            config=asdict(config), config_fingerprint=config_fp,
                            rebuild_from_sequence=1,
                            structure_fingerprint=_structure_fingerprint("", config_fp),
                        )
                        db.add(revision)
                        db.flush()
                if (
                    revision is None
                    or _value(projection.status) != "READY"
                    or projection.config_fingerprint != config_fp
                    or projection.built_through_sequence != scene.sequence - 1
                    or revision.source_max_sequence != scene.sequence - 1
                ):
                    self.mark_dirty(db, project_id, scene.sequence, "APPEND_BOUNDARY_INVALID")
                    return []
                prior_open_chapter_id = db.scalar(select(Chapter.id).where(
                    Chapter.project_id == project_id, Chapter.active.is_(True),
                    Chapter.structure_status == ChapterStructureStatus.PROVISIONAL,
                ).order_by(Chapter.number.desc()).limit(1))
                source = self._provisional_source(db, scene, items)
                row = self._replace_feature(db, projection, scene, source)
                self._append_structure(db, project_id, revision, self._feature_payload(row), config)
                revision.protocol_version = 2
                revision.source_history_fingerprint = projection.source_feature_fingerprint or ""
                revision.source_max_sequence = scene.sequence
                revision.config = asdict(config)
                revision.config_fingerprint = config_fp
                revision.structure_fingerprint = _structure_fingerprint(
                    revision.source_history_fingerprint, config_fp,
                )
                revision.completed_at = datetime.utcnow()
                projection.active_revision_id = revision.id
                projection.structure_fingerprint = revision.structure_fingerprint
                projection.built_through_sequence = scene.sequence
                projection.sealed_through_sequence = db.scalar(select(func.max(Chapter.end_sequence)).where(
                    Chapter.project_id == project_id, Chapter.active.is_(True),
                    Chapter.structure_status == ChapterStructureStatus.SEALED,
                )) or 0
                projection.tail_start_sequence = projection.sealed_through_sequence + 1
                projection.dirty_from_sequence = None
                projection.dirty_reason = None
                db.flush()
                current_open_chapter_id = db.scalar(select(Chapter.id).where(
                    Chapter.project_id == project_id, Chapter.active.is_(True),
                    Chapter.structure_status == ChapterStructureStatus.PROVISIONAL,
                ).order_by(Chapter.number.desc()).limit(1))
                # The only formal D2 writes in a normal append are the prior
                # open Chapter (which may seal) and its successor. This is the
                # exact D1 manifest, not a tail-range approximation.
                return sorted({item for item in (prior_open_chapter_id, current_open_chapter_id) if item})
        except Exception as exc:
            with db.begin_nested():
                self.mark_dirty(db, project_id, scene.sequence, f"TAIL_PREPARE_FAILED:{type(exc).__name__}")
            return []

    def sync_after_scene_commit(self, db: Session, project_id: str, scene_id: str) -> None:
        """Finalize a pre-checkpoint tail feature from canonical committed sources."""
        try:
            with db.begin_nested():
                project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
                if not project: return
                scene = db.get(Scene, scene_id)
                if not scene or scene.project_id != project_id or _value(scene.status) != "OCCURRED" or scene.history_status != "ACTIVE":
                    self.mark_dirty(db, project_id, None, "SCENE_SOURCE_INVALID"); return
                config = NarrativeStructureConfig.resolve(project)
                config_fp = _config_fingerprint(config)
                projection = self._projection(db, project_id)
                revision = db.scalar(select(NarrativeStructureRevision).where(NarrativeStructureRevision.project_id == project_id, NarrativeStructureRevision.active.is_(True)))
                if projection and _value(projection.status) == "DIRTY" and str(projection.dirty_reason or "").startswith("TAIL_PREPARE_FAILED:"):
                    return
                existing = db.scalar(select(NarrativeStructureSceneFeature).where(
                    NarrativeStructureSceneFeature.project_id == project_id,
                    NarrativeStructureSceneFeature.scene_id == scene_id,
                ))
                # Compatibility for explicit projection callers and legacy
                # fixtures which materialize Scene rows without SceneCommit.
                # Normal runtime always has a commit and takes the guarded
                # pre-checkpoint route above.
                from .models import SceneCommit
                committed = db.scalar(select(SceneCommit.id).where(
                    SceneCommit.project_id == project_id, SceneCommit.scene_id == scene_id,
                ))
                if committed is None and existing is None:
                    if projection is None:
                        projection = self._ensure_projection(db, project_id)
                        if scene.sequence != 1:
                            self.mark_dirty(db, project_id, 1, "PROJECTION_BASELINE_MISSING"); return
                        projection.status = NarrativeStructureProjectionStatus.READY
                        projection.config_fingerprint = config_fp
                        projection.feature_accumulator = _empty_accumulator()
                        projection.built_through_sequence = 0
                    if (
                        _value(projection.status) != "READY"
                        or projection.config_fingerprint != config_fp
                        or projection.built_through_sequence != scene.sequence - 1
                    ):
                        self.mark_dirty(db, project_id, scene.sequence, "APPEND_BOUNDARY_INVALID"); return
                    source = NarrativeStructureSourceFingerprintBuilder()._scene(db, scene)
                    self._replace_feature(db, projection, scene, source)
                    projection.built_through_sequence = scene.sequence
                    projection.status = NarrativeStructureProjectionStatus.READY
                    if revision is None:
                        from .narrative_structure import NarrativeStructureService
                        NarrativeStructureService().sync(db, project_id)
                    elif not self.append_open_tail(db, project_id, revision, config):
                        self.mark_dirty(db, project_id, scene.sequence, "TAIL_APPEND_FAILED")
                    return
                if (
                    projection is None or revision is None or existing is None
                    or not existing.active or _value(projection.status) != "READY"
                    or projection.config_fingerprint != config_fp
                    or projection.built_through_sequence != scene.sequence
                    or revision.source_max_sequence != scene.sequence
                ):
                    self.mark_dirty(db, project_id, scene.sequence, "APPEND_BOUNDARY_INVALID"); return
                # Use the frozen structure source contract for exactly one
                # Scene.  Phase16A is a separate accelerator whose checkpoint
                # version handling must not alter structure semantics.
                source = NarrativeStructureSourceFingerprintBuilder()._scene(db, scene)
                self._replace_feature(db, projection, scene, source)
                # A serialized explicit rebuild may have produced a legacy
                # revision immediately before this append finalizer acquired
                # the Project lock. The complete canonical feature projection
                # is now authoritative for the same structure rows, so record
                # the v2 source contract atomically with its fingerprint.
                revision.protocol_version = 2
                revision.source_history_fingerprint = projection.source_feature_fingerprint or ""
                revision.structure_fingerprint = _structure_fingerprint(
                    revision.source_history_fingerprint, config_fp,
                )
                revision.completed_at = datetime.utcnow()
                projection.structure_fingerprint = revision.structure_fingerprint
                projection.active_revision_id = revision.id
                projection.sealed_through_sequence = db.scalar(select(func.max(Chapter.end_sequence)).where(
                    Chapter.project_id == project_id, Chapter.active.is_(True),
                    Chapter.structure_status == ChapterStructureStatus.SEALED,
                )) or 0
                projection.tail_start_sequence = projection.sealed_through_sequence + 1
                projection.status = NarrativeStructureProjectionStatus.READY
                projection.dirty_from_sequence = None; projection.dirty_reason = None
        except Exception as exc:
            with db.begin_nested(): self.mark_dirty(db, project_id, None, f"APPEND_FAILED:{type(exc).__name__}")

    def append_open_tail(
        self,
        db: Session,
        project_id: str,
        revision: NarrativeStructureRevision,
        config: NarrativeStructureConfig,
        expected_source_fingerprint: str | None = None,
    ) -> bool:
        """Materialize only source features newer than the active revision.

        This is intentionally outside SceneCommit.  Structure rows remain
        derived formal rows, while the scene checkpoint stays an exact record
        of the world materialization that produced the Scene.
        """
        projection = self._projection(db, project_id)
        config_fp = _config_fingerprint(config)
        if not projection or _value(projection.status) != "READY" or projection.config_fingerprint != config_fp:
            return False
        if expected_source_fingerprint and expected_source_fingerprint not in {
            revision.source_history_fingerprint, projection.source_feature_fingerprint,
        }:
            raise ValueError("NARRATIVE_STRUCTURE_SOURCE_CHANGED")
        if projection.built_through_sequence <= revision.source_max_sequence:
            return False
        rows = db.scalars(select(NarrativeStructureSceneFeature).where(
            NarrativeStructureSceneFeature.project_id == project_id,
            NarrativeStructureSceneFeature.active.is_(True),
            NarrativeStructureSceneFeature.sequence > revision.source_max_sequence,
        ).order_by(NarrativeStructureSceneFeature.sequence)).all()
        expected_sequences = list(range(revision.source_max_sequence + 1, projection.built_through_sequence + 1))
        if [row.sequence for row in rows] != expected_sequences:
            self.mark_dirty(db, project_id, revision.source_max_sequence + 1, "APPEND_FEATURE_GAP")
            return False
        for row in rows:
            self._append_structure(db, project_id, revision, self._feature_payload(row), config)
        revision.protocol_version = 2
        revision.source_history_fingerprint = projection.source_feature_fingerprint or ""
        revision.source_max_sequence = projection.built_through_sequence
        revision.config = asdict(config)
        revision.config_fingerprint = config_fp
        revision.structure_fingerprint = _structure_fingerprint(
            revision.source_history_fingerprint, config_fp
        )
        revision.completed_at = datetime.utcnow()
        projection.active_revision_id = revision.id
        projection.structure_fingerprint = revision.structure_fingerprint
        projection.sealed_through_sequence = db.scalar(select(func.max(Chapter.end_sequence)).where(
            Chapter.project_id == project_id, Chapter.active.is_(True),
            Chapter.structure_status == ChapterStructureStatus.SEALED,
        )) or 0
        projection.tail_start_sequence = projection.sealed_through_sequence + 1
        return True

    def sync_after_history_change(self, db: Session, project_id: str, from_sequence: int | None = 1, reason: str = "HISTORY_CHANGED") -> None:
        with db.begin_nested(): self.mark_dirty(db, project_id, from_sequence, reason)

    def rebuild_suffix_after_history_change(
        self,
        db: Session,
        project_id: str,
        from_sequence: int,
        *,
        project_locked: bool = False,
    ) -> bool:
        """Rebuild only the affected open/containing Volume suffix.

        Historical changes can invalidate boundaries, so the safe unit is the
        Volume containing the first changed Scene.  Earlier SEALED volumes,
        arcs, and chapters retain their row identity and bindings.
        """
        try:
            with db.begin_nested():
                project_statement = select(Project).where(Project.id == project_id)
                if not project_locked:
                    project_statement = project_statement.with_for_update()
                project = db.scalar(project_statement)
                revision = db.scalar(select(NarrativeStructureRevision).where(
                    NarrativeStructureRevision.project_id == project_id,
                    NarrativeStructureRevision.active.is_(True),
                ))
                if not project or not revision:
                    self.mark_dirty(db, project_id, from_sequence, "HISTORY_REBUILD_BASELINE_MISSING")
                    return False
                config = NarrativeStructureConfig.resolve(project)
                if revision.config_fingerprint != _config_fingerprint(config):
                    self.mark_dirty(db, project_id, 1, "NARRATIVE_STRUCTURE_CONFIG_CHANGED")
                    return False
                volume = db.scalar(select(NarrativeVolume).where(
                    NarrativeVolume.project_id == project_id, NarrativeVolume.active.is_(True),
                    NarrativeVolume.start_sequence <= from_sequence,
                    NarrativeVolume.end_sequence >= from_sequence,
                ).order_by(NarrativeVolume.number).limit(1))
                tail_start = volume.start_sequence if volume else from_sequence
                affected_chapters = db.scalars(select(Chapter).where(
                    Chapter.project_id == project_id, Chapter.active.is_(True),
                    Chapter.end_sequence >= tail_start,
                )).all()
                affected_arcs = db.scalars(select(NarrativeArc).where(
                    NarrativeArc.project_id == project_id, NarrativeArc.active.is_(True),
                    NarrativeArc.end_sequence >= tail_start,
                )).all()
                affected_volumes = db.scalars(select(NarrativeVolume).where(
                    NarrativeVolume.project_id == project_id, NarrativeVolume.active.is_(True),
                    NarrativeVolume.end_sequence >= tail_start,
                )).all()
                # D2 may re-form only the genuinely open tail. Replacing an
                # already SEALED row would invalidate its stable Writer and
                # Quality lineage, even if the preceding prefix is unchanged.
                if (
                    any(_value(item.structure_status) == "SEALED" for item in affected_chapters)
                    or any(_value(item.status) == "SEALED" for item in affected_arcs)
                    or any(_value(item.status) == "SEALED" for item in affected_volumes)
                ):
                    self.mark_dirty(db, project_id, tail_start, "HISTORY_REBUILD_TOUCHES_SEALED")
                    return False
                prefix_chapters = db.scalars(select(Chapter).where(
                    Chapter.project_id == project_id, Chapter.active.is_(True),
                    Chapter.end_sequence < tail_start,
                ).order_by(Chapter.number)).all()
                prefix_arcs = db.scalars(select(NarrativeArc).where(
                    NarrativeArc.project_id == project_id, NarrativeArc.active.is_(True),
                    NarrativeArc.end_sequence < tail_start,
                ).order_by(NarrativeArc.number)).all()
                prefix_volumes = db.scalars(select(NarrativeVolume).where(
                    NarrativeVolume.project_id == project_id, NarrativeVolume.active.is_(True),
                    NarrativeVolume.end_sequence < tail_start,
                ).order_by(NarrativeVolume.number)).all()
                if any(_value(item.structure_status) != "SEALED" for item in prefix_chapters) or any(_value(item.status) != "SEALED" for item in prefix_arcs + prefix_volumes):
                    self.mark_dirty(db, project_id, tail_start, "HISTORY_REBUILD_PREFIX_NOT_SEALED")
                    return False

                scenes = db.scalars(select(Scene).where(
                    Scene.project_id == project_id, Scene.status == "OCCURRED",
                    Scene.history_status == "ACTIVE", Scene.sequence >= tail_start,
                ).order_by(Scene.sequence, Scene.id)).all()
                if not scenes:
                    self.mark_dirty(db, project_id, tail_start, "HISTORY_REBUILD_SOURCE_GAP")
                    return False
                expected_sequences = list(range(tail_start, scenes[-1].sequence + 1))
                if [scene.sequence for scene in scenes] != expected_sequences:
                    self.mark_dirty(db, project_id, tail_start, "HISTORY_REBUILD_SOURCE_GAP")
                    return False
                existing = {row.scene_id: row for row in db.scalars(select(NarrativeStructureSceneFeature).where(
                    NarrativeStructureSceneFeature.project_id == project_id,
                    NarrativeStructureSceneFeature.sequence >= tail_start,
                )).all()}
                features: list[dict[str, Any]] = []
                accumulator = dict((self._projection(db, project_id).feature_accumulator or _empty_accumulator()))
                for scene in scenes:
                    source = NarrativeStructureSourceFingerprintBuilder()._scene(db, scene)
                    feature = NarrativeSceneFeatureBuilder().one(source)
                    prior = existing.pop(scene.id, None)
                    if prior and prior.active:
                        accumulator = _accumulate(accumulator, prior.feature_fingerprint, -1)
                    source_fp = stable_fingerprint(source, "narrative-structure-scene-source-v1")
                    values = self._feature_values(feature, source_fp)
                    row = prior or NarrativeStructureSceneFeature(project_id=project_id, scene_id=scene.id, active=True, **values)
                    if prior is None:
                        db.add(row)
                    else:
                        for key, value in values.items(): setattr(row, key, value)
                        row.active = True
                    accumulator = _accumulate(accumulator, feature["feature_fingerprint"], 1)
                    features.append(feature)
                for row in existing.values():
                    if row.active:
                        accumulator = _accumulate(accumulator, row.feature_fingerprint, -1)
                    row.active = False
                db.flush()

                chapter_plans = ChapterFormationEngine().form(features, config)
                prior_feature = db.scalar(select(NarrativeStructureSceneFeature).where(
                    NarrativeStructureSceneFeature.project_id == project_id,
                    NarrativeStructureSceneFeature.active.is_(True),
                    NarrativeStructureSceneFeature.sequence == tail_start - 1,
                ))
                if prior_feature and chapter_plans:
                    previous_chapter = prefix_chapters[-1] if prefix_chapters else None
                    size = len(previous_chapter.source_scene_ids or []) if previous_chapter else 0
                    boundary = ChapterBoundaryScorer().score(self._feature_payload(prior_feature), chapter_plans[0]["features"][0], size, config)
                    if previous_chapter and size >= config.chapter_max_scenes and "HARD_MAX_SCENES" not in boundary["reason_codes"]:
                        boundary["reason_codes"] = sorted([*boundary["reason_codes"], "HARD_MAX_SCENES"])
                    chapter_plans[0]["boundary_metadata"] = boundary
                chapter_offset = len(prefix_chapters)
                for index, plan in enumerate(chapter_plans, 1):
                    plan["number"] = chapter_offset + index
                    plan["structure_fingerprint"] = stable_fingerprint({key: plan[key] for key in ("number", "status", "start_sequence", "end_sequence", "scene_ids", "boundary_metadata")}, "narrative-chapter-v1")
                arc_plans = NarrativeArcFormationEngine().form(chapter_plans, config)
                arc_offset = len(prefix_arcs)
                for index, plan in enumerate(arc_plans, 1):
                    plan["number"] = arc_offset + index
                    plan["structure_fingerprint"] = stable_fingerprint(
                        {key: value for key, value in plan.items() if key != "structure_fingerprint"},
                        "narrative-arc-v1",
                    )
                volume_plans = NarrativeVolumeFormationEngine().form(arc_plans, config)
                volume_offset = len(prefix_volumes)
                for index, plan in enumerate(volume_plans, 1):
                    plan["number"] = volume_offset + index
                    plan["structure_fingerprint"] = stable_fingerprint(
                        {key: value for key, value in plan.items() if key != "structure_fingerprint"},
                        "narrative-volume-v1",
                    )

                old_chapters = db.scalars(select(Chapter).where(Chapter.project_id == project_id, Chapter.active.is_(True), Chapter.start_sequence >= tail_start)).all()
                old_arcs = db.scalars(select(NarrativeArc).where(NarrativeArc.project_id == project_id, NarrativeArc.active.is_(True), NarrativeArc.start_sequence >= tail_start)).all()
                old_volumes = db.scalars(select(NarrativeVolume).where(NarrativeVolume.project_id == project_id, NarrativeVolume.active.is_(True), NarrativeVolume.start_sequence >= tail_start)).all()
                for row in old_chapters: row.active = False; row.structure_status = ChapterStructureStatus.SUPERSEDED
                for row in old_arcs: row.active = False; row.status = NarrativeArcStatus.SUPERSEDED
                for row in old_volumes: row.active = False; row.status = NarrativeVolumeStatus.SUPERSEDED
                revision.active = False; db.flush()
                source_root = _accumulator_fingerprint(accumulator)
                next_revision = NarrativeStructureRevision(
                    project_id=project_id, active=True, protocol_version=2,
                    source_history_fingerprint=source_root, source_max_sequence=scenes[-1].sequence,
                    config=asdict(config), config_fingerprint=_config_fingerprint(config),
                    rebuild_from_sequence=tail_start,
                    structure_fingerprint=_structure_fingerprint(source_root, _config_fingerprint(config)),
                    completed_at=datetime.utcnow(),
                )
                db.add(next_revision); db.flush()
                chapters_by_number: dict[int, Chapter] = {}
                previous_by_number = {row.number: row for row in old_chapters}
                for plan in chapter_plans:
                    chapter = Chapter(project_id=project_id, number=plan["number"], title=None, source_scene_ids=plan["scene_ids"], content=None, word_count=0, quality_report={}, status="DRAFT", structure_revision_id=next_revision.id, active=True, structure_status=plan["status"], start_sequence=plan["start_sequence"], end_sequence=plan["end_sequence"], structure_fingerprint=plan["structure_fingerprint"], boundary_metadata=plan["boundary_metadata"], supersedes_chapter_id=previous_by_number.get(plan["number"]).id if previous_by_number.get(plan["number"]) else None)
                    db.add(chapter); db.flush()
                    for ordinal, feature in enumerate(plan["features"], 1):
                        db.add(ChapterSceneBinding(chapter_id=chapter.id, scene_id=feature["scene_id"], ordinal=ordinal, scene_sequence=feature["sequence"]))
                    chapters_by_number[plan["number"]] = chapter
                for chapter in prefix_chapters: chapters_by_number[chapter.number] = chapter
                arcs_by_number: dict[int, NarrativeArc] = {}
                previous_arcs = {row.number: row for row in old_arcs}
                for plan in arc_plans:
                    arc = NarrativeArc(project_id=project_id, structure_revision_id=next_revision.id, number=plan["number"], active=True, status=plan["status"], start_sequence=plan["start_sequence"], end_sequence=plan["end_sequence"], dominant_thread_ids=plan["dominant_thread_ids"], supporting_thread_ids=plan["supporting_thread_ids"], structure_metadata=plan["structure_metadata"], structure_fingerprint=plan["structure_fingerprint"], supersedes_arc_id=previous_arcs.get(plan["number"]).id if previous_arcs.get(plan["number"]) else None)
                    db.add(arc); db.flush()
                    for ordinal, number in enumerate(plan["chapter_numbers"], 1): db.add(NarrativeArcChapterBinding(narrative_arc_id=arc.id, chapter_id=chapters_by_number[number].id, ordinal=ordinal))
                    arcs_by_number[plan["number"]] = arc
                for arc in prefix_arcs: arcs_by_number[arc.number] = arc
                previous_volumes = {row.number: row for row in old_volumes}
                for plan in volume_plans:
                    volume_row = NarrativeVolume(project_id=project_id, structure_revision_id=next_revision.id, number=plan["number"], title=None, active=True, status=plan["status"], start_sequence=plan["start_sequence"], end_sequence=plan["end_sequence"], dominant_thread_ids=plan["dominant_thread_ids"], structure_metadata=plan["structure_metadata"], structure_fingerprint=plan["structure_fingerprint"], supersedes_volume_id=previous_volumes.get(plan["number"]).id if previous_volumes.get(plan["number"]) else None)
                    db.add(volume_row); db.flush()
                    for ordinal, number in enumerate(plan["arc_numbers"], 1): db.add(NarrativeVolumeArcBinding(volume_id=volume_row.id, narrative_arc_id=arcs_by_number[number].id, ordinal=ordinal))
                projection = self._ensure_projection(db, project_id)
                projection.status = NarrativeStructureProjectionStatus.READY
                projection.feature_accumulator = accumulator; projection.source_feature_fingerprint = source_root
                projection.config_fingerprint = _config_fingerprint(config); projection.structure_fingerprint = next_revision.structure_fingerprint
                projection.active_revision_id = next_revision.id; projection.built_through_sequence = scenes[-1].sequence
                projection.sealed_through_sequence = db.scalar(select(func.max(Chapter.end_sequence)).where(Chapter.project_id == project_id, Chapter.active.is_(True), Chapter.structure_status == ChapterStructureStatus.SEALED)) or 0
                projection.tail_start_sequence = projection.sealed_through_sequence + 1
                projection.dirty_from_sequence = None; projection.dirty_reason = None; projection.last_rebuilt_at = datetime.utcnow()
                from .formal_state import FormalStateIdentityService
                FormalStateIdentityService().mark_dirty(db, project_id, "NARRATIVE_STRUCTURE_SUFFIX_REBUILD")
                return True
        except Exception:
            with db.begin_nested(): self.mark_dirty(db, project_id, from_sequence, "HISTORY_SUFFIX_REBUILD_FAILED")
            return False

    def status(self, db: Session, project_id: str) -> dict[str, Any]:
        projection = self._projection(db, project_id)
        project = db.get(Project, project_id)
        current_config_fingerprint = _config_fingerprint(NarrativeStructureConfig.resolve(project)) if project else None
        count = db.scalar(select(func.count(NarrativeStructureSceneFeature.id)).where(NarrativeStructureSceneFeature.project_id == project_id, NarrativeStructureSceneFeature.active.is_(True))) or 0
        return {"status": _value(projection.status) if projection else "MISSING", "protocol_version": projection.protocol_version if projection else PROJECTION_PROTOCOL, "active_revision_id": projection.active_revision_id if projection else None, "built_through_sequence": projection.built_through_sequence if projection else 0, "sealed_through_sequence": projection.sealed_through_sequence if projection else 0, "tail_start_sequence": projection.tail_start_sequence if projection else 1, "dirty_from_sequence": projection.dirty_from_sequence if projection else None, "dirty_reason": projection.dirty_reason if projection else None, "source_feature_fingerprint": projection.source_feature_fingerprint if projection else None, "structure_fingerprint": projection.structure_fingerprint if projection else None, "scene_feature_count": count, "config_current": bool(projection and projection.config_fingerprint == current_config_fingerprint), "fast_path_available": bool(projection and _value(projection.status) == "READY" and projection.config_fingerprint == current_config_fingerprint)}


class NarrativeStructureProjectionAudit:
    """Explicit O(N) audit: formal Scene/Timeline/Checkpoint source remains truth."""
    def audit(self, db: Session, project_id: str) -> None:
        service = NarrativeStructureProjectionService(); projection = service._projection(db, project_id)
        if not projection or _value(projection.status) != "READY": raise ValueError("NARRATIVE_STRUCTURE_PROJECTION_NOT_READY")
        source, _ = NarrativeStructureSourceFingerprintBuilder().build(db, project_id)
        expected = NarrativeSceneFeatureBuilder().build(source)
        rows = db.scalars(select(NarrativeStructureSceneFeature).where(NarrativeStructureSceneFeature.project_id == project_id, NarrativeStructureSceneFeature.active.is_(True)).order_by(NarrativeStructureSceneFeature.sequence)).all()
        if len(rows) != len(expected) or [row.scene_id for row in rows] != [item["scene_id"] for item in expected]: raise ValueError("NARRATIVE_STRUCTURE_PROJECTION_INTEGRITY_INVALID")
        accumulator = _empty_accumulator()
        for row, item in zip(rows, expected):
            if service._feature_payload(row) != item: raise ValueError("NARRATIVE_STRUCTURE_PROJECTION_INTEGRITY_INVALID")
            accumulator = _accumulate(accumulator, item["feature_fingerprint"], 1)
        source_fp = _accumulator_fingerprint(accumulator)
        if projection.feature_accumulator != accumulator or projection.source_feature_fingerprint != source_fp or projection.built_through_sequence != max((item["sequence"] for item in expected), default=0):
            raise ValueError("NARRATIVE_STRUCTURE_PROJECTION_INTEGRITY_INVALID")


@event.listens_for(Session, "before_flush")
def _mark_structure_projection_dirty_on_config_change(session: Session, flush_context, instances=None) -> None:
    """Project structure-config edits cannot silently reuse an old tail."""
    if session.info.get("narrative_structure_projection_sync"):
        return
    for project in tuple(session.dirty):
        if not isinstance(project, Project):
            continue
        if not inspect(project).attrs.autonomy_settings.history.has_changes():
            continue
        projection = session.scalar(select(ProjectNarrativeStructureProjection).where(
            ProjectNarrativeStructureProjection.project_id == project.id,
        ))
        if projection:
            projection.status = NarrativeStructureProjectionStatus.DIRTY
            projection.dirty_from_sequence = 1
            projection.dirty_reason = "NARRATIVE_STRUCTURE_CONFIG_CHANGED"
