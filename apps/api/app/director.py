"""Deterministic Director protocol. This module deliberately has no LLM dependency."""
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import AntiAIBible, CanonFact, CanonType, Character, CharacterKnowledge, CharacterMemory, EntityType, KnowledgeStatus, Project, ProposalStatus, ProposalType, RevealConstraint, RevealStatus, Scene, SceneProposal, SceneStatus, SceneExecutionBinding, ScenePerformance, StoryArc, StoryThread, ThreadStatus, TimelineEvent, TimelineEventType, CausalLink, CausalResourceType, CausalRelationType, WorldEntity, WritingBible
from .character_mind import ActiveCharacterCognitionReader, CharacterBeliefViewBuilder

RECENT_SCENE_LIMIT = 10


def _canonical_semantic(value: Any) -> Any:
    """Normalize structured identity data without using insertion or hash order."""
    if isinstance(value, dict):
        return {str(key): _canonical_semantic(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set)):
        values = [_canonical_semantic(item) for item in value]
        return sorted(values, key=lambda item: json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return value


class DirectorKnowledgeReferenceValidator:
    """Single strict binding rule shared by normal and AI Director paths."""
    @staticmethod
    def validate(reference: Any, character_id: str, available: Any) -> bool:
        if not isinstance(reference, dict) or not isinstance(reference.get("knowledge_id"), str) or not reference["knowledge_id"]:
            return False
        rows = []
        if isinstance(available, dict):
            for values in available.values():
                rows.extend(values if isinstance(values, list) else [])
        elif isinstance(available, list):
            rows = available
        row = next((item for item in rows if isinstance(item, dict) and (item.get("knowledge_id") or item.get("id")) == reference["knowledge_id"]), None)
        if not row or row.get("character_id") not in (None, character_id):
            return False
        if "proposition" in reference and reference.get("proposition") != row.get("proposition"):
            return False
        accepted = reference.get("accepted_statuses")
        if accepted is not None:
            statuses = {getattr(value, "value", value) for value in accepted} if isinstance(accepted, list) else set()
            if row.get("status") not in statuses:
                return False
        return True

def extract_entity_references(*values: Any) -> set[str]:
    """Extract only documented entity reference keys; never infer from arbitrary text."""
    references: set[str] = set()
    def collect(value: Any) -> None:
        if isinstance(value, list):
            for item in value: collect(item)
        elif isinstance(value, dict):
            for key in ("entity_id", "location_id"):
                if isinstance(value.get(key), str): references.add(value[key])
            if isinstance(value.get("entity_ids"), list):
                references.update(item for item in value["entity_ids"] if isinstance(item, str))
    for value in values: collect(value)
    return references

def context_fingerprint(context: dict[str, Any]) -> str:
    def semantic(value):
        if isinstance(value, dict):
            return {key: semantic(item) for key, item in value.items() if key not in {"version", "fingerprint", "summary", "intent", "writing_constraints", "anti_ai_constraints"}}
        if isinstance(value, list):
            return [semantic(item) for item in value]
        return value
    payload = semantic({key: value for key, value in context.items() if key not in {"version", "fingerprint"}})
    stable = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return f"director-context-v2:{hashlib.sha256(stable.encode()).hexdigest()}"


@dataclass(frozen=True)
class StoryGravityWeights:
    thread_weight: float = 1.0
    staleness: float = 0.35
    progress: float = 0.25
    repetition: float = 0.6
    character_alignment: float = 0.4
    consequence_alignment: float = 0.45
    arc_alignment: float = 0.3
    goal: float = 1.0
    absence: float = 0.35
    overuse: float = 0.45
    belief_conflict: float = 0.5
    freshness: float = 0.4
    proposal_type_repetition: float = 0.6
    thread_candidate_repetition: float = 0.6
    participant_repetition: float = 0.35
    location_repetition: float = 0.25


@dataclass(frozen=True)
class StoryGravityCandidate:
    candidate_key: str
    proposal_type: str
    primary_thread_id: str | None
    participant_ids: tuple[str, ...]
    location_id: str | None
    focus_type: str
    focus_ids: tuple[str, ...]
    pressure_kind: str
    score_components: dict[str, float]
    score: float
    reason_codes: tuple[str, ...] = ()
    expected_progress: dict[str, Any] = field(default_factory=dict)
    reveal_ids: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"candidate_key": self.candidate_key, "proposal_type": self.proposal_type,
                "primary_thread_id": self.primary_thread_id, "participant_ids": list(self.participant_ids),
                "location_id": self.location_id, "focus_type": self.focus_type, "focus_ids": list(self.focus_ids),
                "pressure_kind": self.pressure_kind, "score_components": self.score_components,
                "score": self.score, "reason_codes": list(self.reason_codes),
                "expected_progress": self.expected_progress, "reveal_ids": list(self.reveal_ids), "evidence": self.evidence}


@dataclass
class StoryGravityReport:
    protocol_version: str
    current_sequence: int
    thread_gravity: list[dict[str, Any]]
    character_gravity: list[dict[str, Any]]
    consequence_pressure: list[dict[str, Any]]
    relationship_pressure: list[dict[str, Any]]
    reveal_opportunities: list[dict[str, Any]]
    repetition_pressure: dict[str, Any]
    candidate_seeds: list[dict[str, Any]]
    gravity_fingerprint: str
    paused_background: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"protocol_version": self.protocol_version, "current_sequence": self.current_sequence,
                "thread_gravity": self.thread_gravity, "character_gravity": self.character_gravity,
                "consequence_pressure": self.consequence_pressure, "relationship_pressure": self.relationship_pressure,
                "reveal_opportunities": self.reveal_opportunities, "repetition_pressure": self.repetition_pressure,
                "candidate_seeds": self.candidate_seeds, "gravity_fingerprint": self.gravity_fingerprint, "paused_background": self.paused_background}


class StoryGravityContext(dict):
    """Mapping-compatible structured context returned by the read-only builder."""


