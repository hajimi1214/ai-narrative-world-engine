"""Deterministic, epistemically constrained character mind and decision protocol."""
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    CausalLink, CausalRelationType, CausalResourceType, Character,
    CharacterDecision, CharacterDecisionType, CharacterKnowledge, CharacterMemory,
    KnowledgeStatus, Project, RetconCognitionInvalidation,
    RetconCognitionInvalidationStatus, Scene, SceneProposal, WorldEntity,
    MemoryRetrievalMode, ProjectModelConfig,
)

MAX_CHARACTER_KNOWLEDGE = 32
MAX_CHARACTER_MEMORIES = 12
MIND_RETRIEVAL_PROTOCOL_VERSION = "character-mind-v1"
_FACT_PROPOSITION = re.compile(r"^(?P<subject_type>[A-Z_]+)\s+(?P<subject_id>[^:]+):\s*(?P<predicate>[^=]+?)\s*=\s*(?P<value>.+)$")
_CUE_FIELDS = {
    "entity_id", "entity_ids", "character_id", "character_ids", "participant_ids",
    "thread_id", "thread_ids", "item_id", "item_ids", "location_id",
}


def _enum(value: Any) -> str:
    return getattr(value, "value", value)


def _stable_fingerprint(prefix: str, payload: Any) -> str:
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}:{hashlib.sha256(rendered.encode()).hexdigest()}"


def character_context_fingerprint(context: dict[str, Any]) -> str:
    return _stable_fingerprint("character-context-v2", {key: value for key, value in context.items() if key not in {"fingerprint", "version"}})


def _value_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id") or value.get("name")
    return None


def context_knowledge_id(item: Any) -> str | None:
    """Return the canonical recalled knowledge id, with frozen-history fallback."""
    if not isinstance(item, dict):
        return None
    value = item.get("knowledge_id")
    if isinstance(value, str) and value:
        return value
    value = item.get("id")
    return value if isinstance(value, str) and value else None


class StructuredActorCueExtractor:
    """Extract explicit IDs only; prose and arbitrary JSON strings are never cues."""
    def extract(self, proposal: SceneProposal, character_id: str | None = None) -> dict[str, tuple[str, ...]]:
        values: dict[str, set[str]] = {key: set() for key in ("entity_ids", "character_ids", "participant_ids", "thread_ids", "item_ids", "location_ids")}
        if proposal.location_id:
            values["location_ids"].add(str(proposal.location_id)); values["entity_ids"].add(str(proposal.location_id))
        if proposal.primary_thread_id:
            values["thread_ids"].add(str(proposal.primary_thread_id))
        for participant_id in proposal.participants or []:
            if isinstance(participant_id, str):
                values["participant_ids"].add(participant_id); values["character_ids"].add(participant_id)
        entry = proposal.entry_state or {}
        contexts = [entry.get("visible_context")]
        actor_contexts = entry.get("actor_visible_context")
        if isinstance(actor_contexts, dict):
            contexts.append(actor_contexts.get(character_id) if character_id else None)
        for context in contexts:
            self._collect(context, values)
        return {key: tuple(sorted(value)) for key, value in values.items()}

    def _collect(self, context: Any, values: dict[str, set[str]]) -> None:
        if not isinstance(context, dict):
            return
        for field in _CUE_FIELDS:
            value = context.get(field)
            items = value if field.endswith("_ids") and isinstance(value, list) else [value] if isinstance(value, str) else []
            for item in items:
                if not isinstance(item, str) or not item:
                    continue
                if field.startswith("entity"):
                    values["entity_ids"].add(item)
                elif field.startswith("character"):
                    values["character_ids"].add(item)
                elif field.startswith("participant"):
                    values["participant_ids"].add(item); values["character_ids"].add(item)
                elif field.startswith("thread"):
                    values["thread_ids"].add(item)
                elif field.startswith("item"):
                    values["item_ids"].add(item)
                elif field == "location_id":
                    values["location_ids"].add(item); values["entity_ids"].add(item)


class CognitionFactIdentityParser:
    """Parse the Phase 7C canonical proposition without guessing legacy text."""
    def parse(self, proposition: str) -> dict[str, Any] | None:
        match = _FACT_PROPOSITION.match(proposition.strip()) if isinstance(proposition, str) else None
        if not match:
            return None
        try:
            value = json.loads(match.group("value"))
        except json.JSONDecodeError:
            return None
        return {"subject_type": match.group("subject_type"), "subject_id": match.group("subject_id").strip(), "predicate": match.group("predicate").strip(), "value": value}


