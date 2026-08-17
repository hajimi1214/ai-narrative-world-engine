import time
import httpx
from .errors import ModelProviderError, MODEL_AUTH_FAILED, MODEL_RATE_LIMITED, MODEL_TIMEOUT, MODEL_UPSTREAM_ERROR
from .provider import ModelResult


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def generate(self, messages: list[dict[str, str]], model: str) -> ModelResult:
        started = time.perf_counter()
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "stream": False},
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelProviderError(MODEL_TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code in (401, 403):
            raise ModelProviderError(MODEL_AUTH_FAILED)
        if response.status_code == 429:
            raise ModelProviderError(MODEL_RATE_LIMITED)
        if response.status_code >= 500:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR)
        if response.status_code >= 400:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR)
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR) from exc
        return ModelResult(content=str(content), latency_ms=latency_ms, request_id=response.headers.get("x-request-id"), provider=self.name, model=model)
