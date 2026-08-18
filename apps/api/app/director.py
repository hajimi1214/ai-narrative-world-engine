"""Deterministic Director protocol. This module deliberately has no LLM dependency."""
from dataclasses import dataclass
import hashlib
import json
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import AntiAIBible, CanonFact, CanonType, Character, CharacterKnowledge, EntityType, KnowledgeStatus, Project, ProposalStatus, ProposalType, RevealConstraint, RevealStatus, Scene, SceneProposal, SceneStatus, StoryArc, StoryThread, ThreadStatus, WorldEntity, WritingBible
from .character_mind import ActiveCharacterCognitionReader

RECENT_SCENE_LIMIT = 10

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
    payload = {key: value for key, value in context.items() if key not in {"version", "fingerprint"}}
    stable = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return f"director-context-v1:{hashlib.sha256(stable.encode()).hexdigest()}"

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
        recent_scenes = session.scalars(select(Scene).where(Scene.project_id == project_id, Scene.status == SceneStatus.OCCURRED).order_by(Scene.sequence.desc(), Scene.id.desc()).limit(RECENT_SCENE_LIMIT)).all()
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
        for item in knowledge: result.setdefault(item.character_id, {"KNOWN": [], "SUSPECTED": [], "FALSE_BELIEF": []})[item.status.value].append({"id": item.id, "proposition": item.proposition, "confidence": item.confidence})
        return result
    def _scene(self, scene): return {"id": scene.id, "sequence": scene.sequence, "location": scene.location, "participants": scene.participants, "intent": scene.intent, "summary": scene.summary, "story_threads": scene.story_threads, "result": scene.result}
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
        for fact_id in proposal.required_canon:
            if fact_id not in canon: self._add(issues, "INVALID_ENTITY_REFERENCE", "BLOCKING", "Required Canon is unavailable to Director Context.", [fact_id], "Reference a project Canon fact.")
        contradictions = proposal.entry_state.get("contradicts_canon_ids", [])
        locked = [fact_id for fact_id in contradictions if canon.get(fact_id, {}).get("locked") and canon[fact_id]["type"] == "CORE_CANON"]
        if locked: self._add(issues, "CANON_CONFLICT", "BLOCKING", "Proposal contradicts locked CORE_CANON.", locked, "Revise entry state to preserve locked facts.")
    def _knowledge(self, proposal, knowledge, issues):
        for character_id, motivation in proposal.character_motivations.items():
            required = motivation.get("required_knowledge", []) if isinstance(motivation, dict) else []
            available = {status: {item["proposition"] for item in knowledge.get(character_id, {}).get(status, [])} for status in ("KNOWN", "SUSPECTED", "FALSE_BELIEF")}
            missing = []
            for requirement in required:
                if isinstance(requirement, str): proposition, accepted = requirement, ["KNOWN"]
                else: proposition, accepted = requirement.get("proposition"), requirement.get("accepted_statuses", ["KNOWN"])
                if not proposition or not any(proposition in available.get(status, set()) for status in accepted): missing.append(proposition or "unspecified proposition")
            if missing: self._add(issues, "KNOWLEDGE_LEAK", "BLOCKING", "Character motivation relies on information this character has not acquired.", [character_id], "Use known clues, suspicion, or add a prior discovery Scene.")
    def _motivation_and_boundaries(self, proposal, characters, issues):
        for character_id in proposal.participants:
            if not proposal.character_motivations.get(character_id): self._add(issues, "CHARACTER_MOTIVATION_GAP", "WARNING", "A participant has no stated reason to join this Scene.", [character_id], "State the character's immediate motivation.")
            conflicts = proposal.entry_state.get("boundary_conflicts", {}).get(character_id, [])
            if conflicts: self._add(issues, "CHARACTER_BOUNDARY_CONFLICT", "ERROR", "Proposal may cross a declared character boundary.", [character_id], "Change pressure or justify a deliberate exception.")
    def _story_value(self, proposal, recent, issues):
        if not proposal.primary_thread_id and not proposal.expected_progress.get("character_arc") and not proposal.expected_progress.get("relationship") and not proposal.expected_progress.get("consequence"):
            self._add(issues, "THREADLESS_SCENE", "ERROR", "Scene does not advance a Story Thread, arc, relationship, or consequence.", [], "Add a concrete narrative progression.")
        goal = proposal.scene_goal.strip().lower()
        if goal and any(goal == (scene.get("intent") or "").strip().lower() for scene in recent): self._add(issues, "DUPLICATE_NARRATIVE_FUNCTION", "WARNING", "Recent Scene already used the same narrative intent.", [], "Change the pressure or advance the thread in a new way.")
    def _reveals(self, session, proposal, issues):
        if not proposal.allowed_reveals: return
        locks = session.scalars(select(RevealConstraint).where(RevealConstraint.project_id == proposal.project_id, RevealConstraint.canon_fact_id.in_(proposal.allowed_reveals), RevealConstraint.status == RevealStatus.LOCKED)).all()
        for lock in locks:
            fact = session.get(CanonFact, lock.canon_fact_id)
            invalid_allowed = [character_id for character_id in lock.allowed_character_ids if not session.get(Character, character_id) or session.get(Character, character_id).project_id != proposal.project_id]
            if not fact or fact.project_id != proposal.project_id or invalid_allowed: self._add(issues, "INVALID_ENTITY_REFERENCE", "BLOCKING", "Reveal Constraint crosses project boundaries.", [lock.id], "Repair the Reveal Constraint references.")
            unauthorized = [character_id for character_id in proposal.participants if character_id not in lock.allowed_character_ids]
            if unauthorized: self._add(issues, "PREMATURE_REVEAL", "BLOCKING", "Proposal reveals a locked Canon fact before its condition is met.", [lock.canon_fact_id] + unauthorized, "Remove this reveal or update its Reveal Constraint through the appropriate workflow.")
        forbidden = set(proposal.forbidden_reveals).intersection(proposal.allowed_reveals)
        if forbidden: self._add(issues, "PREMATURE_REVEAL", "BLOCKING", "Proposal both allows and forbids the same reveal.", list(forbidden), "Remove the forbidden reveal from allowed_reveals.")
    def _world_state(self, proposal, characters, issues):
        if proposal.proposal_type == ProposalType.TRANSITION or not proposal.location_id: return
        elsewhere = [character_id for character_id in proposal.participants if characters.get(character_id, {}).get("current_location") and characters[character_id]["current_location"] != proposal.location_id]
        if elsewhere: self._add(issues, "WORLD_STATE_CONFLICT", "ERROR", "Participants are not currently at the proposed location and no Transition is planned.", elsewhere, "Use a TRANSITION proposal or select their current location.")
    def _overcasting(self, proposal, issues):
        missing_reason = [str(item.get("name", "new entity")) for item in proposal.new_entity_requests if item.get("entity_type") == "CHARACTER" and not item.get("existing_character_gap")]
        if missing_reason: self._add(issues, "OVERCASTING", "WARNING", "New character requests do not explain why existing characters cannot serve the function.", missing_reason, "Provide existing_character_gap for each new character request.")

