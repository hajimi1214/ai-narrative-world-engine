"""Book-level orchestration for the local single-user author workflow.

This module coordinates existing creation, planning, writer and quality services.
It deliberately keeps their domain rules in their own modules.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .ai.factory import get_model_provider
from .api_types import AutoDirectorRunCreatePayload
from .creation import generate_creation_directions
from .model_router import ModelRouter, ProviderCredentialResolver
from .planning import chapter_task_context, generate_plan, persist_plan, validate_plan_references
from .planning import _default_chapter
from .llm_actor import _extract_single_json_object
from .quality import QualityGateService, QualityRepairService
from .settings import get_settings
from .writer import WriterProjectionService
from .autonomy import AutonomousWorldLoopService
from .narrative_structure import NarrativeStructureConfig
from .live_execution import live_execution_broker
from .models import (
    AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage, AutoDirectorStep,
    AutoDirectorStepStatus, Chapter, ChapterWriterDraft, ChapterQualityAssessment, ChapterQualityFinding,
    Project, StoryPlan, StoryPlanChapter, StoryPlanVolume, StoryPlanArc, StoryPlanStatus, WriterDraftStatus, Character,
    WorldEntity, StoryThread, CanonFact, StoryArc, EntityType, CanonType, AutonomousRunStatus,
    AutonomousWorldStep, SceneProposal, ScenePerformance, ResolverMode,
)


def score_direction(direction: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Score only parsed direction fields; prose and model metadata are ignored."""
    protagonist = direction.get("protagonist") or {}
    promises = [str(item).strip() for item in direction.get("first_ten_chapter_promises") or [] if str(item).strip()]
    boundaries = [str(item).strip() for item in direction.get("world_boundaries") or [] if str(item).strip()]
    foreshadowing = [str(item).strip() for item in direction.get("foreshadowing_directions") or [] if str(item).strip()]
    forbidden_events = [str(item).strip() for item in direction.get("forbidden_events") or [] if str(item).strip()]
    allowed_reveals = {str(item).strip() for item in direction.get("allowed_reveals") or [] if str(item).strip()}
    forbidden_reveals = {str(item).strip() for item in direction.get("forbidden_reveals") or [] if str(item).strip()}
    fields = {
        "premise": bool(str(direction.get("premise") or "").strip()),
        "goal_cost": bool(str(protagonist.get("desire") or "").strip() and str(protagonist.get("cost") or "").strip()),
        "volume_goal": bool(str(direction.get("first_volume_goal") or "").strip()),
        "promises": len(promises) >= 3,
        "world_boundaries": len(boundaries) >= 1,
        "foreshadowing": len(foreshadowing) >= 1,
        "conflict": bool(str(direction.get("core_conflict") or "").strip()),
        "chapter_tasks_generatable": bool(str(direction.get("first_volume_goal") or "").strip() and len(promises) >= 3),
    }
    invalid = [key for key, value in fields.items() if not value]
    conflicts = sorted(allowed_reveals.intersection(forbidden_reveals))
    empty_list_items = sum(1 for key in ("world_boundaries", "first_ten_chapter_promises", "foreshadowing_directions", "forbidden_events") for item in direction.get(key, []) if not str(item).strip())
    score = max(0.0, sum(1 for value in fields.values() if value) * 100 / len(fields) - len(forbidden_events) * 5 - len(conflicts) * 10 - empty_list_items * 2)
    return score, {"checks": fields, "invalid_fields": invalid, "forbidden_event_count": len(forbidden_events), "empty_list_item_count": empty_list_items, "conflicting_references": conflicts, "valid": not invalid and not conflicts and not empty_list_items}


def fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:120]


def enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


class AutoDirectorError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


class _UsageTrackingProvider:
    """Delegates a model provider while exposing the last real ModelResult."""

    def __init__(self, provider):
        self.provider = provider
        self.name = getattr(provider, "name", None)
        self.last_result = None
        self.results = []

    def generate(self, messages, model):
        result = self.provider.generate(messages, model)
        self.last_result = result
        self.results.append(result)
        return result


class AutoDirectorAdoptionService:
    """Narrow policy boundary for automatic formal adoption."""

    def adopt(self, db: Session, draft: ChapterWriterDraft, assessment: ChapterQualityAssessment) -> None:
        chapter = db.get(Chapter, draft.chapter_id)
        if not chapter:
            raise AutoDirectorError("AUTO_ADOPTION_CHAPTER_NOT_FOUND")
        if enum_value(draft.status) == "ADOPTED" and chapter.current_writer_draft_id == draft.id:
            return
        if chapter.current_writer_draft_id and chapter.current_writer_draft_id != draft.id:
            current = db.get(ChapterWriterDraft, chapter.current_writer_draft_id)
            if current and enum_value(current.status) == "ADOPTED":
                raise AutoDirectorError("AUTO_ADOPTION_EXISTING_ADOPTED_DRAFT")
        if enum_value(draft.status) != "VALIDATED":
            raise AutoDirectorError("AUTO_ADOPTION_DRAFT_NOT_VALIDATED")
        # Candidate drafts are intentionally assessed as inactive until their
        # first formal adoption. The auto-director is the owner of that
        # transition, so lineage and PASS status are the relevant checks here.
        if enum_value(assessment.status) != "PASS":
            raise AutoDirectorError("AUTO_ADOPTION_QUALITY_NOT_PASS")
        if assessment.stale_at is not None or chapter.current_quality_assessment_id not in {None, assessment.id}:
            raise AutoDirectorError("AUTO_ADOPTION_ASSESSMENT_STALE")
        if draft.content_fingerprint != assessment.content_fingerprint or draft.chapter_source_fingerprint != assessment.chapter_source_fingerprint or draft.writer_context_fingerprint != assessment.writer_context_fingerprint:
            raise AutoDirectorError("AUTO_ADOPTION_FINGERPRINT_MISMATCH")
        report = draft.validation_report or {}
        if report.get("valid") is False:
            raise AutoDirectorError("AUTO_ADOPTION_DRAFT_VALIDATION_FAILED")
        if report.get("task_forbidden_hits") or report.get("forbidden_event_check", {}).get("forbidden_events"):
            raise AutoDirectorError("AUTO_ADOPTION_FORBIDDEN_EVENT")
        task = (draft.source_manifest or {}).get("planning_task") or {}
        covered = {str(item) for item in report.get("task_coverage") or []}
        missing = [str(item) for item in task.get("must_events") or [] if str(item) not in covered]
        if missing:
            raise AutoDirectorError("AUTO_ADOPTION_TASK_INCOMPLETE")
        from .quality import QualityAssessmentAudit
        QualityAssessmentAudit().audit(db, assessment.id)
        latest_assessment = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.chapter_id == chapter.id).order_by(ChapterQualityAssessment.version.desc(), ChapterQualityAssessment.id.desc()))
        if not latest_assessment or latest_assessment.id != assessment.id:
            raise AutoDirectorError("AUTO_ADOPTION_ASSESSMENT_STALE")
        freshness = __import__("app.quality", fromlist=["QualityAssessmentFreshnessChecker"]).QualityAssessmentFreshnessChecker().check(db, assessment, require_current=False)
        if not freshness.get("fresh"):
            raise AutoDirectorError("AUTO_ADOPTION_ASSESSMENT_STALE", str(freshness.get("reasons") or []))
        findings = db.scalars(select(ChapterQualityFinding).where(ChapterQualityFinding.assessment_id == assessment.id)).all()
        if any(enum_value(item.severity) == "BLOCKING" for item in findings):
            raise AutoDirectorError("AUTO_ADOPTION_BLOCKING_FINDING")


