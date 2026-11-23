import pytest
import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.ai.fake import FakeModelProvider
from app.ai.provider import classify_usage
from app.ai.errors import MODEL_UPSTREAM_ERROR, ModelProviderError
from app.ai.openai_compatible import OpenAICompatibleProvider
import app.api as api_module
from app.main import app
from app.model_router import ModelRouter
from app.models import Project, ProjectModelConfig, ProjectProviderCredential, ProviderCredentialPurpose
from app.settings import Settings
from app.embeddings import CredentialVault


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


@pytest.mark.parametrize(
    ("usage", "calls", "expected"),
    [
        ({"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}, 1, (10, 0, "CONFIRMED")),
        ({"prompt_tokens": 8, "completion_tokens": 2}, 1, (10, 0, "CONFIRMED")),
        ({"prompt_tokens": 8}, 1, (8, 1, "UNKNOWN")),
        ({}, 1, (0, 1, "UNKNOWN")),
        ({"total_tokens": 10}, 3, (10, 2, "UNKNOWN")),
        ({}, 0, (0, 0, "NOT_SENT")),
    ],
)
def test_usage_classification_preserves_missing_and_retried_calls(usage, calls, expected):
    result = classify_usage(usage, calls)
    assert (result["total_tokens"], result["unknown_usage_calls"], result["usage_status"]) == expected


def test_openai_provider_preserves_missing_usage_and_successful_internal_retry(monkeypatch):
    responses = [
        httpx.Response(429, request=httpx.Request("POST", "https://provider.example/v1/chat/completions")),
        httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {"total_tokens": 9}}, request=httpx.Request("POST", "https://provider.example/v1/chat/completions")),
    ]
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: responses.pop(0))
    provider = OpenAICompatibleProvider("https://provider.example/v1", "synthetic-key", max_retries=1)
    result = provider.generate([], "fake-model")
    assert result.attempts == 2 and result.usage == {"total_tokens": 9}
    assert classify_usage(result.usage, result.attempts)["unknown_usage_calls"] == 1

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": None}, request=httpx.Request("POST", "https://provider.example/v1/chat/completions")))
    missing = OpenAICompatibleProvider("https://provider.example/v1", "synthetic-key").generate([], "fake-model")
    assert missing.usage == {} and classify_usage(missing.usage, missing.attempts)["usage_status"] == "UNKNOWN"


def test_openai_provider_reports_accounted_retry_calls_on_final_failure(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: httpx.Response(503, request=httpx.Request("POST", "https://provider.example/v1/chat/completions")))
    provider = OpenAICompatibleProvider("https://provider.example/v1", "synthetic-key", max_retries=2)
    with pytest.raises(ModelProviderError) as failure:
        provider.generate([], "fake-model")
    assert failure.value.code == MODEL_UPSTREAM_ERROR
    assert failure.value.accounted_calls == 3


def test_generation_connection_test_is_read_only(session, monkeypatch):
    monkeypatch.setenv("AI_CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
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


def test_generation_preview_can_run_before_project_creation(monkeypatch):
    monkeypatch.setenv("AI_CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    fake = FakeModelProvider('{"ok":true}')
    monkeypatch.setattr(api_module, "get_model_provider", lambda *args, **kwargs: fake)
    response = TestClient(app).post("/model-config/test-generation", json={"provider": "openai_compatible", "base_url": "https://tokenrhythm.studio/v1", "model": "deepseek-v4-pro", "api_key": "temporary-key"})
    assert response.status_code == 200
    assert response.json()["model"] == "deepseek-v4-pro"
    assert fake.calls == 1


@pytest.mark.parametrize("project_scoped", [False, True])
def test_model_preflight_reports_unavailable_credential_storage_before_provider_call(session, monkeypatch, project_scoped):
    monkeypatch.delenv("AI_CREDENTIAL_MASTER_KEY", raising=False)
    project = Project(name="Vault preflight")
    session.add(project); session.commit()
    fake = FakeModelProvider('{"ok":true}')
    monkeypatch.setattr(api_module, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    monkeypatch.setattr(api_module, "get_model_provider", lambda *args, **kwargs: fake)
    if not project_scoped:
        monkeypatch.setattr(api_module, "get_settings", lambda: Settings(_env_file=None))
    path = f"/projects/{project.id}/model-config/test-generation" if project_scoped else "/model-config/test-generation"
    response = TestClient(app).post(path, json={
        "provider": "openai_compatible", "base_url": "https://provider.example/v1",
        "model": "fake-model", "api_key": "synthetic-test-key",
    })
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MODEL_CREDENTIAL_VAULT_NOT_CONFIGURED"
    assert fake.calls == 0


def test_project_model_preflight_without_key_returns_actionable_error(session, monkeypatch):
    monkeypatch.setattr(api_module, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(api_module, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    project = Project(name="Missing model key")
    session.add(project); session.commit()
    fake = FakeModelProvider('{"ok":true}')
    monkeypatch.setattr(api_module, "get_model_provider", lambda *args, **kwargs: fake)
    response = TestClient(app).post(f"/projects/{project.id}/model-config/test-generation", json={
        "provider": "openai_compatible", "base_url": "https://provider.example/v1", "model": "fake-model",
    })
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MODEL_PROVIDER_NOT_CONFIGURED"
    assert fake.calls == 0

def test_generation_preflight_reports_lost_master_key_for_saved_credential(session, monkeypatch):
    master_key = Fernet.generate_key().decode()
    monkeypatch.setenv("AI_CREDENTIAL_MASTER_KEY", master_key)
    project = Project(name="Saved credential preflight")
    session.add(project); session.flush()
    session.add(ProjectModelConfig(project_id=project.id, provider="openai_compatible", base_url="https://provider.example/v1", writer_model="fake-model"))
    session.add(ProjectProviderCredential(
        project_id=project.id, purpose=ProviderCredentialPurpose.GENERATION,
        secret_ciphertext=CredentialVault(master_key).encrypt("synthetic-key"),
        secret_fingerprint="synthetic-fingerprint", secret_hint="....key",
    ))
    session.commit()
    monkeypatch.delenv("AI_CREDENTIAL_MASTER_KEY", raising=False)
    monkeypatch.setattr(api_module, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    monkeypatch.setattr(api_module, "get_settings", lambda: Settings(_env_file=None))
    fake = FakeModelProvider('{"ok":true}')
    monkeypatch.setattr(api_module, "get_model_provider", lambda *args, **kwargs: fake)

    response = TestClient(app).post(f"/projects/{project.id}/model-config/test-generation", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MODEL_CREDENTIAL_VAULT_NOT_CONFIGURED"
    assert fake.calls == 0
