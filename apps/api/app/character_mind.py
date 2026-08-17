"""Deterministic, epistemically constrained character decision protocol."""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Character, CharacterDecision, CharacterDecisionStatus, CharacterDecisionType, CharacterKnowledge, CharacterMemory, KnowledgeStatus, ProposalType, SceneProposal, WorldEntity

MAX_CHARACTER_MEMORIES = 12

def character_context_fingerprint(context: dict[str, Any]) -> str:
    payload = {key: value for key, value in context.items() if key not in {"fingerprint", "version"}}
    stable = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return f"character-context-v1:{hashlib.sha256(stable.encode()).hexdigest()}"

def _value_id(value: Any) -> str | None:
    if isinstance(value, str): return value
    if isinstance(value, dict): return value.get("id") or value.get("name")
    return None

class CharacterContextBuilder:
    def build(self, session: Session, project_id: str, character_id: str, proposal: SceneProposal) -> dict[str, Any]:
        character = session.get(Character, character_id)
        if not character or character.project_id != project_id:
            raise ValueError("Character not found in project")
        if proposal.project_id != project_id:
            raise ValueError("Scene Proposal not found in project")
        participants = session.scalars(select(Character).where(Character.project_id == project_id, Character.id.in_(proposal.participants)).order_by(Character.id)).all() if proposal.participants else []
        other_participants = [{"id": item.id, "name": item.name} for item in participants if item.id != character.id]
        location = session.get(WorldEntity, proposal.location_id) if proposal.location_id else None
        knowledge = session.scalars(select(CharacterKnowledge).where(CharacterKnowledge.character_id == character.id, CharacterKnowledge.status.in_([KnowledgeStatus.KNOWN, KnowledgeStatus.SUSPECTED, KnowledgeStatus.FALSE_BELIEF])).order_by(CharacterKnowledge.status, CharacterKnowledge.id)).all()
        memories = self._memories(session, character, proposal, other_participants)
        relationships = {item["id"]: character.relationships.get(item["id"], {}) for item in other_participants}
        context = {
            "character": {"id": character.id, "name": character.name, "personality": character.personality, "core_values": character.core_values, "boundaries": character.boundaries, "goals": character.goals, "current_state": character.current_state, "physical_state": character.physical_state, "emotional_state": character.emotional_state},
            "scene": {"proposal_id": proposal.id, "location": {"id": location.id, "name": location.name} if location else None, "other_participants": other_participants, "visible_context": proposal.entry_state.get("visible_context", {}), "actor_visible_context": proposal.entry_state.get("actor_visible_context", {}).get(character.id, {})},
            "knowledge": self._knowledge(knowledge),
            "memories": memories,
            "relationships": relationships,
            "abilities": self._abilities(character.abilities),
            "inventory": character.inventory,
        }
        context["fingerprint"] = character_context_fingerprint(context)
        context["version"] = context["fingerprint"]
        return context

    def _knowledge(self, records):
        values = {"KNOWN": [], "SUSPECTED": [], "FALSE_BELIEF": []}
        for record in records:
            status = getattr(record.status, "value", record.status)
            values[status].append({"id": record.id, "proposition": record.proposition, "confidence": record.confidence})
        return values

    def _memories(self, session, character, proposal, participants):
        records = session.scalars(select(CharacterMemory).where(CharacterMemory.character_id == character.id).order_by(CharacterMemory.id)).all()
        related = {proposal.location_id, proposal.primary_thread_id, *[item["id"] for item in participants]}
        def rank(memory):
            distortion = memory.distortion or {}
            links = set(distortion.get("entity_ids", [])) | set(distortion.get("participant_ids", [])) | set(distortion.get("thread_ids", [])) | ({distortion["location_id"]} if isinstance(distortion.get("location_id"), str) else set())
            happened = memory.happened_at.timestamp() if memory.happened_at else 0
            return (bool(links.intersection(related)), memory.importance, abs(memory.emotional_weight), happened, memory.id)
        return [{"memory_id": item.id, "content": item.content, "importance": item.importance, "emotional_weight": item.emotional_weight, "confidence": item.confidence, "distortion": item.distortion, "happened_at": item.happened_at.isoformat() if item.happened_at else None} for item in sorted(records, key=rank, reverse=True)[:MAX_CHARACTER_MEMORIES]]

    def _abilities(self, abilities):
        result = []
        for ability in abilities:
            if isinstance(ability, dict): result.append({key: value for key, value in ability.items() if key != "director_only"})
            else: result.append(ability)
        return result