class CharacterBeliefViewBuilder:
    def __init__(self, parser: CognitionFactIdentityParser | None = None):
        self.parser = parser or CognitionFactIdentityParser()

    def build(self, knowledge: list[CharacterKnowledge]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        identities: dict[str, list[dict[str, Any]]] = {}
        rows: dict[str, dict[str, Any]] = {}
        for row in sorted(knowledge, key=lambda item: item.id):
            identity = self.parser.parse(row.proposition)
            rendered = {"knowledge_id": row.id, "proposition": row.proposition, "status": _enum(row.status), "confidence": row.confidence, "fact_identity": identity}
            rows[row.id] = rendered
            if identity:
                key = json.dumps([identity["subject_type"], identity["subject_id"], identity["predicate"]])
                identities.setdefault(key, []).append(rendered)
        conflicts = []
        for grouped in identities.values():
            values = {json.dumps(item["fact_identity"]["value"], ensure_ascii=True, sort_keys=True) for item in grouped}
            if len(values) > 1:
                identity = grouped[0]["fact_identity"]
                conflicts.append({"subject_type": identity["subject_type"], "subject_id": identity["subject_id"], "predicate": identity["predicate"], "knowledge_ids": [item["knowledge_id"] for item in grouped]})
        return rows, sorted(conflicts, key=lambda item: (item["subject_type"], item["subject_id"], item["predicate"]))


class ActiveCharacterCognitionReader:
    """Read current cognition while preserving immutable historical rows."""
    def _hidden(self, session: Session, project_id: str, character_id: str, resource_type: str) -> set[str]:
        return set(session.scalars(select(RetconCognitionInvalidation.resource_id).where(
            RetconCognitionInvalidation.project_id == project_id,
            RetconCognitionInvalidation.character_id == character_id,
            RetconCognitionInvalidation.resource_type == resource_type,
            RetconCognitionInvalidation.status.in_([RetconCognitionInvalidationStatus.ACTIVE, RetconCognitionInvalidationStatus.RESOLVED]),
        )).all())

    def knowledge(self, session: Session, project_id: str, character_id: str) -> list[CharacterKnowledge]:
        hidden = self._hidden(session, project_id, character_id, "KNOWLEDGE")
        rows = session.scalars(select(CharacterKnowledge).where(CharacterKnowledge.character_id == character_id, CharacterKnowledge.status.in_([KnowledgeStatus.KNOWN, KnowledgeStatus.SUSPECTED, KnowledgeStatus.FALSE_BELIEF])).order_by(CharacterKnowledge.id)).all()
        return [row for row in rows if row.id not in hidden]

    def memories(self, session: Session, project_id: str, character_id: str) -> list[CharacterMemory]:
        hidden = self._hidden(session, project_id, character_id, "MEMORY")
        rows = session.scalars(select(CharacterMemory).where(CharacterMemory.character_id == character_id).order_by(CharacterMemory.id)).all()
        return [row for row in rows if row.id not in hidden]


class _CognitionUsage:
    def __init__(self, session: Session, project_id: str, resource_type: CausalResourceType, relation: CausalRelationType):
        rows = session.scalars(select(CausalLink).where(CausalLink.project_id == project_id, CausalLink.active.is_(True), CausalLink.cause_type == resource_type, CausalLink.relation_type == relation).order_by(CausalLink.sequence, CausalLink.id)).all()
        self.data: dict[str, tuple[int, int]] = {}
        for row in rows:
            count, latest = self.data.get(row.cause_id, (0, -1))
            self.data[row.cause_id] = (count + 1, max(latest, row.sequence or -1))

    def get(self, resource_id: str) -> tuple[int, int]:
        return self.data.get(resource_id, (0, -1))


def _cue_hits(identity: dict[str, Any] | None, cues: dict[str, tuple[str, ...]]) -> int:
    if not identity:
        return 0
    all_ids = set().union(*(set(value) for value in cues.values()))
    return int(identity["subject_id"] in all_ids)


def _scene_metadata(session: Session, project_id: str, scene_id: str | None) -> Scene | None:
    scene = session.get(Scene, scene_id) if scene_id else None
    return scene if scene and scene.project_id == project_id and scene.history_status == "ACTIVE" else None


class CharacterKnowledgeRetriever:
    STATUS_WEIGHT = {"KNOWN": 3, "SUSPECTED": 2, "FALSE_BELIEF": 1}

    def retrieve(self, session: Session, project_id: str, records: list[CharacterKnowledge], cues: dict[str, tuple[str, ...]], beliefs: dict[str, dict[str, Any]], *, usage=None, usage_provider=None, current_sequence: int | None = None) -> list[dict[str, Any]]:
        usage = usage or usage_provider or _CognitionUsage(session, project_id, CausalResourceType.CHARACTER_KNOWLEDGE, CausalRelationType.KNOWLEDGE_INFORMED_DECISION)
        if current_sequence is None:
            current_sequence = session.scalar(select(Scene.sequence).where(Scene.project_id == project_id, Scene.history_status == "ACTIVE").order_by(Scene.sequence.desc()).limit(1)) or 0
        ranked = []
        for row in records:
            item = beliefs[row.id]; count, latest = usage.get(CausalResourceType.CHARACTER_KNOWLEDGE, row.id) if usage_provider else usage.get(row.id); cue = _cue_hits(item["fact_identity"], cues)
            recency = max(0, latest) / max(1, current_sequence) if latest >= 0 else 0
            score = cue * 8 + self.STATUS_WEIGHT.get(item["status"], 0) * 2 + float(row.confidence) + min(count, 4) + recency
            ranked.append((score, cue, count, latest, item))
        ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], -item[3], item[4]["knowledge_id"]))
        return [item[4] for item in ranked[:MAX_CHARACTER_KNOWLEDGE]]


