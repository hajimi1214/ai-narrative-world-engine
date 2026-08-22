import time
from collections import defaultdict, deque
from threading import Lock
import httpx
from .errors import ModelProviderError, MODEL_AUTH_FAILED, MODEL_RATE_LIMITED, MODEL_TIMEOUT, MODEL_UPSTREAM_ERROR
from .provider import ModelResult


class OpenAICompatibleProvider:
    name = "openai_compatible"
    _rate_windows: dict[str, deque[float]] = defaultdict(deque)
    _rate_lock = Lock()

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 120.0, max_retries: int = 0, rate_limit_per_minute: int = 0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, min(int(max_retries), 3))
        self.rate_limit_per_minute = max(0, int(rate_limit_per_minute))

    def _check_rate_limit(self, model: str) -> None:
        if not self.rate_limit_per_minute:
            return
        key = f"{self.base_url}|{model}"
        now = time.monotonic()
        with self._rate_lock:
            window = self._rate_windows[key]
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= self.rate_limit_per_minute:
                raise ModelProviderError(MODEL_RATE_LIMITED)
            window.append(now)

    def generate(self, messages: list[dict[str, str]], model: str) -> ModelResult:
        self._check_rate_limit(model)
        started = time.perf_counter()
        response = None
        for attempt in range(self.max_retries + 1):
            try:
                response = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "stream": False},
                    timeout=self.timeout_seconds,
                )
                if response.status_code not in {429, 500, 502, 503, 504} or attempt >= self.max_retries:
                    break
            except httpx.TimeoutException as exc:
                if attempt >= self.max_retries: raise ModelProviderError(MODEL_TIMEOUT) from exc
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries: raise ModelProviderError(MODEL_UPSTREAM_ERROR) from exc
        if response is None:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code in (401, 403):
            raise ModelProviderError(MODEL_AUTH_FAILED, upstream_status=response.status_code)
        if response.status_code == 429:
            raise ModelProviderError(MODEL_RATE_LIMITED, upstream_status=response.status_code)
        if response.status_code >= 500:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR, upstream_status=response.status_code)
        if response.status_code >= 400:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR, f"Upstream HTTP status {response.status_code}", response.status_code)
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR) from exc
        return ModelResult(content=str(content), latency_ms=latency_ms, request_id=response.headers.get("x-request-id"), provider=self.name, model=model)
