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
    ProjectNarrativeStructureProjection, Scene,
)
from .narrative_structure import (
    ChapterBoundaryScorer, NarrativeArcFormationEngine, NarrativeSceneFeatureBuilder,
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
            db.add(volume); db.flush(); db.add(NarrativeVolumeArcBinding(volume_id=volume.id, narrative_arc_id=current_arc.id, ordinal=1)); return
        arcs = self._open_volume_arcs(db, volume)
        if new_arc is None:
            plan = self._volume_plan(volume.number, [self._arc_as_plan(db, item, config) for item in arcs], "OPEN", config); self._assign_volume(volume, plan); return
        candidate = [self._arc_as_plan(db, item, config) for item in arcs] + [self._arc_as_plan(db, new_arc, config)]
        formed = NarrativeVolumeFormationEngine().form(candidate, config)
        if len(formed) == 1:
            plan = self._volume_plan(volume.number, candidate, "OPEN", config); self._assign_volume(volume, plan)
            db.add(NarrativeVolumeArcBinding(volume_id=volume.id, narrative_arc_id=new_arc.id, ordinal=len(arcs) + 1)); return
        volume.status = NarrativeVolumeStatus.SEALED
        self._assign_volume(volume, self._volume_plan(volume.number, [self._arc_as_plan(db, item, config) for item in arcs], "SEALED", config))
        number = (db.scalar(select(func.max(NarrativeVolume.number)).where(NarrativeVolume.project_id == project_id)) or 0) + 1
        plan = self._volume_plan(number, [self._arc_as_plan(db, new_arc, config)], "OPEN", config)
        next_volume = NarrativeVolume(project_id=project_id, structure_revision_id=revision.id, number=number, title=None, active=True, **{key: plan[key] for key in ("status", "start_sequence", "end_sequence", "dominant_thread_ids", "structure_metadata", "structure_fingerprint")})
        db.add(next_volume); db.flush(); db.add(NarrativeVolumeArcBinding(volume_id=next_volume.id, narrative_arc_id=new_arc.id, ordinal=1))

    def _update_open_arc(self, db: Session, project_id: str, revision: NarrativeStructureRevision, chapter: Chapter, new_chapter: Chapter | None, config: NarrativeStructureConfig) -> None:
        arc = db.scalar(select(NarrativeArc).where(NarrativeArc.project_id == project_id, NarrativeArc.active.is_(True), NarrativeArc.status == NarrativeArcStatus.OPEN).order_by(NarrativeArc.number.desc()).limit(1))
        if arc is None:
            plan = self._arc_plan(1, [self._chapter_plan(1, self._chapter_features(db, chapter), _value(chapter.structure_status), chapter.boundary_metadata or {})], "OPEN", config)
            arc = NarrativeArc(project_id=project_id, structure_revision_id=revision.id, number=1, active=True, **{key: plan[key] for key in ("status", "start_sequence", "end_sequence", "dominant_thread_ids", "supporting_thread_ids", "structure_metadata", "structure_fingerprint")})
            db.add(arc); db.flush(); db.add(NarrativeArcChapterBinding(narrative_arc_id=arc.id, chapter_id=chapter.id, ordinal=1)); self._update_open_volume(db, project_id, revision, arc, None, config); return
        chapters = self._open_arc_chapters(db, arc)
        if new_chapter is None:
            self._assign_arc(arc, self._arc_plan(arc.number, chapters, "OPEN", config)); self._update_open_volume(db, project_id, revision, arc, None, config); return
        candidate = chapters + [self._chapter_plan(new_chapter.number, self._chapter_features(db, new_chapter), _value(new_chapter.structure_status), new_chapter.boundary_metadata or {})]
        formed = NarrativeArcFormationEngine().form(candidate, config)
        if len(formed) == 1:
            self._assign_arc(arc, self._arc_plan(arc.number, candidate, "OPEN", config)); db.add(NarrativeArcChapterBinding(narrative_arc_id=arc.id, chapter_id=new_chapter.id, ordinal=len(chapters) + 1)); self._update_open_volume(db, project_id, revision, arc, None, config); return
        self._assign_arc(arc, self._arc_plan(arc.number, chapters, "SEALED", config))
        number = (db.scalar(select(func.max(NarrativeArc.number)).where(NarrativeArc.project_id == project_id)) or 0) + 1
        plan = self._arc_plan(number, [candidate[-1]], "OPEN", config)
        next_arc = NarrativeArc(project_id=project_id, structure_revision_id=revision.id, number=number, active=True, **{key: plan[key] for key in ("status", "start_sequence", "end_sequence", "dominant_thread_ids", "supporting_thread_ids", "structure_metadata", "structure_fingerprint")})
        db.add(next_arc); db.flush(); db.add(NarrativeArcChapterBinding(narrative_arc_id=next_arc.id, chapter_id=new_chapter.id, ordinal=1)); self._update_open_volume(db, project_id, revision, arc, next_arc, config)

    def _append_structure(self, db: Session, project_id: str, revision: NarrativeStructureRevision, feature: dict[str, Any], config: NarrativeStructureConfig) -> None:
        chapter = db.scalar(select(Chapter).where(Chapter.project_id == project_id, Chapter.active.is_(True), Chapter.structure_status == ChapterStructureStatus.PROVISIONAL).order_by(Chapter.number.desc()).limit(1))
        if chapter is None:
            plan = self._chapter_plan(1, [feature], "PROVISIONAL", {"reason_codes": ["HISTORY_START"], "score": 0.0, "components": {}})
            chapter = Chapter(project_id=project_id, number=1, title=None, source_scene_ids=plan["scene_ids"], content=None, word_count=0, quality_report={}, status="DRAFT", structure_revision_id=revision.id, active=True, structure_status=ChapterStructureStatus.PROVISIONAL, start_sequence=plan["start_sequence"], end_sequence=plan["end_sequence"], structure_fingerprint=plan["structure_fingerprint"], boundary_metadata=plan["boundary_metadata"])
            db.add(chapter); db.flush(); db.add(ChapterSceneBinding(chapter_id=chapter.id, scene_id=feature["scene_id"], ordinal=1, scene_sequence=feature["sequence"])); self._update_open_arc(db, project_id, revision, chapter, None, config); return
        features = self._chapter_features(db, chapter)
        boundary = ChapterBoundaryScorer().score(features[-1], feature, len(features), config)
        hard = len(features) >= config.chapter_max_scenes
        soft = len(features) >= config.chapter_min_scenes and boundary["score"] >= config.chapter_boundary_threshold
        if not hard and not soft:
            features.append(feature); self._assign_chapter(chapter, self._chapter_plan(chapter.number, features, "PROVISIONAL", chapter.boundary_metadata or {})); db.add(ChapterSceneBinding(chapter_id=chapter.id, scene_id=feature["scene_id"], ordinal=len(features), scene_sequence=feature["sequence"])); self._update_open_arc(db, project_id, revision, chapter, None, config); return
        if hard and "HARD_MAX_SCENES" not in boundary["reason_codes"]: boundary["reason_codes"] = sorted([*boundary["reason_codes"], "HARD_MAX_SCENES"])
        self._assign_chapter(chapter, self._chapter_plan(chapter.number, features, "SEALED", chapter.boundary_metadata or {}))
        number = (db.scalar(select(func.max(Chapter.number)).where(Chapter.project_id == project_id)) or 0) + 1
        plan = self._chapter_plan(number, [feature], "PROVISIONAL", boundary)
        next_chapter = Chapter(project_id=project_id, number=number, title=None, source_scene_ids=plan["scene_ids"], content=None, word_count=0, quality_report={}, status="DRAFT", structure_revision_id=revision.id, active=True, structure_status=ChapterStructureStatus.PROVISIONAL, start_sequence=plan["start_sequence"], end_sequence=plan["end_sequence"], structure_fingerprint=plan["structure_fingerprint"], boundary_metadata=plan["boundary_metadata"])
        db.add(next_chapter); db.flush(); db.add(ChapterSceneBinding(chapter_id=next_chapter.id, scene_id=feature["scene_id"], ordinal=1, scene_sequence=feature["sequence"])); self._update_open_arc(db, project_id, revision, chapter, next_chapter, config)

    def sync_after_scene_commit(self, db: Session, project_id: str, scene_id: str) -> None:
        """Best-effort projection update. Formal Scene commit remains authoritative."""
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
                if projection is None:
                    projection = self._ensure_projection(db, project_id)
                    if scene.sequence != 1:
                        self.mark_dirty(db, project_id, 1, "PROJECTION_BASELINE_MISSING"); return
                    projection.status = NarrativeStructureProjectionStatus.READY
                    projection.config_fingerprint = config_fp
                    projection.feature_accumulator = _empty_accumulator()
                    projection.built_through_sequence = 0
                if _value(projection.status) != "READY" or projection.config_fingerprint != config_fp or projection.built_through_sequence != scene.sequence - 1:
                    self.mark_dirty(db, project_id, scene.sequence, "APPEND_BOUNDARY_INVALID"); return
                existing = db.scalar(select(NarrativeStructureSceneFeature).where(NarrativeStructureSceneFeature.project_id == project_id, NarrativeStructureSceneFeature.scene_id == scene_id))
                if existing and existing.active: return
                # Use the frozen structure source contract for exactly one
                # Scene.  Phase16A is a separate accelerator whose checkpoint
                # version handling must not alter structure semantics.
                source = NarrativeStructureSourceFingerprintBuilder()._scene(db, scene)
                feature = NarrativeSceneFeatureBuilder().one(source)
                source_fp = stable_fingerprint(source, "narrative-structure-scene-source-v1")
                row = existing or NarrativeStructureSceneFeature(project_id=project_id, scene_id=scene_id, active=True, **self._feature_values(feature, source_fp))
                if existing is None: db.add(row)
                else:
                    for key, value in self._feature_values(feature, source_fp).items(): setattr(row, key, value)
                    row.active = True
                db.flush()
                accumulator = _accumulate(projection.feature_accumulator, feature["feature_fingerprint"], 1)
                source_root = _accumulator_fingerprint(accumulator)
                projection.feature_accumulator = accumulator; projection.source_feature_fingerprint = source_root
                projection.structure_fingerprint = revision.structure_fingerprint if revision else _structure_fingerprint(source_root, config_fp)
                projection.active_revision_id = revision.id if revision and revision.source_max_sequence == scene.sequence else None
                projection.built_through_sequence = scene.sequence
                projection.sealed_through_sequence = db.scalar(select(func.max(Chapter.end_sequence)).where(
                    Chapter.project_id == project_id, Chapter.active.is_(True),
                    Chapter.structure_status == ChapterStructureStatus.SEALED,
                )) or 0
                projection.tail_start_sequence = projection.sealed_through_sequence + 1; projection.status = NarrativeStructureProjectionStatus.READY
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
        # Chapters/Arcs/Volumes remain in the D1 formal scope.  Their tail
        # updates are safe, but identity must be rebuilt before it can certify
        # a subsequent StateDelta or checkpoint boundary.
        from .formal_state import FormalStateIdentityService
        FormalStateIdentityService().mark_dirty(db, project_id, "NARRATIVE_STRUCTURE_APPEND")
        return True

    def sync_after_history_change(self, db: Session, project_id: str, from_sequence: int | None = 1, reason: str = "HISTORY_CHANGED") -> None:
        with db.begin_nested(): self.mark_dirty(db, project_id, from_sequence, reason)

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
