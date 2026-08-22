import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api_module
import app.planning as planning_module
from app.ai.fake import FakeModelProvider
from app.db import Base
from app.main import app
from app.models import Project, ProjectModelConfig


def test_phase2_plan_generation_creates_versioned_chapter_task_sheets(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="Phase 2", story_seed="一名修复师追查失踪的战争记录")
        db.add(project); db.flush()
        db.add(ProjectModelConfig(project_id=project.id, provider="openai_compatible", base_url="https://tokenrhythm.studio/v1", director_model="deepseek-v4-pro")); db.commit()
        output = {"premise": "修复师发现城市记忆被篡改", "macro_plan": {"logline": "每次修复都会暴露新的代价"}, "volumes": [{"number": 1, "title": "雾港", "summary": "建立谜团", "start_chapter": 1, "end_chapter": 2}], "arcs": [{"number": 1, "volume_number": 1, "title": "追索", "goal": "找到记录源头", "summary": "从失踪案进入主线"}], "chapters": [{"number": 1, "title": "失真的回声", "summary": "主角接到委托", "objective": "接受调查", "conflict": "委托人隐瞒代价", "start_state": {"case": "open"}, "end_state": {"case": "accepted"}, "must_events": ["接案"], "forbidden_events": ["揭示真相"], "scene_beats": ["雨夜来客", "查看残片"]}, {"number": 2, "title": "旧档案", "summary": "发现矛盾", "objective": "确认记录异常", "conflict": "档案管理员阻拦", "start_state": {"case": "accepted"}, "end_state": {"case": "complicated"}, "must_events": ["发现矛盾"], "forbidden_events": [], "scene_beats": ["进入档案库"]}]}
        fake = FakeModelProvider(json.dumps(output, ensure_ascii=False))
        monkeypatch.setattr(planning_module, "get_model_provider", lambda *args, **kwargs: fake)
        monkeypatch.setattr(api_module, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
        payload = {"framing": {"inspiration": "修复师追查失踪战争记录", "genre": "悬疑", "target_chapters": 3, "target_words_per_chapter": 2500, "pov": "THIRD_PERSON_LIMITED", "audience": "成人读者", "ending_known": True, "tone": "克制"}, "premise": "修复师发现城市记忆被篡改"}
        response = TestClient(app).post(f"/projects/{project.id}/planning/generate", json=payload)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["version"] == 1
        assert body["counts"]["chapters"] == 2
        assert body["chapters"][0]["must_events"] == ["接案"]
        assert fake.calls == 1

        chapter_id = body["chapters"][0]["id"]
        patch = TestClient(app).patch(f"/projects/{project.id}/planning/plans/{body['id']}/chapters/{chapter_id}", json={"locked": True, "status": "LOCKED"})
        assert patch.status_code == 200
        blocked = TestClient(app).patch(f"/projects/{project.id}/planning/plans/{body['id']}/chapters/{chapter_id}", json={"title": "不应修改"})
        assert blocked.status_code == 409
