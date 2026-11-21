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
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    AuthorGuidance, AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage,
    AutoDirectorStep, AutoDirectorStepStatus,
    BookCompletionProposal, BookCompletionProposalStatus, BookContract,
    Chapter, ChapterPlanningWindow, ChapterWindowStatus, ForeshadowingLedger,
    ForeshadowingStatus, Project, VolumeContract, VolumeContractStatus,
    VolumeContinuitySnapshot, StoryPlan, StoryPlanChapter, StoryPlanStatus,
    Character, CharacterKnowledge, KnowledgeStatus, PlanChapterStatus,
)
from .planning import _default_chapter, persist_plan, validate_plan_references


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:40]


def _enum(value: Any) -> str:
    return getattr(value, "value", value)


def _merge_items(*groups: Any) -> list[Any]:
    """Combine author constraints without duplicating structured event specs."""
    result, seen = [], set()
    for group in groups:
        values = group.splitlines() if isinstance(group, str) else group if isinstance(group, list) else []
        for value in values:
            value = value.strip() if isinstance(value, str) else value
            if not value:
                continue
            identity = _fingerprint(value)
            if identity not in seen:
                seen.add(identity); result.append(value)
    return result


def safe_author_style(style: dict[str, Any] | None) -> dict[str, Any]:
    """Only prose style crosses from an author contract into Writer context.

    Source documents, plot outlines and arbitrary contract metadata stay in
    the planning domain even when they were stored alongside style settings.
    """
    style = style or {}
    safe = {key: style[key] for key in ("tone", "pov", "audience", "genre") if isinstance(style.get(key), str)}
    for key in ("style_samples", "forbidden_content"):
        safe[key] = [item for item in _merge_items(style.get(key)) if isinstance(item, str)]
    return safe


def _private_plot_text(contract: BookContract, volume: VolumeContract) -> list[str]:
    values = [contract.ending_direction]
    for reveal in volume.forbidden_reveals or []:
        if isinstance(reveal, dict):
            values.extend(reveal.get(key) for key in ("secret", "answer", "description", "proposition"))
        elif isinstance(reveal, str):
            values.append(reveal)
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def _current_text(value: Any, private: list[str], fallback: str) -> str:
    text = str(value or "").strip()
    return text if text and not any(fragment in text for fragment in private) else fallback


