import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.ai.fake import FakeModelProvider
import app.api as api_module
from app.main import app
from app.model_router import ModelRouter
from app.models import Project, ProjectModelConfig
from app.settings import Settings


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def test_phase1_defaults_point_all_generation_roles_at_deepseek():
    settings = Settings(_env_file=None)
    assert settings.ai_provider == "disabled"
    assert settings.ai_base_url == "https://tokenrhythm.studio/v1"
    assert {getattr(settings, field) for field in ModelRouter.DEFAULTS.values()} == {"deepseek-v4-pro"}


def test_project_override_can_use_one_model_for_all_generation_roles():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        project = Project(name="Phase 1 model gateway")
        db.add(project); db.flush()
        db.add(ProjectModelConfig(project_id=project.id, provider="openai_compatible", base_url="https://tokenrhythm.studio/v1", character_model="deepseek-v4-pro", world_model="deepseek-v4-pro", director_model="deepseek-v4-pro", repair_model="deepseek-v4-pro", writer_model="deepseek-v4-pro", critic_model="deepseek-v4-pro"))
        db.commit()
        settings = Settings(_env_file=None, ai_provider="openai_compatible", ai_api_key="local-test-key")
        routes = [ModelRouter().resolve(db, project.id, settings, role) for role in ModelRouter.FIELDS]
        assert {(route.provider, route.base_url, route.model) for route in routes} == {("openai_compatible", "https://tokenrhythm.studio/v1", "deepseek-v4-pro")}


def test_embedding_configuration_is_independent_from_generation_defaults():
    settings = Settings(_env_file=None, ai_embedding_provider="openai_compatible", ai_embedding_base_url="https://embedding.example/v1", ai_embedding_model="text-embedding-v4", ai_embedding_dimension=1024)
    assert settings.ai_base_url == "https://tokenrhythm.studio/v1"
    assert settings.ai_embedding_base_url == "https://embedding.example/v1"
    assert settings.ai_embedding_model == "text-embedding-v4"
    assert settings.ai_embedding_dimension == 1024


def test_generation_connection_test_is_read_only(session, monkeypatch):
    project = Project(name="Phase 1 API test")
    session.add(project); session.commit()
    fake = FakeModelProvider('{"ok":true}')
    monkeypatch.setattr(api_module, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    monkeypatch.setattr(api_module, "get_model_provider", lambda *args, **kwargs: fake)
    response = TestClient(app).post(f"/projects/{project.id}/model-config/test-generation", json={"provider": "openai_compatible", "base_url": "https://tokenrhythm.studio/v1", "model": "deepseek-v4-pro", "api_key": "temporary-key"})
    assert response.status_code == 200
    assert response.json()["model"] == "deepseek-v4-pro"
    assert fake.calls == 1
    assert session.query(ProjectModelConfig).count() == 0
