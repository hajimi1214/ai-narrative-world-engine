"""Deterministic, history-grounded Writer projection.

The writer is deliberately a projection layer: it can propose and validate prose,
but only the explicit adopt operation changes Chapter.content.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ai.errors import ModelProviderError, MODEL_OUTPUT_INVALID
from .ai.factory import get_model_provider
from .execution_trace import ExecutionTraceRecorder, TraceSanitizer, stable_fingerprint
from .model_router import ModelRouter
from .models import (
    CanonFact, CanonType, Chapter, ChapterSceneBinding, ChapterStructureStatus, Character,
    CharacterDecision, CharacterKnowledge, CharacterMemory, Scene,
    SceneExecutionBinding, ScenePerformance, ScenePerformanceTurn,
    SceneStateCheckpoint, StateDeltaBatch, StateDeltaBatchStatus, StateDeltaItem,
    TimelineEvent, TimelineEventType, WorldResolution, ResolutionStatus,
    WritingBible, WriterDraftStatus, WriterPOVMode, ChapterWriterDraft,
    RevealConstraint, RevealStatus, WorldEntity,
)
from .narrative_structure import NarrativeStructureAudit
from .retcon_apply import has_pending_replay


class WriterDomainError(ValueError):
    def __init__(self, code: str, detail: Any | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail if detail is not None else {}


class WriterSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_type: str
    source_id: str


class WriterOutputPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter_title: str | None
    prose: str
    scene_coverage: list[str]
    source_refs: list[WriterSourceRef]
    pov_character_id: str | None


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def _fp(value: Any, prefix: str) -> str:
    return f"{prefix}:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _ids(values: Any) -> list[str]:
    return sorted({str(item) for item in (values or []) if item is not None})


class WriterWordCounter:
    """Count CJK characters and contiguous alphanumeric runs deterministically."""

    _cjk = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
    _latin = re.compile(r"[A-Za-z0-9]+")

    def count(self, content: str | None) -> int:
        text = content or ""
        return len(self._cjk.findall(text)) + len(self._latin.findall(text))


class WriterPOVResolver:
    def resolve(self, db: Session, chapter: Chapter, request: dict[str, Any] | None = None, bible: WritingBible | None = None) -> tuple[WriterPOVMode, str | None]:
        request = request or {}
        raw_mode = request.get("pov_mode")
        mode = WriterPOVMode(raw_mode) if raw_mode else None
        rules = (bible.rules or {}) if bible else {}
        mode = mode or (WriterPOVMode(rules["pov_mode"]) if rules.get("pov_mode") in {item.value for item in WriterPOVMode} else WriterPOVMode.THIRD_PERSON_LIMITED)
        character_id = request.get("pov_character_id") or rules.get("pov_character_id")
        if mode in {WriterPOVMode.FIRST_PERSON, WriterPOVMode.THIRD_PERSON_LIMITED}:
            if not character_id or not any(str(character_id) in _ids(scene.participants) for scene in self._chapter_scenes(db, chapter)):
                raise WriterDomainError("WRITER_POV_REQUIRED")
        if character_id:
            character = db.get(Character, character_id)
            if not character or character.project_id != chapter.project_id:
                raise WriterDomainError("WRITER_POV_REQUIRED")
        return mode, str(character_id) if character_id else None

    @staticmethod
    def _chapter_scenes(db: Session, chapter: Chapter) -> list[Scene]:
        ids = [item.scene_id for item in db.scalars(select(ChapterSceneBinding).where(ChapterSceneBinding.chapter_id == chapter.id).order_by(ChapterSceneBinding.ordinal)).all()]
        return db.scalars(select(Scene).where(Scene.id.in_(ids), Scene.project_id == chapter.project_id, Scene.status == "OCCURRED", Scene.history_status == "ACTIVE").order_by(Scene.sequence, Scene.id)).all() if ids else []


class WriterChapterSourceBuilder:
    protocol = "writer-chapter-source-v1"

    def build(self, db: Session, chapter_id: str | Chapter, *, run_audit: bool = True) -> dict[str, Any]:
        chapter = chapter_id if isinstance(chapter_id, Chapter) else db.get(Chapter, chapter_id)
        if not chapter:
            raise WriterDomainError("CHAPTER_NOT_FOUND")
        if not chapter.active or _value(chapter.structure_status) not in {ChapterStructureStatus.SEALED.value, ChapterStructureStatus.PROVISIONAL.value}:
            raise WriterDomainError("NARRATIVE_STRUCTURE_REQUIRED")
        revision = db.scalar(select(__import__("app.models", fromlist=["NarrativeStructureRevision"]).NarrativeStructureRevision).where(
            __import__("app.models", fromlist=["NarrativeStructureRevision"]).NarrativeStructureRevision.project_id == chapter.project_id,
            __import__("app.models", fromlist=["NarrativeStructureRevision"]).NarrativeStructureRevision.active.is_(True)))
        if not revision:
            raise WriterDomainError("NARRATIVE_STRUCTURE_REQUIRED")
        if has_pending_replay(db, chapter.project_id):
            raise WriterDomainError("RETCON_REPLAY_REQUIRED")
        if run_audit:
            try:
                NarrativeStructureAudit().audit(db, chapter.project_id)
            except ValueError as exc:
                raise WriterDomainError(str(exc)) from exc
        bindings = db.scalars(select(ChapterSceneBinding).where(ChapterSceneBinding.chapter_id == chapter.id).order_by(ChapterSceneBinding.ordinal)).all()
        if not bindings:
            raise WriterDomainError("WRITER_SOURCE_EMPTY")
        scenes: list[dict[str, Any]] = []
        for binding in bindings:
            scene = db.get(Scene, binding.scene_id)
            if not scene or scene.project_id != chapter.project_id or _value(scene.status) != "OCCURRED" or scene.history_status != "ACTIVE":
                raise WriterDomainError("WRITER_SOURCE_SCENE_INVALID")
            checkpoint_rows = db.scalars(select(SceneStateCheckpoint).where(SceneStateCheckpoint.project_id == scene.project_id, SceneStateCheckpoint.scene_id == scene.id, SceneStateCheckpoint.active.is_(True), SceneStateCheckpoint.capture_protocol_version == 3).order_by(SceneStateCheckpoint.version.desc(), SceneStateCheckpoint.id)).all()
            if db.scalar(select(SceneExecutionBinding.id).where(SceneExecutionBinding.project_id == scene.project_id, SceneExecutionBinding.scene_id == scene.id, SceneExecutionBinding.active.is_(True))) and len(checkpoint_rows) != 1:
                raise WriterDomainError("WRITER_SCENE_CHECKPOINT_REQUIRED")
            scenes.append(self._scene(db, scene, binding.ordinal))
        manifest = {
            "protocol": self.protocol,
            "chapter_id": chapter.id,
            "chapter_structure_fingerprint": chapter.structure_fingerprint,
            "structure_status": _value(chapter.structure_status),
            "scenes": scenes,
        }
        source_fp = _fp(manifest, self.protocol)
        return {"chapter": chapter, "revision": revision, "source_scene_ids": [row["scene_id"] for row in scenes], "scenes": scenes, "manifest": manifest, "source_fingerprint": source_fp, "structure_fingerprint": chapter.structure_fingerprint}

    def _scene(self, db: Session, scene: Scene, ordinal: int) -> dict[str, Any]:
        checkpoint = db.scalar(select(SceneStateCheckpoint).where(SceneStateCheckpoint.project_id == scene.project_id, SceneStateCheckpoint.scene_id == scene.id, SceneStateCheckpoint.active.is_(True), SceneStateCheckpoint.capture_protocol_version == 3).order_by(SceneStateCheckpoint.version.desc(), SceneStateCheckpoint.id))
        binding = db.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.project_id == scene.project_id, SceneExecutionBinding.scene_id == scene.id, SceneExecutionBinding.active.is_(True)))
        performance = db.get(ScenePerformance, binding.performance_id) if binding else None
        turns: list[dict[str, Any]] = []
        resolutions: list[dict[str, Any]] = []
        state_changes: list[dict[str, Any]] = []
        events = db.scalars(select(TimelineEvent).where(TimelineEvent.project_id == scene.project_id, TimelineEvent.scene_id == scene.id, TimelineEvent.active.is_(True)).order_by(TimelineEvent.sequence, TimelineEvent.ordinal, TimelineEvent.id)).all()
        for event in events:
            if _value(event.event_type) == TimelineEventType.STATE_CHANGE.value:
                state_changes.append({"id": event.id, "target_type": event.target_type, "target_id": event.target_id, "path": event.path, "before_value": event.before_value, "after_value": event.after_value, "event_fingerprint": event.event_fingerprint})
        if performance:
            turn_rows = db.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == performance.id).order_by(ScenePerformanceTurn.sequence, ScenePerformanceTurn.id)).all()
            for turn in turn_rows:
                decision = db.get(CharacterDecision, turn.character_decision_id)
                turns.append({"id": turn.id, "sequence": turn.sequence, "actor_character_id": turn.actor_character_id, "visibility": _value(turn.action_visibility), "observable_action": turn.observable_action, "spoken_content": turn.spoken_content, "recipient_character_ids": _ids(turn.recipient_character_ids), "decision": self._decision(decision) if decision else None})
                resolution = db.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == turn.id, WorldResolution.status == ResolutionStatus.VALID))
                if resolution:
                    resolutions.append({"id": resolution.id, "turn_id": turn.id, "actor_character_id": turn.actor_character_id, "recipient_character_ids": _ids(resolution.recipient_character_ids), "objective_facts": resolution.objective_facts or [], "actor_observation": resolution.actor_observation, "public_observation": resolution.public_observation})
        batches = db.scalars(select(StateDeltaBatch).where(StateDeltaBatch.project_id == scene.project_id, StateDeltaBatch.applied_scene_id == scene.id, StateDeltaBatch.status == StateDeltaBatchStatus.APPLIED).order_by(StateDeltaBatch.id)).all()
        delta_items = []
        for batch in batches:
            for item in db.scalars(select(StateDeltaItem).where(StateDeltaItem.batch_id == batch.id).order_by(StateDeltaItem.ordinal, StateDeltaItem.id)).all():
                delta_items.append({"id": item.id, "batch_id": batch.id, "ordinal": item.ordinal, "target_type": _value(item.target_type), "target_id": item.target_id, "domain": _value(item.domain), "operation": _value(item.operation), "path": item.path, "before_value": item.before_value, "after_value": item.after_value, "semantic_fingerprint": item.semantic_fingerprint})
        row = {"ordinal": ordinal, "scene_id": scene.id, "sequence": scene.sequence, "world_time": scene.world_time.isoformat() if scene.world_time else None, "location": scene.location, "participants": _ids(scene.participants), "story_threads": _ids(scene.story_threads), "checkpoint_id": checkpoint.id if checkpoint else None, "checkpoint_fingerprint": checkpoint.checkpoint_fingerprint if checkpoint else None, "binding_id": binding.id if binding else None, "performance_id": performance.id if performance else None, "legacy_source": binding is None, "turns": turns, "resolutions": resolutions, "state_delta_items": delta_items, "state_changes": sorted(state_changes, key=lambda item: (item["target_type"] or "", item["target_id"] or "", item["path"] or "", item["id"]))}
        if binding is None:
            row["legacy_facts"] = scene.facts or []
            row["legacy_result"] = scene.result or {}
        return row

    @staticmethod
    def _decision(decision: CharacterDecision) -> dict[str, Any]:
        knowledge_used = [{"knowledge_id": item.get("knowledge_id"), "accepted_statuses": sorted(item.get("accepted_statuses") or [])} for item in (decision.knowledge_used or []) if isinstance(item, dict) and item.get("knowledge_id")]
        memory_refs = [{"memory_id": item.get("memory_id")} if isinstance(item, dict) else {"memory_id": item} for item in (decision.memory_refs or []) if (item.get("memory_id") if isinstance(item, dict) else item)]
        return {"id": decision.id, "character_id": decision.character_id, "decision_type": _value(decision.decision_type), "chosen_action": decision.chosen_action, "knowledge_used": knowledge_used, "memory_refs": memory_refs}


class WriterContextBuilder:
    protocol = "writer-context-v1"

    def build(self, db: Session, source: dict[str, Any], request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request or {}
        chapter: Chapter = source["chapter"]
        bibles = db.scalars(select(WritingBible).where(WritingBible.project_id == chapter.project_id, WritingBible.active.is_(True)).order_by(WritingBible.version, WritingBible.id)).all()
        if len(bibles) > 1:
            raise WriterDomainError("WRITING_BIBLE_AMBIGUOUS")
        bible = bibles[0] if bibles else None
        mode, pov_character_id = WriterPOVResolver().resolve(db, chapter, request, bible)
        rules = bible.rules if bible else {"protocol": "writer-default-v1"}
        bible_fp = _fp(rules, "writing-bible-v1") if bible else "writer-default-v1"
        allowed_reveals = self._allowed_reveals(db, chapter.project_id, request, pov_character_id)
        safe_scenes = self._visible_history(source["scenes"], mode, pov_character_id)
        manifest = dict(source["manifest"])
        manifest["scenes"] = safe_scenes
        context = {
            "writing_rules": rules,
            "chapter": {"id": chapter.id, "number": chapter.number, "title": chapter.title, "structure_status": _value(chapter.structure_status)},
            "formal_history": {"scenes": safe_scenes},
            "pov_subjective_context": self._subjective(db, safe_scenes, pov_character_id, mode, chapter.project_id, set(allowed_reveals)),
            "entity_labels": self._labels(db, source["scenes"]),
            "rendering_contract": {"pov_mode": mode.value, "pov_character_id": pov_character_id, "grounding": "structured references only", "allowed_reveal_ids": allowed_reveals, "no_formal_mutation": True},
            "source_manifest": manifest,
            "fingerprints": {"chapter_structure": source["structure_fingerprint"], "chapter_source": source["source_fingerprint"], "writing_bible": bible_fp},
        }
        context["writer_context_fingerprint"] = _fp({key: value for key, value in context.items() if key not in {"writer_context_fingerprint", "formal_history"}}, self.protocol)
        context["writer_context_fingerprint"] = _fp({"source": source["source_fingerprint"], "bible": bible_fp, "pov_mode": mode.value, "pov_character_id": pov_character_id, "rules": rules}, self.protocol)
        context["writing_bible"] = bible
        context["pov_mode"] = mode
        context["pov_character_id"] = pov_character_id
        context["renderable_source_refs"] = sorted(self._source_refs(safe_scenes, context["pov_subjective_context"]) + [{"source_type": "CANON_FACT", "source_id": item} for item in allowed_reveals], key=lambda item: (item["source_type"], item["source_id"]))
        return context

    @staticmethod
    def _source_refs(scenes: list[dict[str, Any]], subjective: list[dict[str, Any]]) -> list[dict[str, str]]:
        refs: set[tuple[str, str]] = set()
        for scene in scenes:
            refs.add(("SCENE", scene["scene_id"]))
            refs.update(("TURN", item["id"]) for item in scene.get("turns", []))
            refs.update(("CHARACTER_DECISION", item["decision"]["id"]) for item in scene.get("turns", []) if item.get("decision"))
            refs.update(("WORLD_RESOLUTION", item["id"]) for item in scene.get("resolutions", []))
            refs.update(("STATE_DELTA_ITEM", item["id"]) for item in scene.get("state_delta_items", []))
            refs.update(("TIMELINE_EVENT", item["id"]) for item in scene.get("state_changes", []))
        for row in subjective:
            refs.update(("CHARACTER_KNOWLEDGE", item["id"]) for item in row.get("knowledge", []))
            refs.update(("CHARACTER_MEMORY", item["id"]) for item in row.get("memories", []))
        return [{"source_type": kind, "source_id": item_id} for kind, item_id in sorted(refs)]

    @staticmethod
    def _visible_history(scenes: list[dict[str, Any]], mode: WriterPOVMode, pov_character_id: str | None) -> list[dict[str, Any]]:
        rows = json.loads(_canonical(scenes))
        for scene in rows:
            visible = []
            visible_turn_ids = set()
            for turn in scene.get("turns", []):
                visibility = turn.get("visibility")
                actor = turn.get("actor_character_id")
                recipients = set(turn.get("recipient_character_ids") or [])
                allowed = mode == WriterPOVMode.THIRD_PERSON_OMNISCIENT
                if mode == WriterPOVMode.OBJECTIVE:
                    allowed = visibility == "PUBLIC"
                elif mode in {WriterPOVMode.FIRST_PERSON, WriterPOVMode.THIRD_PERSON_LIMITED}:
                    allowed = visibility == "PUBLIC" or (visibility == "TARGETED" and (pov_character_id == actor or pov_character_id in recipients)) or (visibility == "PRIVATE" and pov_character_id == actor)
                    if visibility == "COVERT": allowed = False
                if allowed:
                    visible.append(turn); visible_turn_ids.add(turn["id"])
            scene["turns"] = visible
            scene["resolutions"] = [item for item in scene.get("resolutions", []) if item.get("turn_id") in visible_turn_ids]
            for resolution in scene["resolutions"]:
                if mode == WriterPOVMode.OBJECTIVE:
                    resolution["actor_observation"] = None
                elif mode in {WriterPOVMode.FIRST_PERSON, WriterPOVMode.THIRD_PERSON_LIMITED}:
                    if resolution.get("actor_character_id") != pov_character_id:
                        resolution["actor_observation"] = None
                    if pov_character_id not in set(resolution.get("recipient_character_ids") or []):
                        resolution["public_observation"] = None
                    resolution["objective_facts"] = []
        return rows

    @staticmethod
    def _allowed_reveals(db: Session, project_id: str, request: dict[str, Any], pov_character_id: str | None) -> list[str]:
        requested = set(_ids(request.get("allowed_reveal_ids")))
        allowed = []
        for item in db.scalars(select(RevealConstraint).where(RevealConstraint.project_id == project_id, (RevealConstraint.id.in_(requested) | RevealConstraint.canon_fact_id.in_(requested)))).all() if requested else []:
            status = _value(item.status)
            if status == RevealStatus.REVEALED.value:
                allowed.append(item.canon_fact_id)
        return sorted(allowed)

    def _labels(self, db: Session, scenes: list[dict[str, Any]]) -> dict[str, str]:
        ids = sorted({item for scene in scenes for item in scene["participants"]})
        labels = {item: db.get(Character, item).name for item in ids if db.get(Character, item)}
        entity_ids = sorted({item.get("target_id") for scene in scenes for item in scene.get("state_changes", []) if item.get("target_type") == "WORLD_ENTITY" and item.get("target_id")})
        labels.update({item: db.get(WorldEntity, item).name for item in entity_ids if db.get(WorldEntity, item)})
        return labels

    def _subjective(self, db: Session, scenes: list[dict[str, Any]], pov_character_id: str | None, mode: WriterPOVMode, project_id: str, allowed_reveals: set[str]) -> list[dict[str, Any]]:
        if mode == WriterPOVMode.OBJECTIVE or not pov_character_id:
            return []
        rows = []
        for scene in scenes:
            for turn in scene["turns"]:
                decision = turn.get("decision")
                if decision and decision.get("character_id") == pov_character_id:
                    formal = db.get(CharacterDecision, decision["id"])
                    knowledge_ids = [item.get("knowledge_id") for item in (decision.get("knowledge_used") or []) if isinstance(item, dict) and item.get("knowledge_id")]
                    memory_ids = [item.get("memory_id") if isinstance(item, dict) else item for item in (decision.get("memory_refs") or [])]
                    knowledge = db.scalars(select(CharacterKnowledge).where(CharacterKnowledge.id.in_(knowledge_ids), CharacterKnowledge.character_id == pov_character_id).order_by(CharacterKnowledge.id)).all() if knowledge_ids else []
                    blocked_secret_propositions = set(db.scalars(select(CanonFact.proposition).where(CanonFact.project_id == project_id, CanonFact.fact_type == CanonType.SECRET_CANON, CanonFact.id.not_in(allowed_reveals))).all())
                    knowledge = [item for item in knowledge if item.proposition not in blocked_secret_propositions]
                    memories = db.scalars(select(CharacterMemory).where(CharacterMemory.id.in_(memory_ids), CharacterMemory.character_id == pov_character_id).order_by(CharacterMemory.id)).all() if memory_ids else []
                    rows.append({"scene_id": scene["scene_id"], "character_id": pov_character_id, "chosen_action": decision.get("chosen_action"), "motivation": formal.motivation if formal else None, "knowledge": [{"id": item.id, "proposition": item.proposition, "status": _value(item.status), "confidence": item.confidence} for item in knowledge], "memories": [{"id": item.id, "content": item.content, "confidence": item.confidence} for item in memories]})
        return rows


class WriterGroundingValidator:
    """Validate only structured references; prose itself is never treated as fact."""

    def validate(self, output: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        source = context.get("source_manifest", {})
        scenes = {item["scene_id"]: item for item in source.get("scenes", [])}
        event_ids = {event["id"] for item in scenes.values() for event in item.get("state_changes", [])}
        turn_ids = {turn["id"] for item in scenes.values() for turn in item.get("turns", [])}
        known_entities = set(context.get("entity_labels", {}))
        issues: list[dict[str, Any]] = []
        expected_coverage = [item["scene_id"] for item in source.get("scenes", [])]
        if output.get("scene_coverage") != expected_coverage:
            issues.append({"code": "WRITER_SCENE_COVERAGE_INVALID", "blocking": True})
        allowed_refs = {(item["source_type"], item["source_id"]) for item in context.get("renderable_source_refs", [])}
        allowed_types = {"SCENE", "TURN", "CHARACTER_DECISION", "WORLD_RESOLUTION", "STATE_DELTA_ITEM", "TIMELINE_EVENT", "CHARACTER_KNOWLEDGE", "CHARACTER_MEMORY", "CANON_FACT"}
        for ref in output.get("source_refs", []) or []:
            key = (ref.get("source_type"), ref.get("source_id")) if isinstance(ref, dict) else (None, None)
            if key[0] not in allowed_types or key not in allowed_refs:
                issues.append({"code": "WRITER_SOURCE_REF_INVALID", "blocking": True, "source_type": key[0], "source_id": key[1]})
        expected_pov = context.get("pov_character_id")
        if output.get("pov_character_id") != expected_pov:
            issues.append({"code": "WRITER_POV_MISMATCH", "blocking": True})
        for event in output.get("events", []) or []:
            if not isinstance(event, dict):
                issues.append({"code": "WRITER_UNGROUNDED_EVENT", "blocking": True}); continue
            ref = event.get("source_ref") or event.get("event_id")
            if ref and ref not in event_ids and ref not in turn_ids:
                issues.append({"code": "WRITER_UNGROUNDED_EVENT", "blocking": True, "reference": ref})
            if event.get("scene_id") and event["scene_id"] not in scenes:
                issues.append({"code": "WRITER_UNGROUNDED_EVENT", "blocking": True, "reference": event["scene_id"]})
            if event.get("action") and not ref:
                issues.append({"code": "WRITER_UNGROUNDED_ACTION", "blocking": True})
        for entity in output.get("entities", []) or []:
            entity_id = entity.get("id") if isinstance(entity, dict) else entity
            if entity_id not in known_entities and not any(entity_id in scene.get("participants", []) for scene in scenes.values()):
                issues.append({"code": "WRITER_UNGROUNDED_ENTITY", "blocking": True, "reference": entity_id})
        for location in output.get("locations", []) or []:
            if location not in {scene.get("location") for scene in scenes.values()}:
                issues.append({"code": "WRITER_UNGROUNDED_LOCATION", "blocking": True, "reference": location})
        valid_reveals = set(context.get("rendering_contract", {}).get("allowed_reveal_ids", []) or [])
        for reveal in output.get("reveals", []) or []:
            rid = reveal.get("canon_fact_id") if isinstance(reveal, dict) else reveal
            if rid not in valid_reveals:
                issues.append({"code": "WRITER_UNGROUNDED_REVEAL", "blocking": True, "reference": rid})
        for key, code in (("knowledge", "WRITER_UNGROUNDED_KNOWLEDGE"), ("memories", "WRITER_UNGROUNDED_MEMORY"), ("outcomes", "WRITER_UNGROUNDED_OUTCOME")):
            for item in output.get(key, []) or []:
                if not isinstance(item, dict) or not item.get("source_ref"):
                    issues.append({"code": code, "blocking": True})
        return {"valid": not any(item.get("blocking", True) for item in issues), "issues": issues}


class WriterProjectionAudit:
    def audit(self, db: Session, chapter_id: str) -> dict[str, Any]:
        chapter = db.get(Chapter, chapter_id)
        if not chapter:
            raise WriterDomainError("CHAPTER_NOT_FOUND")
        if not chapter.current_writer_draft_id:
            return {"valid": True, "tracked": False}
        draft = db.get(ChapterWriterDraft, chapter.current_writer_draft_id)
        valid = bool(draft and draft.project_id == chapter.project_id and draft.chapter_id == chapter.id and _value(draft.status) == WriterDraftStatus.ADOPTED.value and chapter.writer_content_fingerprint == draft.content_fingerprint and chapter.writer_context_fingerprint == draft.writer_context_fingerprint and chapter.word_count == draft.word_count and chapter.content == draft.content)
        if not valid:
            raise WriterDomainError("WRITER_PROJECTION_INVALID")
        return {"valid": True, "tracked": True, "draft_id": draft.id}


class WriterProjectionService:
    def __init__(self, provider_factory=None):
        self.provider_factory = provider_factory

    def preview(self, db: Session, chapter_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        source = WriterChapterSourceBuilder().build(db, chapter_id)
        context = WriterContextBuilder().build(db, source, request or {})
        project = db.get(__import__("app.models", fromlist=["Project"]).Project, source["chapter"].project_id)
        return {"chapter_id": chapter_id, "chapter_number": source["chapter"].number, "structure_status": _value(source["chapter"].structure_status), "source_scene_ids": source["source_scene_ids"], "chapter_source_fingerprint": source["source_fingerprint"], "writer_context_fingerprint": context["writer_context_fingerprint"], "writing_bible": {"id": context["writing_bible"].id, "version": context["writing_bible"].version, "fingerprint": context["fingerprints"]["writing_bible"]} if context.get("writing_bible") else {"id": None, "version": None, "fingerprint": "writer-default-v1"}, "pov_mode": context["pov_mode"].value, "pov_character_id": context["pov_character_id"], "target_words": (request or {}).get("target_words", project.target_chapter_words), "min_words": (request or {}).get("min_words", project.min_chapter_words), "max_words": (request or {}).get("max_words", project.max_chapter_words), "source_counts": {"scenes": len(source["scenes"]), "refs": len(context["renderable_source_refs"])}, "visibility": {"renderable_ref_count": len(context["renderable_source_refs"])}, "prompt_fingerprint": _fp({"writer_context": context["writer_context_fingerprint"]}, "writer-prompt-v1"), "request_fingerprint": self.request_fingerprint(request or {}, context)}

    @staticmethod
    def request_fingerprint(request: dict[str, Any], context: dict[str, Any] | None = None) -> str:
        semantic = {key: request.get(key) for key in sorted(request) if key not in {"client_request_id", "force_replace_untracked"}}
        if context:
            semantic["writer_context_fingerprint"] = context.get("writer_context_fingerprint")
        return _fp(semantic, "writer-request-v1")

    def render(self, db: Session, chapter_id: str, request: dict[str, Any] | None = None, *, provider=None, model: str | None = None, settings=None) -> ChapterWriterDraft:
        request = dict(request or {})
        if request.get("idempotency_key") and not request.get("client_request_id"):
            request["client_request_id"] = request["idempotency_key"]
        locked_chapter = db.scalar(select(Chapter).where(Chapter.id == chapter_id).with_for_update())
        if not locked_chapter:
            raise WriterDomainError("CHAPTER_NOT_FOUND")
        source = WriterChapterSourceBuilder().build(db, chapter_id)
        context = WriterContextBuilder().build(db, source, request)
        request_fp = self.request_fingerprint(request, context)
        client_request_id = request.get("client_request_id")
        chapter = source["chapter"]
        if client_request_id:
            existing = db.scalar(select(ChapterWriterDraft).where(ChapterWriterDraft.chapter_id == chapter.id, ChapterWriterDraft.client_request_id == client_request_id))
            if existing:
                if existing.request_fingerprint != request_fp:
                    raise WriterDomainError("WRITER_REQUEST_MISMATCH")
                if _value(existing.status) == WriterDraftStatus.STALE.value:
                    raise WriterDomainError("WRITER_DRAFT_STALE")
                return existing
        prior = db.scalar(select(ChapterWriterDraft).where(ChapterWriterDraft.chapter_id == chapter.id, ChapterWriterDraft.status.in_([WriterDraftStatus.VALIDATED, WriterDraftStatus.ADOPTED])).order_by(ChapterWriterDraft.version.desc(), ChapterWriterDraft.id.desc()))
        if prior and prior.chapter_source_fingerprint != source["source_fingerprint"] and _value(prior.status) not in {WriterDraftStatus.SUPERSEDED.value, WriterDraftStatus.STALE.value}:
            prior.status = WriterDraftStatus.STALE; prior.stale_at = datetime.utcnow()
        version = (db.scalar(select(func.max(ChapterWriterDraft.version)).where(ChapterWriterDraft.chapter_id == chapter.id)) or 0) + 1
        draft = ChapterWriterDraft(project_id=chapter.project_id, chapter_id=chapter.id, version=version, status=WriterDraftStatus.GENERATING, client_request_id=client_request_id, request_fingerprint=request_fp, chapter_structure_fingerprint=source["structure_fingerprint"], chapter_source_fingerprint=source["source_fingerprint"], writer_context_fingerprint=context["writer_context_fingerprint"], source_structure_status=_value(chapter.structure_status), source_scene_ids=source["source_scene_ids"], source_manifest=context["source_manifest"], writing_bible_id=context["writing_bible"].id if context.get("writing_bible") else None, writing_bible_version=context["writing_bible"].version if context.get("writing_bible") else None, writing_bible_fingerprint=context["fingerprints"]["writing_bible"], pov_mode=context["pov_mode"], pov_character_id=context["pov_character_id"], parent_draft_id=prior.id if prior else None)
        db.add(draft); db.flush()
        try:
            route_provider, route_model = self._provider(db, chapter.project_id, provider, model, settings)
        except ModelProviderError as exc:
            trace = ExecutionTraceRecorder().start(db, project_id=chapter.project_id, stage="WRITER", source_type="CHAPTER_WRITER_DRAFT", source_id=draft.id, model=model, input_fingerprint=context["writer_context_fingerprint"])
            draft.status = WriterDraftStatus.FAILED; draft.validation_report = {"valid": False, "issues": [{"code": exc.code, "blocking": True}]}; draft.completed_at = datetime.utcnow(); ExecutionTraceRecorder().fail(trace, exc.code, upstream_status=exc.upstream_status, validation_report=draft.validation_report); db.flush(); return draft
        trace = ExecutionTraceRecorder().start(db, project_id=chapter.project_id, stage="WRITER", source_type="CHAPTER_WRITER_DRAFT", source_id=draft.id, provider=getattr(route_provider, "name", None), model=route_model, input_fingerprint=context["writer_context_fingerprint"])
        try:
            result = route_provider.generate(WriterPromptBuilder().build(context), route_model)
            parsed = self._parse(result.content)
            report = WriterGroundingValidator().validate(parsed, context)
            draft.provider = result.provider; draft.model = result.model; draft.model_request_id = result.request_id; draft.prompt_fingerprint = _fp({"writer_context": context["writer_context_fingerprint"], "model": route_model}, "writer-prompt-v1"); draft.title_candidate = parsed.get("chapter_title"); draft.content = parsed.get("prose", "")
            draft.content_fingerprint = _fp(draft.content, "writer-content-v1"); draft.word_count = WriterWordCounter().count(draft.content); draft.scene_coverage = parsed.get("scene_coverage", []); draft.source_refs = parsed.get("source_refs", [])
            draft.validation_report = report; draft.completed_at = datetime.utcnow()
            if not report["valid"]:
                draft.status = WriterDraftStatus.REJECTED
                ExecutionTraceRecorder().block(trace, report["issues"][0]["code"], validation_report=report, request_id=result.request_id)
            else:
                project = db.get(__import__("app.models", fromlist=["Project"]).Project, chapter.project_id)
                min_words = request.get("min_words", project.min_chapter_words)
                max_words = request.get("max_words", project.max_chapter_words)
                if min_words is not None and draft.word_count < min_words or max_words is not None and draft.word_count > max_words:
                    draft.validation_report = {"valid": False, "issues": [{"code": "WRITER_WORD_COUNT_OUT_OF_RANGE", "blocking": True, "word_count": draft.word_count}]}; draft.status = WriterDraftStatus.REJECTED
                    ExecutionTraceRecorder().block(trace, "WRITER_WORD_COUNT_OUT_OF_RANGE", validation_report=draft.validation_report, request_id=result.request_id)
                else:
                    draft.status = WriterDraftStatus.VALIDATED
                    ExecutionTraceRecorder().succeed(trace, latency_ms=result.latency_ms, request_id=result.request_id, output_fingerprint=draft.content_fingerprint)
        except ModelProviderError as exc:
            draft.status = WriterDraftStatus.FAILED; draft.validation_report = {"valid": False, "issues": [{"code": exc.code, "blocking": True}]}; draft.completed_at = datetime.utcnow(); ExecutionTraceRecorder().fail(trace, exc.code, upstream_status=exc.upstream_status, validation_report=draft.validation_report); db.flush()
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            code = str(exc) if str(exc) in {MODEL_OUTPUT_INVALID, "WRITER_OUTPUT_INVALID"} else MODEL_OUTPUT_INVALID
            draft.status = WriterDraftStatus.FAILED; draft.validation_report = {"valid": False, "issues": [{"code": code, "blocking": True}]}; draft.completed_at = datetime.utcnow(); ExecutionTraceRecorder().fail(trace, code, validation_report=draft.validation_report)
        db.flush()
        return draft

    def adopt(self, db: Session, draft_id: str, *, force_replace_untracked: bool = False) -> Chapter:
        draft = db.get(ChapterWriterDraft, draft_id)
        if not draft:
            raise WriterDomainError("WRITER_DRAFT_NOT_FOUND")
        if _value(draft.status) == WriterDraftStatus.ADOPTED.value:
            chapter = db.get(Chapter, draft.chapter_id)
            if chapter and chapter.current_writer_draft_id == draft.id:
                return chapter
        if _value(draft.status) != WriterDraftStatus.VALIDATED.value:
            raise WriterDomainError("WRITER_DRAFT_STALE" if _value(draft.status) == WriterDraftStatus.STALE.value else "WRITER_DRAFT_NOT_VALIDATED")
        db.scalar(select(Chapter).where(Chapter.id == draft.chapter_id).with_for_update())
        try:
            current = WriterChapterSourceBuilder().build(db, draft.chapter_id)
        except WriterDomainError as exc:
            draft.status = WriterDraftStatus.STALE; draft.stale_at = datetime.utcnow(); db.flush(); raise WriterDomainError("WRITER_SOURCE_CHANGED") from exc
        if current["source_fingerprint"] != draft.chapter_source_fingerprint:
            draft.status = WriterDraftStatus.STALE; draft.stale_at = datetime.utcnow(); db.flush(); raise WriterDomainError("WRITER_DRAFT_STALE")
        chapter = current["chapter"]
        try:
            current_context = WriterContextBuilder().build(db, current, {"pov_mode": _value(draft.pov_mode), "pov_character_id": draft.pov_character_id})
        except WriterDomainError as exc:
            draft.status = WriterDraftStatus.STALE; draft.stale_at = datetime.utcnow(); db.flush(); raise WriterDomainError("WRITER_STYLE_SOURCE_CHANGED") from exc
        if current_context["writer_context_fingerprint"] != draft.writer_context_fingerprint:
            draft.status = WriterDraftStatus.STALE; draft.stale_at = datetime.utcnow(); db.flush(); raise WriterDomainError("WRITER_STYLE_SOURCE_CHANGED")
        if chapter.content is not None and chapter.current_writer_draft_id is None and not force_replace_untracked:
            raise WriterDomainError("CHAPTER_CONTENT_UNTRACKED")
        prior = db.get(ChapterWriterDraft, chapter.current_writer_draft_id) if chapter.current_writer_draft_id else None
        if prior and prior.id != draft.id and _value(prior.status) == WriterDraftStatus.ADOPTED.value:
            prior.status = WriterDraftStatus.SUPERSEDED
        chapter.content = draft.content; chapter.title = draft.title_candidate or chapter.title; chapter.word_count = draft.word_count; chapter.writer_content_fingerprint = draft.content_fingerprint; chapter.writer_context_fingerprint = draft.writer_context_fingerprint; chapter.current_writer_draft_id = draft.id; chapter.written_at = datetime.utcnow(); draft.status = WriterDraftStatus.ADOPTED; draft.adopted_at = datetime.utcnow(); db.flush(); WriterProjectionAudit().audit(db, chapter.id)
        return chapter

    def _provider(self, db: Session, project_id: str, provider, model, settings):
        if provider is not None:
            return provider, model or "writer-test-model"
        settings = settings or __import__("app.settings", fromlist=["get_settings"]).get_settings()
        route = ModelRouter().resolve(db, project_id, settings, "WRITER")
        return get_model_provider(settings, route.provider, route.base_url), model or route.model

    @staticmethod
    def _parse(content: str) -> dict[str, Any]:
        raw = content.strip()
        if raw.startswith("```"):
            raise ValueError("MODEL_OUTPUT_INVALID")
        try:
            value = WriterOutputPayload.model_validate_json(raw)
        except ValidationError as exc:
            raise ValueError("MODEL_OUTPUT_INVALID") from exc
        if not value.prose.strip():
            raise ValueError("WRITER_OUTPUT_INVALID")
        return value.model_dump(mode="json")


class WriterPromptBuilder:
    def build(self, context: dict[str, Any]) -> list[dict[str, str]]:
        safe = {key: value for key, value in context.items() if key not in {"writing_bible", "pov_mode", "pov_character_id"}}
        return [
            {"role": "system", "content": "You are a prose renderer, not a plot planner. FORMAL_HISTORY is factual authority. SUBJECTIVE_POV may be mistaken. WRITING_RULES are style only. RENDERING_FREEDOM applies only to prose expression. Do not invent events, outcomes, decisions, knowledge, secrets, items, injuries, locations, relationships, or causal facts. Do not reveal information outside the POV visibility contract. Return exactly one JSON object matching the output contract."},
            {"role": "user", "content": _canonical({"context": safe, "output_contract": {"chapter_title": "string|null", "prose": "non-empty string", "scene_coverage": "ordered scene id array", "source_refs": "visible structured source references", "pov_character_id": "string|null"}})},
        ]
