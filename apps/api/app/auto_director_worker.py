"""Persistent local worker for FULL_AUTO runs.

The worker is deliberately small: the database checkpoint is the queue and the
existing orchestrator remains the domain owner of each stage.
"""
from __future__ import annotations

import threading
import time
import traceback
from sqlalchemy import select
from .db import SessionLocal
from .models import AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage
from .auto_director import AutoDirectorOrchestrator
from .author_guided_volume import AuthorGuidedVolumeService


class AutoDirectorWorker:
    def __init__(self, session_factory=SessionLocal, poll_seconds: float = 0.5):
        self.session_factory = session_factory
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> bool:
        db = self.session_factory()
        run = None
        try:
            run = db.scalar(select(AutoDirectorRun).where(AutoDirectorRun.status.in_([AutoDirectorRunStatus.CREATED, AutoDirectorRunStatus.RUNNING])).order_by(AutoDirectorRun.created_at).with_for_update())
            if not run: return False
            if run.run_mode == "AUTHOR_GUIDED_VOLUME":
                AuthorGuidedVolumeService().advance_run(db, run)
            else:
                AutoDirectorOrchestrator().advance_to_pause(db, run)
            db.commit()
            return True
        except Exception as exc:
            db.rollback()
            failed = db.scalar(select(AutoDirectorRun).where(AutoDirectorRun.id == run.id).with_for_update()) if run else None
            if failed and failed.status in {AutoDirectorRunStatus.CREATED, AutoDirectorRunStatus.RUNNING}:
                failed_stage = getattr(failed.current_stage, "value", failed.current_stage)
                failed.status = AutoDirectorRunStatus.FAILED
                failed.current_stage = AutoDirectorStage.FAILED
                failed.pause_reason = "WORKER_UNEXPECTED_ERROR"
                failed.next_action = "检查 worker 错误后重试当前阶段或接管运行。"
                failed.context = {**(failed.context or {}), "worker_error": str(exc)[:1000], "worker_error_code": getattr(exc, "code", None) or "WORKER_UNEXPECTED_ERROR", "worker_stage": failed_stage, "worker_traceback": traceback.format_exc(limit=8)[-4000:]}
                failed.pause_reason = getattr(exc, "code", None) or "WORKER_UNEXPECTED_ERROR"
                db.commit()
            return True
        finally:
            db.close()

    def _serve(self):
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.poll_seconds)

    def start(self):
        if self._thread and self._thread.is_alive(): return self
        self._stop.clear(); self._thread = threading.Thread(target=self._serve, name="auto-director-worker", daemon=True); self._thread.start(); return self

    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=2)
