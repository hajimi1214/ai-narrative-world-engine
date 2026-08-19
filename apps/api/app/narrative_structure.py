"""Deterministic history-derived chapter, arc, and volume formation."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .execution_trace import stable_fingerprint
from .models import (
    Chapter, ChapterSceneBinding, ChapterStructureStatus, NarrativeArc,
    NarrativeArcChapterBinding, NarrativeArcStatus, NarrativeStructureRevision,
    NarrativeVolume, NarrativeVolumeArcBinding, NarrativeVolumeStatus, Project,
    RetconApplication, RetconApplicationStatus, Scene, SceneExecutionBinding,
    ScenePerformance, SceneProposal, SceneStateCheckpoint, TimelineEvent,
)


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _ids(values: Any) -> list[str]:
    return sorted({str(value) for value in (values or []) if value is not None})


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


@dataclass(frozen=True)
class NarrativeStructureConfig:
    chapter_min_scenes: int = 2
    chapter_target_scenes: int = 4
    chapter_max_scenes: int = 6
    chapter_boundary_threshold: float = 3.0
    arc_min_chapters: int = 2
    arc_max_chapters: int = 4
    arc_boundary_threshold: float = 2.0
    volume_min_arcs: int = 2
    volume_max_arcs: int = 3
    volume_boundary_threshold: float = 2.0

    @classmethod
    def resolve(cls, project: Project, supplied: dict[str, Any] | None = None) -> "NarrativeStructureConfig":
        values = dict((project.autonomy_settings or {}).get("narrative_structure") or {})
        values.update(supplied or {})
        try:
            config = cls(**values)
        except (TypeError, ValueError) as exc:
            raise ValueError("INVALID_NARRATIVE_STRUCTURE_CONFIG") from exc
        integers = (config.chapter_min_scenes, config.chapter_target_scenes, config.chapter_max_scenes, config.arc_min_chapters, config.arc_max_chapters, config.volume_min_arcs, config.volume_max_arcs)
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in integers):
            raise ValueError("INVALID_NARRATIVE_STRUCTURE_CONFIG")
        if not config.chapter_min_scenes <= config.chapter_target_scenes <= config.chapter_max_scenes:
            raise ValueError("INVALID_NARRATIVE_STRUCTURE_CONFIG")
        if not config.arc_min_chapters <= config.arc_max_chapters or not config.volume_min_arcs <= config.volume_max_arcs:
            raise ValueError("INVALID_NARRATIVE_STRUCTURE_CONFIG")
        if any(value < 0 for value in (config.chapter_boundary_threshold, config.arc_boundary_threshold, config.volume_boundary_threshold)):
            raise ValueError("INVALID_NARRATIVE_STRUCTURE_CONFIG")
        return config


@dataclass(frozen=True)
class NarrativeStructureWeights:
    primary_thread_change: float = 1.6
    thread_discontinuity: float = 1.6
    location_transition: float = 0.8
    participant_transition: float = 0.8
    world_time_gap: float = 0.7
    proposal_type_transition: float = 0.4
    state_change_intensity: float = 0.7
    thread_state_event: float = 1.5
    target_size_pressure: float = 0.8
    arc_thread_family_transition: float = 2.0
    arc_location_phase_transition: float = 0.8
    arc_thread_state_transition: float = 1.0
    volume_thread_family_transition: float = 2.0
    volume_arc_closure: float = 0.5


class NarrativeStructureSourceFingerprintBuilder:
    protocol = "narrative-structure-source-v1"

    def build(self, db: Session, project_id: str) -> tuple[list[dict[str, Any]], str]:
        scenes = db.scalars(select(Scene).where(
            Scene.project_id == project_id,
            Scene.status == "OCCURRED",
            Scene.history_status == "ACTIVE",
        ).order_by(Scene.sequence, Scene.id)).all()
        sequences = [scene.sequence for scene in scenes]
        if sequences and (sequences[0] != 1 or any(right != left + 1 for left, right in zip(sequences, sequences[1:]))):
            raise ValueError("NARRATIVE_STRUCTURE_HISTORY_INVALID")
        rows = [self._scene(db, scene) for scene in scenes]
        return rows, stable_fingerprint(rows, self.protocol)

    def _scene(self, db: Session, scene: Scene) -> dict[str, Any]:
        checkpoints = db.scalars(select(SceneStateCheckpoint).where(
            SceneStateCheckpoint.project_id == scene.project_id,
            SceneStateCheckpoint.scene_id == scene.id,
            SceneStateCheckpoint.active.is_(True),
            SceneStateCheckpoint.capture_protocol_version == 3,
        ).order_by(SceneStateCheckpoint.version.desc(), SceneStateCheckpoint.id)).all()
        if len(checkpoints) > 1:
            raise ValueError("SCENE_CHECKPOINT_CURRENT_AMBIGUOUS")
        checkpoint = checkpoints[0] if checkpoints else None
        binding = db.scalar(select(SceneExecutionBinding).where(
            SceneExecutionBinding.project_id == scene.project_id,
            SceneExecutionBinding.scene_id == scene.id,
            SceneExecutionBinding.active.is_(True),
        ))
        performance = db.get(ScenePerformance, binding.performance_id) if binding else None
        proposal = db.get(SceneProposal, performance.scene_proposal_id) if performance else None
        events = db.scalars(select(TimelineEvent).where(
            TimelineEvent.project_id == scene.project_id,
            TimelineEvent.scene_id == scene.id,
            TimelineEvent.active.is_(True),
        ).order_by(TimelineEvent.sequence, TimelineEvent.ordinal, TimelineEvent.event_type, TimelineEvent.target_type, TimelineEvent.target_id, TimelineEvent.path, TimelineEvent.event_fingerprint)).all()
        return {
            "scene_id": scene.id,
            "sequence": scene.sequence,
            "world_time": _iso(scene.world_time),
            "location_id": scene.location,
            "participant_ids": _ids(scene.participants),
            "thread_ids": _ids(scene.story_threads),
            "status": _value(scene.status),
            "history_status": scene.history_status,
            "checkpoint_fingerprint": checkpoint.checkpoint_fingerprint if checkpoint else None,
            "checkpoint_id": checkpoint.id if checkpoint else None,
            "execution": {
                "binding_id": binding.id if binding else None,
                "proposal_type": _value(proposal.proposal_type) if proposal else None,
                "primary_thread_id": proposal.primary_thread_id if proposal else None,
                "participant_ids": _ids(proposal.participants) if proposal else [],
                "location_id": (proposal.location_id or proposal.proposed_location) if proposal else None,
            },
            "timeline_events": [{
                "id": event.id,
                "event_type": _value(event.event_type),
                "event_fingerprint": event.event_fingerprint,
                "target_type": _value(event.target_type),
                "target_id": event.target_id,
                "path": event.path,
                "sequence": event.sequence,
                "ordinal": event.ordinal,
            } for event in events],
        }


class NarrativeSceneFeatureBuilder:
    def build(self, source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.one(row) for row in source_rows]

    def one(self, row: dict[str, Any]) -> dict[str, Any]:
        state = [event for event in row["timeline_events"] if event["event_type"] == "STATE_CHANGE"]
        thread_events = [event["id"] for event in state if event["target_type"] == "STORY_THREAD" and event["path"] in {"/status", "/progress", "/state"}]
        feature = {
            "scene_id": row["scene_id"], "sequence": row["sequence"], "world_time": row["world_time"],
            "location_id": row["location_id"], "participant_ids": _ids(row["participant_ids"]), "thread_ids": _ids(row["thread_ids"]),
            "primary_thread_id": row["execution"]["primary_thread_id"], "proposal_type": row["execution"]["proposal_type"],
            "state_change_count": len(state),
            "state_change_targets": sorted({f"{event['target_type']}:{event['target_id']}" for event in state}),
            "state_change_paths": sorted({event["path"] for event in state if event["path"]}),
            "thread_state_event_ids": sorted(thread_events),
            "checkpoint_fingerprint": row["checkpoint_fingerprint"],
        }
        feature["feature_fingerprint"] = stable_fingerprint(feature, "narrative-scene-feature-v1")
        return feature


class ChapterBoundaryScorer:
    def __init__(self, weights: NarrativeStructureWeights | None = None): self.weights = weights or NarrativeStructureWeights()

    @staticmethod
    def _discontinuity(left: list[str], right: list[str]) -> float:
        a, b = set(left), set(right)
        if not a and not b: return 0.0
        return 1.0 - (len(a & b) / len(a | b))

    def score(self, left: dict[str, Any], right: dict[str, Any], current_size: int, config: NarrativeStructureConfig) -> dict[str, Any]:
        signals: dict[str, float] = {}
        if left["primary_thread_id"] != right["primary_thread_id"] and (left["primary_thread_id"] or right["primary_thread_id"]): signals["PRIMARY_THREAD_CHANGE"] = self.weights.primary_thread_change
        td = self._discontinuity(left["thread_ids"], right["thread_ids"])
        if td: signals["THREAD_SET_DISCONTINUITY"] = self.weights.thread_discontinuity * td
        if left["location_id"] != right["location_id"]: signals["LOCATION_TRANSITION"] = self.weights.location_transition
        pd = self._discontinuity(left["participant_ids"], right["participant_ids"])
        if pd: signals["PARTICIPANT_TRANSITION"] = self.weights.participant_transition * pd
        if left["proposal_type"] != right["proposal_type"] and (left["proposal_type"] or right["proposal_type"]): signals["PROPOSAL_TYPE_TRANSITION"] = self.weights.proposal_type_transition
        if right["state_change_count"]: signals["STATE_CHANGE_INTENSITY"] = self.weights.state_change_intensity * min(1.0, right["state_change_count"] / 3.0)
        if left["thread_state_event_ids"] or right["thread_state_event_ids"]: signals["THREAD_STATE_EVENT"] = self.weights.thread_state_event
        if left["world_time"] and right["world_time"]:
            try:
                gap = (datetime.fromisoformat(right["world_time"].replace("Z", "+00:00")) - datetime.fromisoformat(left["world_time"].replace("Z", "+00:00"))).total_seconds()
                if gap >= 86400: signals["WORLD_TIME_GAP"] = self.weights.world_time_gap
            except (TypeError, ValueError): pass
        if current_size >= config.chapter_target_scenes: signals["TARGET_SIZE_PRESSURE"] = self.weights.target_size_pressure
        return {"score": round(sum(signals.values()), 6), "reason_codes": sorted(signals), "components": {key: signals[key] for key in sorted(signals)}}


class ChapterFormationEngine:
    def form(self, features: list[dict[str, Any]], config: NarrativeStructureConfig) -> list[dict[str, Any]]:
        if not features: return []
        scorer, groups, current = ChapterBoundaryScorer(), [], [features[0]]
        current_boundary = {"reason_codes": ["HISTORY_START"], "score": 0.0, "components": {}}
        for feature in features[1:]:
            boundary = scorer.score(current[-1], feature, len(current), config)
            hard = len(current) >= config.chapter_max_scenes
            soft = len(current) >= config.chapter_min_scenes and boundary["score"] >= config.chapter_boundary_threshold
            if hard or soft:
                if hard and "HARD_MAX_SCENES" not in boundary["reason_codes"]: boundary["reason_codes"] = sorted([*boundary["reason_codes"], "HARD_MAX_SCENES"])
                groups.append((current, current_boundary)); current, current_boundary = [feature], boundary
            else: current.append(feature)
        groups.append((current, current_boundary))
        result = []
        for index, (group, boundary) in enumerate(groups, 1):
            value = {"number": index, "status": "PROVISIONAL" if index == len(groups) else "SEALED", "start_sequence": group[0]["sequence"], "end_sequence": group[-1]["sequence"], "scene_ids": [item["scene_id"] for item in group], "boundary_metadata": boundary, "features": group}
            value["structure_fingerprint"] = stable_fingerprint({key: value[key] for key in ("number", "status", "start_sequence", "end_sequence", "scene_ids", "boundary_metadata")}, "narrative-chapter-v1")
            result.append(value)
        return result


def _thread_ranking(features: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    counts = Counter(thread for feature in features for thread in feature["thread_ids"])
    ranked = sorted(counts, key=lambda item: (-counts[item], item))
    if not ranked: return [], []
    maximum = counts[ranked[0]]
    dominant = [item for item in ranked if counts[item] == maximum]
    return dominant, [item for item in ranked if item not in dominant]


class NarrativeArcFormationEngine:
    def form(self, chapters: list[dict[str, Any]], config: NarrativeStructureConfig) -> list[dict[str, Any]]:
        if not chapters: return []
        weights = NarrativeStructureWeights()
        groups, current = [], [chapters[0]]
        for chapter in chapters[1:]:
            left_features = [feature for item in current for feature in item["features"]]
            left, right = _thread_ranking(left_features)[0], _thread_ranking(chapter["features"])[0]
            score = weights.arc_thread_family_transition if set(left) != set(right) and (left or right) else 0.0
            left_locations = {item.get("location_id") for item in left_features if item.get("location_id")}
            right_locations = {item.get("location_id") for item in chapter["features"] if item.get("location_id")}
            if left_locations and right_locations and left_locations.isdisjoint(right_locations):
                score += weights.arc_location_phase_transition
            if any(item.get("thread_state_event_ids") for item in chapter["features"]):
                score += weights.arc_thread_state_transition
            if len(current) >= config.arc_max_chapters or (len(current) >= config.arc_min_chapters and score >= config.arc_boundary_threshold): groups.append(current); current = [chapter]
            else: current.append(chapter)
        groups.append(current)
        result = []
        for index, group in enumerate(groups, 1):
            features = [feature for chapter in group for feature in chapter["features"]]
            dominant, supporting = _thread_ranking(features)
            value = {"number": index, "status": "OPEN" if index == len(groups) else "SEALED", "start_sequence": group[0]["start_sequence"], "end_sequence": group[-1]["end_sequence"], "chapter_numbers": [item["number"] for item in group], "dominant_thread_ids": dominant, "supporting_thread_ids": supporting, "structure_metadata": {"reason_codes": ["HARD_MAX_CHAPTERS"] if len(group) >= config.arc_max_chapters else []}}
            value["structure_fingerprint"] = stable_fingerprint(value, "narrative-arc-v1"); result.append(value)
        return result


class NarrativeVolumeFormationEngine:
    def form(self, arcs: list[dict[str, Any]], config: NarrativeStructureConfig) -> list[dict[str, Any]]:
        if not arcs: return []
        weights = NarrativeStructureWeights()
        groups, current = [], [arcs[0]]
        for arc in arcs[1:]:
            changed = bool(set(current[-1]["dominant_thread_ids"]) != set(arc["dominant_thread_ids"]) and (current[-1]["dominant_thread_ids"] or arc["dominant_thread_ids"]))
            score = weights.volume_thread_family_transition if changed else 0.0
            score += weights.volume_arc_closure * sum(item.get("status") == "SEALED" for item in current)
            if len(current) >= config.volume_max_arcs or (len(current) >= config.volume_min_arcs and score >= config.volume_boundary_threshold): groups.append(current); current = [arc]
            else: current.append(arc)
        groups.append(current)
        result = []
        for index, group in enumerate(groups, 1):
            counts = Counter(thread for arc in group for thread in arc["dominant_thread_ids"])
            dominant = sorted(counts, key=lambda item: (-counts[item], item))
            value = {"number": index, "status": "OPEN" if index == len(groups) else "SEALED", "start_sequence": group[0]["start_sequence"], "end_sequence": group[-1]["end_sequence"], "arc_numbers": [item["number"] for item in group], "dominant_thread_ids": dominant, "structure_metadata": {"reason_codes": ["HARD_MAX_ARCS"] if len(group) >= config.volume_max_arcs else []}}
            value["structure_fingerprint"] = stable_fingerprint(value, "narrative-volume-v1"); result.append(value)
        return result


class NarrativeStructureService:
    DERIVED_CORRUPTION_CODES = {
        "NARRATIVE_STRUCTURE_REVISION_INVALID",
        "NARRATIVE_STRUCTURE_FINGERPRINT_INVALID",
        "NARRATIVE_STRUCTURE_NUMBERING_INVALID",
        "NARRATIVE_STRUCTURE_CHAPTER_INVALID",
        "NARRATIVE_STRUCTURE_SUPERSESSION_INVALID",
        "NARRATIVE_STRUCTURE_BINDING_INVALID",
        "NARRATIVE_STRUCTURE_SCENE_COVERAGE_INVALID",
        "NARRATIVE_STRUCTURE_ARC_INVALID",
        "NARRATIVE_STRUCTURE_ARC_COVERAGE_INVALID",
        "NARRATIVE_STRUCTURE_VOLUME_INVALID",
        "NARRATIVE_STRUCTURE_VOLUME_COVERAGE_INVALID",
    }

    def preview(self, db: Session, project_id: str, config_data: dict[str, Any] | None = None) -> dict[str, Any]:
        project = db.get(Project, project_id)
        if not project: raise LookupError("PROJECT_NOT_FOUND")
        config = NarrativeStructureConfig.resolve(project, config_data)
        source, source_fingerprint = NarrativeStructureSourceFingerprintBuilder().build(db, project_id)
        features = NarrativeSceneFeatureBuilder().build(source)
        chapters = ChapterFormationEngine().form(features, config)
        arcs = NarrativeArcFormationEngine().form(chapters, config)
        volumes = NarrativeVolumeFormationEngine().form(arcs, config)
        semantic = {"chapters": [{key: item[key] for key in ("number", "status", "start_sequence", "end_sequence", "scene_ids", "boundary_metadata", "structure_fingerprint")} for item in chapters], "arcs": arcs, "volumes": volumes, "config_fingerprint": stable_fingerprint(asdict(config), "narrative-structure-config-v1"), "source_fingerprint": source_fingerprint}
        return {"source_fingerprint": source_fingerprint, "source_max_sequence": max((item["sequence"] for item in features), default=0), "config": asdict(config), "config_fingerprint": semantic["config_fingerprint"], "structure_fingerprint": stable_fingerprint(semantic, "narrative-structure-v1"), "chapters": chapters, "narrative_arcs": arcs, "volumes": volumes}

    def sync(self, db: Session, project_id: str, config_data: dict[str, Any] | None = None, expected_source_fingerprint: str | None = None) -> tuple[NarrativeStructureRevision, bool]:
        project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
        if not project: raise LookupError("PROJECT_NOT_FOUND")
        pending = db.scalar(select(RetconApplication.id).where(RetconApplication.project_id == project_id, RetconApplication.status == RetconApplicationStatus.APPLIED_PENDING_REPLAY))
        if pending: raise ValueError("RETCON_REPLAY_REQUIRED")
        preview = self.preview(db, project_id, config_data)
        if expected_source_fingerprint and expected_source_fingerprint != preview["source_fingerprint"]: raise ValueError("NARRATIVE_STRUCTURE_SOURCE_CHANGED")
        current = db.scalar(select(NarrativeStructureRevision).where(NarrativeStructureRevision.project_id == project_id, NarrativeStructureRevision.active.is_(True)))
        force_rebuild = False
        if current and current.source_history_fingerprint == preview["source_fingerprint"] and current.config_fingerprint == preview["config_fingerprint"]:
            try:
                NarrativeStructureAudit().audit(db, project_id)
            except ValueError as exc:
                if str(exc) not in self.DERIVED_CORRUPTION_CODES:
                    raise
                force_rebuild = True
            else:
                return current, True

        _, rechecked_source_fingerprint = NarrativeStructureSourceFingerprintBuilder().build(db, project_id)
        if rechecked_source_fingerprint != preview["source_fingerprint"]:
            raise ValueError("NARRATIVE_STRUCTURE_SOURCE_CHANGED")

        old_chapters = db.scalars(select(Chapter).where(Chapter.project_id == project_id, Chapter.active.is_(True)).order_by(Chapter.number)).all()
        old_arcs = db.scalars(select(NarrativeArc).where(NarrativeArc.project_id == project_id, NarrativeArc.active.is_(True)).order_by(NarrativeArc.number)).all()
        old_volumes = db.scalars(select(NarrativeVolume).where(NarrativeVolume.project_id == project_id, NarrativeVolume.active.is_(True)).order_by(NarrativeVolume.number)).all()
        keep: dict[int, Chapter] = {}
        first_divergent_number = 1
        if not force_rebuild:
            for planned in preview["chapters"]:
                prior = next((item for item in old_chapters if item.number == planned["number"]), None)
                matches = bool(prior and prior.source_scene_ids == planned["scene_ids"] and prior.structure_fingerprint == planned["structure_fingerprint"])
                if not matches or _value(prior.structure_status) != "SEALED":
                    first_divergent_number = planned["number"]
                    break
                first_divergent_number = planned["number"] + 1
        for planned in preview["chapters"]:
            prior = next((item for item in old_chapters if item.number == planned["number"]), None)
            if planned["number"] < first_divergent_number and prior and _value(prior.structure_status) == "SEALED" and prior.source_scene_ids == planned["scene_ids"] and prior.structure_fingerprint == planned["structure_fingerprint"]:
                keep[planned["number"]] = prior
        for item in old_chapters:
            if item.number not in keep: item.active = False; item.structure_status = ChapterStructureStatus.SUPERSEDED
        for item in old_arcs: item.active = False; item.status = NarrativeArcStatus.SUPERSEDED
        for item in old_volumes: item.active = False; item.status = NarrativeVolumeStatus.SUPERSEDED
        if current: current.active = False
        db.flush()

        rebuild_from = min((item["start_sequence"] for item in preview["chapters"] if item["number"] not in keep), default=preview["source_max_sequence"] + 1)
        revision = NarrativeStructureRevision(project_id=project_id, active=True, protocol_version=1, source_history_fingerprint=preview["source_fingerprint"], source_max_sequence=preview["source_max_sequence"], config=preview["config"], config_fingerprint=preview["config_fingerprint"], rebuild_from_sequence=rebuild_from, structure_fingerprint=preview["structure_fingerprint"], completed_at=datetime.utcnow())
        db.add(revision); db.flush()
        chapters_by_number: dict[int, Chapter] = {}
        for planned in preview["chapters"]:
            prior = next((item for item in old_chapters if item.number == planned["number"]), None)
            chapter = keep.get(planned["number"])
            if chapter:
                # A retained sealed Chapter keeps the revision that created its row.
                # The active structure revision owns the projection, not the row's provenance.
                chapter.active = True
            else:
                chapter = Chapter(project_id=project_id, number=planned["number"], title=None, source_scene_ids=planned["scene_ids"], content=None, word_count=0, quality_report={}, status="DRAFT", structure_revision_id=revision.id, active=True, structure_status=planned["status"], start_sequence=planned["start_sequence"], end_sequence=planned["end_sequence"], structure_fingerprint=planned["structure_fingerprint"], boundary_metadata=planned["boundary_metadata"], supersedes_chapter_id=prior.id if prior else None)
                db.add(chapter); db.flush()
                for ordinal, feature in enumerate(planned["features"], 1): db.add(ChapterSceneBinding(chapter_id=chapter.id, scene_id=feature["scene_id"], ordinal=ordinal, scene_sequence=feature["sequence"]))
            chapters_by_number[planned["number"]] = chapter
        db.flush()
        arcs_by_number: dict[int, NarrativeArc] = {}
        for planned in preview["narrative_arcs"]:
            prior = next((item for item in old_arcs if item.number == planned["number"]), None)
            arc = NarrativeArc(project_id=project_id, structure_revision_id=revision.id, number=planned["number"], active=True, status=planned["status"], start_sequence=planned["start_sequence"], end_sequence=planned["end_sequence"], dominant_thread_ids=planned["dominant_thread_ids"], supporting_thread_ids=planned["supporting_thread_ids"], structure_metadata=planned["structure_metadata"], structure_fingerprint=planned["structure_fingerprint"], supersedes_arc_id=prior.id if prior else None)
            db.add(arc); db.flush()
            for ordinal, number in enumerate(planned["chapter_numbers"], 1): db.add(NarrativeArcChapterBinding(narrative_arc_id=arc.id, chapter_id=chapters_by_number[number].id, ordinal=ordinal))
            arcs_by_number[planned["number"]] = arc
        db.flush()
        for planned in preview["volumes"]:
            prior = next((item for item in old_volumes if item.number == planned["number"]), None)
            volume = NarrativeVolume(project_id=project_id, structure_revision_id=revision.id, number=planned["number"], title=None, active=True, status=planned["status"], start_sequence=planned["start_sequence"], end_sequence=planned["end_sequence"], dominant_thread_ids=planned["dominant_thread_ids"], structure_metadata=planned["structure_metadata"], structure_fingerprint=planned["structure_fingerprint"], supersedes_volume_id=prior.id if prior else None)
            db.add(volume); db.flush()
            for ordinal, number in enumerate(planned["arc_numbers"], 1): db.add(NarrativeVolumeArcBinding(volume_id=volume.id, narrative_arc_id=arcs_by_number[number].id, ordinal=ordinal))
        db.flush(); NarrativeStructureAudit().audit(db, project_id)
        return revision, False

    def current(self, db: Session, project_id: str) -> dict[str, Any]:
        revision = db.scalar(select(NarrativeStructureRevision).where(NarrativeStructureRevision.project_id == project_id, NarrativeStructureRevision.active.is_(True)))
        source, source_fingerprint = NarrativeStructureSourceFingerprintBuilder().build(db, project_id)
        if not revision: return {"revision": None, "stale": False, "source_fingerprint": source_fingerprint, "structure_fingerprint": None, "chapters": [], "narrative_arcs": [], "volumes": []}
        return self.payload(db, revision) | {"stale": revision.source_history_fingerprint != source_fingerprint, "source_fingerprint": source_fingerprint}

    def payload(self, db: Session, revision: NarrativeStructureRevision) -> dict[str, Any]:
        chapters = db.scalars(select(Chapter).where(Chapter.project_id == revision.project_id, Chapter.active.is_(True)).order_by(Chapter.number)).all()
        arcs = db.scalars(select(NarrativeArc).where(NarrativeArc.project_id == revision.project_id, NarrativeArc.active.is_(True)).order_by(NarrativeArc.number)).all()
        volumes = db.scalars(select(NarrativeVolume).where(NarrativeVolume.project_id == revision.project_id, NarrativeVolume.active.is_(True)).order_by(NarrativeVolume.number)).all()
        arc_chapter_ids = {item.id: db.scalars(select(NarrativeArcChapterBinding.chapter_id).where(NarrativeArcChapterBinding.narrative_arc_id == item.id).order_by(NarrativeArcChapterBinding.ordinal)).all() for item in arcs}
        volume_arc_ids = {item.id: db.scalars(select(NarrativeVolumeArcBinding.narrative_arc_id).where(NarrativeVolumeArcBinding.volume_id == item.id).order_by(NarrativeVolumeArcBinding.ordinal)).all() for item in volumes}
        return {"revision": self.revision_metadata(revision), "structure_fingerprint": revision.structure_fingerprint, "chapters": [{"id": item.id, "number": item.number, "structure_status": _value(item.structure_status), "start_sequence": item.start_sequence, "end_sequence": item.end_sequence, "source_scene_ids": item.source_scene_ids, "boundary_metadata": item.boundary_metadata, "structure_fingerprint": item.structure_fingerprint} for item in chapters], "narrative_arcs": [{"id": item.id, "number": item.number, "status": _value(item.status), "start_sequence": item.start_sequence, "end_sequence": item.end_sequence, "dominant_thread_ids": item.dominant_thread_ids, "supporting_thread_ids": item.supporting_thread_ids, "chapter_ids": arc_chapter_ids[item.id], "structure_metadata": item.structure_metadata} for item in arcs], "volumes": [{"id": item.id, "number": item.number, "status": _value(item.status), "start_sequence": item.start_sequence, "end_sequence": item.end_sequence, "dominant_thread_ids": item.dominant_thread_ids, "arc_ids": volume_arc_ids[item.id], "chapter_ids": [chapter_id for arc_id in volume_arc_ids[item.id] for chapter_id in arc_chapter_ids[arc_id]], "structure_metadata": item.structure_metadata} for item in volumes]}

    @staticmethod
    def revision_metadata(item: NarrativeStructureRevision) -> dict[str, Any]:
        return {"id": item.id, "project_id": item.project_id, "active": item.active, "protocol_version": item.protocol_version, "source_history_fingerprint": item.source_history_fingerprint, "source_max_sequence": item.source_max_sequence, "config": item.config, "config_fingerprint": item.config_fingerprint, "rebuild_from_sequence": item.rebuild_from_sequence, "structure_fingerprint": item.structure_fingerprint, "created_at": item.created_at, "completed_at": item.completed_at}


class NarrativeStructureAudit:
    def audit(self, db: Session, project_id: str) -> None:
        revisions = db.scalars(select(NarrativeStructureRevision).where(NarrativeStructureRevision.project_id == project_id, NarrativeStructureRevision.active.is_(True))).all()
        if len(revisions) != 1: raise ValueError("NARRATIVE_STRUCTURE_REVISION_INVALID")
        chapters = db.scalars(select(Chapter).where(Chapter.project_id == project_id, Chapter.active.is_(True)).order_by(Chapter.number)).all()
        arcs = db.scalars(select(NarrativeArc).where(NarrativeArc.project_id == project_id, NarrativeArc.active.is_(True)).order_by(NarrativeArc.number)).all()
        volumes = db.scalars(select(NarrativeVolume).where(NarrativeVolume.project_id == project_id, NarrativeVolume.active.is_(True)).order_by(NarrativeVolume.number)).all()
        revision = revisions[0]
        _, current_source_fingerprint = NarrativeStructureSourceFingerprintBuilder().build(db, project_id)
        if revision.source_history_fingerprint != current_source_fingerprint:
            raise ValueError("NARRATIVE_STRUCTURE_SOURCE_CHANGED")
        expected = NarrativeStructureService().preview(db, project_id, revision.config)
        if revision.structure_fingerprint != expected["structure_fingerprint"] or revision.config_fingerprint != expected["config_fingerprint"]:
            raise ValueError("NARRATIVE_STRUCTURE_FINGERPRINT_INVALID")
        if [item.number for item in chapters] != list(range(1, len(chapters) + 1)) or [item.number for item in arcs] != list(range(1, len(arcs) + 1)) or [item.number for item in volumes] != list(range(1, len(volumes) + 1)): raise ValueError("NARRATIVE_STRUCTURE_NUMBERING_INVALID")
        current_scenes = db.scalars(select(Scene).where(Scene.project_id == project_id, Scene.status == "OCCURRED", Scene.history_status == "ACTIVE").order_by(Scene.sequence)).all()
        bound = []
        expected_chapters = {item["number"]: item for item in expected["chapters"]}
        for chapter in chapters:
            creation_revision = db.get(NarrativeStructureRevision, chapter.structure_revision_id) if chapter.structure_revision_id else None
            if not creation_revision or creation_revision.project_id != project_id or not chapter.structure_fingerprint:
                raise ValueError("NARRATIVE_STRUCTURE_CHAPTER_INVALID")
            if chapter.structure_fingerprint != expected_chapters[chapter.number]["structure_fingerprint"]:
                raise ValueError("NARRATIVE_STRUCTURE_FINGERPRINT_INVALID")
            if chapter.supersedes_chapter_id:
                superseded = db.get(Chapter, chapter.supersedes_chapter_id)
                if not superseded or superseded.project_id != project_id or superseded.active:
                    raise ValueError("NARRATIVE_STRUCTURE_SUPERSESSION_INVALID")
            bindings = db.scalars(select(ChapterSceneBinding).where(ChapterSceneBinding.chapter_id == chapter.id).order_by(ChapterSceneBinding.ordinal)).all()
            ids = [item.scene_id for item in bindings]
            if ids != chapter.source_scene_ids or [item.ordinal for item in bindings] != list(range(1, len(bindings) + 1)) or [item.scene_sequence for item in bindings] != list(range(chapter.start_sequence, chapter.end_sequence + 1)): raise ValueError("NARRATIVE_STRUCTURE_BINDING_INVALID")
            bound.extend(ids)
        if bound != [scene.id for scene in current_scenes]: raise ValueError("NARRATIVE_STRUCTURE_SCENE_COVERAGE_INVALID")
        chapter_ids = [item.id for item in chapters]
        expected_arcs = {item["number"]: item for item in expected["narrative_arcs"]}
        if any(item.structure_revision_id != revision.id or not item.structure_fingerprint or item.structure_fingerprint != expected_arcs[item.number]["structure_fingerprint"] for item in arcs):
            raise ValueError("NARRATIVE_STRUCTURE_ARC_INVALID")
        for arc in arcs:
            if arc.supersedes_arc_id:
                superseded = db.get(NarrativeArc, arc.supersedes_arc_id)
                if not superseded or superseded.project_id != project_id or superseded.active:
                    raise ValueError("NARRATIVE_STRUCTURE_SUPERSESSION_INVALID")
        arc_bound = [item for arc in arcs for item in db.scalars(select(NarrativeArcChapterBinding.chapter_id).where(NarrativeArcChapterBinding.narrative_arc_id == arc.id).order_by(NarrativeArcChapterBinding.ordinal)).all()]
        if arc_bound != chapter_ids: raise ValueError("NARRATIVE_STRUCTURE_ARC_COVERAGE_INVALID")
        arc_ids = [item.id for item in arcs]
        expected_volumes = {item["number"]: item for item in expected["volumes"]}
        if any(item.structure_revision_id != revision.id or not item.structure_fingerprint or item.structure_fingerprint != expected_volumes[item.number]["structure_fingerprint"] for item in volumes):
            raise ValueError("NARRATIVE_STRUCTURE_VOLUME_INVALID")
        for volume in volumes:
            if volume.supersedes_volume_id:
                superseded = db.get(NarrativeVolume, volume.supersedes_volume_id)
                if not superseded or superseded.project_id != project_id or superseded.active:
                    raise ValueError("NARRATIVE_STRUCTURE_SUPERSESSION_INVALID")
        volume_bound = [item for volume in volumes for item in db.scalars(select(NarrativeVolumeArcBinding.narrative_arc_id).where(NarrativeVolumeArcBinding.volume_id == volume.id).order_by(NarrativeVolumeArcBinding.ordinal)).all()]
        if volume_bound != arc_ids: raise ValueError("NARRATIVE_STRUCTURE_VOLUME_COVERAGE_INVALID")
