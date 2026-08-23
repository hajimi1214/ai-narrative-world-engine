"""Whole-book planning service and validation for Phase 2."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai.errors import ModelProviderError
from .ai.factory import get_model_provider
from .llm_actor import _extract_single_json_object
from .model_router import ModelRouter, ProviderCredentialResolver
from .models import CanonFact, Character, Project, ProjectModelConfig, StoryPlan, StoryPlanArc, StoryPlanChapter, StoryPlanStatus, StoryPlanVolume, StoryThread
from .settings import get_settings


class PlanFraming(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inspiration: str = Field(min_length=1, max_length=8000)
    genre: str = Field(default="未定", max_length=120)
    audience: str = Field(default="成人读者", max_length=120)
    target_chapters: int = Field(default=50, ge=3, le=500)
    target_words_per_chapter: int = Field(default=3000, ge=500, le=20000)
    pov: str = Field(default="THIRD_PERSON_LIMITED", max_length=40)
    ending_known: bool = True
    tone: str = Field(default="克制、具体、有画面感", max_length=1000)
    style_samples: list[str] = Field(default_factory=list, max_length=20)
    forbidden_content: list[str] = Field(default_factory=list, max_length=100)


class PlanCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    framing: PlanFraming
    premise: str | None = Field(default=None, max_length=12000)
    style_guide: dict[str, Any] = Field(default_factory=dict)
    anti_ai_rules: dict[str, Any] = Field(default_factory=dict)


class PlanPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    premise: str | None = Field(default=None, max_length=12000)
    framing: dict[str, Any] | None = None
    macro_plan: dict[str, Any] | None = None
    style_guide: dict[str, Any] | None = None
    anti_ai_rules: dict[str, Any] | None = None
    status: str | None = None


class ChapterPlanPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    summary: str | None = None
    pov_mode: str | None = None
    pov_character_ref: str | None = None
    cast_refs: list[Any] | None = None
    location: str | None = None
    time_anchor: str | None = None
    start_state: dict[str, Any] | None = None
    end_state: dict[str, Any] | None = None
    objective: str | None = None
    conflict: str | None = None
    must_events: list[Any] | None = None
    forbidden_events: list[Any] | None = None
    allowed_reveals: list[Any] | None = None
    forbidden_reveals: list[Any] | None = None
    foreshadow_create: list[Any] | None = None
    foreshadow_payoff: list[Any] | None = None
    character_changes: list[Any] | None = None
    consequences: list[Any] | None = None
    scene_beats: list[Any] | None = None
    target_words: int | None = Field(default=None, ge=500, le=20000)
    pace: str | None = None
    status: str | None = None
    locked: bool | None = None


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize_event_text(value: Any) -> str:
    """Normalize legacy prose event labels without changing their meaning."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"[，。！？；：、,.!?;:\"'“”‘’（）()\[\]{}]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def event_ref(value: Any, field: str) -> str:
    if isinstance(value, dict):
        explicit = value.get("event_ref") or value.get("canonical_key") or value.get("key")
        if explicit:
            return str(explicit)
        label = value.get("label") or value.get("name") or value.get("text") or ""
    else:
        label = value
    digest = hashlib.sha256(normalize_event_text(label).encode("utf-8")).hexdigest()[:20]
    return f"{field}:{digest}"


def event_label(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("label") or value.get("name") or value.get("text") or value.get("event_ref") or "").strip()
    return str(value or "").strip()


def event_spec(value: Any, field: str) -> dict[str, Any]:
    label = event_label(value)
    aliases = value.get("aliases", []) if isinstance(value, dict) else []
    aliases = [str(item).strip() for item in aliases if str(item).strip()]
    return {"event_ref": event_ref(value, field), "label": label, "aliases": aliases}


def task_event_specs(task: dict[str, Any] | None, field: str) -> list[dict[str, Any]]:
    if not task:
        return []
    contracts = (task.get("event_contracts") or {}).get(field) or []
    if contracts:
        return [dict(item) for item in contracts]
    return [event_spec(item, field) for item in task.get(field, []) or []]


def event_match(spec: dict[str, Any], observed_refs: set[str], text: str) -> str | None:
    if spec.get("event_ref") in observed_refs:
        return "EVENT_REF"
    normalized_text = normalize_event_text(text)
    aliases = [*spec.get("aliases", []), spec.get("label", "")]
    if any(normalize_event_text(item) and normalize_event_text(item) in normalized_text for item in aliases):
        return "ALIAS" if spec.get("aliases") else "TEXT"
    return None


def _chapter_payload(item: StoryPlanChapter) -> dict[str, Any]:
    return {column.name: getattr(item, column.name).value if hasattr(getattr(item, column.name), "value") else getattr(item, column.name) for column in item.__table__.columns}


def plan_payload(db: Session, plan: StoryPlan) -> dict[str, Any]:
    volumes = db.scalars(select(StoryPlanVolume).where(StoryPlanVolume.plan_id == plan.id).order_by(StoryPlanVolume.number)).all()
    arcs = db.scalars(select(StoryPlanArc).where(StoryPlanArc.plan_id == plan.id).order_by(StoryPlanArc.number)).all()
    chapters = db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id).order_by(StoryPlanChapter.number)).all()
    base = {column.name: getattr(plan, column.name).value if hasattr(getattr(plan, column.name), "value") else getattr(plan, column.name) for column in plan.__table__.columns}
    return base | {
        "volumes": [{column.name: getattr(item, column.name) for column in item.__table__.columns} for item in volumes],
        "arcs": [{column.name: getattr(item, column.name) for column in item.__table__.columns} for item in arcs],
        "chapters": [_chapter_payload(item) for item in chapters],
        "counts": {"volumes": len(volumes), "arcs": len(arcs), "chapters": len(chapters)},
    }