class StoryGravityContextBuilder:
    """Read-only structured current-history authority for Director scoring."""
    def build(self, session: Session, project_id: str) -> dict[str, Any]:
        project = session.get(Project, project_id)
        if not project:
            raise ValueError("Project not found")
        # Phase 16A projections are optional accelerators.  Their service is
        # read-only here; missing or dirty rows deliberately use this frozen
        # formal-history path instead of repairing data during a Director read.
        from .scaling import ProjectHistoryProjectionService
        fast = ProjectHistoryProjectionService().fast_context(session, project_id, self)
        if fast is not None:
            return StoryGravityContext(fast)
        scenes = session.scalars(select(Scene).where(Scene.project_id == project_id, Scene.status == SceneStatus.OCCURRED, Scene.history_status == "ACTIVE").order_by(Scene.sequence, Scene.id)).all()
        current_sequence = max((scene.sequence for scene in scenes), default=0)
        characters = session.scalars(select(Character).where(Character.project_id == project_id, Character.active.is_(True)).order_by(Character.id)).all()
        threads = session.scalars(select(StoryThread).where(StoryThread.project_id == project_id, StoryThread.status.in_((ThreadStatus.OPEN, ThreadStatus.PAUSED))).order_by(StoryThread.id)).all()
        arc = session.scalar(select(StoryArc).where(StoryArc.project_id == project_id, StoryArc.status == "ACTIVE").order_by(StoryArc.id).limit(1))
        locations = session.scalars(select(WorldEntity).where(WorldEntity.project_id == project_id, WorldEntity.active.is_(True), WorldEntity.entity_type == EntityType.LOCATION).order_by(WorldEntity.id)).all()
        signatures = [self._signature(session, scene) for scene in scenes[-RECENT_SCENE_LIMIT:]]
        knowledge = []
        knowledge_rows_by_character = {}
        memories = []
        for character in characters:
            rows = ActiveCharacterCognitionReader().knowledge(session, project_id, character.id)
            knowledge_rows_by_character[character.id] = rows
            knowledge.extend({"knowledge_id": row.id, "character_id": character.id, "status": getattr(row.status, "value", row.status), "confidence": row.confidence, "proposition": row.proposition, "fact_identity": row.proposition} for row in rows)
            memories.extend({"memory_id": row.id, "character_id": character.id, "importance": row.importance, "emotional_weight": row.emotional_weight} for row in ActiveCharacterCognitionReader().memories(session, project_id, character.id))
        reveal = []
        for constraint in session.scalars(select(RevealConstraint).where(RevealConstraint.project_id == project_id).order_by(RevealConstraint.id)).all():
            if getattr(constraint.status, "value", constraint.status) == RevealStatus.AVAILABLE.value:
                reveal.append({"canon_fact_id": constraint.canon_fact_id, "status": RevealStatus.AVAILABLE.value, "allowed_character_ids": sorted(constraint.allowed_character_ids or [])})
        events = session.scalars(select(TimelineEvent).where(TimelineEvent.project_id == project_id, TimelineEvent.active.is_(True), TimelineEvent.event_type == TimelineEventType.STATE_CHANGE).order_by(TimelineEvent.sequence, TimelineEvent.ordinal, TimelineEvent.id)).all()
        current_events = {}
        for event in events:
            key = (event.target_type, event.target_id, event.path)
            if key not in current_events or (event.sequence or -1, event.ordinal or -1, event.id) > (current_events[key].sequence or -1, current_events[key].ordinal or -1, current_events[key].id):
                current_events[key] = event
        state_changes = [{"id": event.id, "sequence": event.sequence, "ordinal": event.ordinal, "scene_id": event.scene_id, "target_type": event.target_type, "target_id": event.target_id, "path": event.path, "before_value": event.before_value, "after_value": event.after_value, "event_fingerprint": event.event_fingerprint} for event in current_events.values()]
        state_changes.sort(key=lambda item: (item["sequence"] or -1, item["ordinal"] or -1, item["target_type"] or "", item["target_id"] or "", item["path"] or ""))
        links = session.scalars(select(CausalLink).where(CausalLink.project_id == project_id, CausalLink.active.is_(True)).order_by(CausalLink.link_fingerprint, CausalLink.id)).all()
        valid_links = []
        for link in links:
            endpoints_valid = True
            for resource_type, resource_id in ((link.cause_type, link.cause_id), (link.effect_type, link.effect_id)):
                if getattr(resource_type, "value", resource_type) == CausalResourceType.TIMELINE_EVENT.value:
                    event = session.get(TimelineEvent, resource_id)
                    if not event or event.project_id != project_id or not event.active:
                        endpoints_valid = False
            if endpoints_valid:
                valid_links.append(link)
        return StoryGravityContext({"protocol_version": "story-gravity-context-v1", "project": {"id": project.id, "story_seed": project.story_seed, "autonomy_settings": project.autonomy_settings}, "current_sequence": current_sequence, "scenes": [self._scene(scene) for scene in scenes], "recent_scene_signatures": signatures, "characters": [self._character(character, knowledge_rows_by_character.get(character.id, [])) for character in characters], "story_threads": [self._thread(thread) for thread in threads], "story_arc": self._arc(arc), "locations": [{"id": row.id, "name": row.name} for row in locations], "knowledge": sorted(knowledge, key=lambda item: (item["character_id"], item["knowledge_id"])), "memories": sorted(memories, key=lambda item: (item["character_id"], item["memory_id"])), "state_changes": state_changes, "causal_links": [{"id": link.id, "fingerprint": link.link_fingerprint, "scene_id": link.scene_id, "sequence": link.sequence} for link in valid_links], "reveals": reveal})

    def _signature(self, session, scene):
        binding = session.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == scene.id, SceneExecutionBinding.active.is_(True)))
        performance = session.get(ScenePerformance, binding.performance_id) if binding else None
        proposal = session.get(SceneProposal, performance.scene_proposal_id) if performance else None
        return {"scene_id": scene.id, "sequence": scene.sequence, "proposal_type": getattr(proposal.proposal_type, "value", proposal.proposal_type) if proposal else None, "primary_thread_id": proposal.primary_thread_id if proposal else None, "participants": sorted(str(value) for value in (proposal.participants if proposal else scene.participants or [])), "location_id": proposal.location_id if proposal else scene.location}
    def _scene(self, scene): return {"id": scene.id, "sequence": scene.sequence, "location_id": scene.location, "participants": sorted(str(value) for value in scene.participants or []), "story_threads": sorted(str(value) for value in scene.story_threads or [])}
    def _character(self, character, knowledge=None):
        rows = list(knowledge or [])
        _, conflicts = CharacterBeliefViewBuilder().build(rows)
        return {"id": character.id, "narrative_relevance": (character.narrative_relevance or {}).get("score", 0), "location_id": (character.current_state or {}).get("location_id", (character.current_state or {}).get("location")), "has_goal": bool(character.goals), "goal_refs": sorted(str(key) for key in (character.goals or {}).keys()), "goals": character.goals or {}, "current_state": character.current_state or {}, "physical_state": character.physical_state or {}, "emotional_state": character.emotional_state or {}, "relationships": character.relationships or {}, "belief_conflict_count": len(conflicts), "belief_conflicts": conflicts}
    def _thread(self, thread): return {"id": thread.id, "title": thread.title, "type": thread.type, "goal": thread.goal, "weight": max(0.0, min(float(thread.weight or 0), 100.0)), "progress": max(0.0, min(float(thread.progress or 0), 1.0)), "status": getattr(thread.status, "value", thread.status), "state": thread.state or {}}
    def _arc(self, arc): return None if not arc else {"id": arc.id, "status": arc.status, "progress": arc.progress}