class CharacterMemoryRetriever:
    """Deterministic salience retrieval. Text content is never a ranking authority."""
    MAX_PER_SOURCE_SCENE = 3

    def retrieve(self, session: Session, project_id: str, records: list[CharacterMemory], cues: dict[str, tuple[str, ...]], *, usage=None, usage_provider=None, current_sequence: int | None = None, scene_provider=None, scene_metadata_provider=None) -> list[dict[str, Any]]:
        entries = self.rank_entries(session, project_id, records, cues, usage=usage, usage_provider=usage_provider, current_sequence=current_sequence, scene_provider=scene_provider, scene_metadata_provider=scene_metadata_provider)
        return self.select_bounded([entry[6] for entry in entries], strong_memory_ids={entry[6]["memory_id"] for entry in entries if entry[5]})

    def rank_all(self, session: Session, project_id: str, records: list[CharacterMemory], cues: dict[str, tuple[str, ...]], *, usage=None, usage_provider=None, current_sequence: int | None = None, scene_provider=None, scene_metadata_provider=None) -> list[dict[str, Any]]:
        return [entry[6] for entry in self._rank_entries(session, project_id, records, cues, usage=usage, usage_provider=usage_provider, current_sequence=current_sequence, scene_provider=scene_provider, scene_metadata_provider=scene_metadata_provider)]

    def rank_entries(self, session: Session, project_id: str, records: list[CharacterMemory], cues: dict[str, tuple[str, ...]], *, usage=None, usage_provider=None, current_sequence: int | None = None, scene_provider=None, scene_metadata_provider=None) -> list[tuple]:
        return self._rank_entries(session, project_id, records, cues, usage=usage, usage_provider=usage_provider, current_sequence=current_sequence, scene_provider=scene_provider, scene_metadata_provider=scene_metadata_provider)

    def _rank_entries(self, session: Session, project_id: str, records: list[CharacterMemory], cues: dict[str, tuple[str, ...]], *, usage=None, usage_provider=None, current_sequence: int | None = None, scene_provider=None, scene_metadata_provider=None) -> list[tuple]:
        usage = usage or usage_provider or _CognitionUsage(session, project_id, CausalResourceType.CHARACTER_MEMORY, CausalRelationType.MEMORY_INFORMED_DECISION)
        scene_provider = scene_provider or scene_metadata_provider
        project = session.get(Project, project_id)
        max_sequence = current_sequence if current_sequence is not None else (session.scalar(select(Scene.sequence).where(Scene.project_id == project_id, Scene.history_status == "ACTIVE").order_by(Scene.sequence.desc()).limit(1)) or 0)
        ranked = []
        for memory in records:
            source_scene = getattr(memory, "source_scene", None) or getattr(memory, "source_scene_id", None)
            source_lookup = source_scene if source_scene is not None else getattr(memory, "source_sequence", None)
            scene = scene_provider(source_lookup) if scene_provider else _scene_metadata(session, project_id, source_scene)
            cue, strong = self._cue_score(memory, scene, cues); count, latest = usage.get(CausalResourceType.CHARACTER_MEMORY, memory.id) if usage_provider else usage.get(memory.id)
            source_sequence = (scene.get("sequence", -1) if isinstance(scene, dict) else scene.sequence) if scene else -1
            recency = self._recency(memory, scene, project, max_sequence)
            score = cue + 2 * max(0.0, float(memory.importance)) + abs(float(memory.emotional_weight)) + max(0.0, float(memory.confidence)) + min(count, 4) + recency
            happened_at = getattr(memory, "happened_at", None)
            item = {"memory_id": memory.id, "content": memory.content, "importance": getattr(memory, "importance", 0.5), "emotional_weight": getattr(memory, "emotional_weight", 0.0), "confidence": getattr(memory, "confidence", 1.0), "distortion": getattr(memory, "distortion", {}) or {}, "happened_at": happened_at.isoformat() if hasattr(happened_at, "isoformat") else happened_at, "source_scene_id": getattr(memory, "source_scene", None)}
            if getattr(memory, "source_sequence", None) is not None:
                item["source_scene_sequence"] = memory.source_sequence
            ranked.append((score, cue, count, source_sequence, item["happened_at"] or "", strong, item))
        ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], -item[3], item[4], item[6]["memory_id"]))
        return ranked

    def select_bounded(self, ranked_items: list[dict[str, Any]], limit: int = MAX_CHARACTER_MEMORIES, *, strong_memory_ids: set[str] | None = None) -> list[dict[str, Any]]:
        strong_memory_ids = strong_memory_ids or set()
        selected, by_scene = [], {}
        for item in ranked_items:
            source_bucket = memory_source_bucket(item)
            if by_scene.get(source_bucket, 0) >= self.MAX_PER_SOURCE_SCENE and item.get("memory_id") not in strong_memory_ids:
                continue
            selected.append(item); by_scene[source_bucket] = by_scene.get(source_bucket, 0) + 1
            if len(selected) == limit:
                break
        return selected

    def _cue_score(self, memory: CharacterMemory, scene: Scene | None, cues: dict[str, tuple[str, ...]]) -> tuple[float, bool]:
        distortion_value = getattr(memory, "distortion", {})
        distortion = distortion_value if isinstance(distortion_value, dict) else {}
        ids = {
            "entity_ids": set(distortion.get("entity_ids", [])) if isinstance(distortion.get("entity_ids"), list) else set(),
            "participant_ids": set(distortion.get("participant_ids", [])) if isinstance(distortion.get("participant_ids"), list) else set(),
            "thread_ids": set(distortion.get("thread_ids", [])) if isinstance(distortion.get("thread_ids"), list) else set(),
            "location_ids": {distortion["location_id"]} if isinstance(distortion.get("location_id"), str) else set(),
            "item_ids": set(distortion.get("item_ids", [])) if isinstance(distortion.get("item_ids"), list) else set(),
        }
        if scene:
            location = scene.get("location") if isinstance(scene, dict) else scene.location
            participants = scene.get("participants", []) if isinstance(scene, dict) else (scene.participants or [])
            threads = scene.get("story_threads", []) if isinstance(scene, dict) else (scene.story_threads or [])
            if location:
                ids["location_ids"].add(str(location)); ids["entity_ids"].add(str(location))
            ids["participant_ids"].update(str(value) for value in participants if isinstance(value, str))
            ids["thread_ids"].update(str(value) for value in threads if isinstance(value, str))
        location = bool(ids["location_ids"].intersection(cues["location_ids"])); participants = len(ids["participant_ids"].intersection(cues["participant_ids"])); threads = bool(ids["thread_ids"].intersection(cues["thread_ids"])); other = len(ids["entity_ids"].intersection(cues["entity_ids"])) + len(ids["item_ids"].intersection(cues["item_ids"]))
        score = 4 * int(location) + 3 * participants + 3 * int(threads) + 2 * other
        # A direct structured location cue is already strong enough to
        # reactivate a memory; stronger combinations remain strong as well.
        return float(score), score >= 4

    def _recency(self, memory: CharacterMemory, scene: Scene | None, project: Project | None, max_sequence: int) -> float:
        if scene:
            sequence = scene.get("sequence", -1) if isinstance(scene, dict) else scene.sequence
            return 2.0 / (1 + max(0, max_sequence - sequence))
        happened_at = getattr(memory, "happened_at", None)
        if happened_at and project and project.current_world_time:
            return 1.0 / (1 + abs((project.current_world_time - happened_at).total_seconds()) / 86400)
        return 0.0


