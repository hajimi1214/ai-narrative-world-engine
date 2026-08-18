"""Shared, strict StateEffect contract for resolution production and delta derivation."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import StateDeltaDomain, StateDeltaOperation, StateDeltaTargetType


class StateEffectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effect_kind: Literal["STATE_CHANGE"] = "STATE_CHANGE"
    target_type: StateDeltaTargetType
    target_id: str
    domain: StateDeltaDomain
    operation: StateDeltaOperation
    path: str
    value: Any
    reason: str = Field(min_length=1, max_length=1000)
    evidence: dict[str, Any]

    @field_validator("evidence")
    @classmethod
    def require_structured_evidence(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("state effect evidence must be a non-empty object")
        return value