def _forbidden_reveal_refs(values: list[Any]) -> list[Any]:
    """Keep constraint identities without disclosing their private answers."""
    refs = []
    for value in values:
        if isinstance(value, dict) and (value.get("id") or value.get("foreshadow_ref") or value.get("constraint_ref")):
            refs.append({key: value[key] for key in ("id", "foreshadow_ref", "constraint_ref", "allowed_reveal_level") if value.get(key) is not None})
        elif isinstance(value, str):
            try:
                UUID(value)
                refs.append(value)
                continue
            except ValueError:
                pass
            refs.append({"constraint_ref": f"author-private:{_fingerprint(value)}"})
        else:
            refs.append({"constraint_ref": f"author-private:{_fingerprint(value)}"})
    return refs


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

    @staticmethod
    def _lock_volume(db: Session, volume: VolumeContract) -> VolumeContract:
        locked = db.scalar(
            select(VolumeContract)
            .where(VolumeContract.id == volume.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not locked:
            raise AuthorGuidedVolumeError("VOLUME_NOT_FOUND")
        return locked

    @staticmethod
    def _task_text(task: StoryPlanChapter) -> str:
        values = [task.title, task.summary, task.objective, task.conflict]
        values.extend(task.must_events or [])
        values.extend(task.character_changes or [])
        return " ".join(str(item) for item in values if item).lower()

    def analyze_impact(self, db: Session, project_id: str, volume: VolumeContract, *, note: str, scope: str = "WINDOW", character: Character | None = None) -> dict[str, Any]:
        """Return a conservative impact report without touching written history."""
        active_windows = db.scalars(select(ChapterPlanningWindow).where(
            ChapterPlanningWindow.project_id == project_id,
            ChapterPlanningWindow.volume_id == volume.id,
            ChapterPlanningWindow.status.in_([ChapterWindowStatus.PLANNING, ChapterWindowStatus.ACTIVE, ChapterWindowStatus.EXTENDING]),
        ).order_by(ChapterPlanningWindow.start_chapter_number)).all()
        tasks = db.scalars(select(StoryPlanChapter).where(
            StoryPlanChapter.project_id == project_id,
            StoryPlanChapter.number >= (active_windows[0].start_chapter_number if active_windows else 1),
        ).order_by(StoryPlanChapter.number)).all()
        adopted_numbers = set(db.scalars(select(Chapter.number).where(
            Chapter.project_id == project_id,
            Chapter.active.is_(True),
            Chapter.status.in_(["QUALITY_APPROVED", "ADOPTED"]),
        )).all())
        locked_numbers = {task.number for task in tasks if task.locked or task.status == PlanChapterStatus.LOCKED}
        scope_upper = scope.upper()
        needle = (note or "").lower()
        affected = []
        protected = []
        for task in tasks:
            if task.number in adopted_numbers or task.number in locked_numbers:
                protected.append({"chapter_number": task.number, "reason": "ADOPTED" if task.number in adopted_numbers else "AUTHOR_LOCKED"})
                continue
            matches = not needle or any(token in self._task_text(task) for token in needle.split() if len(token) > 1)
            if scope_upper in {"BOOK", "MAINLINE", "VOLUME"} or matches or any(task.number in range(w.start_chapter_number, w.end_chapter_number + 1) for w in active_windows):
                affected.append({"chapter_number": task.number, "task_id": task.id, "window_id": next((w.id for w in active_windows if w.start_chapter_number <= task.number <= w.end_chapter_number), None), "replan_required": True})
        character_ids = [character.id] if character else []
        text_blob = f"{note} {character.name if character else ''}".lower()
        foreshadowings = db.scalars(select(ForeshadowingLedger).where(ForeshadowingLedger.project_id == project_id)).all()
        affected_foreshadowings = [item.id for item in foreshadowings if any(token in f"{item.title} {item.description or ''}".lower() for token in text_blob.split() if len(token) > 1)]
        future_volumes = db.scalars(select(VolumeContract).where(VolumeContract.project_id == project_id, VolumeContract.volume_number >= volume.volume_number, VolumeContract.status != VolumeContractStatus.SEALED).order_by(VolumeContract.volume_number)).all()
        return {"affected_volume_ids": [item.id for item in future_volumes], "affected_chapters": affected, "protected_chapters": protected, "affected_character_ids": character_ids, "affected_foreshadowing_ids": affected_foreshadowings, "affected_timeline": [{"volume_id": item.id, "volume_number": item.volume_number} for item in future_volumes], "requires_replan": bool(affected) and (scope_upper in {"BOOK", "MAINLINE", "VOLUME"} or bool(character)), "reason": "只标记未写、未锁定章节；已采用正文和封存卷保持不变。"}

    def record_guidance(self, db: Session, volume: VolumeContract, payload: dict[str, Any], *, character: Character | None = None) -> AuthorGuidance:
        analysis = self.analyze_impact(db, volume.project_id, volume, note=payload["author_note"], scope=payload.get("affected_scope", "WINDOW"), character=character)
        guidance = AuthorGuidance(project_id=volume.project_id, volume_id=volume.id, author_note=payload["author_note"], author_locked_constraints=payload.get("author_locked_constraints") or [], author_override_reason=payload.get("author_override_reason"), affected_scope=payload.get("affected_scope", "WINDOW"), requires_replan=bool(payload.get("requires_replan", False) or analysis["requires_replan"]), analysis=analysis, status="PENDING_REPLAN" if analysis["requires_replan"] else "APPLIED")
        db.add(guidance); db.flush()
        return guidance

    def add_character(self, db: Session, project_id: str, volume: VolumeContract, payload: dict[str, Any]) -> tuple[Character, AuthorGuidance]:
        existing = db.scalar(select(Character).where(Character.project_id == project_id, Character.name == payload["name"], Character.active.is_(True)))
        if existing:
            return existing, self.record_guidance(db, volume, {"author_note": f"保留人物：{existing.name}", "affected_scope": "VOLUME"}, character=existing)
        values = {key: payload.get(key) for key in ("profile", "personality", "goals", "abilities", "relationships", "secrets")}
        character = Character(project_id=project_id, name=payload["name"], **{key: (value or {}) if key in {"profile", "personality", "goals", "relationships"} else (value or []) for key, value in values.items()}, narrative_relevance={"source": "AUTHOR_GUIDANCE", "first_appearance": {"volume": volume.volume_number}})
        db.add(character); db.flush()
        guidance = self.record_guidance(db, volume, {"author_note": f"新增人物：{character.name}", "affected_scope": "VOLUME", "requires_replan": True}, character=character)
        return character, guidance

    def update_plot_direction(self, db: Session, contract: BookContract, volume: VolumeContract, payload: dict[str, Any]) -> AuthorGuidance:
        if volume.status == VolumeContractStatus.SEALED:
            raise AuthorGuidedVolumeError("VOLUME_SEALED")
        contract.global_plot_direction = payload["global_plot_direction"]
        contract.version += 1
        contract.fingerprint = _fingerprint(_contract_payload(contract))
        data = {"author_note": payload.get("author_note") or "作者修改全书剧情走向", "affected_scope": "MAINLINE", "author_locked_constraints": payload.get("author_locked_constraints") or [], "requires_replan": True}
        return self.record_guidance(db, volume, data)

    def update_volume_contract(self, db: Session, volume: VolumeContract, payload: dict[str, Any]) -> AuthorGuidance:
        if volume.status == VolumeContractStatus.SEALED:
            raise AuthorGuidedVolumeError("VOLUME_SEALED")
        mutable = ("title", "volume_goal", "core_conflict", "target_closing_state", "completion_conditions", "required_events", "forbidden_events", "allowed_reveals", "forbidden_reveals")
        for key in mutable:
            if key in payload and payload[key] is not None:
                setattr(volume, key, payload[key])
        volume.version += 1
        volume.fingerprint = _fingerprint(_volume_payload(volume))
        return self.record_guidance(db, volume, {"author_note": payload["author_note"], "affected_scope": "VOLUME", "requires_replan": True})

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
        length.setdefault("continuous_chapter_progression", bool(payload.get("continuous_chapter_progression", True)))
        style = dict(payload.get("style_contract") or {})
        for key in ("tone", "pov", "audience", "genre"):
            if key not in style and payload.get(key) is not None:
                style[key] = payload[key]
        style["style_samples"] = _merge_items(style.get("style_samples"), payload.get("style_samples"))
        style["forbidden_content"] = _merge_items(style.get("forbidden_content"), payload.get("forbidden_content"))
        style.setdefault("input_mode", payload.get("mode", "inspiration"))
        if payload.get("source_material"):
            style.setdefault("source_material", payload["source_material"])
        values = {
            "project_id": project.id, "title": payload.get("title") or project.name, "theme": payload.get("theme"), "premise": payload.get("premise") or payload.get("inspiration") or payload.get("source_material") or project.story_seed,
            "ending_direction": payload.get("ending_direction"), "protagonist_contract": payload.get("protagonist_contract") or {}, "global_plot_direction": payload.get("global_plot_direction"),
            "global_required_events": payload.get("global_required_events") or [], "global_forbidden_events": _merge_items(payload.get("global_forbidden_events"), style.get("forbidden_content")), "style_contract": style,
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
        volume = VolumeContract(project_id=project.id, book_contract_id=contract.id, volume_number=number, title=payload.get("title") or f"第 {number} 卷", status=VolumeContractStatus.ACTIVE if number == 1 else VolumeContractStatus.DRAFT, estimated_chapter_start=payload.get("estimated_chapter_start") or (1 if number == 1 else None), estimated_chapter_end=payload.get("estimated_chapter_end"), volume_goal=payload.get("volume_goal") or "推进本卷核心目标并改变主角状态。", core_conflict=payload.get("core_conflict") or "当前目标与外部阻力形成冲突", opening_state=payload.get("opening_state") or {}, target_closing_state=payload.get("target_closing_state") or {}, completion_conditions=payload.get("completion_conditions") or [], protagonist_arc=payload.get("protagonist_arc") or contract.protagonist_contract, required_events=payload.get("required_events") or [], forbidden_events=payload.get("forbidden_events") or [], allowed_reveals=payload.get("allowed_reveals") or [], forbidden_reveals=payload.get("forbidden_reveals") or [], next_volume_hooks=payload.get("next_volume_hooks") or [])
        volume.fingerprint = _fingerprint(_volume_payload(volume)); db.add(volume); db.flush()
        return volume

    def next_chapter_number(self, db: Session, project_id: str) -> int:
        # Planning windows are a rolling horizon, not consumed chapter
        # numbers. A volume can be sealed before its preplanned tail is written;
        # the next volume must continue after the last actual chapter instead
        # of jumping over that abandoned horizon.
        maximum = db.scalar(select(Chapter.number).where(Chapter.project_id == project_id).order_by(Chapter.number.desc()).limit(1))
        return int(maximum or 0) + 1

    def ensure_window(self, db: Session, project: Project, volume: VolumeContract, *, author_note: str | None = None, size: int | None = None) -> ChapterPlanningWindow:
        existing = db.scalar(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id, ChapterPlanningWindow.status.in_([ChapterWindowStatus.PLANNING, ChapterWindowStatus.ACTIVE, ChapterWindowStatus.EXTENDING])).order_by(ChapterPlanningWindow.created_at.desc()))
        if existing:
            self._assert_source_snapshot_current(db, volume)
            return existing
        source_snapshot_id = self._assert_source_snapshot_current(db, volume)
        stale_source_window = db.scalar(select(ChapterPlanningWindow).where(
            ChapterPlanningWindow.volume_id == volume.id,
            ChapterPlanningWindow.status == ChapterWindowStatus.STALE,
            ChapterPlanningWindow.error_code == "SOURCE_VOLUME_SNAPSHOT_CHANGED",
        ).order_by(ChapterPlanningWindow.created_at.desc()).limit(1))
        if stale_source_window:
            # Resume the same chapter range, with fresh planning input and a
            # new plan revision, after its previous volume was resealed.
            stale_source_window.source_volume_snapshot_id = source_snapshot_id
            stale_source_window.status = ChapterWindowStatus.ACTIVE
            stale_source_window.author_note = author_note or stale_source_window.author_note
            stale_source_window.plan_fingerprint = _fingerprint({
                "volume": volume.fingerprint,
                "start": stale_source_window.start_chapter_number,
                "end": stale_source_window.end_chapter_number,
                "author_note": stale_source_window.author_note,
                "source_snapshot": source_snapshot_id,
            })
            stale_source_window.error_code = None
            stale_source_window.error_summary = None
            db.flush()
            return stale_source_window
        start = self.next_chapter_number(db, project.id)
        end = start + max(1, min(int(size or self.WINDOW_SIZE), 10)) - 1
        source_snapshot_id = volume.sealed_snapshot_id or source_snapshot_id
        window = ChapterPlanningWindow(project_id=project.id, volume_id=volume.id, start_chapter_number=start, end_chapter_number=end, status=ChapterWindowStatus.ACTIVE, author_note=author_note, plan_fingerprint=_fingerprint({"volume": volume.fingerprint, "start": start, "end": end, "author_note": author_note, "source_snapshot": source_snapshot_id}), source_volume_snapshot_id=source_snapshot_id)
        db.add(window); db.flush()
        return window

    def ensure_followup_window(self, db: Session, project: Project, volume: VolumeContract, previous: ChapterPlanningWindow, *, size: int | None = None) -> ChapterPlanningWindow:
        # A near-complete window may already have created the next bounded
        # window. Reuse that exact contiguous range before looking at the
        # largest planned chapter number; otherwise its own tasks make the
        # allocator skip the whole range (for example 6–10 -> 11–15).
        expected_start = previous.end_chapter_number + 1
        source_snapshot_id = volume.sealed_snapshot_id or self._assert_source_snapshot_current(db, volume)
        existing = db.scalar(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id, ChapterPlanningWindow.start_chapter_number == expected_start).order_by(ChapterPlanningWindow.created_at.desc()))
        if existing and existing.status != ChapterWindowStatus.COMPLETED:
            existing.source_volume_snapshot_id = source_snapshot_id
            existing.status = ChapterWindowStatus.ACTIVE
            existing.error_code = None
            existing.error_summary = None
            return existing
        # The normal path is the contiguous next range. Only move beyond it
        # when another already persisted chapter/plan truly occupies that
        # range and there is no matching bounded window to resume.
        start = max(expected_start, self.next_chapter_number(db, project.id))
        end = start + max(1, min(int(size or self.WINDOW_SIZE), 10)) - 1
        window = ChapterPlanningWindow(project_id=project.id, volume_id=volume.id, start_chapter_number=start, end_chapter_number=end, status=ChapterWindowStatus.ACTIVE, author_note=previous.author_note, plan_fingerprint=_fingerprint({"volume": volume.fingerprint, "start": start, "end": end, "source_snapshot": source_snapshot_id}), source_volume_snapshot_id=source_snapshot_id)
        db.add(window); db.flush()
        return window

    def ensure_window_tasks(self, db: Session, project: Project, volume: VolumeContract, window: ChapterPlanningWindow, contract: BookContract) -> list[str]:
        marker = f"author_window:{window.id}"
        plans = db.scalars(select(StoryPlan).where(StoryPlan.project_id == project.id).order_by(StoryPlan.version.desc())).all()
        existing = next((
            item for item in plans
            if (item.generation_report or {}).get("author_window_id") == window.id
            and (item.generation_report or {}).get("author_window_source_snapshot_id") == window.source_volume_snapshot_id
        ), None)
        if existing:
            return [item.id for item in db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == existing.id)).all()]
        continuity = self.continuity_context(db, volume)
        private = _private_plot_text(contract, volume)
        goal = _current_text(volume.volume_goal, private, "推进当前卷目标")
        conflict = _current_text(volume.core_conflict, private, "外部阻力迫使主角选择")
        style = safe_author_style(contract.style_contract)
        style["forbidden_content"] = [_current_text(item, private, "不得提前揭示后续剧情真相。") for item in style["forbidden_content"]]
        style["style_samples"] = [item for item in style["style_samples"] if not any(fragment in item for fragment in private)]
        forbidden = _merge_items(contract.global_forbidden_events, style["forbidden_content"], volume.forbidden_events)
        forbidden = [_current_text(item, private, "不得提前揭示后续剧情真相。") if isinstance(item, str) else item for item in forbidden]
        # This block is for author/planner operations only. Neither the
        # character task projection nor Writer receives raw source documents,
        # future book events, or the ending contract.
        planning_input = {
            "input_mode": (contract.style_contract or {}).get("input_mode", "inspiration"),
            "source_material": (contract.style_contract or {}).get("source_material", ""),
            "book": {"title": contract.title, "theme": contract.theme, "premise": contract.premise, "global_plot_direction": contract.global_plot_direction, "ending_direction": contract.ending_direction, "global_required_events": contract.global_required_events or [], "global_forbidden_events": contract.global_forbidden_events or [], "author_locked_constraints": contract.author_locked_constraints or []},
            "volume": _volume_payload(volume), "author_note": window.author_note,
        }
        framing = {"target_words_per_chapter": project.target_chapter_words, "pov": style.get("pov", "THIRD_PERSON_LIMITED"), "author_window_id": window.id, "continuity_constraints": continuity, "author_planning_context": planning_input}
        chapters = [{"number": number, "volume_number": volume.volume_number, "arc_number": 1, "title": f"第{number}章", "summary": goal, "objective": f"{goal}，并形成可验证的后果", "conflict": conflict, "must_events": volume.required_events or [], "forbidden_events": forbidden, "allowed_reveals": (volume.allowed_reveals or []) + continuity["touchable_foreshadowings"], "forbidden_reveals": _forbidden_reveal_refs((volume.forbidden_reveals or []) + continuity["forbidden_to_reveal"]), "foreshadow_create": [], "foreshadow_payoff": continuity["payoffable_foreshadowings"], "scene_beats": ["建立本章局势", "形成关键选择", "留下可验证后果"], "locked": False} for number in range(window.start_chapter_number, window.end_chapter_number + 1)]
        plan = persist_plan(db, project, {"framing": framing, "premise": contract.premise, "macro_plan": {"author_window": marker, "volume_goal": goal, "theme": contract.theme}, "style_guide": style, "anti_ai_rules": {}, "volumes": [{"number": volume.volume_number, "title": volume.title or f"第 {volume.volume_number} 卷", "summary": goal, "theme": contract.theme or "", "major_conflict": conflict, "start_chapter": window.start_chapter_number, "end_chapter": window.end_chapter_number}], "chapters": chapters}, report={"author_guided": True, "author_window_id": window.id, "author_window_source_snapshot_id": window.source_volume_snapshot_id}, archive_latest=False)
        plan.status = StoryPlanStatus.APPROVED
        window.plan_fingerprint = plan.source_fingerprint
        window.actual_generated_count = len(chapters)
        window.status = ChapterWindowStatus.ACTIVE
        db.flush()
        return [item.id for item in db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id)).all()]

    def refine_window_plan(self, db: Session, run: AutoDirectorRun, project: Project, contract: BookContract, volume: VolumeContract, window: ChapterPlanningWindow, plan: StoryPlan) -> str:
        """Use the existing planner once, on the worker, before a window runs."""
        from . import auto_director as auto_module

        orchestrator = auto_module.AutoDirectorOrchestrator()
        identity = {"window_id": window.id, "contract": contract.fingerprint, "volume": volume.fingerprint, "author_note": window.author_note}
        input_fp = _fingerprint(identity)
        if (plan.generation_report or {}).get("semantic_planning", {}).get("input_fingerprint") == input_fp:
            return "READY"
        tasks = db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id).order_by(StoryPlanChapter.number)).all()
        written = {item.number for item in db.scalars(select(Chapter).where(Chapter.project_id == project.id, Chapter.active.is_(True))).all() if item.content or item.current_writer_draft_id or _enum(item.status) in {"ADOPTED", "QUALITY_APPROVED"}}
        mutable = {task.number: task for task in tasks if window.start_chapter_number <= task.number <= window.end_chapter_number and not task.locked and _enum(task.status) != "LOCKED" and task.number not in written}
        if not mutable:
            return "READY"
        private = _private_plot_text(contract, volume)
        style = safe_author_style(contract.style_contract)
        style["style_samples"] = [item for item in style["style_samples"] if not any(fragment in item for fragment in private)]
        style["forbidden_content"] = [_current_text(item, private, "不得提前揭示后续剧情真相。") for item in style["forbidden_content"]]
        count = window.end_chapter_number - window.start_chapter_number + 1
        framing = {
            "target_chapters": count, "target_words_per_chapter": project.target_chapter_words,
            "pov": style.get("pov", "THIRD_PERSON_LIMITED"), "style_samples": style["style_samples"],
            "forbidden_content": _merge_items(contract.global_forbidden_events, style["forbidden_content"], volume.forbidden_events),
            "author_planning_context": {"book": _contract_payload(contract), "volume": _volume_payload(volume), "source_material": (contract.style_contract or {}).get("source_material", ""), "input_mode": (contract.style_contract or {}).get("input_mode", "inspiration"), "author_note": window.author_note, "continuity": self.continuity_context(db, volume)},
            "window": {"absolute_start": window.start_chapter_number, "absolute_end": window.end_chapter_number, "mutable_chapters": sorted(mutable)},
            "window_instructions": "只细化当前卷当前窗口；以局部章号 1 到 target_chapters 返回任务，系统映射为全书章号。全书章数只是预估，不安排提前完结。保留作者书籍/卷合同、素材、人物、文风与禁用内容。结局和未授权秘密仅供策划约束，不得写入当前章任务、状态、事件或节拍。每章必须有具体标题、摘要、目标、冲突和场景节拍；已写或作者锁定章节不可改写。",
            "protected_chapters": [{"number": task.number, "title": task.title, "summary": task.summary, "end_state": task.end_state} for task in tasks if task.number not in mutable],
        }
        prior = db.scalars(select(Chapter).where(Chapter.project_id == project.id, Chapter.active.is_(True), Chapter.number < window.start_chapter_number).order_by(Chapter.number.desc()).limit(3)).all()
        framing["previous_chapters"] = [{"number": item.number, "title": item.title, "ending_excerpt": (item.content or "")[-1500:]} for item in reversed(prior)]
        step, live_session, recorded = None, None, False

        def record_usage(result: Any) -> None:
            nonlocal recorded
            usage = orchestrator._model_usage(result)
            metrics = {**usage, "calls": 1, "latency_ms": int(getattr(result, "latency_ms", 0) or 0), "provider": getattr(result, "provider", None), "model": getattr(result, "model", None)}
            previous = step.token_usage or {}
            cumulative = {key: int(previous.get(key, 0) or 0) + usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
            orchestrator._commit_step(step, {"window_id": window.id, "volume_id": volume.id}, plan.id, calls=int(previous.get("calls", 0) or 0) + 1, latency_ms=int(previous.get("latency_ms", 0) or 0) + metrics["latency_ms"], tokens=cumulative["total_tokens"], usage=cumulative, provider=metrics["provider"], model=metrics["model"])
            orchestrator._add_usage_metrics(run, metrics)
            recorded = True

        try:
            orchestrator._check_budget(run)
            step = orchestrator._step(db, run, AutoDirectorStage.CHAPTER_WINDOW_PLANNING, identity)
            run.current_stage = AutoDirectorStage.CHAPTER_WINDOW_PLANNING
            live_session = orchestrator._live_begin(run, f"第 {volume.volume_number} 卷：细化当前章节窗口", "AUTHOR_WINDOW_PLANNING")
            generated, result = auto_module.generate_plan(db, project, framing, contract.premise or project.story_seed, style, {}, on_phase=lambda phase, message: auto_module.live_execution_broker.phase(live_session, phase, message), on_delta=lambda text: auto_module.live_execution_broker.append(live_session, text))
            if result is None:
                raise AuthorGuidedVolumeError("AUTHOR_WINDOW_PLAN_MODEL_REQUIRED", "模型未返回窗口规划；请重试，不会采用占位任务写作。")
            record_usage(result)
            rows = generated.get("chapters") if isinstance(generated, dict) else None
            if not isinstance(rows, list) or not rows or len(rows) > count or any(not isinstance(row, dict) for row in rows):
                raise AuthorGuidedVolumeError("AUTHOR_WINDOW_PLAN_INCOMPLETE", "模型需要返回当前窗口内的完整章节任务。")
            numbers = [int(row.get("number", 0)) for row in rows]
            if len(set(numbers)) != len(numbers):
                raise AuthorGuidedVolumeError("AUTHOR_WINDOW_PLAN_NUMBER_INVALID", "章节号重复。")
            if all(window.start_chapter_number <= number <= window.end_chapter_number for number in numbers):
                mapped = numbers
            elif all(1 <= number <= count for number in numbers):
                mapped = [window.start_chapter_number + number - 1 for number in numbers]
            else:
                raise AuthorGuidedVolumeError("AUTHOR_WINDOW_PLAN_NUMBER_INVALID", "模型章节号超出当前窗口。")
            if not set(mutable).issubset(mapped):
                raise AuthorGuidedVolumeError("AUTHOR_WINDOW_PLAN_INCOMPLETE", "模型遗漏了当前窗口内的待写章节。")
            private = _private_plot_text(contract, volume)
            updates = []
            for raw, number in zip(rows, mapped):
                if number not in mutable:
                    continue
                if any(not str(raw.get(key) or "").strip() for key in ("title", "summary", "objective", "conflict")) or not isinstance(raw.get("scene_beats"), list) or not raw["scene_beats"]:
                    raise AuthorGuidedVolumeError("AUTHOR_WINDOW_PLAN_INCOMPLETE", "每个待写章节都需要具体标题、摘要、目标、冲突和场景节拍。")
                task = mutable[number]
                candidate = {**raw, "number": number, "volume_number": volume.volume_number, "locked": False, "status": "READY"}
                candidate["forbidden_events"] = _merge_items(task.forbidden_events, raw.get("forbidden_events"))
                candidate["forbidden_reveals"] = _forbidden_reveal_refs(_merge_items(task.forbidden_reveals, raw.get("forbidden_reveals")))
                candidate["must_events"] = _merge_items(volume.required_events, raw.get("must_events"))
                candidate["allowed_reveals"] = _merge_items(task.allowed_reveals, raw.get("allowed_reveals"))
                normalized = _default_chapter(number, framing, volume.volume_number, raw=candidate)
                visible = {key: value for key, value in normalized.items() if key != "forbidden_reveals"}
                if any(fragment in json.dumps(visible, ensure_ascii=False) for fragment in private):
                    raise AuthorGuidedVolumeError("AUTHOR_WINDOW_PLAN_REVEAL_LEAK", "窗口规划提前包含了结局或尚未授权的秘密，请重试或修改作者指导。")
                updates.append((task, normalized))
            if validate_plan_references(db, project.id, {"chapters": [row for _, row in updates]}):
                raise AuthorGuidedVolumeError("AUTHOR_WINDOW_PLAN_REFERENCE_INVALID", "章节规划的人物引用类型不正确。")
            for task, normalized in updates:
                for key, value in normalized.items():
                    if key not in {"number", "locked", "status"}:
                        setattr(task, key, value)
                task.status = PlanChapterStatus.READY
            # Use the author's style contract rather than model-generated
            # metadata that can contain future plot details.
            plan.style_guide = style
            plan.source_fingerprint = _fingerprint({"window_input": input_fp, "chapters": [row for _, row in updates], "style": plan.style_guide})
            plan.generation_report = {**(plan.generation_report or {}), "semantic_planning": {"input_fingerprint": input_fp, "updated_chapters": sorted(mutable), "provider": getattr(result, "provider", None), "model": getattr(result, "model", None), "request_id": getattr(result, "request_id", None)}}
            window.plan_fingerprint = plan.source_fingerprint
            window.error_code = None; window.error_summary = None
            step.output_payload = {**(step.output_payload or {}), "updated_chapters": sorted(mutable), "plan_id": plan.id}
            orchestrator._live_complete(live_session, "当前窗口已吸收作者资料并生成具体章节任务")
            run.status = AutoDirectorRunStatus.RUNNING
            run.pause_reason = None
            run.current_stage = AutoDirectorStage.CHAPTER_WINDOW_EXECUTION
            run.next_action = "当前章节窗口规划已保存，下一检查点开始角色行动与正文生产。"
            return "PLANNED"
        except Exception as exc:
            code = getattr(exc, "code", None) or "AUTHOR_WINDOW_PLANNING_FAILED"
            window.error_code = str(code); window.error_summary = str(exc)[:1000]
            if live_session:
                orchestrator._live_fail(live_session, exc)
            if code == "TOKEN_BUDGET_EXCEEDED":
                run.status = AutoDirectorRunStatus.PAUSED; run.current_stage = AutoDirectorStage.CHAPTER_WINDOW_PLANNING; run.pause_reason = str(code); run.next_action = "增加本次 token 预算后继续窗口规划。"
            elif step is not None:
                if not recorded:
                    record_usage(exc)
                orchestrator._fail(run, step, exc)
                run.next_action = "当前窗口尚未完成语义规划；可重试，已写和已锁定章节保持原样。"
            else:
                raise
            return "FAILED"

    def continuity_context(self, db: Session, volume: VolumeContract) -> dict[str, Any]:
        rows = db.scalars(select(ForeshadowingLedger).where(ForeshadowingLedger.project_id == volume.project_id, ForeshadowingLedger.status.not_in([ForeshadowingStatus.PAID_OFF, ForeshadowingStatus.CANCELLED]))).all()
        touchable, payable, forbidden = [], [], []
        for item in rows:
            source = {"id": item.id, "foreshadow_ref": item.foreshadow_ref, "title": item.title, "source_volume_id": item.source_volume_id, "source_chapter_id": item.source_chapter_id, "allowed_reveal_level": item.allowed_reveal_level}
            if item.status == ForeshadowingStatus.FORBIDDEN_TO_REVEAL or (item.earliest_payoff_volume and volume.volume_number < item.earliest_payoff_volume):
                forbidden.append(source)
            else:
                touchable.append(source)
                if item.target_payoff_volume is None or volume.volume_number >= item.target_payoff_volume:
                    payable.append(source)
        source_snapshot_id = volume.sealed_snapshot_id or self._assert_source_snapshot_current(db, volume)
        snapshot = db.get(VolumeContinuitySnapshot, source_snapshot_id) if source_snapshot_id else db.scalar(select(VolumeContinuitySnapshot).where(VolumeContinuitySnapshot.volume_id == volume.id).order_by(VolumeContinuitySnapshot.snapshot_version.desc()))
        return {"source_snapshot_id": snapshot.id if snapshot else source_snapshot_id, "character_states": snapshot.character_states if snapshot else {}, "touchable_foreshadowings": touchable, "payoffable_foreshadowings": payable, "forbidden_to_reveal": forbidden, "unresolved_from_prior_volumes": snapshot.unresolved_foreshadowings if snapshot else []}

    @staticmethod
    def _assert_source_snapshot_current(db: Session, volume: VolumeContract) -> str | None:
        """A downstream volume may only consume its predecessor's current seal."""
        source_snapshot_id = (volume.opening_state or {}).get("source_volume_snapshot_id")
        if not source_snapshot_id:
            return None
        snapshot = db.get(VolumeContinuitySnapshot, source_snapshot_id)
        source_volume = db.get(VolumeContract, snapshot.volume_id) if snapshot else None
        if (
            not snapshot or snapshot.project_id != volume.project_id
            or not source_volume or source_volume.volume_number >= volume.volume_number
            or source_volume.status != VolumeContractStatus.SEALED
            or source_volume.sealed_snapshot_id != snapshot.id
        ):
            raise AuthorGuidedVolumeError("VOLUME_SOURCE_SNAPSHOT_STALE")
        return source_snapshot_id

    @staticmethod
    def _invalidate_downstream_windows(db: Session, volume: VolumeContract, snapshot_id: str | None, code: str) -> None:
        if not snapshot_id:
            return
        windows = db.scalars(select(ChapterPlanningWindow).where(
            ChapterPlanningWindow.project_id == volume.project_id,
            ChapterPlanningWindow.source_volume_snapshot_id == snapshot_id,
            ChapterPlanningWindow.status != ChapterWindowStatus.COMPLETED,
        )).all()
        for window in windows:
            downstream = db.get(VolumeContract, window.volume_id)
            if downstream and downstream.volume_number > volume.volume_number:
                window.status = ChapterWindowStatus.STALE
                window.error_code = code
                window.error_summary = "来源卷已解封；此窗口基于旧封存快照，需在来源卷重新封存后续用。"

    @staticmethod
    def _refresh_downstream_snapshot_references(db: Session, volume: VolumeContract, old_snapshot_id: str | None, new_snapshot_id: str) -> None:
        if not old_snapshot_id or old_snapshot_id == new_snapshot_id:
            return
        downstream_volumes = db.scalars(select(VolumeContract).where(
            VolumeContract.project_id == volume.project_id,
            VolumeContract.volume_number > volume.volume_number,
            VolumeContract.status != VolumeContractStatus.SEALED,
        ).order_by(VolumeContract.volume_number)).all()
        downstream_ids = set()
        for downstream in downstream_volumes:
            opening = dict(downstream.opening_state or {})
            if opening.get("source_volume_snapshot_id") != old_snapshot_id:
                continue
            opening["source_volume_snapshot_id"] = new_snapshot_id
            downstream.opening_state = opening
            downstream.version += 1
            downstream.fingerprint = _fingerprint(_volume_payload(downstream))
            downstream_ids.add(downstream.id)
        if not downstream_ids:
            return
        windows = db.scalars(select(ChapterPlanningWindow).where(
            ChapterPlanningWindow.volume_id.in_(downstream_ids),
            ChapterPlanningWindow.source_volume_snapshot_id == old_snapshot_id,
            ChapterPlanningWindow.status != ChapterWindowStatus.COMPLETED,
        )).all()
        for window in windows:
            # Preserve the old source id on the stale record as audit lineage;
            # ensure_window rebinds and reopens it on the next continuation.
            window.status = ChapterWindowStatus.STALE
            window.error_code = "SOURCE_VOLUME_SNAPSHOT_CHANGED"
            window.error_summary = "来源卷已使用新版本快照重新封存；此窗口需重新规划。"

    def update_foreshadowing(self, db: Session, item: ForeshadowingLedger, status: ForeshadowingStatus, *, volume_id: str | None = None, chapter_id: str | None = None) -> ForeshadowingLedger:
        if item.status == ForeshadowingStatus.PAID_OFF and status != ForeshadowingStatus.PAID_OFF:
            raise AuthorGuidedVolumeError("FORESHADOWING_ALREADY_PAID_OFF")
        item.status = status
        if volume_id:
            item.source_volume_id = item.source_volume_id or volume_id
        if chapter_id:
            item.source_chapter_id = item.source_chapter_id or chapter_id
        item.fingerprint = _fingerprint({"ref": item.foreshadow_ref, "status": _enum(status), "volume": volume_id, "chapter": chapter_id})
        return item

    def progress(self, db: Session, volume: VolumeContract) -> dict[str, Any]:
        start = volume.actual_chapter_start or volume.estimated_chapter_start or 1
        end = volume.actual_chapter_end
        query = select(Chapter).where(Chapter.project_id == volume.project_id, Chapter.number >= start, Chapter.active.is_(True))
        if end is not None:
            query = query.where(Chapter.number <= end)
        chapters = db.scalars(query.order_by(Chapter.number)).all()
        adopted = [item for item in chapters if item.status in {"QUALITY_APPROVED", "ADOPTED"} and item.content]
        incomplete = [item.number for item in chapters if not item.content or item.status not in {"QUALITY_APPROVED", "ADOPTED"}]
        required = list(volume.required_events or [])
        completed = [event for event in required if any(str(event) in (item.content or "") for item in adopted)]
        conditions = list(volume.completion_conditions or [])
        conditions_met = all(any(str(condition) in (item.content or "") for item in adopted) for condition in conditions)
        latest_number = max((item.number for item in adopted), default=None)
        latest_task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.project_id == volume.project_id, StoryPlanChapter.number == latest_number).order_by(StoryPlanChapter.created_at.desc())) if latest_number is not None else None
        target = volume.target_closing_state or {}
        end_state = latest_task.end_state if latest_task else {}
        target_reached = bool(target) and bool(adopted) and all(end_state.get(key) == value for key, value in target.items())
        ready = bool(adopted) and not incomplete and conditions_met and target_reached
        return {"volume_goal_progress": 1.0 if ready else min(0.99, len(adopted) / max(1, len(adopted) + len(incomplete) + 1)), "protagonist_arc_progress": 1.0 if ready else (0.5 if adopted else 0.0), "conflict_progress": 1.0 if ready else (0.5 if adopted else 0.0), "required_events_completed": completed, "incomplete_chapters": incomplete, "unresolved_threads": volume.unresolved_threads or [], "pending_foreshadowings": [], "target_closing_state_reached": target_reached, "should_extend_volume": not ready, "should_prepare_seal": ready, "should_start_next_volume": False, "reason": "本卷仍需继续推进，未满足剧情目标或仍有未通过质量门的章节。" if not ready else "本卷完成条件已满足，等待作者确认封存。"}

    def create_snapshot(self, db: Session, volume: VolumeContract) -> VolumeContinuitySnapshot:
        if volume.status != VolumeContractStatus.READY_TO_SEAL:
            raise AuthorGuidedVolumeError("VOLUME_NOT_READY_TO_SEAL")
        previous_version = db.scalar(select(func.max(VolumeContinuitySnapshot.snapshot_version)).where(VolumeContinuitySnapshot.volume_id == volume.id)) or 0
        snapshot_version = int(previous_version) + 1
        chapters = db.scalars(select(Chapter).where(Chapter.project_id == volume.project_id, Chapter.active.is_(True), Chapter.number >= (volume.actual_chapter_start or 1), Chapter.number <= (volume.actual_chapter_end or 2**31 - 1)).order_by(Chapter.number)).all()
        character_states = {}
        for character in db.scalars(select(Character).where(Character.project_id == volume.project_id, Character.active.is_(True))).all():
            known = db.scalars(select(CharacterKnowledge.proposition).where(CharacterKnowledge.character_id == character.id, CharacterKnowledge.status.in_([KnowledgeStatus.KNOWN, KnowledgeStatus.SUSPECTED]))).all()
            character_states[character.id] = {"character_id": character.id, "name": character.name, "profile": character.profile or {}, "goals": character.goals or {}, "current_state": character.current_state or {}, "physical_state": character.physical_state or {}, "emotional_state": character.emotional_state or {}, "relationships": character.relationships or {}, "known_facts_at_seal": list(known)}
        snapshot = VolumeContinuitySnapshot(project_id=volume.project_id, book_contract_id=volume.book_contract_id, volume_id=volume.id, snapshot_version=snapshot_version, summary=f"第 {volume.volume_number} 卷已完成，共 {len(chapters)} 章。", confirmed_facts=[], character_states=character_states, relationship_changes=[], timeline_end={"chapter": volume.actual_chapter_end}, location_states={}, item_states={}, active_threads=volume.unresolved_threads or [], unresolved_foreshadowings=[], paid_off_foreshadowings=list(volume.foreshadowing_payoff_refs or []), forbidden_future_reveals=volume.forbidden_reveals or [], next_volume_hooks=volume.next_volume_hooks or [], source_fingerprint=_fingerprint({"volume": volume.fingerprint, "snapshot_version": snapshot_version, "chapters": [item.id for item in chapters], "characters": character_states}))
        db.add(snapshot); db.flush(); return snapshot

    def seal(self, db: Session, volume: VolumeContract, author_confirmed: bool = False) -> VolumeContract:
        volume = self._lock_volume(db, volume)
        if volume.status == VolumeContractStatus.SEALED:
            return volume
        if not author_confirmed:
            raise AuthorGuidedVolumeError("AUTHOR_CONFIRMATION_REQUIRED")
        report = self.progress(db, volume)
        if not report["should_prepare_seal"]:
            raise AuthorGuidedVolumeError("VOLUME_COMPLETION_CONDITIONS_UNMET")
        previous_snapshot = db.scalar(select(VolumeContinuitySnapshot).where(
            VolumeContinuitySnapshot.volume_id == volume.id,
        ).order_by(VolumeContinuitySnapshot.snapshot_version.desc()).limit(1))
        previous_snapshot_id = previous_snapshot.id if previous_snapshot else None
        volume.status = VolumeContractStatus.READY_TO_SEAL
        snapshot = self.create_snapshot(db, volume)
        volume.status = VolumeContractStatus.SEALED; volume.sealed_at = datetime.utcnow(); volume.sealed_snapshot_id = snapshot.id
        self._refresh_downstream_snapshot_references(db, volume, previous_snapshot_id, snapshot.id)
        return volume

    def unseal(self, db: Session, volume: VolumeContract, reason: str) -> VolumeContract:
        volume = self._lock_volume(db, volume)
        if volume.status != VolumeContractStatus.SEALED:
            return volume
        if not reason.strip():
            raise AuthorGuidedVolumeError("UNSEAL_REASON_REQUIRED")
        previous_snapshot_id = volume.sealed_snapshot_id
        volume.status = VolumeContractStatus.ACTIVE
        volume.sealed_at = None; volume.sealed_snapshot_id = None
        windows = db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.volume_id == volume.id, ChapterPlanningWindow.status != ChapterWindowStatus.COMPLETED)).all()
        for window in windows: window.status = ChapterWindowStatus.STALE; window.error_code = "VOLUME_UNSEALED"
        self._invalidate_downstream_windows(db, volume, previous_snapshot_id, "SOURCE_VOLUME_UNSEALED")
        db.add(AuthorGuidance(project_id=volume.project_id, volume_id=volume.id, author_note=reason, author_override_reason=reason, affected_scope="VOLUME", requires_replan=True, analysis={"audit": "UNSEAL"}, status="AUDITED"))
        return volume

    def completion_proposal(self, db: Session, contract: BookContract, volume: VolumeContract) -> BookCompletionProposal:
        report = self.progress(db, volume)
        start = volume.actual_chapter_start or volume.estimated_chapter_start or 1
        query = select(Chapter).where(Chapter.project_id == volume.project_id, Chapter.number >= start, Chapter.active.is_(True), Chapter.content.is_not(None), Chapter.status.in_(["QUALITY_APPROVED", "ADOPTED"]))
        if volume.actual_chapter_end is not None:
            query = query.where(Chapter.number <= volume.actual_chapter_end)
        adopted = db.scalars(query.order_by(Chapter.number)).all()
        evidence_ids = [item.id for item in adopted]
        content = "\n".join(item.content or "" for item in adopted)
        global_required = [str(item) for item in contract.global_required_events or []]
        missing_global = [item for item in global_required if item not in content]
        forbidden_global = [str(item) for item in contract.global_forbidden_events or [] if str(item) in content]
        policy = contract.length_policy or {}
        allowed = policy.get("allow_completion_proposal", True)
        ready = allowed and report["should_prepare_seal"] and not volume.unresolved_threads and not missing_global and not forbidden_global
        status = BookCompletionProposalStatus.PROPOSED if ready else BookCompletionProposalStatus.NOT_READY
        reason = report["reason"] if ready else ("书籍合同的全书结局条件尚未满足。" if allowed else "当前书籍合同未允许 AI 提出完结建议。")
        proposal = BookCompletionProposal(project_id=contract.project_id, book_contract_id=contract.id, status=status, reason=reason, unresolved_threads=report["unresolved_threads"], unresolved_foreshadowings=report["pending_foreshadowings"], protagonist_arc_status={"progress": report["protagonist_arc_progress"]}, main_conflict_status={"progress": report["conflict_progress"], "missing_global_required_events": missing_global, "forbidden_global_events": forbidden_global}, ending_requirements=contract.global_required_events or [], evidence_chapter_ids=evidence_ids)
        db.add(proposal); db.flush(); return proposal

    def advance_run(self, db: Session, run: AutoDirectorRun) -> AutoDirectorRun:
        project = db.get(Project, run.project_id)
        if not project: raise AuthorGuidedVolumeError("PROJECT_NOT_FOUND")
        request = dict((run.context or {}).get("request") or run.settings or {})
        contract = self.ensure_contract(db, project, request)
        run.context = {**(run.context or {}), "book_contract_id": contract.id}
        configured_token_budget = request.get("operational_token_budget") or (run.settings or {}).get("operational_token_budget") or (contract.length_policy or {}).get("operational_token_budget") or request.get("max_tokens")
        if configured_token_budget is not None:
            run.settings = {**(run.settings or {}), "max_tokens": int(configured_token_budget), "operational_token_budget": int(configured_token_budget)}
        run.current_stage = AutoDirectorStage.VOLUME_SKELETON
        volume_id = (run.context or {}).get("volume_id")
        volume = db.get(VolumeContract, volume_id) if volume_id else None
        if volume and (volume.project_id != project.id or volume.book_contract_id != contract.id):
            raise AuthorGuidedVolumeError("VOLUME_RUN_SCOPE_MISMATCH")
        if volume is None:
            volume_number = int((run.context or {}).get("current_volume_number") or 1)
            volume = self.ensure_volume(db, project, contract, request.get("volume") or {}, volume_number)
        run.context = {**run.context, "volume_id": volume.id}
        context = dict(run.context or {})
        if context.get("window_complete") and context.get("execute_window") and context.get("window_id"):
            previous = db.get(ChapterPlanningWindow, context["window_id"])
            if previous and previous.status != ChapterWindowStatus.COMPLETED:
                previous.status = ChapterWindowStatus.COMPLETED
                previous.completed_at = datetime.utcnow()
            source_snapshot_id = self._assert_source_snapshot_current(db, volume)
            planned_next = db.get(ChapterPlanningWindow, context.get("next_window_id")) if context.get("next_window_id") else None
            expected_start = previous.end_chapter_number + 1 if previous else None
            if (planned_next and previous and planned_next.volume_id == volume.id
                    and planned_next.start_chapter_number == expected_start
                    and planned_next.status not in {ChapterWindowStatus.COMPLETED, ChapterWindowStatus.STALE}
                    and planned_next.source_volume_snapshot_id == source_snapshot_id):
                window = planned_next
            else:
                window = self.ensure_followup_window(db, project, volume, previous, size=request.get("window_size")) if previous else self.ensure_window(db, project, volume, author_note=request.get("author_note"), size=request.get("window_size"))
            run.context = {**context, "window_complete": False, "next_window_id": None, "current_chapter_number": window.start_chapter_number, "last_adopted_chapter_number": 0, "last_adopted_chapter_id": None, "last_adopted_draft_id": None}
        else:
            current_window = db.get(ChapterPlanningWindow, context.get("window_id")) if context.get("window_id") else None
            window = current_window if current_window and current_window.volume_id == volume.id and current_window.status not in {ChapterWindowStatus.COMPLETED, ChapterWindowStatus.STALE} else self.ensure_window(db, project, volume, author_note=request.get("author_note"), size=request.get("window_size"))
        window.status = ChapterWindowStatus.ACTIVE
        task_ids = self.ensure_window_tasks(db, project, volume, window, contract)
        prior_context = dict(run.context or {})
        current_number = int(prior_context.get("current_chapter_number") or window.start_chapter_number)
        if prior_context.get("window_id") != window.id:
            current_number = window.start_chapter_number
        run.context = {**prior_context, "window_id": window.id, "current_chapter_number": current_number}
        run.context = {**run.context, "window_task_ids": task_ids, "window_task_count": len(task_ids)}
        if not run.context.get("execute_window"):
            run.current_stage = AutoDirectorStage.CHAPTER_WINDOW_PLANNING
            run.status = AutoDirectorRunStatus.PAUSED
            run.pause_reason = "AUTHOR_WINDOW_READY"
            run.next_action = f"第 {volume.volume_number} 卷第 {window.start_chapter_number}-{window.end_chapter_number} 章窗口已准备，作者可继续或输入指导。"
            return run
        # The existing orchestrator owns scenes, Writer, quality, repair and
        # adoption. Author mode only supplies the bounded plan and checkpoint.
        from .auto_director import AutoDirectorOrchestrator
        plan_id = None
        for candidate in db.scalars(select(StoryPlan).where(StoryPlan.project_id == project.id).order_by(StoryPlan.version.desc())).all():
            if (candidate.generation_report or {}).get("author_window_id") == window.id:
                plan_id = candidate.id
                break
        if not plan_id:
            plan = db.scalar(select(StoryPlan).where(StoryPlan.project_id == project.id).order_by(StoryPlan.version.desc()))
            plan_id = plan.id if plan else None
        if not plan_id:
            raise AuthorGuidedVolumeError("WINDOW_PLAN_MISSING")
        db.get(StoryPlan, plan_id).status = StoryPlanStatus.APPROVED
        request = dict(run.settings or {})
        request.setdefault("target_chapters", window.end_chapter_number)
        operational_budget = int((contract.length_policy or {}).get("operational_run_chapter_budget") or request.get("operational_run_chapter_budget") or request.get("window_size") or self.WINDOW_SIZE)
        request["operational_run_chapter_budget"] = operational_budget
        # The shared orchestrator understands a cumulative chapter budget but
        # only executes one bounded window per pass. Give it the absolute
        # checkpoint for the earlier of the run budget or this window end, then
        # restore the cumulative budget for the next pass.
        current_number = int((run.context or {}).get("current_chapter_number") or window.start_chapter_number)
        remaining_window = max(0, window.end_chapter_number - current_number + 1)
        context = dict(run.context or {})
        processed_this_run = max(int(context.get("processed_this_run", 0) or 0), int(context.get("adopted_this_run", 0) or 0), int(context.get("generated_this_run", 0) or 0))
        window_checkpoint = processed_this_run + min(max(0, operational_budget - processed_this_run), remaining_window)
        request["max_chapters"] = max(1, window_checkpoint)
        token_budget = request.get("operational_token_budget") or (run.settings or {}).get("operational_token_budget") or (contract.length_policy or {}).get("operational_token_budget") or request.get("max_tokens")
        run.settings = {**request, "target_chapters": window.end_chapter_number, "max_tokens": token_budget}
        private = _private_plot_text(contract, volume)
        goal = _current_text(volume.volume_goal, private, "推进当前卷目标并改变主角状态。")
        protagonist = {key: _current_text(value, private, "") for key, value in (contract.protagonist_contract or {}).items() if key in {"name", "role", "desire", "cost"} and isinstance(value, str)}
        # Foundation/recovery can turn direction.premise into public canon.
        # Give that path only the current volume contract, never the raw
        # author outline or the future ending stored in the book contract.
        direction = {"title": contract.title or project.name, "premise": goal, "core_conflict": _current_text(volume.core_conflict, private, "外部阻力迫使主角选择"), "first_volume_goal": goal, "style_advice": (contract.style_contract or {}).get("tone", ""), "world_boundaries": [], "protagonist": protagonist}
        execution_request = {**((run.context or {}).get("request") or request), "author_window_id": window.id, "target_chapters": window.end_chapter_number, "target_words_per_chapter": project.target_chapter_words, "pov": (contract.style_contract or {}).get("pov") or "THIRD_PERSON_LIMITED"}
        context = {**(run.context or {}), "request": execution_request, "selected_direction": direction, "plan_id": plan_id, "run_start_chapter_number": window.start_chapter_number, "adopted_this_run": (run.context or {}).get("adopted_this_run", 0), "processed_this_run": processed_this_run, "run_budget_reached": False}
        run.context = context
        plan = db.get(StoryPlan, plan_id)
        if self.refine_window_plan(db, run, project, contract, volume, window, plan) != "READY":
            self._annotate_usage_scope(db, run)
            return run
        # Match the existing orchestrator's exact planning identity. A generic
        # committed step with only window_id would make it plan the book again
        # and overwrite the bounded author window.
        orchestrator = AutoDirectorOrchestrator()
        planning_step = orchestrator._step(db, run, AutoDirectorStage.CHAPTER_PLANNING, {"direction": direction, "framing": execution_request})
        if planning_step.status != AutoDirectorStepStatus.COMMITTED:
            orchestrator._commit_step(planning_step, {"plan_id": plan_id, "author_window_id": window.id, "window_id": window.id, "volume_id": volume.id}, plan_id)
        run.current_stage = AutoDirectorStage.CHAPTER_WINDOW_EXECUTION
        run.status = AutoDirectorRunStatus.RUNNING
        try:
            orchestrator.advance_to_pause(db, run)
        finally:
            persisted_settings = dict(run.settings or {})
            persisted_settings["max_chapters"] = operational_budget
            persisted_settings["operational_run_chapter_budget"] = operational_budget
            run.settings = persisted_settings
        self._annotate_usage_scope(db, run)
        adopted_number = int((run.context or {}).get("last_adopted_chapter_number", 0) or 0)
        processed_number = int((run.context or {}).get("last_processed_chapter_number", adopted_number) or adopted_number)
        if adopted_number:
            window.actual_generated_count = len(db.scalars(select(Chapter.id).where(Chapter.project_id == project.id, Chapter.number >= window.start_chapter_number, Chapter.number <= adopted_number, Chapter.active.is_(True), Chapter.content.is_not(None))).all())
            # Keep the planning horizon bounded: pre-plan only when this
            # window has two chapters remaining, not immediately after it
            # starts.
            remaining = window.end_chapter_number - adopted_number
            if window.end_chapter_number > window.start_chapter_number and 0 < remaining <= 2:
                next_window = self.ensure_followup_window(db, project, volume, window)
                future_task_ids = self.ensure_window_tasks(db, project, volume, next_window, contract)
                # Preplanning must not switch the active chapter contract or
                # make the next worker skip this window's remaining chapters.
                next_window.status = ChapterWindowStatus.PLANNING
                if future_task_ids:
                    db.get(StoryPlan, db.get(StoryPlanChapter, future_task_ids[0]).plan_id).status = StoryPlanStatus.GENERATED
                run.context = {**(run.context or {}), "next_window_id": next_window.id}
        if run.status in {AutoDirectorRunStatus.COMPLETED, AutoDirectorRunStatus.PAUSED} and processed_number >= window.end_chapter_number:
            volume.actual_chapter_start = volume.actual_chapter_start or window.start_chapter_number
            volume.actual_chapter_end = max(volume.actual_chapter_end or 0, window.end_chapter_number)
            context = dict(run.context or {})
            context["window_complete"] = True
            progress = self.progress(db, volume)
            processed_this_run = max(int(context.get("processed_this_run", 0) or 0), int(context.get("adopted_this_run", 0) or 0), int(context.get("generated_this_run", 0) or 0))
            budget_reached = processed_this_run >= operational_budget
            continuous = bool((contract.length_policy or {}).get("continuous_chapter_progression", request.get("continuous_chapter_progression", False)))
            if progress["should_prepare_seal"]:
                run.status = AutoDirectorRunStatus.PAUSED
                run.pause_reason = "WINDOW_COMPLETE"
                run.next_action = "本卷目标已满足，等待作者确认封存后继续下一卷。"
            elif budget_reached:
                run.status = AutoDirectorRunStatus.PAUSED
                run.pause_reason = "RUN_CHAPTER_BUDGET_REACHED"
                run.next_action = f"本次连续推进已处理 {processed_this_run} 章；继续后从第 {window.end_chapter_number + 1} 章接着写。"
            elif continuous:
                run.status = AutoDirectorRunStatus.RUNNING
                run.pause_reason = None
                run.next_action = f"第 {window.start_chapter_number}-{window.end_chapter_number} 章已完成，正在规划第 {window.end_chapter_number + 1} 章及后续窗口。"
            else:
                run.status = AutoDirectorRunStatus.PAUSED
                run.pause_reason = "WINDOW_COMPLETE"
                run.next_action = "当前窗口已完成，作者可继续下一批、扩展当前卷或确认封存。"
            run.current_stage = AutoDirectorStage.VOLUME_PROGRESS_ASSESSMENT
            run.context = context
            window.continuation_decision = "ASSESS_VOLUME"
        elif run.status == AutoDirectorRunStatus.RUNNING:
            run.current_stage = AutoDirectorStage.CHAPTER_WINDOW_EXECUTION
            run.next_action = "正在执行当前章节窗口。"
        return run

    @staticmethod
    def _annotate_usage_scope(db: Session, run: AutoDirectorRun) -> None:
        """Persist durable boundaries for usage projections after each checkpoint."""
        context = dict(run.context or {})
        volume_id = context.get("volume_id")
        window_id = context.get("window_id")
        windows = db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.project_id == run.project_id)).all()
        chapters = db.scalars(select(Chapter).where(Chapter.project_id == run.project_id)).all()
        chapters_by_id = {item.id: item.number for item in chapters}
        for step in db.scalars(select(AutoDirectorStep).where(AutoDirectorStep.run_id == run.id)).all():
            payload = dict(step.output_payload or {})
            chapter_number = payload.get("chapter_number")
            chapter_id = payload.get("chapter_id")
            if chapter_number is None and chapter_id:
                chapter_number = chapters_by_id.get(chapter_id)
            scoped_window_id = payload.get("author_window_id") or window_id
            if chapter_number is not None:
                for candidate in windows:
                    if candidate.start_chapter_number <= int(chapter_number) <= candidate.end_chapter_number:
                        scoped_window_id = candidate.id
                        break
            scoped_volume_id = volume_id
            if scoped_window_id:
                candidate = next((item for item in windows if item.id == scoped_window_id), None)
                if candidate:
                    scoped_volume_id = candidate.volume_id
            payload.update({"chapter_number": chapter_number, "window_id": scoped_window_id, "volume_id": scoped_volume_id})
            step.output_payload = payload