class ActorPerceptionSanitizer:
    """White-list the only data an external character model may receive."""
    def sanitize(self, context: dict[str, Any]) -> dict[str, Any]:
        character = context["character"]
        scene = context["scene"]
        return {
            "character": {key: self._visible(character.get(key)) for key in ("name", "personality", "core_values", "boundaries", "goals", "current_state", "physical_state", "emotional_state")},
            "scene": {
                "location": self._visible(scene.get("location")),
                "other_participants": self._visible(scene.get("other_participants", [])),
                "visible_context": self._visible(scene.get("visible_context", {})),
                "actor_visible_context": self._visible(scene.get("actor_visible_context", {})),
                "performance_observations": self._visible(scene.get("performance_observations", [])),
                "self_turn_history": self._visible(scene.get("self_turn_history", [])),
                "active_participant_ids": self._visible(scene.get("active_participant_ids", [])),
            },
            "knowledge": self._visible(context.get("knowledge", {})),
            "memories": self._visible(context.get("memories", [])),
            "relationships": self._visible(context.get("relationships", {})),
            "abilities": self._visible(context.get("abilities", [])),
            "inventory": self._visible(context.get("inventory", [])),
        }

    def _visible(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._visible(item) for key, item in value.items() if key != "director_only"}
        if isinstance(value, list):
            return [self._visible(item) for item in value]
        return value

@dataclass
class CharacterDecisionIssue:
    code: str
    severity: str
    message: str
    related_entity_ids: list[str]
    suggested_fix: str

@dataclass
class CharacterDecisionValidationReport:
    issues: list[CharacterDecisionIssue]
    @property
    def valid(self): return not any(item.severity == "BLOCKING" for item in self.issues)
    def as_dict(self): return {"valid": self.valid, "issues": [item.__dict__ for item in self.issues]}

