"""LLM adapter for proposing a character decision, without database authority."""
import json
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .ai.errors import ModelProviderError, MODEL_OUTPUT_INVALID
from .ai.provider import ModelProvider, ModelResult
from .models import CharacterDecisionType


CHARACTER_SYSTEM_PROMPT = """You are a current-character decision simulator, not a novelist or director.
Use only the current character information in the input. Facts absent from the input are unknown.
KNOWN is confirmed information. SUSPECTED is only a suspicion. FALSE_BELIEF is genuinely believed by the character but may be wrong; do not correct it without evidence in the input.
Do not use author intent, director intent, future plot, or what would make a story interesting. Choose what this person would most plausibly do now.
The character may WAIT, WITHDRAW, REFUSE, HIDE, or OBSERVE when those are most plausible.
Do not write prose, environmental description, or literary action beats.
When referencing knowledge, memories, abilities, inventory, characters, or entities, use only IDs or propositions present in the input.
Return only one JSON object matching the requested decision fields. No Markdown and no explanation."""


class CharacterDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_type: CharacterDecisionType
    intent: str
    chosen_action: str
    motivation: str
    goal_refs: list[str] = Field(default_factory=list)
    knowledge_used: list[Any] = Field(default_factory=list)
    memory_refs: list[str] = Field(default_factory=list)
    ability_refs: list[str] = Field(default_factory=list)
    inventory_refs: list[str] = Field(default_factory=list)
    relationship_factors: dict[str, Any] = Field(default_factory=dict)
    perceived_risk: str | None = None
    accepted_cost: str | None = None
    expected_personal_result: str | None = None
    uncertainties: list[str] = Field(default_factory=list)
    refused_options: list[str] = Field(default_factory=list)
    boundary_override_reason: str | None = None
    decision_summary: str


def parse_decision_payload(content: str) -> CharacterDecisionPayload:
    value = content.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[-1].strip().startswith("```"):
            value = "\n".join(lines[1:-1]).strip()
    try:
        return CharacterDecisionPayload.model_validate(json.loads(value))
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise ModelProviderError(MODEL_OUTPUT_INVALID) from exc


class LLMCharacterActor:
    def __init__(self, provider: ModelProvider, model: str):
        self.provider = provider
        self.model = model

    def decide(self, actor_view: dict[str, Any]) -> tuple[dict[str, Any], ModelResult]:
        initial_messages = [
            {"role": "system", "content": CHARACTER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(actor_view, ensure_ascii=True, sort_keys=True)},
        ]
        first = self.provider.generate(initial_messages, self.model)
        try:
            payload = parse_decision_payload(first.content)
            return payload.model_dump(mode="json"), first
        except ModelProviderError as first_error:
            if first_error.code != MODEL_OUTPUT_INVALID:
                raise
        repair_messages = initial_messages + [
            {"role": "assistant", "content": first.content},
            {"role": "user", "content": "Return exactly one valid CharacterDecision JSON object. Preserve the original intended decision where possible. Do not add facts, knowledge, memories, items, abilities, characters, or entities that were not present in the actor input. Do not change epistemic status. Do not include Markdown."},
        ]
        second = self.provider.generate(repair_messages, self.model)
        payload = parse_decision_payload(second.content)
        return payload.model_dump(mode="json"), second
