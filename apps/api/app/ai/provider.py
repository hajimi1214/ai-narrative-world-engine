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


class ModelProvider(Protocol):
    def generate(self, messages: list[dict[str, str]], model: str) -> ModelResult: ...
