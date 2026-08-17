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
    }

    @classmethod
    def resolve(cls, error_code: str | None):
        return cls.RULES.get(error_code, (False, False, ["ABORT"]))


class TraceSanitizer:
    BLOCKED = {"api_key", "authorization", "token", "secret", "headers", "prompt", "messages", "actor_context", "world_context", "secret_canon"}

    @classmethod
    def clean(cls, value: Any):
        if isinstance(value, dict):
            return {str(key): cls.clean(item) for key, item in value.items() if str(key).lower() not in cls.BLOCKED}
        if isinstance(value, list):
            return [cls.clean(item) for item in value]
        return value


def stable_fingerprint(value: Any, prefix: str = "execution-output-v1") -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}:" + hashlib.sha256(raw.encode()).hexdigest()


class ExecutionTraceRecorder:
    def create(self, db, *, project_id, stage, source_type, source_id, status=ExecutionStatus.STARTED,
               provider=None, model=None, input_fingerprint=None, output_fingerprint=None,
               latency_ms=None, request_id=None, error_code=None, upstream_status=None,
               validation_report=None, attempt_number=1):
        retryable, repairable, _ = RecoveryPolicy.resolve(error_code)
        trace = ExecutionTrace(project_id=project_id, stage=stage, source_type=source_type,
            source_id=source_id, status=status, provider=provider, model=model,
            input_fingerprint=input_fingerprint, output_fingerprint=output_fingerprint,
            latency_ms=latency_ms, request_id=request_id, error_code=error_code,
            upstream_status=upstream_status, validation_report=TraceSanitizer.clean(validation_report or {}),
            retryable=retryable, repairable=repairable, attempt_number=attempt_number)
        db.add(trace)
        return trace
