"""Shared, strict StateEffect contract for resolution production and delta derivation."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    evidence: dict[str, Any] = Field(default_factory=dict)