def memory_source_bucket(item: dict[str, Any]) -> str:
    """Return the stable diversity identity shared by normal and replay memories."""
    source_scene_id = item.get("source_scene_id") if isinstance(item, dict) else None
    if isinstance(source_scene_id, str) and source_scene_id:
        return f"scene:{source_scene_id}"
    source_sequence = item.get("source_scene_sequence") if isinstance(item, dict) else None
    if source_sequence is not None:
        return f"sequence:{source_sequence}"
    memory_id = item.get("memory_id") if isinstance(item, dict) else None
    return f"memory:{memory_id}"


class CharacterMindViewBuilder:
    def __init__(self, reader: ActiveCharacterCognitionReader | None = None, embedding_provider_factory=None):
        self.reader = reader or ActiveCharacterCognitionReader(); self.cues = StructuredActorCueExtractor(); self.beliefs = CharacterBeliefViewBuilder(); self.knowledge = CharacterKnowledgeRetriever(); self.memories = CharacterMemoryRetriever()
        self.embedding_provider_factory = embedding_provider_factory

    def build(self, session: Session, project_id: str, character_id: str, proposal: SceneProposal) -> dict[str, Any]:
        character = session.get(Character, character_id)
        if not character or character.project_id != project_id:
            raise ValueError("Character not found in project")
        if proposal.project_id != project_id:
            raise ValueError("Scene Proposal not found in project")
        cues = self.cues.extract(proposal, character_id)
        active_knowledge = self.reader.knowledge(session, project_id, character_id)
        beliefs, conflicts = self.beliefs.build(active_knowledge)
        recalled_knowledge = self.knowledge.retrieve(session, project_id, active_knowledge, cues, beliefs)
        memory_records = self.reader.memories(session, project_id, character_id)
        memory_entries = self.memories.rank_entries(session, project_id, memory_records, cues)
        recalled_memories = self.memories.select_bounded([entry[6] for entry in memory_entries], strong_memory_ids={entry[6]["memory_id"] for entry in memory_entries if entry[5]})
        location = session.get(WorldEntity, proposal.location_id) if proposal.location_id else None
        participants = session.scalars(select(Character).where(Character.project_id == project_id, Character.id.in_(proposal.participants or [])).order_by(Character.id)).all() if proposal.participants else []
        recalled_memories = self._hybrid_memories(session, project_id, character_id, cues, memory_records, memory_entries, recalled_memories, character, proposal, location, participants)
        identity = {key: getattr(character, key) for key in ("id", "name", "personality", "core_values", "boundaries", "goals", "current_state", "physical_state", "emotional_state", "relationships", "inventory")}
        result = {"character_id": character.id, "protocol_version": MIND_RETRIEVAL_PROTOCOL_VERSION, "character": identity, "proposal_id": proposal.id, "cues": cues, "knowledge": recalled_knowledge, "memories": recalled_memories, "belief_conflicts": conflicts}
        result["mind_fingerprint"] = _stable_fingerprint("character-mind-v1", result)
        return result

    def _hybrid_memories(self, session: Session, project_id: str, character_id: str, cues: dict[str, tuple[str, ...]], records: list[CharacterMemory], entries: list[tuple], deterministic: list[dict[str, Any]], character: Any = None, scene: Any = None, location: Any = None, participants: list[Any] | None = None) -> list[dict[str, Any]]:
        """Optional derived ranking. Any embedding failure preserves Phase9 recall."""
        config = session.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == project_id))
        if not config or not config.embedding_enabled or config.memory_retrieval_mode != MemoryRetrievalMode.HYBRID_RRF:
            return deterministic
        try:
            from .embeddings import CharacterMemoryHybridRetriever, CharacterMemorySemanticRetriever, CharacterSemanticCueBuilder, EmbeddingRouter, OpenAICompatibleEmbeddingProvider
            from .settings import get_settings
            route = EmbeddingRouter().resolve(session, project_id, get_settings())
            query = CharacterSemanticCueBuilder().build(cues, character=character, scene=scene, location=location, participants=participants)
            if not route.enabled or not query:
                return deterministic
            provider = self.embedding_provider_factory(route) if self.embedding_provider_factory else OpenAICompatibleEmbeddingProvider(route.base_url, route.api_key or "")
            embedding = provider.embed([query], route.model)
            if embedding.dimension != route.dimension:
                return deterministic
            semantic = CharacterMemorySemanticRetriever().retrieve(session, project_id, character_id, embedding.vectors[0], route.embedding_config_fingerprint, [memory.id for memory in records], config.memory_vector_top_k, config.memory_semantic_min_similarity)
            if not semantic:
                return deterministic
            full_items = {entry[6]["memory_id"]: entry[6] for entry in entries}
            deterministic_ids = [entry[6]["memory_id"] for entry in entries]
            strong_ids = {entry[6]["memory_id"] for entry in entries if entry[5]}
            return CharacterMemoryHybridRetriever().merge(full_items, deterministic_ids, semantic, MAX_CHARACTER_MEMORIES, config.memory_rrf_k, strong_ids)
        except Exception:
            return deterministic


