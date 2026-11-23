from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ModelResult:
    content: str
    latency_ms: int
    request_id: str | None
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    attempts: int = 1


def reported_usage(usage: object) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    return {
        key: usage[key]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "tokens")
        if type(usage.get(key)) is int and usage[key] >= 0
    }


def classify_usage(usage: object, calls: int = 1) -> dict[str, int | str]:
    value = reported_usage(usage) if calls else {}
    prompt_present = "prompt_tokens" in value and value["prompt_tokens"] is not None
    completion_present = "completion_tokens" in value and value["completion_tokens"] is not None
    total_key = "total_tokens" if "total_tokens" in value else "tokens" if "tokens" in value else None
    total_present = total_key is not None and value[total_key] is not None
    prompt_tokens = int(value.get("prompt_tokens", 0) or 0)
    completion_tokens = int(value.get("completion_tokens", 0) or 0)
    complete = total_present or prompt_present and completion_present
    total_tokens = int(value[total_key]) if total_present else prompt_tokens + completion_tokens
    calls = max(0, int(calls))
    unknown_calls = max(0, calls - 1) if complete else calls
    status = "NOT_SENT" if calls == 0 else "UNKNOWN" if unknown_calls else "CONFIRMED"
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "unknown_usage_calls": unknown_calls,
        "usage_status": status,
    }


class ModelProvider(Protocol):
    def generate(self, messages: list[dict[str, str]], model: str) -> ModelResult: ...
