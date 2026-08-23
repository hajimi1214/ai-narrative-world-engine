"""Short-lived, safe previews for local AI work.

ExecutionTrace remains the durable audit record. This broker only exposes
progress and a bounded, untrusted model preview while a call is active.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Condition
from typing import Any
import uuid


class LiveExecutionBroker:
    _max_preview_chars = 12000
    _retention = timedelta(minutes=30)

    def __init__(self) -> None:
        self._condition = Condition()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._version = 0

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _changed(self) -> None:
        self._version += 1
        self._condition.notify_all()

    def _cleanup(self) -> None:
        cutoff = datetime.now(timezone.utc) - self._retention
        self._sessions = {key: value for key, value in self._sessions.items() if datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00")) >= cutoff}

    def begin(self, *, project_id: str, trace_id: str, label: str, provider: str | None, model: str | None, stage: str) -> str:
        session_id = str(uuid.uuid4())
        now = self._now()
        with self._condition:
            self._cleanup()
            self._sessions[session_id] = {"id": session_id, "project_id": project_id, "trace_id": trace_id, "label": label, "provider": provider, "model": model, "stage": stage, "phase": "REQUESTING", "message": "正在建立模型请求", "preview": "", "total_chars": 0, "status": "RUNNING", "started_at": now, "updated_at": now, "completed_at": None, "error_code": None}
            self._changed()
        return session_id

    def phase(self, session_id: str, phase: str, message: str) -> None:
        with self._condition:
            session = self._sessions.get(session_id)
            if not session: return
            session.update({"phase": phase, "message": message, "updated_at": self._now()})
            self._changed()

    def append(self, session_id: str, content: str) -> None:
        if not content: return
        with self._condition:
            session = self._sessions.get(session_id)
            if not session: return
            preview = (session["preview"] + str(content))[-self._max_preview_chars:]
            session.update({"preview": preview, "total_chars": session["total_chars"] + len(str(content)), "phase": "STREAMING", "message": "模型正在返回未校验预览", "updated_at": self._now()})
            self._changed()

    def complete(self, session_id: str, message: str) -> None:
        self._finish(session_id, "COMPLETED", "COMPLETED", message, None)

    def fail(self, session_id: str, code: str, message: str) -> None:
        self._finish(session_id, "FAILED", "FAILED", message, code)

    def _finish(self, session_id: str, status: str, phase: str, message: str, error_code: str | None) -> None:
        with self._condition:
            session = self._sessions.get(session_id)
            if not session: return
            now = self._now()
            session.update({"status": status, "phase": phase, "message": message, "error_code": error_code, "updated_at": now, "completed_at": now})
            self._changed()

    def snapshots(self, project_id: str) -> list[dict[str, Any]]:
        with self._condition:
            self._cleanup()
            values = [dict(item) for item in self._sessions.values() if item["project_id"] == project_id]
        return sorted(values, key=lambda item: item["updated_at"], reverse=True)

    def wait_for_snapshots(self, project_id: str, after_version: int, timeout: float = 15.0) -> tuple[int, list[dict[str, Any]]]:
        """Wait for live state changes without putting model content in durable storage."""
        with self._condition:
            if self._version == after_version:
                self._condition.wait(timeout=timeout)
            self._cleanup()
            values = [dict(item) for item in self._sessions.values() if item["project_id"] == project_id]
            return self._version, sorted(values, key=lambda item: item["updated_at"], reverse=True)


live_execution_broker = LiveExecutionBroker()