class ReplayCognitionUsageProvider:
    """Current-history usage view for a replay sequence, including staged refs."""
    def __init__(self, db, replay_session, sequence: int):
        self.data: dict[tuple[CausalResourceType, str], tuple[int, int]] = {}
        replayed_scene_ids = {item.get("scene_id") for item in (replay_session.queue or []) if item.get("mode") == "REPLAY" and item.get("sequence", 0) < sequence}
        links = db.scalars(select(CausalLink).where(CausalLink.project_id == replay_session.project_id, CausalLink.active.is_(True))).all()
        for link in links:
            link_sequence = getattr(link, "sequence", None)
            if link_sequence is None or link_sequence >= sequence or getattr(link, "scene_id", None) in replayed_scene_ids:
                continue
            resource_type = getattr(link, "cause_type", None)
            expected_relation = {CausalResourceType.CHARACTER_KNOWLEDGE: CausalRelationType.KNOWLEDGE_INFORMED_DECISION, CausalResourceType.CHARACTER_MEMORY: CausalRelationType.MEMORY_INFORMED_DECISION}.get(resource_type)
            if expected_relation is None or getattr(link, "relation_type", None) != expected_relation:
                continue
            key = (resource_type, link.cause_id); count, latest = self.data.get(key, (0, -1)); self.data[key] = (count + 1, max(latest, link_sequence))
        state = replay_session.staged_world_state or {}
        for scene_result in (state.get("scene_results", {}) or {}).values():
            scene_sequence = scene_result.get("sequence", -1)
            if scene_sequence < 0 or scene_sequence >= sequence:
                continue
            for decision in scene_result.get("decisions", []) or []:
                for reference in decision.get("decision", {}).get("knowledge_used", []) or []:
                    ident = context_knowledge_id(reference)
                    if ident:
                        key = (CausalResourceType.CHARACTER_KNOWLEDGE, ident); count, latest = self.data.get(key, (0, -1)); self.data[key] = (count + 1, max(latest, scene_sequence))
                for reference in decision.get("decision", {}).get("memory_refs", []) or []:
                    ident = reference if isinstance(reference, str) else reference.get("memory_id") if isinstance(reference, dict) else None
                    if ident:
                        key = (CausalResourceType.CHARACTER_MEMORY, ident); count, latest = self.data.get(key, (0, -1)); self.data[key] = (count + 1, max(latest, scene_sequence))

    def get(self, resource_type: CausalResourceType, resource_id: str) -> tuple[int, int]:
        return self.data.get((resource_type, resource_id), (0, -1))


class ReplaySceneMetadataProvider:
    """Structured scene metadata for temporal memory ranking; never creates Scene rows."""
    def __init__(self, replay_session, current_sequence: int):
        self.current_sequence = current_sequence
        state = replay_session.staged_world_state or {}
        self.by_id: dict[str, dict[str, Any]] = {}
        self.by_sequence: dict[int, dict[str, Any]] = {}
        for row in state.get("current_world", {}).get("scenes", []) or []:
            self._add(row)
        for scene_id, result in (state.get("scene_results", {}) or {}).items():
            situation = result.get("situation", {}) or {}
            # Preserve the distinction between a missing structured field and
            # an explicitly empty value.  Replay results may only contain the
            # fields known at step time and must inherit the rest from the
            # temporal baseline.
            row = {"id": scene_id}
            if "sequence" in result and result.get("sequence") is not None:
                row["sequence"] = result["sequence"]
            for field in ("location", "participants", "story_threads"):
                if field in situation:
                    row[field] = situation[field]
            self._add(row)

    def _add(self, row: dict[str, Any]) -> None:
        sequence = row.get("sequence")
        scene_id = row.get("id")
        existing = self.by_id.get(scene_id) if scene_id else None
        if sequence is None and existing is not None:
            sequence = existing.get("sequence")
        if not isinstance(sequence, int) or sequence >= self.current_sequence:
            return
        by_sequence = self.by_sequence.get(sequence)
        if existing is None and by_sequence is not None:
            existing = by_sequence
        if existing is not None and scene_id and existing.get("id") not in (None, scene_id):
            raise ValueError("REPLAY_SCENE_METADATA_AMBIGUOUS")
        normalized = {
            "id": scene_id if scene_id is not None else (existing or {}).get("id"),
            "sequence": sequence,
            "location": (existing or {}).get("location"),
            "participants": list((existing or {}).get("participants") or []),
            "story_threads": list((existing or {}).get("story_threads") or []),
        }
        for field in ("location", "participants", "story_threads"):
            if field in row:
                value = row[field]
                normalized[field] = list(value or []) if field != "location" else value
        if normalized["id"]:
            self.by_id[normalized["id"]] = normalized
        self.by_sequence[sequence] = normalized

    def by_scene(self, scene_id: str | None) -> dict[str, Any] | None:
        return self.by_id.get(scene_id) if scene_id else None

    def by_sequence_value(self, sequence: int | None) -> dict[str, Any] | None:
        return self.by_sequence.get(sequence) if sequence is not None else None