class StoryGravityEngine:
    def __init__(self, weights: StoryGravityWeights | None = None):
        self.weights = weights or StoryGravityWeights()

    def build(self, context: dict[str, Any]) -> StoryGravityReport:
        current = context["current_sequence"]
        scenes = context["scenes"]
        recent = scenes[-2:]
        history_stats = context.get("history_stats") or {}
        thread_stats = history_stats.get("thread_stats") or {}
        character_stats = history_stats.get("character_stats") or {}
        consequence = sorted(context["state_changes"], key=lambda row: (-(row["sequence"] or -1), -(row["ordinal"] or -1), row["target_type"] or "", row["target_id"] or "", row["path"] or ""))
        for row in consequence:
            row["freshness"] = round(1.0 / (1.0 + max(0, current - (row.get("sequence") or current))) * self.weights.freshness, 8)
            row["pressure_score"] = row["freshness"]
        relationship = []
        for row in consequence:
            path = row.get("path") or ""
            if row.get("target_type") == "CHARACTER" and path.startswith("/relationships/"):
                parts = path.split("/")
                other_id = parts[2] if len(parts) > 2 and parts[2] else None
                ids = tuple(sorted(value for value in (row.get("target_id"), other_id) if value))
                if len(ids) == 2:
                    relationship.append({**row, "character_ids": ids})
        character_consequence = {}
        character_relationship = {}
        for row in consequence:
            if row.get("target_type") == "CHARACTER" and row.get("target_id"):
                character_consequence[row["target_id"]] = character_consequence.get(row["target_id"], 0.0) + row["pressure_score"]
        for row in relationship:
            for character_id in row["character_ids"]:
                character_relationship[character_id] = character_relationship.get(character_id, 0.0) + row["pressure_score"]
        thread_rows = []
        for thread in context["story_threads"]:
            if thread["status"] != ThreadStatus.OPEN.value:
                continue
            stat = thread_stats.get(thread["id"])
            touched = [scene["sequence"] for scene in scenes if thread["id"] in scene.get("story_threads", [])]
            last = stat.get("last_touched_sequence") if stat else (max(touched) if touched else None)
            stale = current - last if last is not None else current + 1
            repetition = sum(thread["id"] in scene.get("story_threads", []) for scene in recent)
            progress_signal = 1.0 - abs(thread["progress"] - 0.5)
            explicit_ids = self._thread_character_ids(thread)
            scene_alignment = stat.get("scene_alignment_count", 0) if stat else sum(1 for scene in scenes if thread["id"] in scene.get("story_threads", []) and set(scene.get("participants", [])).intersection({row["id"] for row in context["characters"]}))
            character_alignment = (len(explicit_ids) + scene_alignment) * self.weights.character_alignment
            consequence_alignment = sum(row["pressure_score"] for row in consequence if row.get("target_type") == "STORY_THREAD" and row.get("target_id") == thread["id"])
            consequence_alignment += sum(row["pressure_score"] for row in consequence if row.get("scene_id") and (thread["id"] in row.get("thread_ids", []) if "thread_ids" in row else any(scene["id"] == row["scene_id"] and thread["id"] in scene.get("story_threads", []) for scene in scenes)))
            components = {"base_weight": thread["weight"] * self.weights.thread_weight, "staleness": min(stale, 20) * self.weights.staleness, "progress_pressure": progress_signal * self.weights.progress, "recent_repetition_penalty": -repetition * self.weights.repetition, "character_alignment": character_alignment, "consequence_alignment": consequence_alignment * self.weights.consequence_alignment, "arc_alignment": self.weights.arc_alignment if context.get("story_arc") and (thread["state"].get("arc_id") == context["story_arc"].get("id") or context["story_arc"].get("id") in (thread["state"].get("arc_ids") or [])) else 0.0}
            thread_rows.append({"thread_id": thread["id"], "status": thread["status"], "last_touched_sequence": last, "staleness": stale, "score_components": components, "thread_gravity_score": round(sum(components.values()), 8)})
        thread_rows.sort(key=lambda row: (-row["thread_gravity_score"], row["thread_id"]))
        char_rows = []
        for character in context["characters"]:
            stat = character_stats.get(character["id"])
            sequences = [scene["sequence"] for scene in scenes if character["id"] in scene.get("participants", [])]
            recent_count = sum(character["id"] in scene.get("participants", []) for scene in recent)
            last_participation = stat.get("last_participation_sequence") if stat else (max(sequences) if sequences else None)
            absence = current - last_participation if last_participation is not None else current + 1
            components = {"narrative_relevance": float(character["narrative_relevance"] or 0), "goal_pressure": self.weights.goal if character["has_goal"] else 0.0, "absence": min(absence, 20) * self.weights.absence, "overuse_penalty": -recent_count * self.weights.overuse, "belief_conflict_signal": float(character.get("belief_conflict_count", 0)) * self.weights.belief_conflict, "consequence_pressure": character_consequence.get(character["id"], 0.0), "relationship_pressure": character_relationship.get(character["id"], 0.0)}
            char_rows.append({"character_id": character["id"], "last_participation_sequence": last_participation, "score_components": components, "character_gravity_score": round(sum(components.values()), 8)})
        char_rows.sort(key=lambda row: (-row["character_gravity_score"], row["character_id"]))
        repetition = {"proposal_types": {}, "threads": {}, "participants": {}, "locations": {}}
        for signature in context["recent_scene_signatures"]:
            for key, value in (("proposal_types", signature.get("proposal_type")), ("threads", signature.get("primary_thread_id")), ("locations", signature.get("location_id"))):
                if value: repetition[key][value] = repetition[key].get(value, 0) + 1
            for value in signature.get("participants", []): repetition["participants"][value] = repetition["participants"].get(value, 0) + 1
        seeds = [{"thread_id": row["thread_id"], "score": row["thread_gravity_score"]} for row in thread_rows[:5]]
        paused = [thread for thread in context["story_threads"] if thread["status"] == ThreadStatus.PAUSED.value]
        fast = context.get("protocol_version") == "story-gravity-context-v2"
        protocol = "story-gravity-v2" if fast else "story-gravity-v1"
        semantic = {"protocol": protocol, "current_sequence": current, "threads": thread_rows, "paused": paused, "characters": char_rows, "consequence": consequence, "relationship": relationship, "reveals": context["reveals"], "repetition": repetition, "source_ids": {"events": [row["id"] for row in consequence], "projection": history_stats.get("projection_fingerprint")} if fast else {"scenes": [row["id"] for row in context["scenes"]], "events": [row["id"] for row in consequence], "causal_links": [row["fingerprint"] for row in context.get("causal_links", [])]}}
        fingerprint = protocol + ":" + hashlib.sha256(json.dumps(semantic, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return StoryGravityReport(protocol, current, thread_rows, char_rows, consequence, relationship, context["reveals"], repetition, seeds, fingerprint, paused)

    @staticmethod
    def _thread_character_ids(thread: dict[str, Any]) -> set[str]:
        state = thread.get("state") or {}
        values = []
        for field in ("character_id", "character_ids", "participant_ids"):
            value = state.get(field)
            values.extend(value if isinstance(value, list) else [value])
        return {value for value in values if isinstance(value, str) and value}


class DirectorCandidateEngine:
    MAX_THREAD_CANDIDATES = 3
    def __init__(self, weights: StoryGravityWeights | None = None):
        self.weights = weights or StoryGravityWeights()

    def generate(self, context: dict[str, Any], gravity: StoryGravityReport) -> list[StoryGravityCandidate]:
        candidates = []
        top_character = gravity.character_gravity[0] if gravity.character_gravity else None
        character = next((row for row in context["characters"] if row["id"] == top_character["character_id"]), None) if top_character else None
        for top_thread in gravity.thread_gravity[:self.MAX_THREAD_CANDIDATES]:
            thread = next(row for row in context["story_threads"] if row["id"] == top_thread["thread_id"])
            aligned_ids = StoryGravityEngine._thread_character_ids(thread)
            stats = (context.get("history_stats") or {}).get("thread_stats", {}).get(thread["id"], {})
            aligned_ids.update(stats.get("aligned_participant_ids", []))
            if not stats:
                aligned_ids.update(participant for scene in context["scenes"] if thread["id"] in scene.get("story_threads", []) for participant in scene.get("participants", []))
            aligned = [row for row in context["characters"] if row["id"] in aligned_ids]
            aligned.sort(key=lambda row: (-(next((item["character_gravity_score"] for item in gravity.character_gravity if item["character_id"] == row["id"]), 0.0)), row["id"]))
            thread_character = aligned[0] if aligned else character
            participants = (thread_character["id"],) if thread_character else ()
            location = thread_character.get("location_id") if thread_character else None
            candidates.append(self._candidate(ProposalType.CONTINUE_THREAD.value, thread["id"], participants, location, "THREAD", (thread["id"],), "thread", {"thread_gravity": top_thread["thread_gravity_score"]}, ("STALE_THREAD",) if top_thread["staleness"] > 3 else (), gravity=gravity, expected_progress={"thread": thread["id"]}, evidence={"thread_id": thread["id"]}))
            fresh = [row for row in gravity.consequence_pressure if (row.get("target_type") == "STORY_THREAD" and row.get("target_id") == thread["id"]) or (thread["id"] in row.get("thread_ids", []) if "thread_ids" in row else any(scene.get("id") == row.get("scene_id") and thread["id"] in scene.get("story_threads", []) for scene in context["scenes"]))]
            if fresh and gravity.repetition_pressure["threads"].get(thread["id"], 0) < 2:
                candidates.append(self._candidate(ProposalType.ESCALATION.value, thread["id"], participants, location, "THREAD", (thread["id"],), "escalation", {"thread_gravity": top_thread["thread_gravity_score"], "fresh_consequence": fresh[0]["pressure_score"]}, ("THREAD_ESCALATION",), gravity=gravity, expected_progress={"thread": thread["id"], "escalation": True}, evidence={"timeline_event_ids": sorted(row["id"] for row in fresh), "thread_id": thread["id"]}))
        if character:
            candidates.append(self._candidate(ProposalType.CHARACTER_DRIVEN.value, None, (character["id"],), character.get("location_id"), "CHARACTER", (character["id"],), "goal", top_character["score_components"], ("CHARACTER_GOAL",) if character["has_goal"] else (), gravity=gravity, expected_progress={"character_arc": True}, evidence={"character_id": character["id"]}))
        for event in gravity.consequence_pressure[:3]:
            target_character = next((row for row in context["characters"] if event.get("target_type") == "CHARACTER" and row["id"] == event.get("target_id")), None)
            participants = (target_character["id"],) if target_character else ((character["id"],) if character else ())
            location = target_character.get("location_id") if target_character else (character.get("location_id") if character else None)
            evidence = {"timeline_event_ids": [event["id"]], "target_type": event.get("target_type"), "target_id": event.get("target_id"), "path": event.get("path")}
            candidates.append(self._candidate(ProposalType.CONSEQUENCE.value, None, participants, location, event.get("target_type") or "STATE", (event.get("target_id"),) if event.get("target_id") else (), "consequence", {"freshness": event.get("pressure_score", 0.0)}, ("RECENT_STATE_CHANGE",), gravity=gravity, expected_progress={"consequence": {key: evidence[key] for key in ("target_type", "target_id", "path")}}, evidence=evidence))
        for reveal in gravity.reveal_opportunities:
            allowed = tuple(sorted(reveal.get("allowed_character_ids") or []))
            if allowed:
                authorized = [row for row in context["characters"] if row["id"] in allowed]
                ranked = sorted(authorized, key=lambda row: (-(next((item["character_gravity_score"] for item in gravity.character_gravity if item["character_id"] == row["id"]), 0.0)), row["id"]))
                anchor = ranked[0] if ranked else None
                if anchor:
                    location = anchor.get("location_id")
                    participants = tuple(sorted(row["id"] for row in authorized if row.get("location_id") == location))
                    candidates.append(self._candidate(ProposalType.REVEAL.value, None, participants, location, "REVEAL", (reveal["canon_fact_id"],), "reveal", {"availability": 1.0}, ("AVAILABLE_REVEAL",), reveal_ids=(reveal["canon_fact_id"],), gravity=gravity, expected_progress={"reveal": {"canon_fact_ids": [reveal["canon_fact_id"]]}}, evidence={"canon_fact_ids": [reveal["canon_fact_id"]], "allowed_character_ids": list(allowed)}))
        if gravity.relationship_pressure and len(context["characters"]) >= 2:
            participants = tuple(gravity.relationship_pressure[0].get("character_ids", ())) or tuple(sorted(row["id"] for row in context["characters"][:2]))
            locations = sorted({row.get("location_id") for row in context["characters"] if row["id"] in participants and row.get("location_id")})
            ranked_participants = sorted((row for row in context["characters"] if row["id"] in participants), key=lambda row: (-(next((item["character_gravity_score"] for item in gravity.character_gravity if item["character_id"] == row["id"]), 0.0)), row["id"]))
            location = next((row.get("location_id") for row in ranked_participants if row.get("location_id")), locations[0] if locations else None)
            relationship_type = ProposalType.TRANSITION.value if len(locations) > 1 else ProposalType.RELATIONSHIP.value
            relationship_evidence = gravity.relationship_pressure[0]
            progress = {"transition": {"participant_ids": list(participants), "destination_location_id": location}} if relationship_type == ProposalType.TRANSITION.value else {"relationship": {"character_ids": list(participants)}}
            candidates.append(self._candidate(relationship_type, None, participants, location, "RELATIONSHIP", participants, "relationship", {"relationship_pressure": relationship_evidence.get("pressure_score", 1.0)}, ("STRUCTURED_RELATIONSHIP", "LOCATION_TRANSITION") if relationship_type == ProposalType.TRANSITION.value else ("STRUCTURED_RELATIONSHIP",), gravity=gravity, expected_progress=progress, evidence={"timeline_event_ids": [relationship_evidence["id"]] if relationship_evidence.get("id") else [], "target_type": relationship_evidence.get("target_type"), "target_id": relationship_evidence.get("target_id"), "path": relationship_evidence.get("path")}))
        if not candidates:
            candidates.append(self._candidate(ProposalType.NEW_THREAD.value, None, (), None, "PROJECT", (), "new_thread", {"fallback": 0.1}, ("NO_OPEN_THREAD",), gravity=gravity, expected_progress={"character_arc": False}, evidence={}))
        return self.rank(candidates)

    def _candidate(self, proposal_type, thread_id, participants, location, focus_type, focus_ids, pressure, components, reasons, *, gravity, expected_progress, evidence, reveal_ids=()):
        repetition = gravity.repetition_pressure
        components = dict(components)
        components.update({
            "proposal_type_repetition_penalty": -repetition["proposal_types"].get(proposal_type, 0) * self.weights.proposal_type_repetition,
            "thread_repetition_penalty": -repetition["threads"].get(thread_id, 0) * self.weights.thread_candidate_repetition if thread_id else 0.0,
            "participant_repetition_penalty": -sum(repetition["participants"].get(value, 0) for value in participants) * self.weights.participant_repetition,
            "location_repetition_penalty": -repetition["locations"].get(location, 0) * self.weights.location_repetition if location else 0.0,
        })
        canonical_evidence = json.dumps(_canonical_semantic(evidence), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        evidence_identity = hashlib.sha256(canonical_evidence.encode()).hexdigest()
        key = "|".join([proposal_type, thread_id or "", ",".join(sorted(participants)), location or "", focus_type, ",".join(sorted(str(value) for value in focus_ids)), pressure, ",".join(sorted(reveal_ids)), f"evidence:{evidence_identity}"])
        score = round(sum(float(value) for value in components.values()), 8)
        return StoryGravityCandidate(key, proposal_type, thread_id, tuple(sorted(participants)), location, focus_type, tuple(sorted(str(value) for value in focus_ids)), pressure, {key: round(float(value), 8) for key, value in components.items()}, score, tuple(reasons), expected_progress, tuple(sorted(reveal_ids)), evidence)
    def rank(self, candidates):
        return sorted(candidates, key=lambda item: (-item.score, item.proposal_type, item.primary_thread_id or "", item.candidate_key))
    def select(self, candidates):
        return self.rank(candidates)[0] if candidates else None
    def top_diverse(self, candidates, k=3):
        ranked = self.rank(candidates)
        result = []
        seen = set()
        remaining = []
        for candidate in ranked:
            identity = (candidate.proposal_type, candidate.primary_thread_id, candidate.participant_ids, candidate.location_id)
            if identity not in seen and len(result) < k:
                result.append(candidate); seen.add(identity)
            else:
                remaining.append(candidate)
        if len(result) < k:
            result.extend(remaining[:k - len(result)])
        return result


class DirectorProposalFactory:
    def create(self, project_id: str, context: dict[str, Any], gravity: StoryGravityReport, candidate: StoryGravityCandidate) -> dict[str, Any]:
        participant_ids = list(candidate.participant_ids)
        meta = {"protocol": "story-gravity-v1", "gravity_fingerprint": gravity.gravity_fingerprint, "candidate_key": candidate.candidate_key, "reason_codes": list(candidate.reason_codes), "score_components": candidate.score_components, "focus_type": candidate.focus_type, "focus_ids": list(candidate.focus_ids), "pressure_kind": candidate.pressure_kind, "evidence": candidate.evidence}
        motivations = {character_id: {"reason": "The current situation creates a structured opportunity for an autonomous choice."} for character_id in participant_ids}
        return {"proposal_type": candidate.proposal_type, "primary_thread_id": candidate.primary_thread_id, "location_id": candidate.location_id, "proposed_location": None, "participants": participant_ids, "scene_goal": "Create an opportunity to respond to the current pressure.", "character_motivations": motivations, "entry_state": {"director_meta": meta}, "planned_pressure": "A structured external pressure creates multiple plausible responses.", "expected_progress": candidate.expected_progress, "allowed_reveals": list(candidate.reveal_ids), "forbidden_reveals": [], "required_canon": [], "possible_outcomes": ["The pressure is refused, delayed, or investigated.", "The situation changes through an autonomous character choice."], "new_entity_requests": [], "risk_flags": list(candidate.reason_codes), "director_reasoning_summary": "Selected a structured situation; character decisions remain autonomous."}


class DirectorCandidatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_type: ProposalType
    primary_thread_id: str | None = None
    location_id: str | None = None
    participants: list[str] = Field(default_factory=list)
    scene_goal: str
    planned_pressure: str
    expected_progress: dict[str, Any] = Field(default_factory=dict)
    allowed_reveals: list[str] = Field(default_factory=list)
    required_knowledge: dict[str, list[dict[str, Any]]] | list[dict[str, Any]] = Field(default_factory=dict)
    possible_outcomes: list[str] = Field(default_factory=list)
    reasoning_summary: str


class DirectorModelContextSanitizer:
    """Expose only structured story inputs to an optional Director model."""
    def sanitize(self, context: dict[str, Any], gravity: StoryGravityReport | None = None) -> dict[str, Any]:
        result = {"protocol": "director-model-context-v1", "project": {"id": context.get("project", {}).get("id"), "story_seed": context.get("project", {}).get("story_seed")}, "current_sequence": context.get("current_sequence", 0), "active_story_threads": context.get("story_threads", context.get("active_story_threads", [])), "active_characters": context.get("characters", context.get("active_characters", [])), "recent_scene_signatures": context.get("recent_scene_signatures", [])}
        if gravity:
            result["story_gravity"] = gravity.as_dict()
        return result


class LLMDirectorCandidateGenerator:
    """Optional candidate-only model adapter; deterministic code remains authority."""
    def generate(self, provider, model: str, context: dict[str, Any], gravity: StoryGravityReport | None = None) -> DirectorCandidatePayload:
        safe = DirectorModelContextSanitizer().sanitize(context, gravity)
        result = provider.generate([{"role": "system", "content": "Return one structured Director situation candidate. Never script character action or outcome."}, {"role": "user", "content": json.dumps(safe, ensure_ascii=True, sort_keys=True)}], model)
        try:
            payload = json.loads(result.content)
            return DirectorCandidatePayload.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError("MODEL_OUTPUT_INVALID") from exc

    def validate_references(self, candidate: DirectorCandidatePayload, context: dict[str, Any]) -> list[str]:
        character_ids = {row.get("id") for row in context.get("characters", context.get("active_characters", []))}
        threads = {row.get("id"): row for row in context.get("story_threads", context.get("active_story_threads", []))}
        thread_ids = {thread_id for thread_id, row in threads.items() if row.get("status", ThreadStatus.OPEN.value) == ThreadStatus.OPEN.value}
        errors = ["INVALID_GRAVITY_REFERENCE"] if candidate.primary_thread_id and candidate.primary_thread_id not in thread_ids else []
        if any(value not in character_ids for value in candidate.participants): errors.append("INVALID_GRAVITY_REFERENCE")
        location_ids = {row.get("id") for row in context.get("locations", [])}
        if candidate.location_id and candidate.location_id not in location_ids: errors.append("INVALID_GRAVITY_REFERENCE")
        reveal_rows = {row.get("canon_fact_id"): row for row in context.get("reveals", [])}
        reveal_ids = set(reveal_rows)
        if any(value not in reveal_ids for value in candidate.allowed_reveals): errors.append("INVALID_GRAVITY_REFERENCE")
        for reveal_id in candidate.allowed_reveals:
            allowed = set(reveal_rows.get(reveal_id, {}).get("allowed_character_ids") or [])
            if not set(candidate.participants).issubset(allowed): errors.append("UNAUTHORIZED_REVEAL_PARTICIPANT")
        knowledge = {row.get("knowledge_id"): row for row in context.get("knowledge", [])}
        references = candidate.required_knowledge.items() if isinstance(candidate.required_knowledge, dict) else ((ref.get("character_id"), [ref]) for ref in candidate.required_knowledge)
        for character_id, refs in references:
            for ref in refs:
                if character_id not in character_ids or not DirectorKnowledgeReferenceValidator.validate(ref, character_id, context.get("knowledge", [])):
                    errors.append("INVALID_GRAVITY_REFERENCE")
        forbidden = {"required_action", "forced_action", "must_accept", "must_succeed", "forced_outcome"}
        def contains(value):
            if isinstance(value, dict): return bool(forbidden.intersection(value)) or any(contains(item) for item in value.values())
            if isinstance(value, list): return any(contains(item) for item in value)
            return False
        if contains(candidate.model_dump(mode="json")): errors.append("DIRECTOR_CHARACTER_PUPPETEERING")
        return sorted(set(errors))

@dataclass
class ValidationIssue:
    code: str
    severity: str
    message: str
    related_entity_ids: list[str]
    suggested_fix: str

@dataclass
class ValidationReport:
    issues: list[ValidationIssue]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "BLOCKING" for issue in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "issues": [issue.__dict__ for issue in self.issues]}

class DirectorContextBuilder:
    def build(self, session: Session, project_id: str, include_secret_canon: bool = True) -> dict[str, Any]:
        project = session.get(Project, project_id)
        if not project:
            raise ValueError("Project not found")
        characters = session.scalars(select(Character).where(Character.project_id == project_id, Character.active.is_(True)).order_by(Character.id)).all()
        character_ids = [character.id for character in characters]
        open_threads = session.scalars(select(StoryThread).where(StoryThread.project_id == project_id, StoryThread.status == ThreadStatus.OPEN).order_by(StoryThread.weight.desc(), StoryThread.id)).all()
        paused_threads = session.scalars(select(StoryThread).where(StoryThread.project_id == project_id, StoryThread.status == ThreadStatus.PAUSED).order_by(StoryThread.weight.desc(), StoryThread.id)).all()
        recent_scenes = session.scalars(select(Scene).where(Scene.project_id == project_id, Scene.status == SceneStatus.OCCURRED, Scene.history_status == "ACTIVE").order_by(Scene.sequence.desc(), Scene.id.desc()).limit(RECENT_SCENE_LIMIT)).all()
        active_arc = session.scalar(select(StoryArc).where(StoryArc.project_id == project_id, StoryArc.status == "ACTIVE").order_by(StoryArc.id.desc()))
        reader = ActiveCharacterCognitionReader()
        knowledge = [item for character in characters for item in reader.knowledge(session, project_id, character.id)] if character_ids else []
        related_ids = self._related_entity_ids(characters, open_threads + paused_threads, recent_scenes)
        entities = session.scalars(select(WorldEntity).where(WorldEntity.project_id == project_id, WorldEntity.active.is_(True)).order_by(WorldEntity.id)).all()
        selected_entities = [entity for entity in entities if entity.id in related_ids or entity.name in related_ids]
        canon = session.scalars(select(CanonFact).where(CanonFact.project_id == project_id).order_by(CanonFact.id)).all()
        selected_canon = [fact for fact in canon if fact.fact_type == CanonType.CORE_CANON or (fact.fact_type == CanonType.WORLD_FACT and fact.locked) or fact.id in related_ids]
        if include_secret_canon:
            selected_canon.extend(self._relevant_secret_canon(session, project_id, canon, related_ids, character_ids, [thread.id for thread in open_threads + paused_threads], active_arc.id if active_arc else None))
        writing = session.scalar(select(WritingBible).where(WritingBible.project_id == project_id, WritingBible.active.is_(True)).order_by(WritingBible.version.desc(), WritingBible.id))
        anti_ai = session.scalar(select(AntiAIBible).where(AntiAIBible.project_id == project_id, AntiAIBible.active.is_(True)).order_by(AntiAIBible.version.desc(), AntiAIBible.id))
        context = {
            "project": {"id": project.id, "creation_mode": project.creation_mode.value, "story_seed": project.story_seed, "autonomy_settings": project.autonomy_settings, "current_world_time": project.current_world_time.isoformat() if project.current_world_time else None, "chapter_words": {"target": project.target_chapter_words, "min": project.min_chapter_words, "max": project.max_chapter_words}},
            "current_story_arc": self._arc(active_arc),
            "active_story_threads": [self._thread(thread) for thread in open_threads],
            "paused_story_threads": [self._thread(thread) for thread in paused_threads],
            "active_characters": [self._character(character) for character in characters],
            "character_knowledge": self._knowledge(knowledge),
            "recent_scenes": [self._scene(scene) for scene in recent_scenes],
            "canon": [self._canon(fact) for fact in selected_canon],
            "world_entities": [{"id": entity.id, "type": getattr(entity.entity_type, "value", entity.entity_type), "name": entity.name, "profile": entity.profile} for entity in selected_entities],
            "writing_constraints": self._writing_constraints(writing.rules if writing else {}),
            "anti_ai_constraints": {"avoid_repeated_conclusions": True, "avoid_static_delay": True, "principles": (anti_ai.writing_principles[:3] if anti_ai else [])},
        }
        gravity_context = StoryGravityContextBuilder().build(session, project_id)
        gravity_report = StoryGravityEngine().build(gravity_context)
        context["current_sequence"] = gravity_context["current_sequence"]
        context["recent_scene_signatures"] = gravity_context["recent_scene_signatures"]
        context["story_gravity_inputs"] = {"thread_ids": [row["id"] for row in gravity_context["story_threads"]], "state_change_ids": [row["id"] for row in gravity_context["state_changes"]], "reveal_ids": [row["canon_fact_id"] for row in gravity_context["reveals"]]}
        context["current_history_fingerprint"] = gravity_report.gravity_fingerprint
        context["story_gravity_report"] = gravity_report.as_dict()
        context["fingerprint"] = context_fingerprint(context)
        context["version"] = context["fingerprint"]
        return context

    def _related_entity_ids(self, characters, threads, scenes) -> set[str]:
        ids: set[str] = set()
        for character in characters: ids.update(extract_entity_references(character.current_state))
        for thread in threads: ids.update(extract_entity_references(thread.state))
        for scene in scenes: ids.update(extract_entity_references(scene.facts))
        return ids
    def _relevant_secret_canon(self, session, project_id, canon, related_ids, character_ids, thread_ids, arc_id):
        reveal_ids = {lock.canon_fact_id for lock in session.scalars(select(RevealConstraint).where(RevealConstraint.project_id == project_id).order_by(RevealConstraint.id)).all() if set(lock.allowed_character_ids).intersection(character_ids)}
        relevant = related_ids | set(character_ids) | set(thread_ids) | ({arc_id} if arc_id else set())
        result = []
        for fact in canon:
            if fact.fact_type != CanonType.SECRET_CANON: continue
            data_refs = extract_entity_references(fact.data) | set(fact.data.get("thread_ids", [])) | set(fact.data.get("arc_ids", []))
            if fact.data.get("global_director_required") is True or fact.id in reveal_ids or data_refs.intersection(relevant): result.append(fact)
        return result
    def _arc(self, arc): return None if not arc else {"id": arc.id, "title": arc.title, "core_question": arc.core_question, "core_conflict": arc.core_conflict, "progress": arc.progress, "state": arc.status}
    def _thread(self, thread): return {"id": thread.id, "title": thread.title, "type": thread.type, "weight": thread.weight, "goal": thread.goal, "progress": thread.progress, "state": thread.state}
    def _character(self, character): return {"id": character.id, "name": character.name, "role_level": character.profile.get("role_level", "SUPPORTING"), "current_location": character.current_state.get("location_id", character.current_state.get("location")), "current_goals": character.goals, "core_values": character.core_values, "boundaries": character.boundaries, "physical_state": character.physical_state, "emotional_state": character.emotional_state, "relevant_abilities": character.abilities, "narrative_relevance": character.narrative_relevance}
    def _knowledge(self, knowledge):
        result: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for item in knowledge: result.setdefault(item.character_id, {"KNOWN": [], "SUSPECTED": [], "FALSE_BELIEF": []})[item.status.value].append({"id": item.id, "knowledge_id": item.id, "proposition": item.proposition, "confidence": item.confidence, "status": item.status.value})
        return result
    def _scene(self, scene): return {"id": scene.id, "sequence": scene.sequence, "location": scene.location, "participants": scene.participants, "intent": scene.intent, "story_threads": scene.story_threads, "result": scene.result}
    def _canon(self, fact): return {"id": fact.id, "type": fact.fact_type.value, "proposition": fact.proposition, "locked": fact.locked, "data": fact.data}
    def _writing_constraints(self, rules): return {key: rules[key] for key in ("pacing", "scene_structure", "point_of_view", "plot_rules") if key in rules}

class DirectorConstraintChecker:
    def validate(self, session: Session, context: dict[str, Any], proposal: SceneProposal) -> ValidationReport:
        issues: list[ValidationIssue] = []
        characters = {character["id"]: character for character in context["active_characters"]}
        threads = {thread["id"]: thread for thread in context["active_story_threads"]}
        canon = {fact["id"]: fact for fact in context["canon"]}
        self._references(session, proposal, characters, threads, issues)
        self._canon_conflict(proposal, canon, issues)
        self._knowledge(proposal, context["character_knowledge"], issues)
        self._motivation_and_boundaries(proposal, characters, issues)
        self._story_value(proposal, context["recent_scenes"], issues)
        self._reveals(session, proposal, issues)
        self._world_state(proposal, characters, issues)
        self._overcasting(proposal, issues)
        self._puppeteering(proposal, issues)
        return ValidationReport(issues)

    def _add(self, issues, code, severity, message, ids, fix): issues.append(ValidationIssue(code, severity, message, ids, fix))
    def _references(self, session, proposal, characters, threads, issues):
        missing_characters = [item for item in proposal.participants if item not in characters]
        if missing_characters: self._add(issues, "INVALID_ENTITY_REFERENCE", "BLOCKING", "Proposal references characters outside the active project.", missing_characters, "Use active project character IDs.")
        if proposal.primary_thread_id and proposal.primary_thread_id not in threads: self._add(issues, "INVALID_ENTITY_REFERENCE", "BLOCKING", "Primary Story Thread is not active in this project.", [proposal.primary_thread_id], "Choose an active Story Thread or use NEW_THREAD.")
        if proposal.location_id:
            location = session.get(WorldEntity, proposal.location_id)
            if not location or location.project_id != proposal.project_id or getattr(location.entity_type, "value", location.entity_type) != EntityType.LOCATION.value or not location.active:
                self._add(issues, "INVALID_ENTITY_REFERENCE", "BLOCKING", "location_id must reference an active LOCATION in this project.", [proposal.location_id], "Use an existing project LOCATION ID or a proposed new location.")
    def _canon_conflict(self, proposal, canon, issues):
        for fact_id in proposal.required_canon or []:
            if fact_id not in canon: self._add(issues, "INVALID_ENTITY_REFERENCE", "BLOCKING", "Required Canon is unavailable to Director Context.", [fact_id], "Reference a project Canon fact.")
        contradictions = (proposal.entry_state or {}).get("contradicts_canon_ids", [])
        locked = [fact_id for fact_id in contradictions if canon.get(fact_id, {}).get("locked") and canon[fact_id]["type"] == "CORE_CANON"]
        if locked: self._add(issues, "CANON_CONFLICT", "BLOCKING", "Proposal contradicts locked CORE_CANON.", locked, "Revise entry state to preserve locked facts.")
    def _knowledge(self, proposal, knowledge, issues):
        for character_id, motivation in proposal.character_motivations.items():
            required = motivation.get("required_knowledge", []) if isinstance(motivation, dict) else []
            available = {status: {item["proposition"] for item in knowledge.get(character_id, {}).get(status, [])} for status in ("KNOWN", "SUSPECTED", "FALSE_BELIEF")}
            missing = []
            for requirement in required:
                if isinstance(requirement, str):
                    proposition, accepted = requirement, ["KNOWN"]
                    valid = bool(proposition) and any(proposition in available.get(status, set()) for status in accepted)
                else:
                    proposition, accepted = requirement.get("proposition"), requirement.get("accepted_statuses", ["KNOWN"])
                    knowledge_id = requirement.get("knowledge_id")
                    if knowledge_id:
                        valid = DirectorKnowledgeReferenceValidator.validate(requirement, character_id, knowledge.get(character_id, {}))
                    else:
                        valid = bool(proposition) and any(proposition in available.get(status, set()) for status in accepted)
                if not valid: missing.append(proposition or "unspecified proposition")
            if missing: self._add(issues, "KNOWLEDGE_LEAK", "BLOCKING", "Character motivation relies on information this character has not acquired.", [character_id], "Use known clues, suspicion, or add a prior discovery Scene.")
    def _motivation_and_boundaries(self, proposal, characters, issues):
        for character_id in proposal.participants:
            if not proposal.character_motivations.get(character_id): self._add(issues, "CHARACTER_MOTIVATION_GAP", "WARNING", "A participant has no stated reason to join this Scene.", [character_id], "State the character's immediate motivation.")
            conflicts = proposal.entry_state.get("boundary_conflicts", {}).get(character_id, [])
            if conflicts: self._add(issues, "CHARACTER_BOUNDARY_CONFLICT", "ERROR", "Proposal may cross a declared character boundary.", [character_id], "Change pressure or justify a deliberate exception.")
    def _story_value(self, proposal, recent, issues):
        progress = proposal.expected_progress or {}
        if not proposal.primary_thread_id and not progress.get("character_arc") and not progress.get("relationship") and not progress.get("consequence"):
            self._add(issues, "THREADLESS_SCENE", "ERROR", "Scene does not advance a Story Thread, arc, relationship, or consequence.", [], "Add a concrete narrative progression.")
        goal = proposal.scene_goal.strip().lower()
        if goal and any(goal == (scene.get("intent") or "").strip().lower() for scene in recent): self._add(issues, "DUPLICATE_NARRATIVE_FUNCTION", "WARNING", "Recent Scene already used the same narrative intent.", [], "Change the pressure or advance the thread in a new way.")
    def _reveals(self, session, proposal, issues):
        if not proposal.allowed_reveals: return
        locks = session.scalars(select(RevealConstraint).where(RevealConstraint.project_id == proposal.project_id, RevealConstraint.canon_fact_id.in_(proposal.allowed_reveals))).all()
        constraints = {lock.canon_fact_id: lock for lock in locks}
        missing = set(proposal.allowed_reveals) - set(constraints)
        if missing: self._add(issues, "INVALID_ENTITY_REFERENCE", "BLOCKING", "Reveal is not available through a project Reveal Constraint.", sorted(missing), "Use only available authorized Reveal Constraints.")
        for lock in locks:
            fact = session.get(CanonFact, lock.canon_fact_id)
            invalid_allowed = [character_id for character_id in lock.allowed_character_ids if not session.get(Character, character_id) or session.get(Character, character_id).project_id != proposal.project_id]
            if not fact or fact.project_id != proposal.project_id or invalid_allowed: self._add(issues, "INVALID_ENTITY_REFERENCE", "BLOCKING", "Reveal Constraint crosses project boundaries.", [lock.id], "Repair the Reveal Constraint references.")
            unauthorized = [character_id for character_id in proposal.participants if character_id not in lock.allowed_character_ids]
            if getattr(lock.status, "value", lock.status) == RevealStatus.LOCKED.value:
                self._add(issues, "PREMATURE_REVEAL", "BLOCKING", "Proposal reveals a locked Canon fact before its condition is met.", [lock.canon_fact_id], "Remove this reveal or update its Reveal Constraint through the appropriate workflow.")
            if unauthorized: self._add(issues, "PREMATURE_REVEAL", "BLOCKING", "Proposal reveals a locked Canon fact before its condition is met.", [lock.canon_fact_id] + unauthorized, "Remove this reveal or update its Reveal Constraint through the appropriate workflow.")
        forbidden = set(proposal.forbidden_reveals or []).intersection(proposal.allowed_reveals or [])
        if forbidden: self._add(issues, "PREMATURE_REVEAL", "BLOCKING", "Proposal both allows and forbids the same reveal.", list(forbidden), "Remove the forbidden reveal from allowed_reveals.")
    def _world_state(self, proposal, characters, issues):
        if proposal.proposal_type == ProposalType.TRANSITION or not proposal.location_id: return
        elsewhere = [character_id for character_id in proposal.participants if characters.get(character_id, {}).get("current_location") and characters[character_id]["current_location"] != proposal.location_id]
        if elsewhere: self._add(issues, "WORLD_STATE_CONFLICT", "BLOCKING", "Participants are not currently at the proposed location and no Transition is planned.", elsewhere, "Use a TRANSITION proposal or select their current location.")
    def _overcasting(self, proposal, issues):
        missing_reason = [str(item.get("name", "new entity")) for item in (proposal.new_entity_requests or []) if item.get("entity_type") == "CHARACTER" and not item.get("existing_character_gap")]
        if missing_reason: self._add(issues, "OVERCASTING", "WARNING", "New character requests do not explain why existing characters cannot serve the function.", missing_reason, "Provide existing_character_gap for each new character request.")

    def _puppeteering(self, proposal, issues):
        forbidden = {"chosen_action", "forced_action", "required_action", "must_accept", "must_succeed", "forced_outcome", "decision_type"}
        def contains(value):
            if isinstance(value, dict):
                return forbidden.intersection(value) or any(contains(item) for item in value.values())
            if isinstance(value, list):
                return any(contains(item) for item in value)
            return False
        if contains(proposal.character_motivations) or contains(proposal.entry_state) or contains(proposal.expected_progress):
            self._add(issues, "DIRECTOR_CHARACTER_PUPPETEERING", "BLOCKING", "Director proposal contains a coercive character-action field.", list(proposal.participants), "Describe external pressure and preserve autonomous character choice.")

class HeuristicDirector:
    def propose(self, context: dict[str, Any]) -> dict[str, Any]:
        thread = context["active_story_threads"][0] if context["active_story_threads"] else None
        characters = sorted(context["active_characters"], key=lambda item: item["narrative_relevance"].get("score", 0), reverse=True)
        primary = characters[0] if characters else None
        location = primary["current_location"] if primary else None
        goal = (primary["current_goals"].get("current") if primary else None) or (thread["goal"] if thread else None) or "create a consequential choice"
        return {"proposal_type": ProposalType.CONTINUE_THREAD.value if thread else ProposalType.CHARACTER_DRIVEN.value, "primary_thread_id": thread["id"] if thread else None, "location_id": location, "proposed_location": None, "participants": [primary["id"]] if primary else [], "scene_goal": f"Advance {goal}", "character_motivations": {primary["id"]: {"reason": goal}} if primary else {}, "entry_state": {}, "planned_pressure": "A concrete obstacle complicates the character's current objective.", "expected_progress": {"thread": thread["id"]} if thread else {"character_arc": True}, "allowed_reveals": [], "forbidden_reveals": [], "required_canon": [], "possible_outcomes": ["The character gains a usable clue.", "The obstacle raises the cost of the current goal."], "new_entity_requests": [], "risk_flags": [], "director_reasoning_summary": f"Selected {'the highest-weight active thread' if thread else 'the most narratively relevant active character'} and grounded the Scene in the character's current goal."}
