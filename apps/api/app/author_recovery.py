"""Persistent recovery policy for the local writing worker."""
from __future__ import annotations

from datetime import datetime, timezone

from .ai.errors import (
    MODEL_AUTH_FAILED, MODEL_PROVIDER_NOT_CONFIGURED, MODEL_PROVIDER_UNSUPPORTED,
    MODEL_RATE_LIMITED, MODEL_TIMEOUT, MODEL_UPSTREAM_ERROR, ModelProviderError,
)
from .models import AutoDirectorRun, AutoDirectorRunStatus


class LostRunClaim(RuntimeError):
    """Another worker or the author now owns the run."""


def guarded_provider(provider, guard):
    if not guard:
        return provider

    class GuardedProvider:
        name = getattr(provider, "name", None)

        def generate(self, messages, model):
            try:
                guard()
            except ModelProviderError as exc:
                exc.accounted_calls = 0
                raise
            return provider.generate(messages, model)

        def generate_stream(self, messages, model, on_delta):
            try:
                guard()
            except ModelProviderError as exc:
                exc.accounted_calls = 0
                raise
            return provider.generate_stream(messages, model, on_delta)

    return GuardedProvider()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def provider_failure(exc: BaseException) -> ModelProviderError | None:
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, ModelProviderError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def is_transient(code: str | None, status: int | None = None) -> bool:
    if code in {MODEL_RATE_LIMITED, MODEL_TIMEOUT}:
        return True
    return code == MODEL_UPSTREAM_ERROR and (status is None or status in {500, 502, 503, 504})


def recoverable_provider_failure(exc: BaseException) -> ModelProviderError | None:
    failure = provider_failure(exc)
    if failure and is_transient(failure.code, failure.upstream_status):
        return failure
    return None


def retry_limit(run: AutoDirectorRun) -> int:
    configured = (run.settings or {}).get("max_retries", 2)
    return max(0, min(5, int(configured if configured is not None else 2)))


def configured_token_budget(run: AutoDirectorRun) -> int:
    settings = run.settings or {}
    request = (run.context or {}).get("request") or {}
    return int(settings.get("operational_token_budget") or request.get("operational_token_budget") or settings.get("max_tokens") or 0)


def unknown_call_reserve(run_or_settings, context: dict | None = None) -> int:
    if context is None:
        settings = run_or_settings.settings or {}
        context = run_or_settings.context or {}
    else:
        settings = run_or_settings or {}
        context = context or {}
    request = context.get("request") or {}
    target_words = int(settings.get("target_words_per_chapter") or request.get("target_words_per_chapter") or 0)
    return int(settings.get("recovery_unknown_call_reserve_tokens") or max(1024, min(8192, target_words * 2)))


def token_usage(run: AutoDirectorRun) -> int:
    return max(int(run.total_tokens or 0), int((run.token_usage or {}).get("total_tokens") or 0))


def budget_usage(run: AutoDirectorRun) -> int:
    return token_usage(run) + int((run.context or {}).get("recovery_reserved_tokens") or 0)


def release_claim(run: AutoDirectorRun) -> None:
    run.worker_claim_token = None
    run.worker_lease_until = None


def revoke_claim(run: AutoDirectorRun) -> None:
    release_claim(run)
    run.next_retry_at = None


def clear_retry(run: AutoDirectorRun) -> None:
    run.next_retry_at = None
    run.recovery_attempts = 0
    run.recovery_key = None
    run.recovery_error_code = None


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def recovery_payload(run: AutoDirectorRun) -> dict:
    code = run.recovery_error_code or run.pause_reason
    if run.status == AutoDirectorRunStatus.COMPLETED:
        state = "COMPLETED"
    elif run.status in {AutoDirectorRunStatus.FAILED, AutoDirectorRunStatus.BLOCKED}:
        state = "NEEDS_AUTHOR"
    elif run.status == AutoDirectorRunStatus.PAUSED:
        intentional_pause = bool((run.context or {}).get("stop_requested"))
        state = "PAUSED" if intentional_pause or code not in {
            MODEL_AUTH_FAILED, MODEL_PROVIDER_NOT_CONFIGURED, MODEL_PROVIDER_UNSUPPORTED,
            "QUALITY_REVIEW_REQUIRED", "AUTO_RETRY_EXHAUSTED",
        } else "NEEDS_AUTHOR"
    elif run.next_retry_at:
        state = "WAITING_RETRY"
    elif run.worker_claim_token and run.worker_lease_until and run.worker_lease_until > utc_now():
        state = "RUNNING"
    else:
        state = "QUEUED"
    return {
        "state": state,
        "next_retry_at": _iso(run.next_retry_at) if state == "WAITING_RETRY" else None,
        "attempts": int(run.recovery_attempts or 0),
        "max_attempts": retry_limit(run),
        "last_checkpoint": {"stage": run.checkpoint_stage, "saved_at": _iso(run.checkpoint_saved_at)}
        if run.checkpoint_stage and run.checkpoint_saved_at else None,
        "error_code": code,
        "usage_uncertain": bool(run.recovery_usage_uncertain),
        "reserved_tokens": int((run.context or {}).get("recovery_reserved_tokens") or 0),
    }
