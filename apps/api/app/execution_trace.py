import hashlib
import json
from typing import Any

from .models import ExecutionStatus, ExecutionTrace


class RecoveryPolicy:
    RULES = {
        "MODEL_AUTH_FAILED": (False, False, ["ABORT", "CHECK_CREDENTIAL"]),
        "MODEL_RATE_LIMITED": (True, False, ["RETRY", "ABORT"]),
        "MODEL_TIMEOUT": (True, False, ["RETRY", "ABORT"]),
        "MODEL_UPSTREAM_ERROR": (True, False, ["RETRY", "ABORT"]),
        "MODEL_OUTPUT_INVALID": (False, True, ["AI_REPAIR", "MANUAL_EDIT", "ABORT"]),
        "CANON_CONTRADICTION": (False, True, ["AI_REPAIR", "MANUAL_EDIT", "ABORT"]),
        "OBSERVATION_LEAK": (False, True, ["AI_REPAIR", "MANUAL_EDIT", "ABORT"]),
        "WORLD_INFORMATION_MISSING": (False, False, ["EDIT_WORLD", "ABORT"]),
        "REVISION_STALE": (False, False, ["REPREVIEW", "ABORT"]),
        "TARGET_STATE_STALE": (False, False, ["REPREVIEW", "ABORT"]),
        "ROLLBACK_TARGET_STALE": (False, False, ["MANUAL_REVIEW", "ABORT"]),
        "WORLD_CONTEXT_STALE": (True, False, ["RETRY", "ABORT"]),
    }

    @classmethod
    def resolve(cls, error_code: str | None):
        return cls.RULES.get(error_code, (False, False, ["ABORT"]))


class TraceSanitizer:
    BLOCKED = {"api_key", "authorization", "authorization_header", "token", "access_token", "secret", "headers", "prompt", "messages", "actor_context", "world_context", "secret_canon", "raw_output", "raw_response", "response_body"}

    @classmethod
    def _key(cls, key: Any) -> str:
        return str(key).lower().replace("-", "_").replace(" ", "_")

    @classmethod
    def clean(cls, value: Any):
        if isinstance(value, dict):
            return {str(key): cls.clean(item) for key, item in value.items() if cls._key(key) not in cls.BLOCKED}
        if isinstance(value, list):
            return [cls.clean(item) for item in value]
        return value


def stable_fingerprint(value: Any, prefix: str = "execution-output-v1") -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}:" + hashlib.sha256(raw.encode()).hexdigest()


class ExecutionTraceRecorder:
    def start(self, db, *, project_id, stage, source_type, source_id,
               provider=None, model=None, input_fingerprint=None, output_fingerprint=None,
               latency_ms=None, request_id=None, error_code=None, upstream_status=None,
               validation_report=None, attempt_number=1, parent_trace_id=None):
        trace = ExecutionTrace(project_id=project_id, stage=stage, source_type=source_type,
            source_id=source_id, status=ExecutionStatus.STARTED, provider=provider, model=model,
            input_fingerprint=input_fingerprint, output_fingerprint=output_fingerprint,
            latency_ms=latency_ms, request_id=request_id, error_code=error_code,
            upstream_status=upstream_status, validation_report=TraceSanitizer.clean(validation_report or {}),
            retryable=False, repairable=False, attempt_number=attempt_number, parent_trace_id=parent_trace_id)
        db.add(trace)
        return trace

    def _finish(self, trace, status, *, error_code=None, upstream_status=None, validation_report=None, latency_ms=None, request_id=None, output_fingerprint=None):
        trace.status = status
        trace.error_code = error_code
        trace.upstream_status = upstream_status
        trace.validation_report = TraceSanitizer.clean(validation_report or {})
        trace.latency_ms = latency_ms
        trace.request_id = request_id
        trace.output_fingerprint = output_fingerprint
        trace.retryable, trace.repairable, _ = RecoveryPolicy.resolve(error_code)
        return trace

    def succeed(self, trace, *, latency_ms=None, request_id=None, output_fingerprint=None):
        return self._finish(trace, ExecutionStatus.SUCCEEDED, latency_ms=latency_ms, request_id=request_id, output_fingerprint=output_fingerprint)

    def fail(self, trace, error_code, *, upstream_status=None, validation_report=None, latency_ms=None, request_id=None):
        return self._finish(trace, ExecutionStatus.FAILED, error_code=error_code, upstream_status=upstream_status, validation_report=validation_report, latency_ms=latency_ms, request_id=request_id)

    def block(self, trace, error_code, *, validation_report=None, upstream_status=None, latency_ms=None, request_id=None):
        return self._finish(trace, ExecutionStatus.BLOCKED, error_code=error_code, upstream_status=upstream_status, validation_report=validation_report, latency_ms=latency_ms, request_id=request_id)

    # Kept for final Revision API traces, which have no long-lived model attempt.
    def create(self, db, *, status=ExecutionStatus.STARTED, **kwargs):
        trace = self.start(db, **kwargs)
        if status != ExecutionStatus.STARTED:
            self._finish(trace, status, error_code=kwargs.get("error_code"), upstream_status=kwargs.get("upstream_status"), validation_report=kwargs.get("validation_report"), latency_ms=kwargs.get("latency_ms"), request_id=kwargs.get("request_id"), output_fingerprint=kwargs.get("output_fingerprint"))
        return trace
