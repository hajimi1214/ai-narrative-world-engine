import time
import json
from collections import defaultdict, deque
from threading import Lock
import httpx
from .errors import ModelProviderError, MODEL_AUTH_FAILED, MODEL_RATE_LIMITED, MODEL_TIMEOUT, MODEL_UPSTREAM_ERROR
from .provider import ModelResult, reported_usage


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
                raise ModelProviderError(MODEL_RATE_LIMITED, accounted_calls=0)
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
                if attempt >= self.max_retries: raise ModelProviderError(MODEL_TIMEOUT, accounted_calls=attempt + 1) from exc
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries: raise ModelProviderError(MODEL_UPSTREAM_ERROR, accounted_calls=attempt + 1) from exc
        if response is None:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR, accounted_calls=self.max_retries + 1)
        latency_ms = int((time.perf_counter() - started) * 1000)
        retry_after_header = response.headers.get("retry-after")
        try:
            retry_after = max(0, int(float(retry_after_header))) if retry_after_header else None
        except (ValueError, OverflowError):
            retry_after = None
        if response.status_code in (401, 403):
            raise ModelProviderError(MODEL_AUTH_FAILED, upstream_status=response.status_code, accounted_calls=attempt + 1)
        if response.status_code == 429:
            raise ModelProviderError(MODEL_RATE_LIMITED, upstream_status=response.status_code, accounted_calls=attempt + 1, retry_after=retry_after)
        if response.status_code >= 500:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR, upstream_status=response.status_code, accounted_calls=attempt + 1)
        if response.status_code >= 400:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR, f"Upstream HTTP status {response.status_code}", response.status_code, accounted_calls=attempt + 1)
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR, accounted_calls=attempt + 1) from exc
        return ModelResult(content=str(content), latency_ms=latency_ms, request_id=response.headers.get("x-request-id"), provider=self.name, model=model, usage=reported_usage(body.get("usage")), attempts=attempt + 1)

    def generate_stream(self, messages: list[dict[str, str]], model: str, on_delta) -> ModelResult:
        """Consume provider SSE server-side; callers receive only a safe preview callback."""
        self._check_rate_limit(model)
        started = time.perf_counter(); chunks: list[str] = []; usage: dict[str, int] = {}; request_id = None
        try:
            with httpx.stream("POST", f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, json={"model": model, "messages": messages, "stream": True}, timeout=self.timeout_seconds) as response:
                request_id = response.headers.get("x-request-id")
                retry_after_header = response.headers.get("retry-after")
                try:
                    retry_after = max(0, int(float(retry_after_header))) if retry_after_header else None
                except (ValueError, OverflowError):
                    retry_after = None
                if response.status_code in (401, 403): raise ModelProviderError(MODEL_AUTH_FAILED, upstream_status=response.status_code, accounted_calls=1)
                if response.status_code == 429: raise ModelProviderError(MODEL_RATE_LIMITED, upstream_status=response.status_code, retry_after=retry_after)
                if response.status_code >= 500: raise ModelProviderError(MODEL_UPSTREAM_ERROR, upstream_status=response.status_code, accounted_calls=1)
                if response.status_code >= 400: raise ModelProviderError(MODEL_UPSTREAM_ERROR, f"Upstream HTTP status {response.status_code}", response.status_code, accounted_calls=1)
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"): continue
                    data = line[5:].strip()
                    if data == "[DONE]": break
                    try: body = json.loads(data)
                    except json.JSONDecodeError: continue
                    delta = (((body.get("choices") or [{}])[0].get("delta") or {}).get("content"))
                    if delta:
                        text = str(delta); chunks.append(text); on_delta(text)
                    if isinstance(body.get("usage"), dict): usage = body["usage"]
        except httpx.TimeoutException as exc:
            raise ModelProviderError(MODEL_TIMEOUT, accounted_calls=1) from exc
        except ModelProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR, accounted_calls=1) from exc
        content = "".join(chunks)
        if not content: raise ModelProviderError(MODEL_UPSTREAM_ERROR, "Stream completed without content", accounted_calls=1)
        return ModelResult(content=content, latency_ms=int((time.perf_counter() - started) * 1000), request_id=request_id, provider=self.name, model=model, usage=reported_usage(usage))