class HeuristicDirector:
    def propose(self, context: dict[str, Any]) -> dict[str, Any]:
        thread = context["active_story_threads"][0] if context["active_story_threads"] else None
        characters = sorted(context["active_characters"], key=lambda item: item["narrative_relevance"].get("score", 0), reverse=True)
        primary = characters[0] if characters else None
        location = primary["current_location"] if primary else None
        goal = (primary["current_goals"].get("current") if primary else None) or (thread["goal"] if thread else None) or "create a consequential choice"
        return {"proposal_type": ProposalType.CONTINUE_THREAD.value if thread else ProposalType.CHARACTER_DRIVEN.value, "primary_thread_id": thread["id"] if thread else None, "location_id": location, "proposed_location": None, "participants": [primary["id"]] if primary else [], "scene_goal": f"Advance {goal}", "character_motivations": {primary["id"]: {"reason": goal}} if primary else {}, "entry_state": {}, "planned_pressure": "A concrete obstacle complicates the character's current objective.", "expected_progress": {"thread": thread["id"]} if thread else {"character_arc": True}, "allowed_reveals": [], "forbidden_reveals": [], "required_canon": [], "possible_outcomes": ["The character gains a usable clue.", "The obstacle raises the cost of the current goal."], "new_entity_requests": [], "risk_flags": [], "director_reasoning_summary": f"Selected {'the highest-weight active thread' if thread else 'the most narratively relevant active character'} and grounded the Scene in the character's current goal."}
