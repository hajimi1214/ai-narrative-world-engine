"""LLM adapter for proposing a character decision, without database authority."""
import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .ai.errors import ModelProviderError, MODEL_OUTPUT_INVALID
from .ai.provider import ModelProvider, ModelResult
from .models import CharacterDecisionType


CHARACTER_SYSTEM_PROMPT = """You are a current-character decision simulator, not a novelist or director.
Use only the current character information in the input. Facts absent from the input are unknown.
KNOWN is confirmed information. SUSPECTED is only a suspicion. FALSE_BELIEF is genuinely believed by the character but may be wrong; do not correct it without evidence in the input.
UNKNOWN contains opaque canon IDs only. Never cite, infer, or use an UNKNOWN item as knowledge.
Each character has one independent Agent. Follow only this Agent's profile and subjective context; never speak for another character or the director.
Do not use author intent, director intent, future plot, or what would make a story interesting. Choose what this person would most plausibly do now.
The character may WAIT, WITHDRAW, REFUSE, HIDE, or OBSERVE when those are most plausible.
Do not write prose, environmental description, or literary action beats.
When referencing knowledge, memories, abilities, inventory, characters, or entities, use only IDs or propositions present in the input.
Return only one JSON object matching the requested decision fields. No Markdown and no explanation."""


class KnowledgeReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledge_id: str
    proposition: str
    accepted_statuses: list[Literal["KNOWN", "SUSPECTED", "FALSE_BELIEF"]]


class CharacterDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_type: CharacterDecisionType
    intent: str
    chosen_action: str
    motivation: str
    target_character_id: str | None
    target_entity_id: str | None
    goal_refs: list[str]
    knowledge_used: list[KnowledgeReference]
    memory_refs: list[str]
    ability_refs: list[str]
    inventory_refs: list[str]
    relationship_factors: dict[str, Any]
    perceived_risk: str | None
    accepted_cost: str | None
    expected_personal_result: str | None
    uncertainties: list[str]
    refused_options: list[str]
    boundary_override_reason: str | None
    decision_summary: str


def build_character_decision_contract() -> dict[str, Any]:
    schema = CharacterDecisionPayload.model_json_schema()
    return {
        "required_fields": schema["required"],
        "all_fields_required": True,
        "empty_value_rules": {"lists": [], "relationship_factors": {}, "nullable_text": None},
        "server_fields_forbidden": ["project_id", "scene_proposal_id", "character_id", "context_fingerprint", "status", "created_at"],
        "fields": {
            "decision_type": {"type": "enum string", "allowed_values": [item.value for item in CharacterDecisionType]},
            "intent": {"type": "string"}, "chosen_action": {"type": "string"}, "motivation": {"type": "string"}, "target_character_id": {"type": "string | null"}, "target_entity_id": {"type": "string | null"},
            "goal_refs": {"type": "string[]"},
            "knowledge_used": {"type": "object[]", "item": {"knowledge_id": "string", "proposition": "string", "accepted_statuses": ["KNOWN | SUSPECTED | FALSE_BELIEF"]}},
            "memory_refs": {"type": "string[]"}, "ability_refs": {"type": "string[]"}, "inventory_refs": {"type": "string[]"},
            "relationship_factors": {"type": "object"}, "perceived_risk": {"type": "string | null"}, "accepted_cost": {"type": "string | null"},
            "expected_personal_result": {"type": "string | null"}, "uncertainties": {"type": "string[]"}, "refused_options": {"type": "string[]"},
            "boundary_override_reason": {"type": "string | null"}, "decision_summary": {"type": "string"},
        },
    }


class DecisionPayloadParseError(ModelProviderError):
    def __init__(self, diagnostics: list[dict[str, str]]):
        super().__init__(MODEL_OUTPUT_INVALID)
        self.diagnostics = diagnostics


def _validation_diagnostics(error: ValidationError) -> list[dict[str, str]]:
    return [{"path": ".".join(str(part) for part in item["loc"]) or "$", "type": item["type"], "message": item["msg"]} for item in error.errors()]


def _extract_single_json_object(value: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    skip_until = 0
    for index, character in enumerate(value):
        if index < skip_until or character != "{":
            continue
        try:
            candidate, end = decoder.raw_decode(value, index)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            objects.append(candidate)
            skip_until = end
    if len(objects) == 1:
        return objects[0]
    if len(objects) > 1:
        raise DecisionPayloadParseError([{"path": "$", "type": "multiple_json_objects", "message": "Output must contain exactly one JSON object."}])
    raise DecisionPayloadParseError([{"path": "$", "type": "invalid_json", "message": "Output must contain one valid JSON object."}])


def parse_decision_payload(content: str) -> CharacterDecisionPayload:
    value = content.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[-1].strip().startswith("```"):
            value = "\n".join(lines[1:-1]).strip()
    try:
        return CharacterDecisionPayload.model_validate(_extract_single_json_object(value))
    except ValidationError as exc:
        raise DecisionPayloadParseError(_validation_diagnostics(exc)) from exc
    except TypeError as exc:
        raise DecisionPayloadParseError([{"path": "$", "type": "invalid_type", "message": "Output must be a JSON object."}]) from exc


class LLMCharacterActor:
    def __init__(self, provider: ModelProvider, model: str):
        self.provider = provider
        self.model = model

    def decide(self, actor_view: dict[str, Any]) -> tuple[dict[str, Any], ModelResult]:
        request_payload = {
            "actor_view": actor_view,
            "output_contract": build_character_decision_contract(),
            "instruction": "Return exactly one JSON object conforming to output_contract. The output contract is formatting metadata, not character knowledge. Use [] for no references, {} for no relationship factors, and null for absent optional text. Never invent IDs. Do not add fields or omit required fields.",
        }
        initial_messages = [
            {"role": "system", "content": CHARACTER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(request_payload, ensure_ascii=True, sort_keys=True)},
        ]
        first = self.provider.generate(initial_messages, self.model)
        try:
            payload = parse_decision_payload(first.content)
            return payload.model_dump(mode="json"), first
        except DecisionPayloadParseError as first_error:
            diagnostics = first_error.diagnostics
        repair_messages = initial_messages + [
            {"role": "assistant", "content": first.content},
            {"role": "user", "content": json.dumps({"validation_errors": diagnostics, "instruction": "Return exactly one valid CharacterDecision JSON object. Preserve the original intended decision where possible. Do not add facts, knowledge, memories, items, abilities, characters, or entities that were not present in the actor input. Do not change epistemic status. Do not include Markdown."}, ensure_ascii=True, sort_keys=True)},
        ]
        second = self.provider.generate(repair_messages, self.model)
        payload = parse_decision_payload(second.content)
        return payload.model_dump(mode="json"), second