def approved_plan(db: Session, project_id: str) -> StoryPlan | None:
    return db.scalar(select(StoryPlan).where(StoryPlan.project_id == project_id, StoryPlan.status == StoryPlanStatus.APPROVED).order_by(StoryPlan.version.desc(), StoryPlan.id.desc()))


def chapter_task_context(db: Session, project_id: str, chapter_number: int, *, required: bool = False) -> dict[str, Any] | None:
    """Return the immutable planning contract for one chapter, if the project opted into Phase 3."""
    plan = approved_plan(db, project_id)
    if not plan:
        return None
    task = db.scalar(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id, StoryPlanChapter.number == chapter_number))
    if not task:
        if required:
            raise ValueError("PLAN_CHAPTER_MISSING")
        return None
    event_contracts = {field: [event_spec(item, field) for item in (getattr(task, field) or [])] for field in ("must_events", "forbidden_events", "scene_beats")}
    return {
        "plan_id": plan.id, "plan_version": plan.version, "plan_fingerprint": plan.source_fingerprint,
        "chapter_plan_id": task.id, "chapter_number": task.number, "volume_number": task.volume_number, "arc_number": task.arc_number,
        "title": task.title, "summary": task.summary, "pov_mode": task.pov_mode, "pov_character_ref": task.pov_character_ref,
        "cast_refs": task.cast_refs or [], "location": task.location, "time_anchor": task.time_anchor,
        "start_state": task.start_state or {}, "end_state": task.end_state or {}, "objective": task.objective, "conflict": task.conflict,
        "must_events": task.must_events or [], "forbidden_events": task.forbidden_events or [], "allowed_reveals": task.allowed_reveals or [], "forbidden_reveals": task.forbidden_reveals or [],
        "foreshadow_create": task.foreshadow_create or [], "foreshadow_payoff": task.foreshadow_payoff or [], "character_changes": task.character_changes or [], "consequences": task.consequences or [], "scene_beats": task.scene_beats or [],
        "event_contracts": event_contracts,
        "target_words": task.target_words, "pace": task.pace, "status": task.status.value if hasattr(task.status, "value") else task.status, "locked": task.locked,
    }


def next_chapter_task_context(db: Session, project_id: str) -> dict[str, Any] | None:
    from .models import Chapter
    latest = db.scalar(select(Chapter.number).where(Chapter.project_id == project_id).order_by(Chapter.number.desc()))
    return chapter_task_context(db, project_id, int(latest or 0) + 1)