class ReplayCharacterMindViewBuilder:
    """Phase 9 retrieval over Temporal cognition and replay sandbox state."""
    def __init__(self, embedding_provider_factory=None):
        self.embedding_provider_factory = embedding_provider_factory

    def build(self, db, replay_session, scene, proposal, character_id: str) -> dict[str, Any]:
        from .historical import TemporalCharacterCognitionReader
        from .replay import ReplayWorldView
        world = ReplayWorldView(replay_session); character = world.character(character_id)
        if not character:
            raise ValueError("REPLAY_CHARACTER_STATE_UNAVAILABLE")
        cognition = TemporalCharacterCognitionReader().read(db, replay_session.project_id, character_id, replay_session, scene.sequence)
        cues = StructuredActorCueExtractor().extract(proposal, character_id)
        beliefs, conflicts = CharacterBeliefViewBuilder().build(cognition["knowledge"])
        usage = ReplayCognitionUsageProvider(db, replay_session, scene.sequence)
        metadata = ReplaySceneMetadataProvider(replay_session, scene.sequence)
        knowledge = CharacterKnowledgeRetriever().retrieve(db, replay_session.project_id, cognition["knowledge"], cues, beliefs, usage_provider=usage, current_sequence=scene.sequence)
        def source_scene_provider(memory_source):
            return metadata.by_scene(memory_source) or metadata.by_sequence_value(memory_source if isinstance(memory_source, int) else None)
        memory_retriever = CharacterMemoryRetriever()
        memory_entries = memory_retriever.rank_entries(db, replay_session.project_id, cognition["memories"], cues, usage_provider=usage, current_sequence=scene.sequence, scene_provider=source_scene_provider)
        memories = memory_retriever.select_bounded([entry[6] for entry in memory_entries], strong_memory_ids={entry[6]["memory_id"] for entry in memory_entries if entry[5]})
        replay_character = {key: character.get(key) for key in ("id", "name", "goals", "current_state", "emotional_state")}
        memories = CharacterMindViewBuilder(embedding_provider_factory=self.embedding_provider_factory)._hybrid_memories(db, replay_session.project_id, character_id, cues, cognition["memories"], memory_entries, memories, replay_character, proposal, None, None)
        for item in memories:
            if item.get("source_scene_id"):
                source = metadata.by_scene(item["source_scene_id"])
            else:
                source = metadata.by_sequence_value(next((getattr(row, "source_sequence", None) for row in cognition["memories"] if row.id == item.get("memory_id")), None))
            if source:
                item["source_scene_sequence"] = source["sequence"]
        result = {"character_id": character_id, "protocol_version": MIND_RETRIEVAL_PROTOCOL_VERSION, "character": {key: character.get(key) for key in ("id", "name", "personality", "core_values", "boundaries", "goals", "current_state", "physical_state", "emotional_state", "relationships", "inventory")}, "proposal_id": proposal.id, "cues": cues, "knowledge": knowledge, "memories": memories, "belief_conflicts": conflicts}
        result["mind_fingerprint"] = _stable_fingerprint("replay-character-mind-v1", result)
        return result


class CharacterContextBuilder:
    def __init__(self, mind_builder: CharacterMindViewBuilder | None = None):
        self.mind_builder = mind_builder or CharacterMindViewBuilder()

    def build(self, session: Session, project_id: str, character_id: str, proposal: SceneProposal) -> dict[str, Any]:
        mind = self.mind_builder.build(session, project_id, character_id, proposal); character = session.get(Character, character_id)
        participants = session.scalars(select(Character).where(Character.project_id == project_id, Character.id.in_(proposal.participants or [])).order_by(Character.id)).all() if proposal.participants else []
        others = [{"id": row.id, "name": row.name} for row in participants if row.id != character.id]
        location = session.get(WorldEntity, proposal.location_id) if proposal.location_id else None
        knowledge = {"KNOWN": [], "SUSPECTED": [], "FALSE_BELIEF": []}
        for row in mind["knowledge"]:
            knowledge[row["status"]].append(row)
        context = {
            "character": {key: mind["character"][key] for key in ("id", "name", "personality", "core_values", "boundaries", "goals", "current_state", "physical_state", "emotional_state")},
            "scene": {"proposal_id": proposal.id, "location": {"id": location.id, "name": location.name} if location else None, "other_participants": others, "visible_context": (proposal.entry_state or {}).get("visible_context", {}), "actor_visible_context": ((proposal.entry_state or {}).get("actor_visible_context", {}) or {}).get(character.id, {})},
            "knowledge": knowledge, "memories": mind["memories"], "belief_conflicts": mind["belief_conflicts"], "relationships": {row["id"]: character.relationships.get(row["id"], {}) for row in others}, "abilities": self._abilities(character.abilities), "inventory": character.inventory, "mind_fingerprint": mind["mind_fingerprint"],
        }
        context["fingerprint"] = character_context_fingerprint(context); context["version"] = context["fingerprint"]
        return context

    def _abilities(self, abilities: list[Any]) -> list[Any]:
        return [{key: value for key, value in ability.items() if key != "director_only"} if isinstance(ability, dict) else ability for ability in abilities]


