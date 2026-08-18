"""Sequential rehearsal artifacts; no canon or world-state execution authority."""
import json
from dataclasses import dataclass
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from .ai.errors import ModelProviderError, MODEL_OUTPUT_INVALID
from .ai.provider import ModelProvider, ModelResult
from .character_mind import ActorPerceptionSanitizer, CharacterContextBuilder, CharacterDecisionConstraintChecker, character_context_fingerprint
from .llm_actor import CHARACTER_SYSTEM_PROMPT, CharacterDecisionPayload, DecisionPayloadParseError, build_character_decision_contract
from .models import ActionVisibility, CanonFact, Character, CharacterDecision, CharacterDecisionStatus, CharacterKnowledge, PerformanceStatus, RevealConstraint, RevealStatus, SceneProposal, ScenePerformance, WorldResolution, ResolutionStatus


class WorldResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["INTERACT", "INSPECT", "MOVE", "USE_ABILITY", "USE_ITEM", "ENVIRONMENT", "OTHER"]
    description: str
    target_entity_id: str | None
    target_character_id: str | None


class PerformanceActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visibility: ActionVisibility
    observable_action: str | None
    spoken_content: str | None
    requires_world_resolution: bool
    world_resolution_request: WorldResolutionRequest | None
    disclosure_knowledge_ids: list[str]
    target_character_id: str | None = None


class CharacterPerformancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: CharacterDecisionPayload
    action: PerformanceActionPayload


def performance_contract() -> dict[str, Any]:
    schema = CharacterPerformancePayload.model_json_schema()
    return {"required_fields": schema["required"], "decision": build_character_decision_contract(), "action": {"required_fields": ["visibility", "observable_action", "spoken_content", "requires_world_resolution", "world_resolution_request", "disclosure_knowledge_ids", "target_character_id"], "visibility_values": [item.value for item in ActionVisibility], "fields": {"visibility": "enum string", "observable_action": "string | null", "spoken_content": "string | null", "requires_world_resolution": "boolean", "world_resolution_request": {"kind": ["INTERACT", "INSPECT", "MOVE", "USE_ABILITY", "USE_ITEM", "ENVIRONMENT", "OTHER"], "description": "string", "target_entity_id": "string | null", "target_character_id": "string | null"}, "disclosure_knowledge_ids": "string[]", "target_character_id": "string | null"}, "rules": ["world_resolution_request must be null when requires_world_resolution is false", "world_resolution_request must be present when requires_world_resolution is true"]}}


def _parse_performance(content: str) -> CharacterPerformancePayload:
    value = content.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[-1].strip().startswith("```"):
            value = "\n".join(lines[1:-1]).strip()
    from .llm_actor import _extract_single_json_object, _validation_diagnostics
    try:
        return CharacterPerformancePayload.model_validate(_extract_single_json_object(value))
    except DecisionPayloadParseError:
        raise
    except ValidationError as exc:
        error = ModelProviderError(MODEL_OUTPUT_INVALID)
        error.diagnostics = _validation_diagnostics(exc)
        raise error from exc


class HeuristicCharacterPerformer:
    def perform(self, context: dict[str, Any]) -> tuple[dict[str, Any], ModelResult | None]:
        from .character_mind import HeuristicCharacterActor
        decision = HeuristicCharacterActor().decide(context)
        decision.update({"target_character_id": None, "target_entity_id": None})
        affordance = next(iter(context.get("scene", {}).get("world_affordances", []) or []), None)
        if affordance:
            action = {"visibility": ActionVisibility.PUBLIC.value, "observable_action": affordance.get("description"), "spoken_content": None, "requires_world_resolution": True, "world_resolution_request": affordance, "disclosure_knowledge_ids": [], "target_character_id": None}
        else:
            action = {"visibility": ActionVisibility.PUBLIC.value, "observable_action": decision["chosen_action"], "spoken_content": None, "requires_world_resolution": False, "world_resolution_request": None, "disclosure_knowledge_ids": [], "target_character_id": None}
        return {"decision": decision, "action": action}, None


