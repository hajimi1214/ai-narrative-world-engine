from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelResult:
    content: str
    latency_ms: int
    request_id: str | None
    provider: str
    model: str


class ModelProvider(Protocol):
    def generate(self, messages: list[dict[str, str]], model: str) -> ModelResult: ...
