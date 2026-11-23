"""Natural-language novel creation wizard (Phase 2)."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai.factory import get_model_provider
from .ai.errors import MODEL_OUTPUT_INVALID, ModelProviderError
from .llm_actor import _extract_single_json_object
from .model_router import ModelRouter, ProviderCredentialResolver
from .models import ProjectModelConfig
from .settings import get_settings


def _direction_contract() -> dict[str, Any]:
    return {
        "directions": [{
            "title": "string",
            "selling_point": "string",
            "premise": "string",
            "ending": "string",
            "core_conflict": "string",
            "world_boundaries": ["string"],
            "world_encyclopedia": [{"name": "string", "entity_type": "CITY|LOCATION|SECT|FACTION|COUNTRY|ITEM|SYSTEM|HISTORY|CUSTOM", "description": "string", "rules": ["string"]}],
            "world_facts": [{"proposition": "string", "fact_type": "WORLD_FACT|CORE_CANON|SECRET_CANON"}],
            "protagonist": {"name": "string", "role": "string", "desire": "string", "cost": "string"},
            "main_characters": [{"name": "string", "role": "string", "desire": "string", "secret": "string"}],
            "story_threads": [{"title": "string", "goal": "string", "weight": "number"}],
            "first_volume_goal": "string",
            "first_ten_chapter_promises": ["string"],
            "foreshadowing_directions": ["string"],
            "style_advice": "string",
        }]
    }


def generate_creation_directions(db: Session, project_id: str, request: dict[str, Any], on_delta=None, on_usage=None) -> tuple[dict[str, Any], Any]:
    settings = get_settings()
    route = ModelRouter().resolve(db, project_id, settings, "DIRECTOR")
    key = ProviderCredentialResolver().generation_key(db, project_id, settings)
    config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == project_id))
    provider = get_model_provider(
        settings, route.provider, route.base_url, key,
        timeout_seconds=config.request_timeout_seconds if config else None,
        max_retries=config.max_retries if config else 0,
        rate_limit_per_minute=config.rate_limit_per_minute if config else 0,
    )
    guard = db.info.get("author_model_call_guard")

    def generate(messages):
        if guard:
            try:
                guard()
            except ModelProviderError as exc:
                exc.accounted_calls = 0
                raise
        stream = getattr(provider, "generate_stream", None)
        try:
            result = stream(messages, route.model, on_delta) if on_delta and callable(stream) else provider.generate(messages, route.model)
        except ModelProviderError as exc:
            if on_usage:
                on_usage(exc)
            raise
        if on_usage:
            on_usage(result)
        return result
    contract = _direction_contract()
    prompt = {
        "instruction": "根据作者输入生成三套彼此明显不同、可执行的长篇中文小说方向。只输出 JSON，不写正文，不制造空泛营销语。每套方向要有可兑现的结局、第一卷目标、前十章承诺、伏笔方向和具体世界边界。每套必须给出可直接写入世界百科的实体、可验证的世界事实、可推进的剧情线程。角色动机必须包含欲望与代价。",
        "output_contract": contract,
        "author_input": request,
    }
    messages = [
        {"role": "system", "content": "你是长篇小说创作顾问和连续性策划编辑。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ]
    result = generate(messages)
    try:
        parsed = _extract_single_json_object(result.content)
    except (ValueError, TypeError, json.JSONDecodeError, ModelProviderError) as first_error:
        repair = messages + [
            {"role": "assistant", "content": result.content},
            {"role": "user", "content": json.dumps({"instruction": "上一条不是有效 JSON。只修复格式，保留三套故事方向和内容，不要 Markdown。", "error": str(first_error)[:500], "output_contract": contract}, ensure_ascii=False)},
        ]
        result = generate(repair)
        parsed = _extract_single_json_object(result.content)
    directions = parsed.get("directions") if isinstance(parsed, dict) else None
    if not isinstance(directions, list) or len(directions) < 3:
        raise ValueError("CREATION_DIRECTIONS_INCOMPLETE")
    return {"directions": directions[:3], "provider": result.provider, "model": result.model, "request_id": result.request_id}, result