class LLMCharacterPerformer:
    def __init__(self, provider: ModelProvider, model: str): self.provider, self.model = provider, model
    def perform(self, actor_view: dict[str, Any]) -> tuple[dict[str, Any], ModelResult]:
        messages = [{"role": "system", "content": CHARACTER_SYSTEM_PROMPT + "\nYou control only yourself. Describe attempts, not world outcomes or another character's reaction. Separate private decision from observable action. If the environment, object, uncertain information, combat, travel, or ability resolution must answer, set requires_world_resolution=true and provide a request."}, {"role": "user", "content": json.dumps({"actor_view": actor_view, "output_contract": performance_contract(), "instruction": "Return exactly one JSON object conforming to the contract. Do not add or omit fields."}, ensure_ascii=True, sort_keys=True)}]
        first = self.provider.generate(messages, self.model)
        try: return _parse_performance(first.content).model_dump(mode="json"), first
        except ModelProviderError as error:
            diagnostics = getattr(error, "diagnostics", [{"path": "$", "type": "invalid_output", "message": "Output does not match the contract."}])
        repair = messages + [{"role": "assistant", "content": first.content}, {"role": "user", "content": json.dumps({"validation_errors": diagnostics, "instruction": "Repair into exactly one valid performance JSON object. Preserve meaning and do not add facts."}, ensure_ascii=True)}]
        second = self.provider.generate(repair, self.model)
        return _parse_performance(second.content).model_dump(mode="json"), second


class PerformanceCharacterContextBuilder:
    def build(self, session: Session, project_id: str, character_id: str, proposal: SceneProposal, performance_id: str, turns: list[Any]) -> dict[str, Any]:
        context = CharacterContextBuilder().build(session, project_id, character_id, proposal)
        performance = session.get(__import__("app.models", fromlist=["ScenePerformance"]).ScenePerformance, performance_id)
        active_ids = set((performance.active_participant_ids if performance else proposal.participants) or [])
        context["scene"]["other_participants"] = [item for item in context["scene"]["other_participants"] if item["id"] in active_ids]
        context["scene"]["active_participant_ids"] = sorted(active_ids)
        visible, own = [], []
        for turn in turns:
            recipients = turn.recipient_character_ids or []
            if character_id in recipients:
                if turn.observable_action or turn.spoken_content:
                    visible.append({"turn": turn.sequence, "event_type": "CHARACTER_ACTION", "source_character_id": turn.actor_character_id, "observable_action": turn.observable_action, "spoken_content": turn.spoken_content})
            if turn.actor_character_id == character_id:
                decision = session.get(CharacterDecision, turn.character_decision_id)
                own.append({"turn": turn.sequence, "decision_type": decision.decision_type.value if decision else None, "chosen_action": decision.chosen_action if decision else None, "target_character_id": decision.target_character_id if decision else None, "observable_action": turn.observable_action, "spoken_content": turn.spoken_content})
        context["scene"]["performance_observations"] = visible[-12:]
        world_observations = []
        resolutions = session.execute(select(WorldResolution, __import__("app.models", fromlist=["ScenePerformanceTurn"]).ScenePerformanceTurn.sequence).join(__import__("app.models", fromlist=["ScenePerformanceTurn"]).ScenePerformanceTurn, WorldResolution.performance_turn_id == __import__("app.models", fromlist=["ScenePerformanceTurn"]).ScenePerformanceTurn.id).where(WorldResolution.performance_id == performance_id, WorldResolution.status == ResolutionStatus.VALID).order_by(__import__("app.models", fromlist=["ScenePerformanceTurn"]).ScenePerformanceTurn.sequence, WorldResolution.id)).all()
        for resolution, _ in resolutions:
            if character_id not in (resolution.recipient_character_ids or []):
                continue
            source_turn = session.get(__import__("app.models", fromlist=["ScenePerformanceTurn"]).ScenePerformanceTurn, resolution.performance_turn_id)
            if source_turn and source_turn.actor_character_id == character_id and resolution.actor_observation:
                world_observations.append({"source": "WORLD", "resolution_id": resolution.id, "source_turn": source_turn.sequence, "observation": resolution.actor_observation})
            if resolution.public_observation:
                world_observations.append({"source": "WORLD", "resolution_id": resolution.id, "source_turn": source_turn.sequence if source_turn else None, "observation": resolution.public_observation})
        context["scene"]["world_observations"] = world_observations[-12:]
        context["scene"]["self_turn_history"] = own[-12:]
        context["fingerprint"] = character_context_fingerprint({key: value for key, value in context.items() if key not in {"fingerprint", "version"}})
        context["version"] = context["fingerprint"]
        return context


