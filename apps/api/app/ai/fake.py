from collections.abc import Iterable
from .errors import ModelProviderError
from .provider import ModelResult


class FakeModelProvider:
    name = "fake"

    def __init__(self, responses: str | Iterable[str] | None = None, error: ModelProviderError | None = None):
        self.responses = [responses] if isinstance(responses, str) else list(responses or [])
        self.error = error
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    def generate(self, messages: list[dict[str, str]], model: str) -> ModelResult:
        self.calls += 1
        self.messages.append(messages)
        if self.error:
            raise self.error
        content = self.responses[min(self.calls - 1, len(self.responses) - 1)] if self.responses else '{"decision_type":"WAIT","intent":"observe","chosen_action":"wait","motivation":"The character needs more information.","decision_summary":"Wait and observe."}'
        return ModelResult(content=content, latency_ms=0, request_id="fake-request", provider=self.name, model=model)