class AutoDirectorOrchestrator:
    @staticmethod
    def _chapter_limit(payload: dict[str, Any]) -> int:
        target = int(payload.get("target_chapters") or 10)
        maximum = payload.get("max_chapters")
        return min(target, int(maximum)) if maximum is not None else target

    @staticmethod
    def _model_tokens(result: Any) -> int:
        usage = getattr(result, "usage", None) or {}
        return int(usage.get("total_tokens", usage.get("tokens", 0)) or 0)

    @staticmethod
    def _model_usage(result: Any) -> dict[str, int]:
        usage = getattr(result, "usage", None) or {}
        return {"prompt_tokens": int(usage.get("prompt_tokens", 0) or 0), "completion_tokens": int(usage.get("completion_tokens", 0) or 0), "total_tokens": int(usage.get("total_tokens", usage.get("tokens", 0)) or 0)}

    @classmethod
    def _tracked_metrics(cls, *trackers: _UsageTrackingProvider) -> dict[str, Any]:
        results = [result for tracker in trackers for result in tracker.results]
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for result in results:
            current = cls._model_usage(result)
            for key in usage:
                usage[key] += current[key]
        providers = sorted({str(getattr(result, "provider", "")) for result in results if getattr(result, "provider", None)})
        models = sorted({str(getattr(result, "model", "")) for result in results if getattr(result, "model", None)})
        return {"calls": len(results), "latency_ms": sum(int(getattr(result, "latency_ms", 0) or 0) for result in results), "tokens": usage["total_tokens"], "usage": usage, "provider": ",".join(providers) or None, "model": ",".join(models) or None}

    @staticmethod
    def _steps(db: Session, run: AutoDirectorRun) -> list[AutoDirectorStep]:
        return db.scalars(select(AutoDirectorStep).where(AutoDirectorStep.run_id == run.id)).all()

    @staticmethod
    def _live_begin(run: AutoDirectorRun, label: str, stage: str, *, provider: str | None = None, model: str | None = None) -> str:
        return live_execution_broker.begin(
            project_id=run.project_id,
            trace_id=f"auto-director:{run.id}:{stage}:{datetime.utcnow().timestamp()}",
            label=label,
            provider=provider,
            model=model,
            stage=stage,
        )

    @staticmethod
    def _live_complete(session_id: str, message: str) -> None:
        live_execution_broker.complete(session_id, message)

    @staticmethod
    def _live_fail(session_id: str, exc: Exception) -> None:
        code = str(getattr(exc, "code", None) or getattr(exc, "error_code", None) or "AUTO_DIRECTOR_STAGE_FAILED")
        status = getattr(exc, "upstream_status", None)
        message = f"模型请求未完成{f'（HTTP {status}）' if status else ''}，可重试当前阶段。"
        live_execution_broker.fail(session_id, code, message)

    def _check_budget(self, run: AutoDirectorRun) -> None:
        limit = int((run.settings or {}).get("max_tokens", 0) or 0)
        used = int((run.token_usage or {}).get("total_tokens", 0) or 0)
        if limit and used >= limit:
            raise AutoDirectorError("TOKEN_BUDGET_EXCEEDED", f"已达到本次运行 token 预算 {limit}")

    def _step(self, db: Session, run: AutoDirectorRun, stage: AutoDirectorStage, input_data: Any) -> AutoDirectorStep:
        fp = fingerprint(input_data)
        existing = db.scalar(select(AutoDirectorStep).where(AutoDirectorStep.run_id == run.id, AutoDirectorStep.stage == stage, AutoDirectorStep.input_fingerprint == fp))
        if existing and existing.status == AutoDirectorStepStatus.COMMITTED:
            return existing
        if existing:
            existing.attempt += 1
            existing.status = AutoDirectorStepStatus.RUNNING
            existing.started_at = datetime.utcnow()
            return existing
        step = AutoDirectorStep(run_id=run.id, stage=stage, input_fingerprint=fp, status=AutoDirectorStepStatus.RUNNING, started_at=datetime.utcnow())
        db.add(step); db.flush()
        return step

    def _commit_step(self, step: AutoDirectorStep, payload: dict[str, Any], artifact_id: str | None = None, *, calls: int = 0, latency_ms: int = 0, tokens: int = 0, usage: dict[str, int] | None = None, provider: str | None = None, model: str | None = None) -> None:
        step.status = AutoDirectorStepStatus.COMMITTED
        step.output_payload = payload
        step.output_artifact_id = artifact_id
        token_usage = {**(usage or {}), "calls": calls, "tokens": tokens, "latency_ms": latency_ms, "provider": provider, "model": model, "estimated_cost": None, "cost_status": "UNKNOWN"}
        step.token_usage = token_usage
        step.calls = calls
        step.prompt_tokens = int(token_usage.get("prompt_tokens", 0))
        step.completion_tokens = int(token_usage.get("completion_tokens", 0))
        step.total_tokens = int(token_usage.get("total_tokens", tokens) or tokens)
        step.latency_ms = latency_ms
        step.provider = provider
        step.model = model
        step.estimated_cost = None
        step.cost_status = "UNKNOWN"
        step.output_payload = {**payload, "provider": provider, "model": model, "token_usage": token_usage}
        step.completed_at = datetime.utcnow()

    def _fail(self, run: AutoDirectorRun, step: AutoDirectorStep, exc: Exception, blocked: bool = False) -> None:
        code = getattr(exc, "code", None) or getattr(exc, "error_code", None) or "AUTO_DIRECTOR_STAGE_FAILED"
        upstream_status = getattr(exc, "upstream_status", None)
        step.status = AutoDirectorStepStatus.BLOCKED if blocked else AutoDirectorStepStatus.FAILED
        step.error_code = str(code)
        step.error_summary = f"{str(exc)[:900]}{f' (HTTP {upstream_status})' if upstream_status else ''}"
        step.output_payload = {**(step.output_payload or {}), "upstream_status": upstream_status}
        step.completed_at = datetime.utcnow()
        run.status = AutoDirectorRunStatus.BLOCKED if blocked else AutoDirectorRunStatus.FAILED
        run.current_stage = AutoDirectorStage.BLOCKED if blocked else AutoDirectorStage.FAILED
        run.pause_reason = str(code)
        run.next_action = "检查错误后重试或接管运行。"

    def _add_usage(self, run: AutoDirectorRun, step: AutoDirectorStep) -> None:
        self._add_usage_metrics(run, {**(step.token_usage or {}), "provider": step.provider, "model": step.model})

    def _add_usage_metrics(self, run: AutoDirectorRun, metrics: dict[str, Any]) -> None:
        usage = dict(run.token_usage or {})
        total = int(metrics.get("total_tokens", metrics.get("tokens", 0)) or 0)
        usage["prompt_tokens"] = int(usage.get("prompt_tokens", 0)) + int(metrics.get("prompt_tokens", 0) or 0)
        usage["completion_tokens"] = int(usage.get("completion_tokens", 0)) + int(metrics.get("completion_tokens", 0) or 0)
        usage["total_tokens"] = int(usage.get("total_tokens", 0)) + total
        usage["total_calls"] = int(usage.get("total_calls", 0)) + int(metrics.get("calls", 0) or 0)
        usage["latency_ms"] = int(usage.get("latency_ms", 0)) + int(metrics.get("latency_ms", 0) or 0)
        providers = set(usage.get("providers") or [])
        models = set(usage.get("models") or [])
        providers.update(item.strip() for item in str(metrics.get("provider") or "").split(",") if item.strip())
        models.update(item.strip() for item in str(metrics.get("model") or "").split(",") if item.strip())
        usage["providers"] = sorted(providers)
        usage["models"] = sorted(models)
        usage["estimated_cost"] = None
        usage["cost_status"] = "UNKNOWN"
        run.token_usage = usage
        run.total_calls = int(usage["total_calls"])
        run.prompt_tokens = int(usage["prompt_tokens"])
        run.completion_tokens = int(usage["completion_tokens"])
        run.total_tokens = int(usage["total_tokens"])
        run.latency_ms = int(usage["latency_ms"])
        run.estimated_cost = None
        run.cost_status = "UNKNOWN"

    @staticmethod
    def _merge_step_usage(step: AutoDirectorStep, metrics: dict[str, Any]) -> None:
        current = dict(step.token_usage or {})
        current["prompt_tokens"] = int(current.get("prompt_tokens", 0)) + int(metrics.get("prompt_tokens", 0) or 0)
        current["completion_tokens"] = int(current.get("completion_tokens", 0)) + int(metrics.get("completion_tokens", 0) or 0)
        current["total_tokens"] = int(current.get("total_tokens", current.get("tokens", 0))) + int(metrics.get("total_tokens", metrics.get("tokens", 0)) or 0)
        current["calls"] = int(current.get("calls", 0)) + int(metrics.get("calls", 0) or 0)
        current["latency_ms"] = int(current.get("latency_ms", 0)) + int(metrics.get("latency_ms", 0) or 0)
        providers = set(current.get("providers") or [])
        models = set(current.get("models") or [])
        providers.update(item.strip() for item in str(metrics.get("provider") or "").split(",") if item.strip())
        models.update(item.strip() for item in str(metrics.get("model") or "").split(",") if item.strip())
        current["providers"] = sorted(providers)
        current["models"] = sorted(models)
        current["estimated_cost"] = None; current["cost_status"] = "UNKNOWN"
        step.token_usage = current
        step.calls = current["calls"]; step.prompt_tokens = current["prompt_tokens"]; step.completion_tokens = current["completion_tokens"]; step.total_tokens = current["total_tokens"]; step.latency_ms = current["latency_ms"]; step.provider = ",".join(current["providers"]) or None; step.model = ",".join(current["models"]) or None; step.estimated_cost = None; step.cost_status = "UNKNOWN"

    def create(self, db: Session, project: Project, payload: AutoDirectorRunCreatePayload) -> AutoDirectorRun:
        request = payload.model_dump()
        key = payload.idempotency_key or f"auto-{project.id}-{fingerprint(payload.model_dump())}"
        existing = db.scalar(select(AutoDirectorRun).where(AutoDirectorRun.project_id == project.id, AutoDirectorRun.idempotency_key == key))
        if existing:
            return existing
        if request.get("run_mode") == "AUTHOR_GUIDED_VOLUME":
            from .author_guided_volume import AuthorGuidedVolumeService
            contract = AuthorGuidedVolumeService().ensure_contract(db, project, request)
            run = AutoDirectorRun(project_id=project.id, idempotency_key=key, status=AutoDirectorRunStatus.RUNNING, current_stage=AutoDirectorStage.BOOK_CONTRACT, run_mode="AUTHOR_GUIDED_VOLUME", settings=request, context={"request": request, "book_contract_id": contract.id, "current_volume_number": 1})
            db.add(run); db.flush(); return run
        request["max_chapters"] = self._chapter_limit(request)
        # FULL_AUTO remains unattended, but it shares the durable book and
        # volume boundary used by the author-guided workflow. Estimates are
        # planning data only, never book-completion conditions.
        from .author_guided_volume import AuthorGuidedVolumeService
        boundary_service = AuthorGuidedVolumeService()
        boundary_request = {
            **request,
            "title": request.get("name") or project.name,
            "premise": request.get("inspiration") or project.story_seed,
            "global_plot_direction": request.get("inspiration") or project.story_seed,
            "estimated_chapters": request.get("target_chapters"),
            "operational_run_chapter_budget": request["max_chapters"],
        }
        contract = boundary_service.ensure_contract(db, project, boundary_request)
        volume = boundary_service.ensure_volume(db, project, contract, {"estimated_chapter_start": 1})
        run = AutoDirectorRun(project_id=project.id, idempotency_key=key, status=AutoDirectorRunStatus.RUNNING, current_stage=AutoDirectorStage.IDEA, run_mode="FULL_AUTO", settings={**request, "effective_max_chapters": request["max_chapters"]}, context={"request": request, "book_contract_id": contract.id, "volume_id": volume.id, "current_volume_number": volume.volume_number, "current_chapter_number": 1, "completed_chapters": []})
        db.add(run); db.flush()
        return run

    def advance_to_pause(self, db: Session, run: AutoDirectorRun) -> AutoDirectorRun:
        project = db.get(Project, run.project_id)
        if not project:
            raise AutoDirectorError("PROJECT_NOT_FOUND")
        if run.status in {AutoDirectorRunStatus.PAUSED, AutoDirectorRunStatus.FAILED, AutoDirectorRunStatus.BLOCKED, AutoDirectorRunStatus.COMPLETED} or (run.context or {}).get("stop_requested"):
            return run
        if run.run_mode == "FULL_AUTO" and (run.context or {}).get("volume_id"):
            from .models import VolumeContract, VolumeContractStatus
            volume = db.get(VolumeContract, run.context["volume_id"])
            if not volume or volume.project_id != project.id:
                raise AutoDirectorError("VOLUME_BOUNDARY_NOT_FOUND")
            if volume.status == VolumeContractStatus.SEALED:
                run.status = AutoDirectorRunStatus.BLOCKED
                run.current_stage = AutoDirectorStage.BLOCKED
                run.pause_reason = "VOLUME_SEALED"
                run.next_action = "当前卷已封存；请创建下一卷或由作者明确解封后再继续。"
                return run
        try:
            if run.current_stage == AutoDirectorStage.NEXT_CHAPTER:
                if (run.context or {}).get("stop_requested") or run.status != AutoDirectorRunStatus.RUNNING:
                    return run
                run.current_stage = AutoDirectorStage.CHAPTER_PLANNING
            if run.context.get("selected_direction") and not run.context.get("foundation"):
                self._prepare_foundation(db, run, run.context["selected_direction"])
                run.current_stage = AutoDirectorStage.STORY_MACRO
                run.next_action = "世界百科、世界事实、人物档案和剧情线程已保存；正在生成整本规划。"
                return run
            if run.current_stage in {AutoDirectorStage.IDEA, AutoDirectorStage.FRAMING, AutoDirectorStage.DIRECTION_SELECTION} and not run.context.get("directions"):
                request = dict(run.context.get("request") or {})
                step = self._step(db, run, AutoDirectorStage.FRAMING, request)
                if step.status == AutoDirectorStepStatus.COMMITTED:
                    data = step.output_payload
                else:
                    self._check_budget(run)
                    settings = get_settings(); route = ModelRouter().resolve(db, project.id, settings, "DIRECTOR")
                    live_session = self._live_begin(run, "自动导演：生成整书方向", "FRAMING", provider=route.provider, model=route.model)
                    live_execution_broker.phase(live_session, "GENERATING_DIRECTIONS", "正在生成并比较整书方向、角色和世界设定")
                    try:
                        result, model_result = generate_creation_directions(db, project.id, request, on_delta=lambda content: live_execution_broker.append(live_session, content))
                        self._live_complete(live_session, "方向、主要角色和世界边界已生成")
                    except Exception as exc:
                        self._live_fail(live_session, exc)
                        raise
                    data = result
                    self._commit_step(step, data, calls=1, latency_ms=getattr(model_result, "latency_ms", 0), tokens=self._model_tokens(model_result), usage=self._model_usage(model_result), provider=getattr(model_result, "provider", None), model=getattr(model_result, "model", None))
                    self._add_usage(run, step)
                directions = data.get("directions", [])[:3]
                scored = [score_direction(item) for item in directions]
                valid_indices = [index for index, (_, report) in enumerate(scored) if report.get("valid")]
                if not valid_indices:
                    repair_input = {"request": request, "invalid_reports": [{"index": i, "report": report} for i, (_, report) in enumerate(scored)], "instruction": "修复所有结构化缺失、冲突和空字段，返回三套完整可执行方向。"}
                    repair_step = self._step(db, run, AutoDirectorStage.FRAMING, repair_input)
                    if repair_step.status != AutoDirectorStepStatus.COMMITTED:
                        self._check_budget(run)
                        repaired, repair_result = generate_creation_directions(db, project.id, {**request, "repair_context": repair_input})
                        repaired_directions = repaired.get("directions", [])[:3]
                        repair_scored = [score_direction(item) for item in repaired_directions]
                        repair_valid = [i for i, (_, report) in enumerate(repair_scored) if report.get("valid")]
                        self._commit_step(repair_step, {"directions": repaired_directions, "reports": [report for _, report in repair_scored], "repair_reason": "ALL_DIRECTIONS_INVALID"}, calls=1, latency_ms=getattr(repair_result, "latency_ms", 0), tokens=self._model_tokens(repair_result), usage=self._model_usage(repair_result), provider=getattr(repair_result, "provider", None), model=getattr(repair_result, "model", None))
                        self._add_usage(run, repair_step)
                        directions, scored, valid_indices = repaired_directions, repair_scored, repair_valid
                    else:
                        directions = (repair_step.output_payload or {}).get("directions", [])
                        scored = [score_direction(item) for item in directions]
                        valid_indices = [index for index, (_, report) in enumerate(scored) if report.get("valid")]
                if not valid_indices:
                    raise AutoDirectorError("DIRECTION_REPAIR_FAILED", "方向修复后仍没有结构化验证通过的候选。")
                best = max(valid_indices, key=lambda index: scored[index][0])
                context = {**run.context, "directions": directions, "recommended_direction_index": best, "direction_scores": [score for score, _ in scored], "direction_validation_reports": [report for _, report in scored]}
                if (run.settings or {}).get("require_direction_confirmation", True):
                    run.context = context
                    run.status = AutoDirectorRunStatus.PAUSED
                    run.current_stage = AutoDirectorStage.DIRECTION_SELECTION
                    run.pause_reason = "DIRECTION_CONFIRMATION_REQUIRED"
                    run.next_action = "请选择一套整书方向；确认后将自动生成世界、角色、规划和正文。"
                    return run
                run.context = {**context, "selected_direction": directions[best], "selected_direction_fingerprint": fingerprint(directions[best]), "direction_score": scored[best][0], "direction_validation_report": scored[best][1]}
                run.current_stage = AutoDirectorStage.CAST_PREPARATION
                run.next_action = "正在自动准备书级设定、角色和世界边界。"
                return run
                return run
            if run.current_stage == AutoDirectorStage.DIRECTION_SELECTION:
                return run
            self._plan_and_first_chapter(db, run, project)
        except Exception as exc:
            self._fail(run, locals().get("step") or self._step(db, run, run.current_stage, run.context), exc, blocked=getattr(exc, "code", None) in {"DIRECTION_REPAIR_FAILED", "CHAPTER_TASK_CONTRACT_FAILED", "AUTO_ADOPTION_QUALITY_NOT_PASS"})
        return run

    def select_direction(self, db: Session, run: AutoDirectorRun, index: int | None = None, direction: dict[str, Any] | None = None) -> AutoDirectorRun:
        if run.current_stage != AutoDirectorStage.DIRECTION_SELECTION or run.status != AutoDirectorRunStatus.PAUSED:
            raise AutoDirectorError("DIRECTION_SELECTION_NOT_PENDING")
        choices = run.context.get("directions") or []
        selected = direction or (choices[index or 0] if 0 <= (index or 0) < len(choices) else None)
        if not selected:
            raise AutoDirectorError("DIRECTION_NOT_FOUND")
        run.context = {**run.context, "selected_direction": selected}
        run.status = AutoDirectorRunStatus.RUNNING
        run.pause_reason = None
        run.current_stage = AutoDirectorStage.CAST_PREPARATION
        return run

    def _prepare_foundation(self, db: Session, run: AutoDirectorRun, direction: dict[str, Any]) -> None:
        """Materialize only the structured inputs required by existing runtime services."""
        project = db.get(Project, run.project_id)
        if not project:
            raise AutoDirectorError("PROJECT_NOT_FOUND")
        context = dict(run.context or {})
        if context.get("foundation"):
            return
        live_session = self._live_begin(run, "自动导演：建立人物与世界", "FOUNDATION")
        live_execution_broker.phase(live_session, "MATERIALIZING_WORLD", "正在把 AI 方向写入世界百科、世界事实和人物档案")
        try:
            self._materialize_foundation(db, run, project, direction, context)
            self._live_complete(live_session, "世界百科、世界事实、人物档案和主线线程已建立")
        except Exception as exc:
            self._live_fail(live_session, exc)
            raise

    def _materialize_foundation(self, db: Session, run: AutoDirectorRun, project: Project, direction: dict[str, Any], context: dict[str, Any]) -> None:
        world_ids: list[str] = []
        world_specs = list(direction.get("world_encyclopedia") or [])
        world_specs.extend({"name": raw_name, "entity_type": "LOCATION", "description": raw_name} for raw_name in direction.get("world_boundaries") or [])
        seen_world_names: set[str] = set()
        for raw in world_specs:
            raw = raw if isinstance(raw, dict) else {"name": raw}
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            if name in seen_world_names:
                continue
            seen_world_names.add(name)
            try:
                entity_type = EntityType(str(raw.get("entity_type") or "LOCATION").upper())
            except ValueError:
                entity_type = EntityType.CUSTOM
            entity = WorldEntity(project_id=project.id, entity_type=entity_type, name=name[:200], profile={"boundary": name, "description": raw.get("description", ""), "rules": raw.get("rules") or [], "auto_director": True})
            db.add(entity); db.flush(); world_ids.append(entity.id)
        if not world_ids:
            entity = WorldEntity(project_id=project.id, entity_type=EntityType.LOCATION, name="故事起点", profile={"auto_director": True})
            db.add(entity); db.flush(); world_ids.append(entity.id)
        protagonist = direction.get("protagonist") or {}
        character_specs = [protagonist, *(direction.get("main_characters") or [])]
        character_ids: list[str] = []
        for position, raw in enumerate(character_specs):
            name = str(raw.get("name") or ("主角" if position == 0 else f"主要角色{position}"))[:200]
            character = Character(project_id=project.id, name=name, profile={"role": raw.get("role", "PROTAGONIST" if position == 0 else "SUPPORTING"), "auto_director": True}, goals={"current": raw.get("desire") or direction.get("premise") or "推动故事继续"}, current_state={"location_id": world_ids[0]}, personality={"secret": raw.get("secret", "")}, narrative_relevance={"score": 5 if position == 0 else 2})
            db.add(character); db.flush(); character_ids.append(character.id)
        thread_specs = list(direction.get("story_threads") or [])
        thread_specs.insert(0, {"title": direction.get("core_conflict") or "主线冲突", "goal": direction.get("premise") or "推进主线", "weight": 5.0, "type": "MAIN"})
        threads: list[StoryThread] = []
        for index, raw in enumerate(thread_specs):
            raw = raw if isinstance(raw, dict) else {"title": raw}
            title = str(raw.get("title") or "剧情线程")[:200]
            thread_item = StoryThread(project_id=project.id, title=title, type=str(raw.get("type") or ("MAIN" if index == 0 else "SUB"))[:100], weight=float(raw.get("weight") or (5.0 if index == 0 else 2.0)), goal=str(raw.get("goal") or direction.get("premise") or "推进故事"), state={"auto_director": True})
            db.add(thread_item)
            threads.append(thread_item)
        arc = StoryArc(project_id=project.id, title=str(direction.get("title") or "第一幕")[:200], core_question=str(direction.get("core_conflict") or "主角要付出什么代价？"), core_conflict=str(direction.get("core_conflict") or "目标与代价"), status="ACTIVE")
        db.add(arc)
        fact_specs = list(direction.get("world_facts") or [])
        fact_specs.insert(0, {"proposition": direction.get("premise") or project.story_seed or "故事从这里开始", "fact_type": "WORLD_FACT"})
        facts: list[CanonFact] = []
        for raw in fact_specs:
            raw = raw if isinstance(raw, dict) else {"proposition": raw}
            proposition = str(raw.get("proposition") or "").strip()
            if not proposition:
                continue
            try:
                fact_type = CanonType(str(raw.get("fact_type") or "WORLD_FACT").upper())
            except ValueError:
                fact_type = CanonType.WORLD_FACT
            fact = CanonFact(project_id=project.id, fact_type=fact_type, proposition=proposition, data={"auto_director": True, "world_boundary_ids": world_ids}, locked=False)
            db.add(fact)
            facts.append(fact)
        # The runtime needs one concrete, inspectable starting point even when
        # a direction supplied only abstract world rules. This is a reference
        # to author-provided material, not a newly invented canon event.
        primary_world = db.get(WorldEntity, world_ids[0])
        if primary_world:
            primary_world.profile = {
                **(primary_world.profile or {}),
                "inspectable": str(primary_world.profile.get("description") or direction.get("premise") or project.story_seed or "故事的既定开端"),
                "auto_director_runtime_anchor": True,
            }
        db.flush()
        main_thread = threads[0]
        first_fact = facts[0]
        run.context = {**context, "foundation": {"world_entity_ids": world_ids, "character_ids": character_ids, "thread_id": main_thread.id, "thread_ids": [item.id for item in threads], "arc_id": arc.id, "canon_fact_id": first_fact.id, "canon_fact_ids": [item.id for item in facts]}}

        foundation_step = self._step(db, run, AutoDirectorStage.CAST_PREPARATION, {"direction": direction})
        if foundation_step.status != AutoDirectorStepStatus.COMMITTED:
            self._commit_step(foundation_step, {"character_ids": character_ids, "thread_ids": [item.id for item in threads], "thread_id": main_thread.id, "arc_id": arc.id}, main_thread.id)
        world_step = self._step(db, run, AutoDirectorStage.WORLD_BUILDING, {"direction_fingerprint": fingerprint(direction), "world_entity_ids": world_ids})
        if world_step.status != AutoDirectorStepStatus.COMMITTED:
            self._commit_step(world_step, {"world_entity_ids": world_ids, "canon_fact_ids": [item.id for item in facts], "canon_fact_id": first_fact.id}, first_fact.id)

    def _seed_runtime_context(self, db: Session, run: AutoDirectorRun, project: Project, chapter_number: int, planning_task: dict[str, Any]) -> None:
        """Make existing direction and task facts available to a paused resolver."""
        context = run.context or {}
        seeded = dict(context.get("runtime_context_seeded") or {})
        if seeded.get(str(chapter_number)):
            return
        direction = context.get("selected_direction") or {}
        boundaries = [str(item) for item in direction.get("world_boundaries") or [] if str(item).strip()]
        proposition = "；".join(part for part in [
            str(direction.get("premise") or "").strip(),
            f"本章目标：{planning_task.get('objective')}" if planning_task.get("objective") else "",
            f"本章冲突：{planning_task.get('conflict')}" if planning_task.get("conflict") else "",
            f"既定世界边界：{'；'.join(boundaries[:3])}" if boundaries else "",
        ] if part)[:4000]
        fact = CanonFact(
            project_id=project.id,
            fact_type=CanonType.WORLD_FACT,
            proposition=proposition or f"第 {chapter_number} 章从已确认的书级设定继续。",
            data={"auto_director": True, "global_world_rule": True, "chapter_number": chapter_number, "purpose": "RUNTIME_CONTEXT_RECOVERY"},
            locked=False,
        )
        db.add(fact)
        world_ids = ((context.get("foundation") or {}).get("world_entity_ids") or [])
        runtime_entity = db.get(WorldEntity, world_ids[0]) if world_ids else None
        if runtime_entity:
            runtime_entity.profile = {
                **(runtime_entity.profile or {}),
                "inspectable": proposition or runtime_entity.name,
                "auto_director_runtime_anchor": True,
            }
        seeded[str(chapter_number)] = fact.id
        run.context = {**context, "runtime_context_seeded": seeded}
        db.flush()

    @staticmethod
    def _resume_runtime_after_auto_context_seed(db: Session, runtime: AutonomousWorldLoopService, autonomous) -> None:
        """Rebase only an untouched runtime after this orchestrator adds canon."""
        sequence, world_fingerprint = runtime._fingerprint(db, autonomous.project_id)
        history_fingerprint = runtime._history_fingerprint(db, autonomous.project_id)
        if autonomous.committed_scene_count == 0:
            autonomous.start_world_fingerprint = world_fingerprint
            autonomous.start_history_fingerprint = history_fingerprint
        autonomous.current_world_fingerprint = world_fingerprint
        autonomous.current_history_fingerprint = history_fingerprint
        paused_step = db.scalar(select(AutonomousWorldStep).where(
            AutonomousWorldStep.run_id == autonomous.id,
            AutonomousWorldStep.scene_id.is_(None),
        ).order_by(AutonomousWorldStep.ordinal.desc()))
        if paused_step and paused_step.status.value == "PAUSED":
            paused_step.world_fingerprint_before = world_fingerprint
            proposal = db.get(SceneProposal, paused_step.proposal_id) if paused_step.proposal_id else None
            performance = db.get(ScenePerformance, paused_step.performance_id) if paused_step.performance_id else None
            if proposal:
                if not proposal.location_id:
                    anchor = db.scalar(select(WorldEntity).where(
                        WorldEntity.project_id == autonomous.project_id,
                        WorldEntity.active.is_(True),
                    ).order_by(WorldEntity.created_at, WorldEntity.id))
                    if anchor:
                        proposal.location_id = anchor.id
                from .director import DirectorContextBuilder
                proposal.context_fingerprint = DirectorContextBuilder().build(db, autonomous.project_id)["fingerprint"]
            if performance and proposal:
                performance.proposal_context_fingerprint = proposal.context_fingerprint
            # The LLM has already reported that the supplied information was
            # insufficient. Use the newly materialized structured anchor for
            # this one persisted resolution rather than asking the same
            # question again; character decisions remain model-driven.
            autonomous.resolver_mode = ResolverMode.HEURISTIC
        if enum_value(autonomous.status) == "BLOCKED" and autonomous.stop_reason == "AUTONOMY_BASELINE_CHANGED" and autonomous.committed_scene_count == 0:
            autonomous.status = AutonomousRunStatus.PAUSED
            autonomous.active = True
            autonomous.stop_reason = "WORLD_INFORMATION_MISSING"
        db.flush()
        runtime.resume(db, autonomous.id)

    def _plan_and_first_chapter(self, db: Session, run: AutoDirectorRun, project: Project) -> None:
        direction = run.context["selected_direction"]
        framing = dict(run.context.get("request") or {})
        # Book length defines the macro plan. max_chapters only defines how
        # many finished chapters this unattended run should produce.
        framing.update({"target_chapters": int(framing.get("target_chapters") or 10), "target_words_per_chapter": int(framing.get("target_words_per_chapter") or project.target_chapter_words), "pov": framing.get("pov") or "THIRD_PERSON_LIMITED"})
        step = self._step(db, run, AutoDirectorStage.CHAPTER_PLANNING, {"direction": direction, "framing": framing})
        if step.status != AutoDirectorStepStatus.COMMITTED:
            self._check_budget(run)
            settings = get_settings(); route = ModelRouter().resolve(db, project.id, settings, "DIRECTOR")
            live_session = self._live_begin(run, "自动导演：生成整书规划", "STORY_PLAN", provider=route.provider, model=route.model)
            def update_plan_phase(phase: str, message: str) -> None:
                live_execution_broker.phase(live_session, phase, message)
            try:
                generated, result = generate_plan(db, project, framing, direction.get("premise"), {"tone": direction.get("style_advice", "")}, {"forbidden_patterns": []}, on_phase=update_plan_phase, on_delta=lambda content: live_execution_broker.append(live_session, content))
                self._live_complete(live_session, "整书主线、卷纲、故事弧和首批章节任务已生成")
            except Exception as exc:
                self._live_fail(live_session, exc)
                raise
            generated.setdefault("framing", framing); generated.setdefault("premise", direction.get("premise")); generated.setdefault("style_guide", {"tone": direction.get("style_advice", "")}); generated.setdefault("anti_ai_rules", {"forbidden_patterns": []})
            errors = validate_plan_references(db, project.id, generated)
            if errors: raise AutoDirectorError("PLAN_REFERENCE_INVALID", str(errors))
            plan = persist_plan(db, project, generated, provider=result.provider, model=result.model, request_id=result.request_id, report={"auto_director": True, "latency_ms": result.latency_ms})
            plan.status = StoryPlanStatus.APPROVED
            for planned_chapter in db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id)).all():
                planned_chapter.locked = True
            db.flush(); self._commit_step(step, {"plan_id": plan.id}, plan.id, calls=1, latency_ms=result.latency_ms, tokens=self._model_tokens(result), usage=self._model_usage(result), provider=result.provider, model=result.model); self._add_usage(run, step)
            run.context = {**(run.context or {}), "plan_id": plan.id}
            run.current_stage = AutoDirectorStage.STORY_MACRO
            run.next_action = "整书规划已保存；正在准备卷纲、章节任务和第一章世界运行。"
            return
        plan_id = step.output_payload["plan_id"]
        plan = db.get(StoryPlan, plan_id)
        if not plan:
            raise AutoDirectorError("STORY_PLAN_NOT_FOUND")
        run.context = {**(run.context or {}), "plan_id": plan_id}
        macro_step = self._step(db, run, AutoDirectorStage.STORY_MACRO, {"plan_id": plan_id, "macro_plan": plan.macro_plan or {}})
        if macro_step.status != AutoDirectorStepStatus.COMMITTED:
            self._commit_step(macro_step, {"plan_id": plan_id, "macro_plan": (plan.macro_plan or {})})
        volume_count = db.scalar(select(func.count(StoryPlanVolume.id)).where(StoryPlanVolume.plan_id == plan_id)) or 0
        volume_step = self._step(db, run, AutoDirectorStage.VOLUME_PLANNING, {"plan_id": plan_id, "volume_count": volume_count})
        if volume_step.status != AutoDirectorStepStatus.COMMITTED:
            self._commit_step(volume_step, {"plan_id": plan_id, "volume_count": volume_count})
        detail_step = self._step(db, run, AutoDirectorStage.CHAPTER_DETAIL, {"plan_id": plan_id, "chapter_number": int((run.context or {}).get("current_chapter_number", 1))})
        if detail_step.status != AutoDirectorStepStatus.COMMITTED:
            self._commit_step(detail_step, {"plan_id": plan_id, "chapter_count": db.scalar(select(func.count(StoryPlanChapter.id)).where(StoryPlanChapter.plan_id == plan_id)) or 0})
        chapter_number = int((run.context or {}).get("current_chapter_number", 1))
        plan_chapter = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan_id, StoryPlanChapter.number == chapter_number))
        if not plan_chapter:
            plan_chapter, detail_result = self._generate_next_chapter_task(db, run, project, plan, chapter_number, framing)
            detail_step = self._step(db, run, AutoDirectorStage.CHAPTER_DETAIL, {"plan_id": plan_id, "chapter_number": chapter_number, "task_id": plan_chapter.id})
            if detail_step.status != AutoDirectorStepStatus.COMMITTED:
                self._commit_step(detail_step, {"plan_id": plan_id, "chapter_number": chapter_number, "chapter_task_id": plan_chapter.id}, plan_chapter.id, calls=1, latency_ms=detail_result.latency_ms, tokens=self._model_tokens(detail_result), usage=self._model_usage(detail_result), provider=detail_result.provider, model=detail_result.model)
                self._add_usage(run, detail_step)
        # The runtime owns formal scenes and chapter formation. Do not create a
        # placeholder Chapter here; SceneCommitService will form it from the
        # committed scene and preserve the existing structure invariants.
        planning_task = chapter_task_context(db, project.id, chapter_number, required=True)
        existing_chapter = db.scalar(select(Chapter).where(Chapter.project_id == project.id, Chapter.number == chapter_number, Chapter.active.is_(True)))
        # A recovered chapter that already has a committed scene can proceed
        # to Writer. A brand-new chapter retains the configured scene budget
        # so its formal chapter projection is complete.
        max_scene_count = max(1, NarrativeStructureConfig.resolve(project).chapter_max_scenes)
        existing_scene_count = len(existing_chapter.source_scene_ids or []) if existing_chapter else 0
        scene_count = 1 if existing_scene_count else max(1, max_scene_count - 1)
        scene_step = None
        autonomous_run_ids = []
        if run.status != AutoDirectorRunStatus.RUNNING or (run.context or {}).get("stop_requested"):
            run.next_action = "已在场景检查点暂停，继续后恢复下一场景。"
            return
        scene_events: list[dict[str, Any]] = []
        def collect_scene_usage(event: dict[str, Any]) -> None:
            scene_events.append(dict(event))
        runtime_request_id = f"{run.id}-chapter-{chapter_number}"
        existing_autonomous = db.scalar(select(__import__("app.models", fromlist=["AutonomousWorldRun"]).AutonomousWorldRun).where(
            __import__("app.models", fromlist=["AutonomousWorldRun"]).AutonomousWorldRun.project_id == project.id,
            __import__("app.models", fromlist=["AutonomousWorldRun"]).AutonomousWorldRun.client_request_id == runtime_request_id,
        ))
        autonomous = existing_autonomous or AutonomousWorldLoopService().create_run(db, project.id, scene_budget=scene_count, max_turns_per_scene=2, performance_mode="LLM", resolver_mode="LLM", config={"auto_director_run_id": run.id, "plan_id": plan_id, "plan_chapter_id": plan_chapter.id, "chapter_number": chapter_number, "planning_task": planning_task}, client_request_id=runtime_request_id)
        db.flush()
        autonomous_run_ids.append(autonomous.id)
        runtime_live = self._live_begin(run, f"第 {chapter_number} 章：角色 Agent 与世界运行", "WORLD_RUNTIME")
        live_execution_broker.phase(runtime_live, "AGENT_ACTING", "角色 Agent 正在根据目标、记忆和世界边界行动")
        try:
            runtime = AutonomousWorldLoopService()
            # A resolver may correctly decline an action when a legacy project
            # has only prose boundaries. Supply a canonical runtime anchor and
            # resume the same persisted scene once; do not regenerate it.
            paused_runtime_step = db.scalar(select(AutonomousWorldStep).where(
                AutonomousWorldStep.run_id == autonomous.id,
                AutonomousWorldStep.scene_id.is_(None),
            ).order_by(AutonomousWorldStep.ordinal.desc()))
            runtime_needs_context = (
                enum_value(autonomous.status) == "PAUSED"
                and autonomous.stop_reason in {"WORLD_INFORMATION_MISSING", "AUTONOMY_SCENE_FAILED"}
                and paused_runtime_step is not None
                and paused_runtime_step.stop_reason == "WORLD_INFORMATION_MISSING"
            )
            if runtime_needs_context:
                self._seed_runtime_context(db, run, project, chapter_number, planning_task)
                self._resume_runtime_after_auto_context_seed(db, runtime, autonomous)
            elif enum_value(autonomous.status) == "BLOCKED" and autonomous.stop_reason == "AUTONOMY_BASELINE_CHANGED" and int(autonomous.committed_scene_count or 0) == 0:
                self._resume_runtime_after_auto_context_seed(db, runtime, autonomous)
            result = runtime.advance(db, autonomous.id, max_scenes=scene_count, request_key=f"{run.id}-chapter-{chapter_number}", request_offset=0, usage_collector=collect_scene_usage)
            if result.get("run", {}).get("stop_reason") == "WORLD_INFORMATION_MISSING":
                self._seed_runtime_context(db, run, project, chapter_number, planning_task)
                self._resume_runtime_after_auto_context_seed(db, runtime, autonomous)
                result = runtime.advance(db, autonomous.id, max_scenes=scene_count, request_key=f"{run.id}-chapter-{chapter_number}", request_offset=0, usage_collector=collect_scene_usage)
            self._live_complete(runtime_live, "角色行动与世界裁定已提交，正在形成章节")
        except Exception as exc:
            self._live_fail(runtime_live, exc)
            raise
        if scene_events:
            metrics = {"calls": sum(int(item.get("calls", 0) or 0) for item in scene_events), "prompt_tokens": sum(int(item.get("prompt_tokens", 0) or 0) for item in scene_events), "completion_tokens": sum(int(item.get("completion_tokens", 0) or 0) for item in scene_events), "total_tokens": sum(int(item.get("total_tokens", 0) or 0) for item in scene_events), "latency_ms": sum(int(item.get("latency_ms", 0) or 0) for item in scene_events), "provider": ",".join(sorted({str(item.get("provider")) for item in scene_events if item.get("provider")})), "model": ",".join(sorted({str(item.get("model")) for item in scene_events if item.get("model")}))}
            self._merge_step_usage(detail_step, metrics)
            self._add_usage_metrics(run, metrics)
        committed_scene_steps = [item for item in result.get("steps", []) if item.get("scene_id")]
        scene_step = committed_scene_steps[-1] if committed_scene_steps else None
        if len(committed_scene_steps) < scene_count:
            raise AutoDirectorError("CHAPTER_SCENE_NOT_COMMITTED", str(result))
        run.context = {**(run.context or {}), "autonomous_run_ids": [*(run.context or {}).get("autonomous_run_ids", []), *autonomous_run_ids]}
        chapter = db.scalar(select(Chapter).where(Chapter.project_id == project.id, Chapter.number == chapter_number, Chapter.active.is_(True)))
        if not chapter:
            raise AutoDirectorError("CHAPTER_FORMATION_FAILED")
        run.current_chapter_id = chapter.id
        run.current_stage = AutoDirectorStage.CHAPTER_EXECUTION
        writer_step = self._step(db, run, AutoDirectorStage.CHAPTER_EXECUTION, {"chapter_id": chapter.id, "plan_id": plan_id})
        if writer_step.status != AutoDirectorStepStatus.COMMITTED:
            self._check_budget(run)
            settings = get_settings(); route = ModelRouter().resolve(db, project.id, settings, "WRITER")
            key = ProviderCredentialResolver().generation_key(db, project.id, settings)
            pov_character_id = (run.context.get("foundation") or {}).get("character_ids", [None])[0]
            writer_provider = _UsageTrackingProvider(get_model_provider(settings, route.provider, route.base_url, key))
            writer_live = self._live_begin(run, f"第 {chapter_number} 章：正文写作", "WRITER", provider=route.provider, model=route.model)
            live_execution_broker.phase(writer_live, "WRITING", "Writer 正在依据角色 Agent 行动和章节任务生成正文")
            try:
                draft = WriterProjectionService().render(db, chapter.id, {"idempotency_key": f"{run.id}-chapter-{chapter_number}", "pov_mode": "THIRD_PERSON_LIMITED", "pov_character_id": pov_character_id}, provider=writer_provider, model=route.model, settings=settings)
                self._live_complete(writer_live, "正文草稿已生成，正在进入质量审计")
            except Exception as exc:
                self._live_fail(writer_live, exc)
                raise
            if draft.status not in {WriterDraftStatus.VALIDATED, WriterDraftStatus.ADOPTED}:
                raise AutoDirectorError("WRITER_DRAFT_FAILED", str(draft.validation_report))
            metrics = self._tracked_metrics(writer_provider)
            self._commit_step(writer_step, {"draft_id": draft.id, "chapter_id": chapter.id}, draft.id, **metrics); self._add_usage(run, writer_step)
        draft_id = writer_step.output_payload["draft_id"]
        run.current_stage = AutoDirectorStage.QUALITY_REVIEW
        quality_step = self._step(db, run, AutoDirectorStage.QUALITY_REVIEW, {"draft_id": draft_id})
        if quality_step.status != AutoDirectorStepStatus.COMMITTED:
            self._check_budget(run)
            settings = get_settings(); route = ModelRouter().resolve(db, project.id, settings, "CRITIC")
            key = ProviderCredentialResolver().generation_key(db, project.id, settings)
            draft = db.get(ChapterWriterDraft, draft_id)
            if not draft:
                raise AutoDirectorError("WRITER_DRAFT_NOT_FOUND")
            critic_provider = _UsageTrackingProvider(get_model_provider(settings, route.provider, route.base_url, key))
            critic_live = self._live_begin(run, f"第 {chapter_number} 章：质量审计", "CRITIC", provider=route.provider, model=route.model)
            live_execution_broker.phase(critic_live, "REVIEWING", "质量 Agent 正在审计任务覆盖、连续性和正文问题")
            try:
                assessment = QualityGateService().assess(db, chapter.id, {"idempotency_key": f"{run.id}-quality-{chapter_number}"}, provider=critic_provider, model=route.model, settings=settings, draft=draft, require_current=False)
                self._live_complete(critic_live, f"质量审计完成：{enum_value(assessment.status)}")
            except Exception as exc:
                self._live_fail(critic_live, exc)
                raise
            metrics = self._tracked_metrics(critic_provider)
            self._commit_step(quality_step, {"assessment_id": assessment.id, "status": enum_value(assessment.status)}, assessment.id, **metrics); self._add_usage(run, quality_step)
        assessment = db.get(ChapterQualityAssessment, quality_step.output_payload["assessment_id"])
        if assessment and enum_value(assessment.status) == "REPAIR_REQUIRED":
            run.current_stage = AutoDirectorStage.QUALITY_REPAIR
            repair_step = self._step(db, run, AutoDirectorStage.QUALITY_REPAIR, {"assessment_id": assessment.id})
            if repair_step.status != AutoDirectorStepStatus.COMMITTED:
                repair_count = sum(1 for item in self._steps(db, run) if item.stage == AutoDirectorStage.QUALITY_REPAIR and item.status == AutoDirectorStepStatus.COMMITTED)
                if repair_count >= int((run.settings or {}).get("max_repairs", 0) or 0):
                    self._commit_step(repair_step, {"skipped": True, "debt": "MAX_REPAIRS_REACHED"})
                    run.context = {**(run.context or {}), "quality_debts": [*(run.context or {}).get("quality_debts", []), "MAX_REPAIRS_REACHED"]}
                else:
                    try:
                        self._check_budget(run)
                        settings = get_settings(); repair_route = ModelRouter().resolve(db, project.id, settings, "REPAIR"); critic_route = ModelRouter().resolve(db, project.id, settings, "CRITIC"); key = ProviderCredentialResolver().generation_key(db, project.id, settings)
                        repair_provider = _UsageTrackingProvider(get_model_provider(settings, repair_route.provider, repair_route.base_url, key))
                        repair_critic_provider = _UsageTrackingProvider(get_model_provider(settings, critic_route.provider, critic_route.base_url, key))
                        draft, repaired = QualityRepairService().repair(db, assessment.id, {"idempotency_key": f"{run.id}-chapter-{chapter_number}-repair-{repair_count + 1}"}, repair_provider=repair_provider, repair_model=repair_route.model, critic_provider=repair_critic_provider, critic_model=critic_route.model, settings=settings)
                        metrics = self._tracked_metrics(repair_provider, repair_critic_provider)
                        self._commit_step(repair_step, {"draft_id": draft.id, "assessment_id": repaired.id if repaired else None}, draft.id, **metrics); self._add_usage(run, repair_step)
                    except Exception as repair_exc:
                        self._commit_step(repair_step, {"skipped": True, "debt": "REPAIR_FAILED", "error": str(repair_exc)[:500]})
                        run.context = {**(run.context or {}), "quality_debts": [*(run.context or {}).get("quality_debts", []), "REPAIR_FAILED"]}
        run.current_stage = AutoDirectorStage.AUTHOR_ADOPTION
        run.next_action = "质量门通过后自动采用正文并准备下一章。"
        if assessment and enum_value(assessment.status) == "PASS":
            self.adopt_chapter(db, run)
        elif assessment and enum_value(assessment.status) == "REPAIR_REQUIRED":
            repaired_step = self._steps(db, run)
            repair_payload = next((item.output_payload or {} for item in reversed(repaired_step) if item.stage == AutoDirectorStage.QUALITY_REPAIR and item.status == AutoDirectorStepStatus.COMMITTED and item.output_payload and item.output_payload.get("draft_id")), None)
            repaired_assessment = db.get(ChapterQualityAssessment, repair_payload.get("assessment_id")) if repair_payload else None
            if repaired_assessment and enum_value(repaired_assessment.status) == "PASS":
                run.context = {**(run.context or {}), "adopt_draft_id": repair_payload["draft_id"]}
                self.adopt_chapter(db, run)
            else:
                self._record_quality_debt_and_advance(run)

    @staticmethod
    def _record_quality_debt_and_advance(run: AutoDirectorRun) -> None:
        """Keep an imperfect draft visible without stalling the book queue."""
        debt = "QUALITY_GATE_NOT_PASS"
        context = run.context or {}
        existing_debts = [str(item) for item in context.get("quality_debts", [])]
        if debt not in existing_debts:
            existing_debts.append(debt)
        number = int(context.get("current_chapter_number", 1))
        generated_this_run = int(context.get("generated_this_run", 0)) + 1
        generated_chapters = [*context.get("generated_chapters", [])]
        if number not in generated_chapters:
            generated_chapters.append(number)
        configured_budget = int((run.settings or {}).get("max_chapters") or (run.settings or {}).get("operational_run_chapter_budget") or 10)
        run.context = {**context, "quality_debts": existing_debts, "generated_this_run": generated_this_run, "generated_chapters": generated_chapters, "current_chapter_number": number + 1, "current_chapter_id": None}
        run.current_chapter_id = None
        if generated_this_run >= configured_budget:
            run.status = AutoDirectorRunStatus.PAUSED
            run.current_stage = AutoDirectorStage.NEXT_CHAPTER
            run.pause_reason = "RUN_CHAPTER_BUDGET_REACHED"
            run.next_action = f"本次运行已生成 {generated_this_run} 章；第 {number} 章保留质量债务，继续运行可推进下一章。"
            run.context = {**run.context, "run_budget_reached": True}
        else:
            run.status = AutoDirectorRunStatus.RUNNING
            run.current_stage = AutoDirectorStage.NEXT_CHAPTER
            run.pause_reason = None
            run.next_action = f"第 {number} 章保留质量债务，正在准备第 {number + 1} 章。"

    def adopt_chapter(self, db: Session, run: AutoDirectorRun) -> AutoDirectorRun:
        draft_id = (run.context.get("adopt_draft_id") or self._latest_draft_id(db, run))
        if not draft_id: raise AutoDirectorError("DRAFT_NOT_FOUND")
        draft = db.get(ChapterWriterDraft, draft_id)
        assessment = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.writer_draft_id == draft_id).order_by(ChapterQualityAssessment.version.desc()))
        if not draft or not assessment:
            raise AutoDirectorError("AUTO_ADOPTION_LINEAGE_MISSING")
        AutoDirectorAdoptionService().adopt(db, draft, assessment)
        from .quality import QualityRepairService
        if enum_value(draft.origin) == "QUALITY_REPAIR": QualityRepairService().adopt(db, draft_id)
        else:
            assessment.active = True
            WriterProjectionService().adopt(db, draft_id)
            QualityGateService().approve(db, assessment.id)
        number = int((run.context or {}).get("current_chapter_number", 1))
        prior_completed = [*(run.context or {}).get("completed_chapters", [])]
        completed = list(prior_completed)
        if number not in completed: completed.append(number)
        adopted_this_run = int((run.context or {}).get("adopted_this_run", 0)) + (0 if number in prior_completed else 1)
        run.context = {**(run.context or {}), "completed_chapters": completed, "adopted_chapters": int((run.context or {}).get("adopted_chapters", 0)) + (0 if number in prior_completed else 1), "adopted_this_run": adopted_this_run, "last_adopted_chapter_number": number, "last_adopted_chapter_id": draft.chapter_id, "last_adopted_draft_id": draft.id}
        if run.run_mode == "FULL_AUTO" and (run.context or {}).get("volume_id"):
            from .models import VolumeContract, VolumeContractStatus
            volume = db.get(VolumeContract, run.context["volume_id"])
            if not volume or volume.status == VolumeContractStatus.SEALED:
                raise AutoDirectorError("VOLUME_SEALED")
            volume.actual_chapter_start = volume.actual_chapter_start or number
            volume.actual_chapter_end = max(int(volume.actual_chapter_end or 0), number)
        # A chapter budget is an operational checkpoint for this worker run,
        # never evidence that the book itself has ended. The next request can
        # resume from the persisted chapter number without creating a duplicate.
        configured_budget = int((run.settings or {}).get("max_chapters") or (run.settings or {}).get("operational_run_chapter_budget") or 10)
        if adopted_this_run >= configured_budget:
            run.status = AutoDirectorRunStatus.PAUSED
            run.current_stage = AutoDirectorStage.NEXT_CHAPTER
            run.pause_reason = "RUN_CHAPTER_BUDGET_REACHED"
            run.next_action = f"本次运行已完成 {adopted_this_run} 章；继续运行以推进第 {number + 1} 章。"
            run.context = {**run.context, "current_chapter_number": number + 1, "run_budget_reached": True}
            run.current_chapter_id = None
        else:
            run.status = AutoDirectorRunStatus.RUNNING; run.current_stage = AutoDirectorStage.NEXT_CHAPTER; run.context = {**run.context, "current_chapter_number": number + 1}; run.next_action = f"正在准备第 {number + 1} 章。"; run.current_chapter_id = None
        return run

    def _generate_next_chapter_task(self, db: Session, run: AutoDirectorRun, project: Project, plan: StoryPlan, chapter_number: int, framing: dict[str, Any]) -> tuple[StoryPlanChapter, Any]:
        """Extend a long plan just-in-time with a real director-model task sheet."""
        settings = get_settings()
        route = ModelRouter().resolve(db, project.id, settings, "DIRECTOR")
        key = ProviderCredentialResolver().generation_key(db, project.id, settings)
        provider = get_model_provider(settings, route.provider, route.base_url, key)
        volume = db.scalar(select(StoryPlanVolume).where(StoryPlanVolume.plan_id == plan.id, StoryPlanVolume.start_chapter <= chapter_number, StoryPlanVolume.end_chapter >= chapter_number))
        arc = db.scalar(select(StoryPlanArc).where(StoryPlanArc.plan_id == plan.id, StoryPlanArc.volume_number == (volume.number if volume else 1)).order_by(StoryPlanArc.number))
        previous = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id, StoryPlanChapter.number == chapter_number - 1))
        contract = {"chapter": {"number": chapter_number, "volume_number": "integer", "arc_number": "integer", "title": "string", "summary": "string", "pov_mode": "string", "objective": "string", "conflict": "string", "start_state": "object", "end_state": "object", "must_events": ["string"], "forbidden_events": ["string"], "allowed_reveals": ["string"], "forbidden_reveals": ["string"], "foreshadow_create": ["string"], "foreshadow_payoff": ["string"], "character_changes": ["string"], "consequences": ["string"], "scene_beats": ["string"]}}
        context = {"book": plan.macro_plan or {}, "volume": {"number": volume.number, "title": volume.title, "summary": volume.summary, "theme": volume.theme, "core_question": volume.core_question, "major_conflict": volume.major_conflict, "main_thread": volume.main_thread, "ending_turn": volume.ending_turn} if volume else {}, "arc": {"number": arc.number, "title": arc.title, "goal": arc.goal, "summary": arc.summary} if arc else {}, "previous_chapter": {"title": previous.title, "summary": previous.summary, "end_state": previous.end_state} if previous else {}, "direction": {"premise": (run.context.get("selected_direction") or {}).get("premise"), "core_conflict": (run.context.get("selected_direction") or {}).get("core_conflict")}}
        messages = [{"role": "system", "content": "你是长篇小说分章策划编辑。"}, {"role": "user", "content": json.dumps({"instruction": f"只输出一个 JSON 对象。为第 {chapter_number} 章生成可执行章节任务单，必须延续已给出的全书、卷纲、故事弧和上一章后果。所有字段都必须具体，禁止空字段、模板话和正文。", "output_contract": contract, "context": context}, ensure_ascii=False)}]
        result = provider.generate(messages, route.model)
        try:
            parsed = _extract_single_json_object(result.content)
            raw = parsed.get("chapter") if isinstance(parsed, dict) else None
            if not isinstance(raw, dict):
                raise ValueError("CHAPTER_TASK_MISSING")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AutoDirectorError("CHAPTER_TASK_CONTRACT_FAILED", str(exc)) from exc
        task = _default_chapter(chapter_number, framing, volume.number if volume else 1, arc.number if arc else 1, raw)
        task["number"] = chapter_number
        task["volume_number"] = volume.number if volume else int(task.get("volume_number") or 1)
        task["arc_number"] = arc.number if arc else int(task.get("arc_number") or 1)
        row = StoryPlanChapter(project_id=project.id, plan_id=plan.id, **task)
        db.add(row); db.flush()
        return row, result

    @staticmethod
    def _latest_draft_id(db: Session, run: AutoDirectorRun) -> str | None:
        chapter_id = run.current_chapter_id
        draft = db.scalar(select(ChapterWriterDraft).where(ChapterWriterDraft.chapter_id == chapter_id).order_by(ChapterWriterDraft.version.desc())) if chapter_id else None
        return draft.id if draft else None
