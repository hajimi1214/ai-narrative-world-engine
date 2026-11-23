"""Background writing against durable database checkpoints."""
from __future__ import annotations

import logging
import threading
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import OperationalError

from .ai.errors import (
    MODEL_AUTH_FAILED, MODEL_PROVIDER_NOT_CONFIGURED, MODEL_PROVIDER_UNSUPPORTED,
    MODEL_RATE_LIMITED, MODEL_TIMEOUT, MODEL_UPSTREAM_ERROR, ModelProviderError,
)
from .author_guided_volume import AuthorGuidedVolumeService
from .auto_director import AutoDirectorOrchestrator
from .db import SessionLocal
from .models import AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage, AutoDirectorStep, AutoDirectorStepStatus
from .author_recovery import (
    LostRunClaim, budget_usage, clear_retry, configured_token_budget,
    is_transient, provider_failure, release_claim, revoke_claim, retry_limit, unknown_call_reserve, utc_now,
)

logger = logging.getLogger(__name__)
ACTIVE = (AutoDirectorRunStatus.CREATED, AutoDirectorRunStatus.RUNNING)


class AutoDirectorWorker:
    def __init__(self, session_factory=SessionLocal, poll_seconds: float = 0.5, *, lease_seconds: float = 45):
        self.session_factory = session_factory
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _claim(self) -> tuple[str, str] | None:
        now = utc_now()
        eligible = (
            AutoDirectorRun.status.in_(ACTIVE),
            or_(AutoDirectorRun.next_retry_at.is_(None), AutoDirectorRun.next_retry_at <= now),
            or_(AutoDirectorRun.worker_lease_until.is_(None), AutoDirectorRun.worker_lease_until <= now),
        )
        with self.session_factory() as db:
            candidate = db.execute(
                select(AutoDirectorRun.id, AutoDirectorRun.worker_claim_token, AutoDirectorRun.run_mode,
                       AutoDirectorRun.settings, AutoDirectorRun.context)
                .where(*eligible).order_by(AutoDirectorRun.updated_at, AutoDirectorRun.id).limit(1)
            ).first()
            if not candidate:
                return None
            token = str(uuid4())
            values = {"worker_claim_token": token, "worker_lease_until": now + timedelta(seconds=self.lease_seconds), "next_retry_at": None}
            if candidate.worker_claim_token:
                values["recovery_usage_uncertain"] = True
                values["context"] = {
                    **(candidate.context or {}),
                    "recovery_reserved_tokens": int((candidate.context or {}).get("recovery_reserved_tokens") or 0) + unknown_call_reserve(
                        candidate.settings, candidate.context,
                    ),
                }
            claimed = db.execute(update(AutoDirectorRun).where(AutoDirectorRun.id == candidate.id, *eligible).values(**values))
            db.commit()
            return (candidate.id, token) if claimed.rowcount == 1 else None

    @staticmethod
    def _fence(db, run_id: str, token: str) -> None:
        with db.no_autoflush:
            result = db.execute(update(AutoDirectorRun).where(
                AutoDirectorRun.id == run_id, AutoDirectorRun.worker_claim_token == token,
            ).values(worker_claim_token=token, updated_at=AutoDirectorRun.updated_at)
                .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise LostRunClaim()

    def _renew(self, stop: threading.Event, run_id: str, token: str) -> None:
        while not stop.wait(self.lease_seconds / 3):
            try:
                with self.session_factory() as db:
                    result = db.execute(update(AutoDirectorRun).where(
                        AutoDirectorRun.id == run_id, AutoDirectorRun.worker_claim_token == token,
                    ).values(worker_lease_until=utc_now() + timedelta(seconds=self.lease_seconds), updated_at=AutoDirectorRun.updated_at))
                    db.commit()
                    if result.rowcount != 1:
                        return
            except OperationalError:
                # A SQLite writer transaction itself excludes competing claims.
                continue

    @staticmethod
    def _save_checkpoint(db, run: AutoDirectorRun) -> None:
        explicit = db.info.pop("author_checkpoint_stage", None)
        if explicit:
            run.checkpoint_stage = explicit
            run.checkpoint_saved_at = utc_now()
            return
        db.flush()
        step = db.scalar(select(AutoDirectorStep).where(
            AutoDirectorStep.run_id == run.id,
            AutoDirectorStep.status == AutoDirectorStepStatus.COMMITTED,
            AutoDirectorStep.completed_at.is_not(None),
        ).order_by(AutoDirectorStep.completed_at.desc(), AutoDirectorStep.id.desc()).limit(1))
        if step and (not run.checkpoint_saved_at or step.completed_at >= run.checkpoint_saved_at):
            run.checkpoint_stage = getattr(step.stage, "value", step.stage)
            run.checkpoint_saved_at = step.completed_at

    @staticmethod
    def _stop_for_budget(run: AutoDirectorRun) -> None:
        context = dict(run.context or {})
        if run.current_stage != AutoDirectorStage.PAUSED:
            context["resume_stage"] = getattr(run.current_stage, "value", run.current_stage)
        run.context = context
        run.status = AutoDirectorRunStatus.PAUSED
        run.current_stage = AutoDirectorStage.PAUSED
        run.pause_reason = "TOKEN_BUDGET_EXCEEDED"
        run.recovery_error_code = run.pause_reason
        run.next_action = "提高本次 token 预算后从已保存的检查点继续，或接管运行。"
        release_claim(run)

    def _handle_failure(self, db, run: AutoDirectorRun, *, code: str, stage: str,
                        upstream_status: int | None = None, step_id: str | None = None,
                        retry_after: int | None = None, reserve: int = 0,
                        accounted_calls: int = 1) -> None:
        pending_events = list(db.info.get("auto_pending_usage_events") or [])
        pending_metrics = None
        if pending_events:
            orchestrator = AutoDirectorOrchestrator()
            pending_metrics = orchestrator._tracked_metrics(pending_events)
            orchestrator._persist_pending_failure_usage(db, run, stage)
        if code == "TOKEN_BUDGET_EXCEEDED":
            self._stop_for_budget(run)
            return
        if is_transient(code, upstream_status):
            failed_step = db.get(AutoDirectorStep, step_id) if step_id else None
            usage = (failed_step.token_usage or {}) if failed_step and failed_step.run_id == run.id else {}
            already_reserved = bool(int(usage.get("unknown_usage_calls") or 0))
            usage_uncertain = already_reserved or bool(pending_metrics and pending_metrics["usage"].get("usage_status") == "UNKNOWN")
            persisted_call_usage = (run.context or {}).get("last_failed_usage") or {}
            persisted_failure_usage = (run.context or {}).get("last_persisted_failure_usage") or {}
            step_has_call_usage = int(usage.get("calls", 0) or 0) > 0
            run_has_call_usage = (
                persisted_call_usage.get("stage") == stage
                and int(persisted_call_usage.get("calls", 0) or 0) > 0
            )
            persisted_for_failure = (
                persisted_failure_usage.get("stage") == stage
                and code in (persisted_failure_usage.get("error_codes") or [])
                and int(persisted_failure_usage.get("calls") or 0) >= max(0, int(accounted_calls or 0))
            )
            if not pending_metrics and not already_reserved and not persisted_for_failure and not step_has_call_usage and not run_has_call_usage:
                calls = max(0, int(accounted_calls or 0))
                reserve_tokens = reserve or unknown_call_reserve(run) * calls
                context = dict(run.context or {})
                context["recovery_reserved_tokens"] = int(context.get("recovery_reserved_tokens") or 0) + reserve_tokens
                run.context = context
                usage_uncertain = reserve_tokens > 0
            elif persisted_for_failure:
                usage_uncertain = int(persisted_failure_usage.get("unknown_usage_calls") or 0) > 0
            elif run_has_call_usage:
                usage_uncertain = int((persisted_call_usage.get("usage") or {}).get("unknown_usage_calls", 0) or 0) > 0
            if usage_uncertain:
                run.recovery_usage_uncertain = True
            context = dict(run.context or {})
            context.pop("last_persisted_failure_usage", None)
            run.context = context
        if is_transient(code, upstream_status):
            budget = configured_token_budget(run)
            if budget and budget_usage(run) >= budget:
                self._stop_for_budget(run)
                return
            chapter = (run.context or {}).get("current_chapter_number")
            key = str(step_id or f"{stage}:{chapter or 0}")
            previous = int(run.recovery_attempts or 0) if key == run.recovery_key else 0
            run.recovery_key = key
            run.recovery_error_code = code
            if previous < retry_limit(run):
                run.recovery_attempts = previous + 1
                seconds = max(min(120, 5 * 4 ** previous), retry_after or 0)
                run.next_retry_at = utc_now() + timedelta(seconds=seconds)
                run.status = AutoDirectorRunStatus.RUNNING
                run.pause_reason = None
                try:
                    run.current_stage = AutoDirectorStage(stage)
                except ValueError:
                    pass
                run.next_action = "模型服务暂时不可用。到达重试时间后从当前检查点自动继续。"
                return
            run.status = AutoDirectorRunStatus.BLOCKED
            run.current_stage = AutoDirectorStage.BLOCKED
            run.pause_reason = "AUTO_RETRY_EXHAUSTED"
            run.next_action = "自动重试已用尽。检查模型连接后重试当前步骤，或接管运行。"
            return

        requires_author = code in {MODEL_AUTH_FAILED, MODEL_PROVIDER_NOT_CONFIGURED, MODEL_PROVIDER_UNSUPPORTED}
        run.status = AutoDirectorRunStatus.BLOCKED if requires_author else AutoDirectorRunStatus.FAILED
        run.current_stage = AutoDirectorStage.BLOCKED if requires_author else AutoDirectorStage.FAILED
        run.pause_reason = code
        run.recovery_error_code = code
        run.next_action = (
            "检查项目的模型连接和凭据，然后重试当前步骤。" if requires_author
            else "检查失败原因后重试当前步骤，或接管运行。"
        )
        context = dict(run.context or {})
        context.setdefault("worker_error", code)
        context.update({"worker_error_code": code, "worker_stage": stage})
        run.context = context

    def _advance(self, db, run: AutoDirectorRun, run_id: str, token: str) -> None:
        author = run.run_mode == "AUTHOR_GUIDED_VOLUME"
        db.info["author_commit_fence"] = lambda session: self._fence(session, run_id, token)
        if author:
            db.info["author_recovery"] = True
        db.info["auto_pending_total_tokens"] = 0
        db.info["auto_pending_reserved_tokens"] = 0
        db.info["author_model_call_guard"] = lambda: self._check_model_call_budget(db, run)
        budget = configured_token_budget(run)
        if budget and budget_usage(run) >= budget:
            self._stop_for_budget(run)
            return
        if author:
            AuthorGuidedVolumeService().advance_run(db, run)
        else:
            AutoDirectorOrchestrator().advance_to_pause(db, run)
        self._save_checkpoint(db, run)
        if run.status == AutoDirectorRunStatus.RUNNING:
            budget = configured_token_budget(run)
            if budget and budget_usage(run) >= budget:
                self._stop_for_budget(run)
        if run.status in {AutoDirectorRunStatus.FAILED, AutoDirectorRunStatus.BLOCKED, AutoDirectorRunStatus.PAUSED}:
            step_id = (run.context or {}).get("failed_step_id")
            step = db.get(AutoDirectorStep, step_id) if step_id else None
            details = (step.output_payload or {}) if step and step.run_id == run.id else {}
            code = run.pause_reason
            if is_transient(code, details.get("upstream_status")) or code in {
                MODEL_AUTH_FAILED, MODEL_PROVIDER_NOT_CONFIGURED, MODEL_PROVIDER_UNSUPPORTED,
                "TOKEN_BUDGET_EXCEEDED",
            }:
                stage = getattr(step.stage, "value", step.stage) if step else getattr(run.current_stage, "value", run.current_stage)
                self._handle_failure(db, run, code=code, stage=stage, upstream_status=details.get("upstream_status"),
                                     step_id=step_id, retry_after=details.get("retry_after"),
                                     accounted_calls=int((details.get("last_failed_usage") or {}).get("calls", 1) or 0))
                release_claim(run)
                return
        if (run.status == AutoDirectorRunStatus.RUNNING and run.next_retry_at is None) or run.pause_reason in {
            "WINDOW_COMPLETE", "RUN_CHAPTER_BUDGET_REACHED", "AUTHOR_WINDOW_READY",
        }:
            clear_retry(run)
        release_claim(run)

    @staticmethod
    def _check_model_call_budget(db, run) -> None:
        budget = configured_token_budget(run)
        pending = int(db.info.get("auto_pending_total_tokens") or 0)
        pending += int(db.info.get("auto_pending_reserved_tokens") or 0)
        if budget and budget_usage(run) + pending >= budget:
            error = ModelProviderError("TOKEN_BUDGET_EXCEEDED", accounted_calls=0)
            raise error

    def run_once(self) -> bool:
        try:
            claimed = self._claim()
        except OperationalError:
            return False
        if not claimed:
            return False
        run_id, token = claimed
        renewal_stop = threading.Event()
        renewal = threading.Thread(target=self._renew, args=(renewal_stop, run_id, token), daemon=True)
        renewal.start()
        try:
            with self.session_factory() as db:
                run = db.get(AutoDirectorRun, run_id)
                if not run or run.worker_claim_token != token:
                    return True
                initial_stage = getattr(run.current_stage, "value", run.current_stage)
                try:
                    self._advance(db, run, run_id, token)
                    self._fence(db, run_id, token)
                    db.commit()
                except LostRunClaim:
                    db.rollback()
                except OperationalError:
                    db.rollback()
                    # Release an uncontended claim promptly. If another transaction
                    # still holds SQLite's lock, its lease allows later recovery.
                    try:
                        self._fence(db, run_id, token)
                        run = db.get(AutoDirectorRun, run_id, populate_existing=True)
                        release_claim(run)
                        db.commit()
                    except (LostRunClaim, OperationalError):
                        db.rollback()
                except Exception as exc:
                    db.rollback()
                    try:
                        self._fence(db, run_id, token)
                        run = db.get(AutoDirectorRun, run_id, populate_existing=True)
                        upstream = provider_failure(exc)
                        code = (upstream.code if upstream else getattr(exc, "code", None)) or "WORKER_UNEXPECTED_ERROR"
                        self._handle_failure(db, run, code=code, stage=initial_stage,
                                             upstream_status=upstream.upstream_status if upstream else None,
                                             retry_after=getattr(upstream, "retry_after", None),
                                             reserve=getattr(upstream, "reserved_tokens", 0),
                                             accounted_calls=getattr(upstream, "accounted_calls", 1))
                        context = dict(run.context or {})
                        context.update({"worker_error": str(exc)[:1000], "worker_error_code": code, "worker_stage": initial_stage})
                        run.context = context
                        self._fence(db, run_id, token)
                        release_claim(run)
                        db.commit()
                    except (LostRunClaim, OperationalError):
                        db.rollback()
        finally:
            renewal_stop.set()
        return True

    def _serve(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.error("Writing worker could not complete a database operation")
            self._stop.wait(self.poll_seconds)

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="auto-director-worker", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