def validate_task_output(output: dict[str, Any], task: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Validate model-declared task completion without pretending prose is structured fact."""
    if not task:
        return []
    coverage = output.get("task_coverage")
    if not isinstance(coverage, list):
        return [{"code": "PLAN_TASK_COVERAGE_MISSING", "blocking": True}]
    covered = {str(item) for item in coverage}
    coverage_text = "\n".join(covered)
    issues = []
    for spec in task_event_specs(task, "must_events"):
        matched = "EVENT_REF" if spec["event_ref"] in covered else event_match(spec, set(), coverage_text)
        if not matched:
            issues.append({"code": "PLAN_REQUIRED_EVENT_MISSING", "blocking": True, "event": spec["label"], "event_ref": spec["event_ref"]})
    forbidden_hits = output.get("task_forbidden_hits") or []
    forbidden_text = "\n".join(str(item) for item in forbidden_hits)
    for spec in task_event_specs(task, "forbidden_events"):
        if spec["event_ref"] in {str(item) for item in forbidden_hits} or event_match(spec, set(), forbidden_text):
            issues.append({"code": "PLAN_FORBIDDEN_EVENT_PRESENT", "blocking": True, "event": spec["label"], "event_ref": spec["event_ref"]})
    return issues


def validate_plan_references(db: Session, project_id: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate explicit IDs while allowing human-readable refs for a new project."""
    errors: list[dict[str, Any]] = []
    character_ids = {row.id for row in db.scalars(select(Character).where(Character.project_id == project_id)).all()}
    thread_ids = {row.id for row in db.scalars(select(StoryThread).where(StoryThread.project_id == project_id)).all()}
    canon_ids = {row.id for row in db.scalars(select(CanonFact).where(CanonFact.project_id == project_id)).all()}
    known = character_ids | thread_ids | canon_ids
    for chapter in data.get("chapters", []):
        for field in ("pov_character_ref",):
            ref = chapter.get(field)
            if ref and ref in known and ref not in character_ids:
                errors.append({"path": f"chapters[{chapter.get('number')}].{field}", "code": "REFERENCE_TYPE_MISMATCH"})
        for field in ("cast_refs",):
            for ref in _as_list(chapter.get(field)):
                if isinstance(ref, str) and ref in known and ref not in character_ids:
                    errors.append({"path": f"chapters[{chapter.get('number')}].{field}", "code": "REFERENCE_TYPE_MISMATCH"})
    for arc in data.get("arcs", []):
        for ref in _as_list(arc.get("thread_refs")):
            if isinstance(ref, str) and ref in known and ref not in thread_ids:
                errors.append({"path": f"arcs[{arc.get('number')}].thread_refs", "code": "REFERENCE_TYPE_MISMATCH"})
    return errors


def _default_chapter(number: int, framing: dict[str, Any], volume: int = 1, arc: int = 1, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = raw or {}
    number = int(raw.get("number", number))
    return {
        "number": number, "volume_number": int(raw.get("volume_number", volume)), "arc_number": int(raw.get("arc_number", arc)),
        "title": str(raw.get("title") or f"第{number}章"), "summary": str(raw.get("summary") or "本章推进主线并留下可验证的后果。"),
        "pov_mode": str(raw.get("pov_mode") or framing.get("pov") or "THIRD_PERSON_LIMITED"), "pov_character_ref": raw.get("pov_character_ref"),
        "cast_refs": _as_list(raw.get("cast_refs")), "location": raw.get("location"), "time_anchor": raw.get("time_anchor"),
        "start_state": raw.get("start_state") if isinstance(raw.get("start_state"), dict) else {}, "end_state": raw.get("end_state") if isinstance(raw.get("end_state"), dict) else {},
        "objective": str(raw.get("objective") or "让主角为目标付出一次具体代价"), "conflict": str(raw.get("conflict") or "外部阻力与角色选择形成不可逆变化"),
        "must_events": _as_list(raw.get("must_events")), "forbidden_events": _as_list(raw.get("forbidden_events")), "allowed_reveals": _as_list(raw.get("allowed_reveals")), "forbidden_reveals": _as_list(raw.get("forbidden_reveals")),
        "foreshadow_create": _as_list(raw.get("foreshadow_create")), "foreshadow_payoff": _as_list(raw.get("foreshadow_payoff")), "character_changes": _as_list(raw.get("character_changes")), "consequences": _as_list(raw.get("consequences")), "scene_beats": _as_list(raw.get("scene_beats")),
        "target_words": int(raw.get("target_words", framing.get("target_words_per_chapter", 3000))), "pace": str(raw.get("pace") or "MEDIUM"), "status": str(raw.get("status") or "READY"), "locked": bool(raw.get("locked", False)),
    }


def persist_plan(db: Session, project: Project, payload: dict[str, Any], *, provider: str | None = None, model: str | None = None, request_id: str | None = None, report: dict[str, Any] | None = None, archive_latest: bool = True) -> StoryPlan:
    latest = db.scalar(select(StoryPlan).where(StoryPlan.project_id == project.id).order_by(StoryPlan.version.desc()))
    version = (latest.version + 1) if latest else 1
    framing = payload.get("framing") or {}
    chapters = payload.get("chapters") or []
    target = int(framing.get("target_chapters", 50))
    if not chapters:
        chapters = [_default_chapter(i, framing) for i in range(1, target + 1)]
    normalized = {"framing": framing, "premise": payload.get("premise"), "macro_plan": payload.get("macro_plan") or {}, "style_guide": payload.get("style_guide") or {}, "anti_ai_rules": payload.get("anti_ai_rules") or {}, "chapters": chapters, "arcs": payload.get("arcs") or [], "volumes": payload.get("volumes") or []}
    plan = StoryPlan(project_id=project.id, version=version, status=StoryPlanStatus.GENERATED, framing=framing, premise=payload.get("premise"), macro_plan=payload.get("macro_plan") or {}, style_guide=payload.get("style_guide") or {}, anti_ai_rules=payload.get("anti_ai_rules") or {}, source_fingerprint=_fingerprint(normalized), provider=provider, model=model, request_id=request_id, generation_report=report or {})
    if archive_latest and latest and latest.status != StoryPlanStatus.ARCHIVED:
        latest.status = StoryPlanStatus.ARCHIVED
    db.add(plan); db.flush()
    for raw in payload.get("volumes") or []:
        db.add(StoryPlanVolume(plan_id=plan.id, number=int(raw.get("number", 1)), title=str(raw.get("title", "第一卷")), summary=str(raw.get("summary", "建立核心冲突并形成第一轮承诺。")), start_chapter=int(raw.get("start_chapter", 1)), end_chapter=int(raw.get("end_chapter", target)), arc_numbers=_as_list(raw.get("arc_numbers")), turning_points=_as_list(raw.get("turning_points")), theme=str(raw.get("theme", "")), core_question=str(raw.get("core_question", "")), major_conflict=str(raw.get("major_conflict", "")), start_state=raw.get("start_state") if isinstance(raw.get("start_state"), dict) else {}, end_state=raw.get("end_state") if isinstance(raw.get("end_state"), dict) else {}, main_thread=str(raw.get("main_thread", "")), ending_turn=str(raw.get("ending_turn", "")), foreshadowing=_as_list(raw.get("foreshadowing"))))
    for raw in payload.get("arcs") or []:
        db.add(StoryPlanArc(plan_id=plan.id, volume_number=int(raw.get("volume_number", 1)), number=int(raw.get("number", 1)), title=str(raw.get("title", "主线弧")), goal=str(raw.get("goal", "推动核心问题")), summary=str(raw.get("summary", "")), turning_points=_as_list(raw.get("turning_points")), thread_refs=_as_list(raw.get("thread_refs")), core_question=str(raw.get("core_question", "")), start_state=raw.get("start_state") if isinstance(raw.get("start_state"), dict) else {}, end_state=raw.get("end_state") if isinstance(raw.get("end_state"), dict) else {}))
    for index, raw in enumerate(chapters, 1):
        db.add(StoryPlanChapter(project_id=project.id, plan_id=plan.id, **_default_chapter(index, framing, raw=raw)))
    db.flush()
    return plan


def generation_context(db: Session, project: Project, framing: dict[str, Any], premise: str | None) -> dict[str, Any]:
    characters = [{"id": x.id, "name": x.name, "profile": x.profile, "goals": x.goals} for x in db.scalars(select(Character).where(Character.project_id == project.id, Character.active.is_(True))).all()]
    facts = [{"id": x.id, "proposition": x.proposition} for x in db.scalars(select(CanonFact).where(CanonFact.project_id == project.id)).all()]
    threads = [{"id": x.id, "title": x.title, "goal": x.goal, "status": x.status.value if hasattr(x.status, "value") else x.status} for x in db.scalars(select(StoryThread).where(StoryThread.project_id == project.id)).all()]
    return {"project": project.name, "framing": framing, "premise": premise or project.story_seed, "existing_characters": characters, "world_facts": facts, "story_threads": threads}


INITIAL_CHAPTER_WINDOW = 12


def _planning_context(db: Session, project: Project, framing: dict[str, Any], premise: str | None) -> dict[str, Any]:
    """Keep the model context focused: the long brief must not be sent twice."""
    context = generation_context(db, project, framing, premise)
    story_brief = str(premise or project.story_seed or framing.get("inspiration") or "").strip()
    compact_framing = {key: value for key, value in framing.items() if key != "inspiration"}
    return {
        "project": context["project"],
        "story_brief": story_brief,
        "framing": compact_framing,
        "existing_characters": context["existing_characters"],
        "world_facts": context["world_facts"],
        "story_threads": context["story_threads"],
    }


def _fallback_chapter_window(framing: dict[str, Any], count: int) -> list[dict[str, Any]]:
    """A macro plan remains usable if the optional detail call has a transient outage."""
    return [_default_chapter(number, framing) for number in range(1, count + 1)]


def _local_macro_fallback(project: Project, framing: dict[str, Any], premise: str | None) -> dict[str, Any]:
    """Turn an author's existing long outline into an editable checkpoint on model outage."""
    source = str(project.story_seed or premise or framing.get("inspiration") or "")
    match = re.search(r"【分卷大纲】(.*?)(?=【前十章承诺】|\Z)", source, re.S)
    outline = match.group(1) if match else source
    entries = list(re.finditer(r"卷([一二三四五六七八九十]+)《([^》]+)》[：:]\s*(.*?)(?=\s*卷[一二三四五六七八九十]+《|\Z)", outline, re.S))
    target = int(framing.get("target_chapters") or 50)
    volume_count = len(entries) or min(10, max(1, (target + 44) // 45))
    volumes, arcs = [], []
    for index in range(volume_count):
        entry = entries[index] if index < len(entries) else None
        start = index * target // volume_count + 1
        end = (index + 1) * target // volume_count
        title = entry.group(2).strip() if entry else f"第{index + 1}卷"
        summary = re.sub(r"\s+", " ", entry.group(3)).strip() if entry else "围绕既定主线推进，并在卷末留下下一卷的明确问题。"
        volumes.append({"number": index + 1, "title": title, "summary": summary, "start_chapter": start, "end_chapter": end, "turning_points": []})
        arcs.append({"number": index + 1, "volume_number": index + 1, "title": title, "goal": summary[:120], "summary": summary, "turning_points": []})
    return {
        "premise": premise or source[:1200],
        "macro_plan": {"logline": premise or source[:500], "core_conflict": premise or "以作者已保存的大纲为准，待模型恢复后可局部深化。", "planning_source": "AUTHOR_OUTLINE_FALLBACK"},
        "volumes": volumes,
        "arcs": arcs,
        "chapters": _fallback_chapter_window(framing, min(target, INITIAL_CHAPTER_WINDOW)),
        "generation_warning": "MODEL_MACRO_DEFERRED",
    }


def generate_plan(db: Session, project: Project, framing: dict[str, Any], premise: str | None, style_guide: dict[str, Any], anti_ai_rules: dict[str, Any], on_phase: Callable[[str, str], None] | None = None) -> tuple[dict[str, Any], Any]:
    settings = get_settings(); route = ModelRouter().resolve(db, project.id, settings, "DIRECTOR")
    key = ProviderCredentialResolver().generation_key(db, project.id, settings)
    config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == project.id))
    provider = get_model_provider(settings, route.provider, route.base_url, key, timeout_seconds=(config.request_timeout_seconds if config else None), max_retries=(config.max_retries if config else 0), rate_limit_per_minute=(config.rate_limit_per_minute if config else 0))
    target_chapters = int(framing.get("target_chapters") or 50)
    detailed_chapters = min(target_chapters, INITIAL_CHAPTER_WINDOW)
    context = _planning_context(db, project, framing, premise)
    if on_phase: on_phase("PREPARING", "正在整理整书约束与已保存资料")

    # Small books can be planned in one turn. For a long serial, asking for its
    # entire architecture and detailed task sheets together is a common 502 cause
    # with OpenAI-compatible gateways, so make the architecture checkpoint first.
    if target_chapters <= INITIAL_CHAPTER_WINDOW:
        contract = {"framing": "object", "premise": "string", "macro_plan": "object", "style_guide": "object", "anti_ai_rules": "object", "volumes": "array covering the full book", "arcs": "array covering the full book", "chapters": f"array with exactly chapters 1-{detailed_chapters}"}
        instruction = f"Return exactly one JSON object. Build a coherent Chinese novel plan, not prose. The book has {target_chapters} chapters. Volumes and arcs must cover chapter 1 through chapter {target_chapters}. Every returned chapter must include start/end state, conflict, mandatory and forbidden events, reveal permissions, foreshadowing, character changes, consequences, and 3-6 scene beats. Do not invent UUIDs: use existing IDs only when supplied, otherwise use stable human-readable names. Avoid generic AI phrasing, symmetrical slogans, empty metaphors, and omniscient knowledge leaks. Preserve chronology. Output keys must match the contract."
        messages = [{"role": "system", "content": "你是长篇小说总策划与连续性编辑。"}, {"role": "user", "content": json.dumps({"instruction": instruction, "output_contract": contract, "context": context, "style_guide": style_guide, "anti_ai_rules": anti_ai_rules}, ensure_ascii=False)}]
        if on_phase: on_phase("GENERATING", "正在生成整书结构与章节任务")
        result = provider.generate(messages, route.model)
        try:
            if on_phase: on_phase("VALIDATING", "正在校验结构化规划")
            return _extract_single_json_object(result.content), result
        except (ValueError, TypeError, json.JSONDecodeError) as first_error:
            if on_phase: on_phase("REPAIRING", "正在修复模型返回的结构")
            repair_messages = messages + [{"role": "assistant", "content": result.content}, {"role": "user", "content": json.dumps({"instruction": "你的上一条输出不是有效 JSON。只修复 JSON 格式，不改变故事内容；返回完整、可解析的 JSON 对象，不要 Markdown。", "error": str(first_error)[:500], "output_contract": contract}, ensure_ascii=False)}]
            result = provider.generate(repair_messages, route.model)
            return _extract_single_json_object(result.content), result

    macro_contract = {"framing": "object", "premise": "string", "macro_plan": "object with logline, ending and core promises", "style_guide": "object", "anti_ai_rules": "object", "volumes": "concise array covering every chapter", "arcs": "concise array covering every chapter"}
    macro_instruction = f"Return exactly one compact JSON object for a {target_chapters}-chapter Chinese novel. Create the complete book architecture: volumes and arcs must cover every chapter from 1 through {target_chapters}, with no gaps. Keep every volume and arc concise (one short summary and 2-4 turning points); do not return chapters, scenes, or prose in this call. Preserve chronology and the supplied story constraints."
    macro_messages = [{"role": "system", "content": "你是长篇小说总策划与连续性编辑。"}, {"role": "user", "content": json.dumps({"instruction": macro_instruction, "output_contract": macro_contract, "context": context, "style_guide": style_guide, "anti_ai_rules": anti_ai_rules}, ensure_ascii=False)}]
    try:
        if on_phase: on_phase("GENERATING_MACRO", "正在生成全书主线、卷纲与故事弧")
        macro_result = provider.generate(macro_messages, route.model)
        if on_phase: on_phase("VALIDATING_MACRO", "正在校验全书结构")
        macro = _extract_single_json_object(macro_result.content)
    except (ModelProviderError, ValueError, TypeError, json.JSONDecodeError):
        if on_phase: on_phase("FALLBACK", "模型未返回可用结构，正在整理作者已保存的十卷大纲")
        return _local_macro_fallback(project, framing, premise), None

    detail_contract = {"chapters": f"array with exactly chapters 1-{detailed_chapters}"}
    detail_instruction = f"Return exactly one JSON object containing executable task sheets for chapters 1-{detailed_chapters} only. Each chapter needs title, summary, volume_number, arc_number, POV, start/end state, objective, conflict, mandatory and forbidden events, reveal permissions, foreshadowing, character changes, consequences, and 3-6 scene beats. Use the approved macro architecture below; do not repeat it and do not write prose."
    detail_messages = [{"role": "system", "content": "你是长篇小说分章策划编辑。"}, {"role": "user", "content": json.dumps({"instruction": detail_instruction, "output_contract": detail_contract, "story_brief": context["story_brief"], "framing": context["framing"], "macro_architecture": {"macro_plan": macro.get("macro_plan") or {}, "volumes": macro.get("volumes") or [], "arcs": macro.get("arcs") or []}}, ensure_ascii=False)}]
    try:
        if on_phase: on_phase("GENERATING_CHAPTERS", f"正在生成第 1-{detailed_chapters} 章任务单")
        details_result = provider.generate(detail_messages, route.model)
        if on_phase: on_phase("VALIDATING_CHAPTERS", "正在校验章节任务与全书结构的一致性")
        details = _extract_single_json_object(details_result.content)
        macro["chapters"] = (details.get("chapters") or [])[:detailed_chapters]
        return macro, details_result
    except (ModelProviderError, ValueError, TypeError, json.JSONDecodeError):
        if on_phase: on_phase("CHAPTER_FALLBACK", "章节任务将以可编辑草稿继续，后续可按窗口深化")
        macro["chapters"] = _fallback_chapter_window(framing, detailed_chapters)
        macro["generation_warning"] = "CHAPTER_DETAIL_DEFERRED"
        return macro, macro_result