class PerformanceObservationRouter:
    def recipients(self, visibility: ActionVisibility, participants: list[str], actor_id: str, target_id: str | None) -> list[str]:
        if visibility == ActionVisibility.PUBLIC: return [item for item in participants if item != actor_id]
        if visibility == ActionVisibility.TARGETED and target_id and target_id in participants and target_id != actor_id: return [target_id]
        return []


@dataclass
class PerformanceIssue:
    code: str; severity: str; message: str; related_entity_ids: list[str]; suggested_fix: str

@dataclass
class PerformanceActionReport:
    issues: list[PerformanceIssue]
    @property
    def valid(self): return not any(item.severity == "BLOCKING" for item in self.issues)
    def as_dict(self): return {"valid": self.valid, "issues": [item.__dict__ for item in self.issues]}


class PerformanceActionConstraintChecker:
    def validate(self, session: Session, context: dict[str, Any], proposal: SceneProposal, decision: CharacterDecision, action: PerformanceActionPayload, active_participant_ids: list[str] | None = None, world_view=None) -> PerformanceActionReport:
        issues: list[PerformanceIssue] = []
        def add(code, message, ids=None): issues.append(PerformanceIssue(code, "BLOCKING", message, ids or [], "Correct the performance action and try the turn again."))
        participants = set(active_participant_ids or ({item["id"] for item in context["scene"]["other_participants"]} | {context["character"]["id"]}))
        if action.target_character_id != decision.target_character_id: add("TARGET_MISMATCH", "Action and CharacterDecision target_character_id must match.", [item for item in (action.target_character_id, decision.target_character_id) if item])
        if action.visibility == ActionVisibility.TARGETED and (not action.target_character_id or action.target_character_id not in participants or action.target_character_id == decision.character_id): add("INVALID_TARGET", "TARGETED action must name another active Scene participant.")
        if action.visibility in {ActionVisibility.COVERT, ActionVisibility.PRIVATE} and action.target_character_id: add("INVALID_TARGET", "COVERT and PRIVATE actions cannot route to a recipient.", [action.target_character_id])
        own_knowledge = {item["id"] for status in context["knowledge"].values() for item in status}
        foreign = [item for item in action.disclosure_knowledge_ids if item not in own_knowledge]
        if foreign: add("KNOWLEDGE_LEAK", "Disclosure references knowledge unavailable to this character.", foreign)
        if action.requires_world_resolution and action.world_resolution_request is None: add("INVALID_WORLD_REQUEST", "World resolution requires a structured request.")
        if not action.requires_world_resolution and action.world_resolution_request is not None: add("INVALID_WORLD_REQUEST", "A non-resolving action cannot include a world request.")
        if action.world_resolution_request:
            request = action.world_resolution_request
            if request.target_character_id and (request.target_character_id not in participants or request.target_character_id == decision.character_id): add("INVALID_TARGET", "World request targets an unavailable active participant.", [request.target_character_id])
            if request.target_entity_id:
                target = world_view.entity(request.target_entity_id) if world_view else session.get(__import__("app.models", fromlist=["WorldEntity"]).WorldEntity, request.target_entity_id)
                visible_ids = set()
                visible_ids.add(context["scene"].get("location", {}).get("id") if context["scene"].get("location") else None)
                for item in context["inventory"]:
                    if isinstance(item, dict) and isinstance(item.get("id"), str): visible_ids.add(item["id"])
                def collect(value):
                    if isinstance(value, dict):
                        for key in ("entity_id", "location_id"):
                            if isinstance(value.get(key), str): visible_ids.add(value[key])
                        if isinstance(value.get("entity_ids"), list): visible_ids.update(item for item in value["entity_ids"] if isinstance(item, str))
                        for child in value.values(): collect(child)
                    elif isinstance(value, list):
                        for child in value: collect(child)
                collect(context["scene"].get("visible_context", {}))
                target_active = target.get("active", True) if isinstance(target, dict) else getattr(target, "active", True)
                if not target or (not world_view and target.project_id != decision.project_id) or target_active is False or request.target_entity_id not in visible_ids: add("INVALID_TARGET", "World request targets an unavailable or non-visible entity.", [request.target_entity_id])
        propositions = {item["proposition"] for status in context["knowledge"].values() for item in status if item["id"] in action.disclosure_knowledge_ids}
        forbidden = set(proposal.forbidden_reveals or [])
        canon = session.scalars(select(CanonFact).where(CanonFact.project_id == decision.project_id)).all()
        locked = session.scalars(select(RevealConstraint).where(RevealConstraint.project_id == decision.project_id, RevealConstraint.status == RevealStatus.LOCKED)).all()
        for fact in canon:
            if fact.proposition in propositions and (fact.id in forbidden or fact.proposition in forbidden): add("PREMATURE_REVEAL", "Action discloses a forbidden Canon fact.", [fact.id])
        if any(item.canon_fact_id in {fact.id for fact in canon if fact.proposition in propositions} for item in locked): add("PREMATURE_REVEAL", "Action discloses a locked Canon fact.")
        return PerformanceActionReport(issues)


