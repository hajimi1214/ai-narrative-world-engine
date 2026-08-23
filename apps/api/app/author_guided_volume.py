"""Author-owned book and volume workflow.

This service owns durable boundaries around the existing scene/writer engine.
It intentionally creates only a bounded planning window and never interprets
an estimate as a book completion condition.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    AuthorGuidance, AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage,
    BookCompletionProposal, BookCompletionProposalStatus, BookContract,
    Chapter, ChapterPlanningWindow, ChapterWindowStatus, ForeshadowingLedger,
    ForeshadowingStatus, Project, VolumeContract, VolumeContractStatus,
    VolumeContinuitySnapshot, StoryPlan, StoryPlanChapter, StoryPlanStatus,
)
from .planning import persist_plan


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:40]


def _enum(value: Any) -> str:
    return getattr(value, "value", value)


def _contract_payload(contract: BookContract) -> dict[str, Any]:
    return {"id": contract.id, "project_id": contract.project_id, "title": contract.title, "theme": contract.theme, "premise": contract.premise, "ending_direction": contract.ending_direction, "protagonist_contract": contract.protagonist_contract or {}, "global_plot_direction": contract.global_plot_direction, "global_required_events": contract.global_required_events or [], "global_forbidden_events": contract.global_forbidden_events or [], "style_contract": contract.style_contract or {}, "author_locked_constraints": contract.author_locked_constraints or [], "length_policy": contract.length_policy or {}, "version": contract.version, "fingerprint": contract.fingerprint, "status": contract.status}


def _volume_payload(volume: VolumeContract) -> dict[str, Any]:
    return {"id": volume.id, "project_id": volume.project_id, "book_contract_id": volume.book_contract_id, "volume_number": volume.volume_number, "title": volume.title, "status": _enum(volume.status), "estimated_chapter_start": volume.estimated_chapter_start, "estimated_chapter_end": volume.estimated_chapter_end, "actual_chapter_start": volume.actual_chapter_start, "actual_chapter_end": volume.actual_chapter_end, "volume_goal": volume.volume_goal, "core_conflict": volume.core_conflict, "opening_state": volume.opening_state or {}, "target_closing_state": volume.target_closing_state or {}, "completion_conditions": volume.completion_conditions or [], "protagonist_arc": volume.protagonist_arc or {}, "main_cast": volume.main_cast or [], "new_cast": volume.new_cast or [], "required_events": volume.required_events or [], "forbidden_events": volume.forbidden_events or [], "allowed_reveals": volume.allowed_reveals or [], "forbidden_reveals": volume.forbidden_reveals or [], "foreshadowing_seed_refs": volume.foreshadowing_seed_refs or [], "foreshadowing_payoff_refs": volume.foreshadowing_payoff_refs or [], "unresolved_threads": volume.unresolved_threads or [], "next_volume_hooks": volume.next_volume_hooks or [], "version": volume.version, "fingerprint": volume.fingerprint, "sealed_at": volume.sealed_at.isoformat() if volume.sealed_at else None, "sealed_snapshot_id": volume.sealed_snapshot_id}


def _window_payload(window: ChapterPlanningWindow) -> dict[str, Any]:
    return {"id": window.id, "project_id": window.project_id, "volume_id": window.volume_id, "start_chapter_number": window.start_chapter_number, "end_chapter_number": window.end_chapter_number, "actual_generated_count": window.actual_generated_count, "status": _enum(window.status), "plan_fingerprint": window.plan_fingerprint, "source_volume_snapshot_id": window.source_volume_snapshot_id, "author_note": window.author_note, "continuation_decision": window.continuation_decision, "error_code": window.error_code, "error_summary": window.error_summary, "completed_at": window.completed_at.isoformat() if window.completed_at else None}


class AuthorGuidedVolumeError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


class AuthorGuidedVolumeService:
    WINDOW_SIZE = 5

    def ensure_contract(self, db: Session, project: Project, payload: dict[str, Any]) -> BookContract:
        contract = db.scalar(select(BookContract).where(BookContract.project_id == project.id))
        if contract:
            return contract
        length = dict(payload.get("length_policy") or {})
        estimated = length.get("estimated_chapters", payload.get("estimated_chapters"))
        if estimated is not None:
            estimated = int(estimated)
        length.setdefault("mode", "ESTIMATE_ONLY" if estimated else "OPEN_ENDED")
        length.setdefault("estimated_chapters", estimated)
        length.setdefault("estimated_volumes", payload.get("estimated_volumes"))
        length.setdefault("completion_strategy", "AUTHOR_CONFIRMATION")
        length.setdefault("operational_run_chapter_budget", payload.get("operational_run_chapter_budget"))
        length.setdefault("operational_token_budget", payload.get("operational_token_budget"))
        values = {
            "project_id": project.id, "title": payload.get("title") or project.name, "theme": payload.get("theme"), "premise": payload.get("premise") or project.story_seed,
            "ending_direction": payload.get("ending_direction"), "protagonist_contract": payload.get("protagonist_contract") or {}, "global_plot_direction": payload.get("global_plot_direction"),
            "global_required_events": payload.get("global_required_events") or [], "global_forbidden_events": payload.get("global_forbidden_events") or [], "style_contract": payload.get("style_contract") or {},
            "author_locked_constraints": payload.get("author_locked_constraints") or [], "length_policy": length, "version": 1, "status": "ACTIVE",
        }
        values["fingerprint"] = _fingerprint(values)
        contract = BookContract(**values); db.add(contract); db.flush()
        return contract

    def ensure_volume(self, db: Session, project: Project, contract: BookContract, payload: dict[str, Any] | None = None, number: int = 1) -> VolumeContract:
        volume = db.scalar(select(VolumeContract).where(VolumeContract.project_id == project.id, VolumeContract.volume_number == number))
        if volume:
            return volume
        payload = payload or {}
        volume = VolumeContract(project_id=project.id, book_contract_id=contract.id, volume_number=number, title=payload.get("title") or f"第 {number} 卷", status=VolumeContractStatus.ACTIVE, estimated_chapter_start=payload.get("estimated_chapter_start") or (1 if number == 1 else None), estimated_chapter_end=payload.get("estimated_chapter_end"), volume_goal=payload.get("volume_goal") or "推进本卷核心目标并改变主角状态。", core_conflict=payload.get("core_conflict") or contract.global_plot_direction, opening_state=payload.get("opening_state") or {}, target_closing_state=payload.get("target_closing_state") or {}, completion_conditions=payload.get("completion_conditions") or [], protagonist_arc=payload.get("protagonist_arc") or contract.protagonist_contract, required_events=payload.get("required_events") or [], forbidden_events=payload.get("forbidden_events") or [], allowed_reveals=payload.get("allowed_reveals") or [], forbidden_reveals=payload.get("forbidden_reveals") or [], next_volume_hooks=payload.get("next_volume_hooks") or [])
        volume.fingerprint = _fingerprint(_volume_payload(volume)); db.add(volume); db.flush()
        return volume

    def next_chapter_number(self, db: Session, project_id: str) -> int:
        maximum = db.scalar(select(Chapter.number).where(Chapter.project_id == project_id).order_by(Chapter.number.desc()).limit(1))
        return int(maximum or 0) + 1

    def ensure_window(self, db: Session, project: Project, volume: VolumeContract, *, author_note: str | None = None, size: int | None = None) -> ChapterPlanningWindow:
        existing = db.scalar(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id, ChapterPlanningWindow.status.in_([ChapterWindowStatus.PLANNING, ChapterWindowStatus.ACTIVE, ChapterWindowStatus.EXTENDING])).order_by(ChapterPlanningWindow.created_at.desc()))
        if existing:
            return existing
        start = self.next_chapter_number(db, project.id)
        end = start + max(1, min(int(size or self.WINDOW_SIZE), 10)) - 1
        window = ChapterPlanningWindow(project_id=project.id, volume_id=volume.id, start_chapter_number=start, end_chapter_number=end, status=ChapterWindowStatus.ACTIVE, author_note=author_note, plan_fingerprint=_fingerprint({"volume": volume.fingerprint, "start": start, "end": end, "author_note": author_note}), source_volume_snapshot_id=volume.sealed_snapshot_id)
        db.add(window); db.flush()
        return window

    def ensure_window_tasks(self, db: Session, project: Project, volume: VolumeContract, window: ChapterPlanningWindow, contract: BookContract) -> list[str]:
        marker = f"author_window:{window.id}"
        plans = db.scalars(select(StoryPlan).where(StoryPlan.project_id == project.id).order_by(StoryPlan.version.desc())).all()
        existing = next((item for item in plans if (item.generation_report or {}).get("author_window_id") == window.id), None)
        if existing:
            return [item.id for item in db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == existing.id)).all()]
        framing = {"target_words_per_chapter": project.target_chapter_words, "pov": (contract.style_contract or {}).get("pov", "THIRD_PERSON_LIMITED"), "author_window_id": window.id}
        chapters = [{"number": number, "volume_number": volume.volume_number, "arc_number": 1, "title": f"第{number}章", "summary": volume.volume_goal or "推进当前卷目标", "objective": "推进当前卷目标并形成不可逆后果", "conflict": volume.core_conflict or "外部阻力迫使主角选择", "must_events": volume.required_events or [], "forbidden_events": volume.forbidden_events or [], "allowed_reveals": volume.allowed_reveals or [], "forbidden_reveals": volume.forbidden_reveals or [], "scene_beats": ["建立本章局势", "形成关键选择", "留下可验证后果"], "locked": False} for number in range(window.start_chapter_number, window.end_chapter_number + 1)]
        plan = persist_plan(db, project, {"framing": framing, "premise": contract.premise, "macro_plan": {"author_window": marker, "volume_goal": volume.volume_goal}, "style_guide": contract.style_contract or {}, "anti_ai_rules": {}, "volumes": [{"number": volume.volume_number, "title": volume.title or f"第 {volume.volume_number} 卷", "start_chapter": window.start_chapter_number, "end_chapter": window.end_chapter_number}], "chapters": chapters}, report={"author_guided": True, "author_window_id": window.id}, archive_latest=False)
        plan.status = StoryPlanStatus.APPROVED
        window.plan_fingerprint = plan.source_fingerprint
        db.flush()
        return [item.id for item in db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id)).all()]

    def progress(self, db: Session, volume: VolumeContract) -> dict[str, Any]:
        chapters = db.scalars(select(Chapter).where(Chapter.project_id == volume.project_id, Chapter.number >= (volume.actual_chapter_start or 1), Chapter.active.is_(True)).order_by(Chapter.number)).all()
        adopted = [item for item in chapters if item.status in {"QUALITY_APPROVED", "ADOPTED"} and item.content]
        required = list(volume.required_events or [])
        completed = [event for event in required if any(str(event) in (item.content or "") for item in adopted)]
        target_reached = bool(volume.target_closing_state) and bool(adopted)
        conditions_met = not volume.completion_conditions or len(completed) >= len(volume.completion_conditions)
        ready = bool(adopted) and conditions_met and target_reached
        return {"volume_goal_progress": min(1.0, len(adopted) / max(1, len(adopted) + 1)), "protagonist_arc_progress": 1.0 if adopted else 0.0, "conflict_progress": 1.0 if adopted else 0.0, "required_events_completed": completed, "unresolved_threads": volume.unresolved_threads or [], "pending_foreshadowings": [], "target_closing_state_reached": target_reached, "should_extend_volume": not ready, "should_prepare_seal": ready, "should_start_next_volume": False, "reason": "本卷仍需继续推进。" if not ready else "本卷完成条件已满足，等待作者确认封存。"}

    def create_snapshot(self, db: Session, volume: VolumeContract) -> VolumeContinuitySnapshot:
        if volume.status != VolumeContractStatus.READY_TO_SEAL:
            raise AuthorGuidedVolumeError("VOLUME_NOT_READY_TO_SEAL")
        existing = db.scalar(select(VolumeContinuitySnapshot).where(VolumeContinuitySnapshot.volume_id == volume.id))
        if existing:
            return existing
        chapters = db.scalars(select(Chapter).where(Chapter.project_id == volume.project_id, Chapter.active.is_(True), Chapter.number >= (volume.actual_chapter_start or 1), Chapter.number <= (volume.actual_chapter_end or 2**31 - 1)).order_by(Chapter.number)).all()
        snapshot = VolumeContinuitySnapshot(project_id=volume.project_id, book_contract_id=volume.book_contract_id, volume_id=volume.id, summary=f"第 {volume.volume_number} 卷已完成，共 {len(chapters)} 章。", confirmed_facts=[], character_states={}, relationship_changes=[], timeline_end={"chapter": volume.actual_chapter_end}, location_states={}, item_states={}, active_threads=volume.unresolved_threads or [], unresolved_foreshadowings=[], paid_off_foreshadowings=list(volume.foreshadowing_payoff_refs or []), forbidden_future_reveals=volume.forbidden_reveals or [], next_volume_hooks=volume.next_volume_hooks or [], source_fingerprint=_fingerprint({"volume": volume.fingerprint, "chapters": [item.id for item in chapters]}))
        db.add(snapshot); db.flush(); return snapshot

    def seal(self, db: Session, volume: VolumeContract, author_confirmed: bool = False) -> VolumeContract:
        if volume.status == VolumeContractStatus.SEALED:
            return volume
        if not author_confirmed:
            raise AuthorGuidedVolumeError("AUTHOR_CONFIRMATION_REQUIRED")
        report = self.progress(db, volume)
        if not report["should_prepare_seal"]:
            raise AuthorGuidedVolumeError("VOLUME_COMPLETION_CONDITIONS_UNMET")
        volume.status = VolumeContractStatus.READY_TO_SEAL
        snapshot = self.create_snapshot(db, volume)
        volume.status = VolumeContractStatus.SEALED; volume.sealed_at = datetime.utcnow(); volume.sealed_snapshot_id = snapshot.id
        return volume

    def unseal(self, db: Session, volume: VolumeContract, reason: str) -> VolumeContract:
        if volume.status != VolumeContractStatus.SEALED:
            return volume
        if not reason.strip():
            raise AuthorGuidedVolumeError("UNSEAL_REASON_REQUIRED")
        volume.status = VolumeContractStatus.ACTIVE
        volume.sealed_at = None; volume.sealed_snapshot_id = None
        windows = db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id, ChapterPlanningWindow.status != ChapterWindowStatus.COMPLETED)).all()
        for window in windows: window.status = ChapterWindowStatus.STALE; window.error_code = "VOLUME_UNSEALED"
        db.add(AuthorGuidance(project_id=volume.project_id, volume_id=volume.id, author_note=reason, author_override_reason=reason, affected_scope="VOLUME", requires_replan=True, analysis={"audit": "UNSEAL"}, status="AUDITED"))
        return volume

    def completion_proposal(self, db: Session, contract: BookContract, volume: VolumeContract) -> BookCompletionProposal:
        report = self.progress(db, volume)
        status = BookCompletionProposalStatus.PROPOSED if report["should_prepare_seal"] and not volume.unresolved_threads else BookCompletionProposalStatus.NOT_READY
        proposal = BookCompletionProposal(project_id=contract.project_id, book_contract_id=contract.id, status=status, reason=report["reason"], unresolved_threads=report["unresolved_threads"], unresolved_foreshadowings=report["pending_foreshadowings"], protagonist_arc_status={"progress": report["protagonist_arc_progress"]}, main_conflict_status={"progress": report["conflict_progress"]}, ending_requirements=contract.global_required_events or [], evidence_chapter_ids=[])
        db.add(proposal); db.flush(); return proposal

    def advance_run(self, db: Session, run: AutoDirectorRun) -> AutoDirectorRun:
        project = db.get(Project, run.project_id)
        if not project: raise AuthorGuidedVolumeError("PROJECT_NOT_FOUND")
        request = dict((run.context or {}).get("request") or run.settings or {})
        contract = self.ensure_contract(db, project, request)
        run.context = {**(run.context or {}), "book_contract_id": contract.id}
        run.current_stage = AutoDirectorStage.VOLUME_SKELETON
        volume = self.ensure_volume(db, project, contract, request.get("volume") or {})
        run.context = {**run.context, "volume_id": volume.id}
        window = self.ensure_window(db, project, volume, author_note=request.get("author_note"), size=request.get("window_size"))
        task_ids = self.ensure_window_tasks(db, project, volume, window, contract)
        run.context = {**run.context, "window_id": window.id, "current_chapter_number": window.start_chapter_number}
        run.context = {**run.context, "window_task_ids": task_ids, "window_task_count": len(task_ids)}
        run.current_stage = AutoDirectorStage.CHAPTER_WINDOW_EXECUTION
        run.status = AutoDirectorRunStatus.PAUSED; run.pause_reason = "AUTHOR_WINDOW_READY"; run.next_action = f"第 {volume.volume_number} 卷第 {window.start_chapter_number}-{window.end_chapter_number} 章窗口已准备，作者可继续或输入指导。"
        return run