class ActorPerceptionSanitizer:
    """White-list the only data an external character model may receive."""
    def sanitize(self, context: dict[str, Any]) -> dict[str, Any]:
        character, scene = context["character"], context["scene"]
        return {"character": {key: self._visible(character.get(key)) for key in ("name", "personality", "core_values", "boundaries", "goals", "current_state", "physical_state", "emotional_state")}, "scene": {key: self._visible(scene.get(key, [] if key.endswith("s") else {})) for key in ("location", "other_participants", "visible_context", "actor_visible_context", "performance_observations", "self_turn_history", "active_participant_ids", "world_observations")}, "knowledge": self._visible(context.get("knowledge", {})), "memories": self._visible(context.get("memories", [])), "belief_conflicts": self._visible(context.get("belief_conflicts", [])), "relationships": self._visible(context.get("relationships", {})), "abilities": self._visible(context.get("abilities", [])), "inventory": self._visible(context.get("inventory", []))}

    def _visible(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._visible(item) for key, item in value.items() if key != "director_only"}
        if isinstance(value, list):
            return [self._visible(item) for item in value]
        return value


@dataclass
class CharacterDecisionIssue:
    code: str; severity: str; message: str; related_entity_ids: list[str]; suggested_fix: str


@dataclass
class CharacterDecisionValidationReport:
    issues: list[CharacterDecisionIssue]
    @property
    def valid(self) -> bool:
        return not any(item.severity == "BLOCKING" for item in self.issues)
    def as_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "issues": [item.__dict__ for item in self.issues]}