class CharacterDecisionConstraintChecker:
    def validate(self, session: Session, context: dict[str, Any], decision: CharacterDecision) -> CharacterDecisionValidationReport:
        issues: list[CharacterDecisionIssue] = []
        add = lambda code, severity, message, ids, fix: issues.append(CharacterDecisionIssue(code, severity, message, ids, fix))
        character = context["character"]
        scene = context["scene"]
        if decision.context_fingerprint != context["fingerprint"]: add("CHARACTER_CONTEXT_STALE", "BLOCKING", "Character perspective changed after this decision was generated.", [decision.character_id], "Run the character simulation again.")
        self._knowledge(context, decision, add)
        memory_ids = {item["memory_id"] for item in context["memories"]}
        foreign_memories = [item for item in decision.memory_refs if item not in memory_ids]
        if foreign_memories: add("FOREIGN_MEMORY", "BLOCKING", "Decision references memories unavailable to this character.", foreign_memories, "Use only memory IDs from Actor Context.")
        inventory_ids = {_value_id(item) for item in context["inventory"]}
        missing_inventory = [item for item in decision.inventory_refs if item not in inventory_ids]
        if missing_inventory: add("INVENTORY_MISSING", "BLOCKING", "Decision uses an item the character does not hold.", missing_inventory, "Remove the item or acquire it in a later Scene.")
        self._abilities(context, decision, add)
        if decision.boundary_override_reason is None and decision.relationship_factors.get("boundary_conflict"):
            add("CHARACTER_BOUNDARY_CONFLICT", "ERROR", "Decision conflicts with a declared hard boundary.", [decision.character_id], "Record a boundary override reason or choose a different action.")
        goals = {str(value) for value in character["goals"].values() if isinstance(value, str)}
        if decision.goal_refs and not set(decision.goal_refs).intersection(goals): add("GOAL_DISCONNECT", "WARNING", "Decision does not reference a current character goal.", [decision.character_id], "Connect the action to an existing goal or record an explicit new priority.")
        motivation = decision.motivation.lower()
        if any(term in motivation for term in ("director need", "plot need", "chapter goal", "outline requirement", "剧情需要", "大纲要求", "导演需要")): add("DIRECTOR_PUPPETING", "BLOCKING", "Decision motivation is authorial rather than character-driven.", [decision.character_id], "Ground motivation in the character's goals, knowledge, or relationships.")
        if scene["location"] and character["current_state"].get("location_id") and character["current_state"]["location_id"] != scene["location"]["id"] and decision.decision_type != CharacterDecisionType.WAIT:
            add("IMPOSSIBLE_LOCATION", "BLOCKING", "Character is not at the proposal location.", [decision.character_id], "Use WAIT or a future Transition before acting at that location.")
        self._targets(session, context, decision, add)
        return CharacterDecisionValidationReport(issues)

    def _knowledge(self, context, decision, add):
        available = {status: {item["proposition"] for item in values} for status, values in context["knowledge"].items()}
        for reference in decision.knowledge_used:
            if isinstance(reference, str): proposition, statuses = reference, ["KNOWN"]
            else: proposition, statuses = reference.get("proposition"), reference.get("accepted_statuses", ["KNOWN"])
            if not proposition or not any(proposition in available.get(status, set()) for status in statuses): add("KNOWLEDGE_LEAK", "BLOCKING", "Decision uses knowledge outside the character's allowed epistemic state.", [decision.character_id], "Use only the character's own matching knowledge state.")
    def _abilities(self, context, decision, add):
        available = {}
        for ability in context["abilities"]:
            key = _value_id(ability)
            if key: available[key] = ability
        for reference in decision.ability_refs:
            ability = available.get(reference)
            if not ability: add("ABILITY_UNKNOWN", "BLOCKING", "Character cannot intentionally use an unknown ability.", [reference], "Use a visible ability from Actor Context.")
            elif isinstance(ability, dict) and ability.get("status", "AVAILABLE") != "AVAILABLE": add("ABILITY_UNAVAILABLE", "BLOCKING", "Ability is currently unavailable to the character.", [reference], "Choose an available ability or wait for recovery.")
    def _targets(self, session, context, decision, add):
        if decision.target_character_id:
            participant_ids = {item["id"] for item in context["scene"]["other_participants"]} | {context["character"]["id"]}
            target = session.get(Character, decision.target_character_id)
            if not target or target.project_id != decision.project_id or decision.target_character_id not in participant_ids: add("INVALID_TARGET", "BLOCKING", "Target character is unavailable in this Scene.", [decision.target_character_id], "Target an existing Scene participant.")
        if decision.target_entity_id:
            target = session.get(WorldEntity, decision.target_entity_id)
            scene_location = context["scene"]["location"] or {}
            if not target or target.project_id != decision.project_id or decision.target_entity_id != scene_location.get("id"): add("INVALID_TARGET", "BLOCKING", "Target entity is unavailable in this Scene.", [decision.target_entity_id], "Target the current Scene location or a future exposed entity.")

class HeuristicCharacterActor:
    def decide(self, context: dict[str, Any]) -> dict[str, Any]:
        character = context["character"]
        goal = character["goals"].get("current") or next((value for value in character["goals"].values() if isinstance(value, str)), "assess the situation")
        known = context["knowledge"]["KNOWN"]
        inventory = context["inventory"]
        knowledge_used = [{"proposition": known[0]["proposition"], "accepted_statuses": ["KNOWN"]}] if known else []
        inventory_refs = [_value_id(inventory[0])] if inventory and _value_id(inventory[0]) else []
        return {"decision_type": CharacterDecisionType.INVESTIGATE.value, "intent": str(goal), "chosen_action": f"Inspect the available evidence related to {goal} before escalating.", "motivation": f"The character's current goal is {goal} and the visible pressure warrants verification.", "goal_refs": [goal], "knowledge_used": knowledge_used, "memory_refs": [item["memory_id"] for item in context["memories"][:1]], "ability_refs": [], "inventory_refs": inventory_refs, "relationship_factors": {}, "perceived_risk": "The visible pressure may make direct action costly.", "accepted_cost": "Time and attention.", "expected_personal_result": "A more informed next choice.", "uncertainties": ["The visible pressure may conceal unknown constraints."], "refused_options": [], "decision_summary": f"The character investigates because {goal} is the strongest current goal."}