class TurnScheduler:
    def next_actor(self, performance: Any, turns: list[Any], target_character_id: str | None = None) -> str | None:
        active = performance.active_participant_ids or []
        if not active: return None
        if target_character_id in active and target_character_id != (turns[-1].actor_character_id if turns else None): return target_character_id
        if not turns: return active[0]
        last = turns[-1].actor_character_id
        index = active.index(last) if last in active else -1
        return active[(index + 1) % len(active)]


def is_quiescent_cycle(performance: Any, turns: list[Any], session: Session) -> bool:
    active = list(performance.active_participant_ids or [])
    if not active or len(turns) < len(active): return False
    recent = turns[-len(active):]
    if {turn.actor_character_id for turn in recent} != set(active): return False
    for turn in recent:
        decision = session.get(CharacterDecision, turn.character_decision_id)
        if not decision or getattr(decision.decision_type, "value", decision.decision_type) not in {"WAIT", "OBSERVE", "REFUSE"}: return False
        if (turn.observable_action or "").strip() or (turn.spoken_content or "").strip() or turn.requires_world_resolution: return False
    return True


class PerformancePostTurnStateResolver:
    """Apply the shared post-turn state machine for normal and recovery turns."""
    def apply(self, performance: Any, turns: list[Any], turn: Any, decision: Any, action: Any, session: Session) -> None:
        performance.status = PerformanceStatus.RUNNING
        performance.stop_reason = None
        if action.requires_world_resolution:
            performance.status = PerformanceStatus.AWAITING_WORLD
            return
        if getattr(decision.decision_type, "value", decision.decision_type) == "WITHDRAW":
            performance.active_participant_ids = [item for item in (performance.active_participant_ids or []) if item != turn.actor_character_id]
            if len(performance.active_participant_ids) < 2:
                performance.status = PerformanceStatus.PAUSED
                performance.stop_reason = "INSUFFICIENT_ACTIVE_PARTICIPANTS"
                return
        if performance.status == PerformanceStatus.RUNNING and is_quiescent_cycle(performance, turns, session):
            performance.status = PerformanceStatus.PAUSED
            performance.stop_reason = "QUIESCENT"
            return
        if performance.status == PerformanceStatus.RUNNING and performance.turn_count >= performance.max_turns:
            performance.status = PerformanceStatus.PAUSED
            performance.stop_reason = "TURN_LIMIT"