class CharacterDecisionConstraintChecker:
    def validate(self, session: Session, context: dict[str, Any], decision: CharacterDecision, world_view=None) -> CharacterDecisionValidationReport:
        issues: list[CharacterDecisionIssue] = []; add = lambda code, severity, message, ids, fix: issues.append(CharacterDecisionIssue(code, severity, message, ids, fix))
        character, scene = context["character"], context["scene"]
        if decision.context_fingerprint != context["fingerprint"]: add("CHARACTER_CONTEXT_STALE", "BLOCKING", "Character perspective changed after this decision was generated.", [decision.character_id], "Run the character simulation again.")
        self._knowledge(context, decision, add)
        memory_ids = {item["memory_id"] for item in context["memories"]}; invalid_memories = [str(item) for item in decision.memory_refs if self._memory_id(item) not in memory_ids]
        if invalid_memories: add("MEMORY_NOT_RECALLED", "BLOCKING", "Decision references memories outside this recalled mind view.", invalid_memories, "Use only recalled memory IDs from Actor Context.")
        inventory_ids = {_value_id(item) for item in context["inventory"]}; missing_inventory = [item for item in decision.inventory_refs if item not in inventory_ids]
        if missing_inventory: add("INVENTORY_MISSING", "BLOCKING", "Decision uses an item the character does not hold.", missing_inventory, "Remove the item or acquire it in a later Scene.")
        self._abilities(context, decision, add)
        if decision.boundary_override_reason is None and decision.relationship_factors.get("boundary_conflict"): add("CHARACTER_BOUNDARY_CONFLICT", "ERROR", "Decision conflicts with a declared hard boundary.", [decision.character_id], "Record a boundary override reason or choose a different action.")
        goals = {str(value) for value in character["goals"].values() if isinstance(value, str)}
        if decision.goal_refs and not set(decision.goal_refs).intersection(goals): add("GOAL_DISCONNECT", "WARNING", "Decision does not reference a current character goal.", [decision.character_id], "Connect the action to an existing goal or record an explicit new priority.")
        if any(term in decision.motivation.lower() for term in ("director need", "plot need", "chapter goal", "outline requirement", "剧情需要", "大纲要求", "导演需要")): add("DIRECTOR_PUPPETING", "BLOCKING", "Decision motivation is authorial rather than character-driven.", [decision.character_id], "Ground motivation in the character's goals, knowledge, or relationships.")
        if scene["location"] and character["current_state"].get("location_id") and character["current_state"]["location_id"] != scene["location"]["id"] and decision.decision_type != CharacterDecisionType.WAIT: add("IMPOSSIBLE_LOCATION", "BLOCKING", "Character is not at the proposal location.", [decision.character_id], "Use WAIT or a future Transition before acting at that location.")
        self._targets(session, context, decision, add, world_view)
        return CharacterDecisionValidationReport(issues)

    def _knowledge(self, context: dict[str, Any], decision: CharacterDecision, add) -> None:
        # Historical Replay contexts remain frozen and can contain pre-Phase 9
        # rows keyed only by ``id``.  Such rows are visible history but not an
        # explicit-recall authority for new decisions.
        recalled = {
            item["knowledge_id"]: item
            for values in context["knowledge"].values()
            for item in values
            if isinstance(item, dict) and isinstance(item.get("knowledge_id"), str)
        }
        for reference in decision.knowledge_used or []:
            if not isinstance(reference, dict) or not isinstance(reference.get("knowledge_id"), str):
                proposition = reference if isinstance(reference, str) else reference.get("proposition") if isinstance(reference, dict) else None
                statuses = reference.get("accepted_statuses", ["KNOWN"]) if isinstance(reference, dict) else ["KNOWN"]
                legacy_match = next((item for values in context["knowledge"].values() for item in values if isinstance(item, dict) and item.get("proposition") == proposition and (_enum(item.get("status", "KNOWN")) in {_enum(value) for value in statuses})), None)
                if legacy_match:
                    add("LEGACY_KNOWLEDGE_REFERENCE_UNATTRIBUTED", "WARNING", "Historical proposition-only knowledge reference has no explicit resource provenance.", [decision.character_id], "Use a recalled knowledge_id in new decisions.")
                else:
                    add("KNOWLEDGE_NOT_RECALLED", "BLOCKING", "Decision knowledge must match a recalled subjective belief.", [decision.character_id], "Use an exact recalled knowledge reference and status.")
                continue
            row, statuses = recalled.get(reference["knowledge_id"]), reference.get("accepted_statuses")
            if not row or reference.get("proposition") != row["proposition"] or not isinstance(statuses, list) or row["status"] not in {_enum(value) for value in statuses}:
                add("KNOWLEDGE_NOT_RECALLED", "BLOCKING", "Decision knowledge must exactly match an explicitly recalled subjective belief.", [reference["knowledge_id"]], "Use an exact recalled knowledge reference and status.")

    def _memory_id(self, reference: Any) -> str | None:
        return reference if isinstance(reference, str) else reference.get("memory_id") if isinstance(reference, dict) and isinstance(reference.get("memory_id"), str) else None

    def _abilities(self, context: dict[str, Any], decision: CharacterDecision, add) -> None:
        available = {_value_id(item): item for item in context["abilities"] if _value_id(item)}
        for reference in decision.ability_refs:
            ability = available.get(reference)
            if not ability: add("ABILITY_UNKNOWN", "BLOCKING", "Character cannot intentionally use an unknown ability.", [reference], "Use a visible ability from Actor Context.")
            elif isinstance(ability, dict) and ability.get("status", "AVAILABLE") != "AVAILABLE": add("ABILITY_UNAVAILABLE", "BLOCKING", "Ability is currently unavailable to the character.", [reference], "Choose an available ability or wait for recovery.")

    def _targets(self, session: Session, context: dict[str, Any], decision: CharacterDecision, add, world_view=None) -> None:
        if decision.target_character_id:
            participant_ids = {item["id"] for item in context["scene"]["other_participants"]} | {context["character"]["id"]}; target = session.get(Character, decision.target_character_id)
            if not target or target.project_id != decision.project_id or decision.target_character_id not in participant_ids: add("INVALID_TARGET", "BLOCKING", "Target character is unavailable in this Scene.", [decision.target_character_id], "Target an existing Scene participant.")
        if decision.target_entity_id:
            target = world_view.entity(decision.target_entity_id) if world_view else session.get(WorldEntity, decision.target_entity_id); location = context["scene"]["location"] or {}; active = target.get("active", True) if isinstance(target, dict) else getattr(target, "active", True)
            if not target or (not world_view and target.project_id != decision.project_id) or active is False or decision.target_entity_id != location.get("id"): add("INVALID_TARGET", "BLOCKING", "Target entity is unavailable in this Scene.", [decision.target_entity_id], "Target the current Scene location or a future exposed entity.")


class HeuristicCharacterActor:
    def decide(self, context: dict[str, Any]) -> dict[str, Any]:
        character = context["character"]; goal = character["goals"].get("current") or next((value for value in character["goals"].values() if isinstance(value, str)), "assess the situation"); known, inventory = context["knowledge"]["KNOWN"], context["inventory"]
        # Frozen replay contexts may contain legacy knowledge rows without the
        # Phase 9 explicit-reference contract.  They are not safe to cite.
        recalled = next((row for row in known if isinstance(row, dict) and isinstance(row.get("knowledge_id"), str) and isinstance(row.get("proposition"), str) and isinstance(row.get("status"), str)), None)
        knowledge_used = [{"knowledge_id": recalled["knowledge_id"], "proposition": recalled["proposition"], "accepted_statuses": [recalled["status"]]}] if recalled else []
        inventory_refs = [_value_id(inventory[0])] if inventory and _value_id(inventory[0]) else []
        return {"decision_type": CharacterDecisionType.INVESTIGATE.value, "intent": str(goal), "chosen_action": f"Inspect the available evidence related to {goal} before escalating.", "motivation": f"The character's current goal is {goal} and the visible pressure warrants verification.", "goal_refs": [goal], "knowledge_used": knowledge_used, "memory_refs": [item["memory_id"] for item in context["memories"][:1]], "ability_refs": [], "inventory_refs": inventory_refs, "relationship_factors": {}, "perceived_risk": "The visible pressure may make direct action costly.", "accepted_cost": "Time and attention.", "expected_personal_result": "A more informed next choice.", "uncertainties": ["The visible pressure may conceal unknown constraints."], "refused_options": [], "boundary_override_reason": None, "decision_summary": f"The character investigates because {goal} is the strongest current goal."}
