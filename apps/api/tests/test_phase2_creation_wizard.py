import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api_module
import app.creation as creation_module
from app.ai.fake import FakeModelProvider
from app.db import Base
from app.main import app
from app.models import Project, ProjectModelConfig


def test_creation_wizard_returns_three_editable_directions(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="向导测试", story_seed="修复师追查失踪记录")
        db.add(project); db.flush()
        db.add(ProjectModelConfig(project_id=project.id, provider="openai_compatible", base_url="https://tokenrhythm.studio/v1", director_model="deepseek-v4-pro")); db.commit()
        output = {"directions": [{"title": f"方向 {index}", "premise": "一个可持续的长篇冲突", "core_conflict": "人物与代价", "ending": "完成兑现"} for index in range(1, 4)]}
        fake = FakeModelProvider(json.dumps(output, ensure_ascii=False))
        monkeypatch.setattr(creation_module, "get_model_provider", lambda *args, **kwargs: fake)
        monkeypatch.setattr(api_module, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
        response = TestClient(app).post(f"/projects/{project.id}/creation-directions", json={"mode": "inspiration", "inspiration": "修复师追查失踪记录", "genre": "悬疑", "target_chapters": 30})
        assert response.status_code == 200, response.text
        assert len(response.json()["directions"]) == 3
        assert fake.calls == 1


def test_creation_wizard_repairs_invalid_json_once(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="修复测试")
        db.add(project); db.flush(); db.add(ProjectModelConfig(project_id=project.id, provider="openai_compatible", base_url="https://tokenrhythm.studio/v1", director_model="deepseek-v4-pro")); db.commit()
        output = {"directions": [{"title": str(index), "premise": "p"} for index in range(3)]}
        fake = FakeModelProvider(["not json", json.dumps(output)])
        monkeypatch.setattr(creation_module, "get_model_provider", lambda *args, **kwargs: fake)
        monkeypatch.setattr(api_module, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
        response = TestClient(app).post(f"/projects/{project.id}/creation-directions", json={"mode": "blank", "genre": "科幻"})
        assert response.status_code == 200, response.text
        assert fake.calls == 2
