import math

import pytest
from cryptography.fernet import Fernet
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
import app.api as api_module
from app.main import app
from app.embeddings import (
    CharacterMemoryEmbeddingAudit,
    CharacterMemoryHybridRetriever,
    CharacterMemoryRRFMerger,
    CharacterMemorySemanticRetriever,
    CharacterSemanticCueBuilder,
    CredentialVault,
    EmbeddingRoute,
    EmbeddingResult,
    FakeEmbeddingProvider,
    MemoryEmbeddingIndexService,
    memory_content_fingerprint,
    OpenAICompatibleEmbeddingProvider,
)
from app.model_router import ProviderCredentialResolver
from app.settings import Settings
from app.character_mind import CharacterMindViewBuilder
from app.ai.errors import ModelProviderError
from app.models import Character, CharacterMemory, CharacterMemoryEmbedding, EmbeddingStatus, MemoryRetrievalMode, Project, ProjectModelConfig, ProjectProviderCredential, ProviderCredentialPurpose


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def seed(session):
    project = Project(name="embedding-test"); session.add(project); session.flush()
    actor = Character(project_id=project.id, name="A"); other = Character(project_id=project.id, name="B")
    session.add_all([actor, other]); session.flush()
    first = CharacterMemory(character_id=actor.id, content="tomb bell", importance=0.6, emotional_weight=0, distortion={})
    second = CharacterMemory(character_id=actor.id, content="harbor fog", importance=0.5, emotional_weight=0, distortion={})
    foreign = CharacterMemory(character_id=other.id, content="foreign", importance=1, emotional_weight=0, distortion={})
    session.add_all([first, second, foreign]); session.commit()
    return project, actor, other, first, second, foreign


def test_credential_vault_encrypts_without_retaining_plaintext():
    key = CredentialVault(Fernet.generate_key().decode())
    ciphertext = key.encrypt("project-secret")
    assert "project-secret" not in ciphertext and key.decrypt(ciphertext) == "project-secret"
    assert CredentialVault.hint("project-secret") == "....cret"


def test_rrf_is_ordinal_deterministic_and_hybrid_keeps_authoritative_items():
    merged = CharacterMemoryRRFMerger(60).merge(["a", "b"], ["b", "c"])
    assert [item[0] for item in merged] == ["b", "a", "c"]
    items = [{"memory_id": "a"}, {"memory_id": "b"}]
    assert [row["memory_id"] for row in CharacterMemoryHybridRetriever().merge(items, [("c", .99), ("b", .8)])] == ["b", "a"]


def test_semantic_candidate_outside_deterministic_top12_can_be_rescued():
    items = {f"m{index}": {"memory_id": f"m{index}", "source_scene_id": f"scene-{index}"} for index in range(20)}
    deterministic = [f"m{index}" for index in range(20)]
    result = CharacterMemoryHybridRetriever().merge(items, deterministic, [("m19", 1.0)] + [(f"m{index}", 0.1) for index in range(12)], 12)
    assert "m19" in {item["memory_id"] for item in result}


def test_structured_semantic_cues_are_stable_and_do_not_use_prose():
    cues = {"thread_ids": ("thread-b", "thread-a"), "location_ids": ("tomb",), "entity_ids": (), "participant_ids": (), "item_ids": ()}
    assert CharacterSemanticCueBuilder().build(cues) == '{"location_ids":["tomb"],"thread_ids":["thread-a","thread-b"]}'


def test_semantic_cue_preserves_normalized_cjk_visible_text_without_hidden_fields():
    cue = CharacterSemanticCueBuilder().build(
        {"entity_ids": (), "participant_ids": (), "thread_ids": (), "location_ids": (), "item_ids": ()},
        character={"id": "actor", "goals": {"current": "寻找父亲"}, "current_state": {}, "emotional_state": {}},
        scene={"location_id": "archive", "participants": ["actor"], "entry_state": {"visible_context": {"situation": "一个重要的人再次不告而别", "secret": "SECRET_CANON"}, "director_reasoning": "hidden"}},
    )
    assert "一个重要的人再次不告而别" in cue and "\\u4e00" not in cue
    assert "SECRET_CANON" not in cue and "hidden" not in cue


def test_normal_hybrid_pipeline_rescues_cjk_semantic_memory_and_falls_back_exactly(session, monkeypatch):
    from test_character_mind import seed as seed_mind
    project, _location, actor, _other, _outsider, proposal = seed_mind(session)
    proposal.entry_state = {"visible_context": {"situation": "一个重要的人再次不告而别"}}
    target = CharacterMemory(character_id=actor.id, content="父亲离开家后，再也没有回来。", importance=0, emotional_weight=0, distortion={})
    others = [CharacterMemory(character_id=actor.id, content=f"ordinary {index}", importance=10 + index, emotional_weight=0, distortion={}) for index in range(13)]
    session.add_all([target, *others]); session.flush()
    deterministic = CharacterMindViewBuilder().build(session, project.id, actor.id, proposal)
    deterministic_ids = [item["memory_id"] for item in deterministic["memories"]]
    assert target.id not in deterministic_ids
    config = ProjectModelConfig(project_id=project.id, provider="openai_compatible", base_url="https://example.test/v1", embedding_enabled=True, embedding_use_main_connection=True, embedding_model="fake", embedding_dimension=2, memory_retrieval_mode=MemoryRetrievalMode.HYBRID_RRF, memory_vector_top_k=14)
    session.add(config); session.flush()
    route = EmbeddingRoute(True, "openai_compatible", "https://example.test/v1", "fake", 2, "secret", "PROJECT", "cjk-hybrid")
    monkeypatch.setattr("app.embeddings.EmbeddingRouter.resolve", lambda *_args, **_kwargs: route)
    for memory in [target, *others]:
        session.add(CharacterMemoryEmbedding(project_id=project.id, character_id=actor.id, memory_id=memory.id, embedding_config_fingerprint=route.embedding_config_fingerprint, provider="fake", model="fake", dimension=2, content_fingerprint=memory_content_fingerprint(memory.content), status=EmbeddingStatus.READY, embedding=[1.0, 0.0] if memory.id == target.id else [0.0, 1.0]))
    session.commit()

    class CjkProvider:
        def __init__(self): self.inputs = []
        def embed(self, inputs, _model):
            self.inputs.extend(inputs)
            return EmbeddingResult([[1.0, 0.0] for _ in inputs], "fake", "fake", 2, 0)

    provider = CjkProvider()
    hybrid = CharacterMindViewBuilder(embedding_provider_factory=lambda _route: provider).build(session, project.id, actor.id, proposal)
    assert target.id in {item["memory_id"] for item in hybrid["memories"]}
    assert any("一个重要的人再次不告而别" in item for item in provider.inputs)
    failed = CharacterMindViewBuilder(embedding_provider_factory=lambda _route: FakeEmbeddingProvider(error=ModelProviderError("MODEL_TIMEOUT"))).build(session, project.id, actor.id, proposal)
    assert [item["memory_id"] for item in failed["memories"]] == deterministic_ids


def test_index_is_derived_and_auditable_without_mutating_memory(session, monkeypatch):
    project, actor, _, first, _, _ = seed(session)
    route = EmbeddingRoute(True, "openai_compatible", "https://example.test/v1", "fake", 2, "secret", "PROJECT", "cfg")
    provider = FakeEmbeddingProvider({first.content: [1.0, 0.0]})
    monkeypatch.setattr("app.embeddings.EmbeddingRouter.resolve", lambda *_args, **_kwargs: route)
    before = first.content
    result = MemoryEmbeddingIndexService(provider_factory=lambda _route: provider).index_memories(session, project.id, memory_ids=[first.id])
    session.commit()
    row = session.scalar(select(CharacterMemoryEmbedding).where(CharacterMemoryEmbedding.memory_id == first.id))
    assert result["indexed"] == 1 and provider.calls == 1 and session.get(CharacterMemory, first.id).content == before
    assert row.status == EmbeddingStatus.READY and row.content_fingerprint == memory_content_fingerprint(before)
    assert CharacterMemoryEmbeddingAudit().audit(session, row.id)["valid"]


def test_index_failure_is_persisted_and_retryable(session, monkeypatch):
    project, actor, _, first, _, _ = seed(session)
    route = EmbeddingRoute(True, "openai_compatible", "https://example.test/v1", "fake", 2, "secret", "PROJECT", "cfg-failure")
    monkeypatch.setattr("app.embeddings.EmbeddingRouter.resolve", lambda *_args, **_kwargs: route)
    failed = MemoryEmbeddingIndexService(provider_factory=lambda _route: FakeEmbeddingProvider(error=__import__("app.ai.errors", fromlist=["ModelProviderError"]).ModelProviderError("MODEL_TIMEOUT")))
    result = failed.index_memories(session, project.id, memory_ids=[first.id])
    session.commit()
    row = session.scalar(select(CharacterMemoryEmbedding).where(CharacterMemoryEmbedding.memory_id == first.id))
    assert result["failed"] == 1 and row.status == EmbeddingStatus.FAILED and row.attempt_count == 1
    retried = MemoryEmbeddingIndexService(provider_factory=lambda _route: FakeEmbeddingProvider({first.content: [1.0, 0.0]}))
    result = retried.index_memories(session, project.id, memory_ids=[first.id])
    session.commit()
    session.refresh(row)
    assert result["indexed"] == 1 and row.status == EmbeddingStatus.READY and row.attempt_count == 2 and row.last_error_code is None


def test_index_dimension_mismatch_is_persisted_with_safe_code(session, monkeypatch):
    project, _actor, _, first, _, _ = seed(session)
    route = EmbeddingRoute(True, "openai_compatible", "https://example.test/v1", "fake", 2, "secret", "PROJECT", "cfg-dimension")
    monkeypatch.setattr("app.embeddings.EmbeddingRouter.resolve", lambda *_args, **_kwargs: route)
    result = MemoryEmbeddingIndexService(provider_factory=lambda _route: FakeEmbeddingProvider({first.content: [1.0, 0.0, 0.0]})).index_memories(session, project.id, memory_ids=[first.id])
    session.commit()
    row = session.scalar(select(CharacterMemoryEmbedding).where(CharacterMemoryEmbedding.memory_id == first.id))
    assert result["error_code"] == "EMBEDDING_DIMENSION_MISMATCH" and row.status == EmbeddingStatus.FAILED and row.last_error_code == "EMBEDDING_DIMENSION_MISMATCH" and row.attempt_count == 1


def test_semantic_retrieval_enforces_project_character_and_content_eligibility(session):
    project, actor, other, first, second, foreign = seed(session)
    for memory, vector in ((first, [1.0, 0.0]), (second, [0.0, 1.0]), (foreign, [1.0, 0.0])):
        session.add(CharacterMemoryEmbedding(project_id=project.id, character_id=memory.character_id, memory_id=memory.id, embedding_config_fingerprint="cfg", provider="fake", model="fake", dimension=2, content_fingerprint=memory_content_fingerprint(memory.content), status=EmbeddingStatus.READY, embedding=vector))
    session.commit()
    found = CharacterMemorySemanticRetriever().retrieve(session, project.id, actor.id, [1.0, 0.0], "cfg", [first.id, second.id])
    assert [memory_id for memory_id, _ in found] == [first.id, second.id]
    first.content = "changed"; session.commit()
    found_after_change = CharacterMemorySemanticRetriever().retrieve(session, project.id, actor.id, [1.0, 0.0], "cfg", [first.id, second.id])
    assert first.id not in {memory_id for memory_id, _ in found_after_change}


def test_model_config_writes_encrypted_credentials_and_never_returns_them(session, monkeypatch):
    project, *_ = seed(session)
    monkeypatch.setenv("AI_CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(api_module, "SessionLocal", sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False))
    client = TestClient(app)
    response = client.put(f"/projects/{project.id}/model-config", json={"provider": "openai_compatible", "base_url": "https://example.test/v1", "api_key": "generation-secret", "embedding_api_key": "embedding-secret"})
    assert response.status_code == 200
    credentials = session.scalars(select(ProjectProviderCredential).where(ProjectProviderCredential.project_id == project.id)).all()
    assert len(credentials) == 2 and all("secret" not in record.secret_ciphertext for record in credentials)
    safe = client.get(f"/projects/{project.id}/model-config").json()
    assert "generation-secret" not in str(safe) and "embedding-secret" not in str(safe)
    assert safe["credentials"]["GENERATION"]["configured"] is True


def test_model_config_get_edit_put_roundtrip_preserves_credentials(session, monkeypatch):
    project, *_ = seed(session); master_key = Fernet.generate_key().decode(); monkeypatch.setenv("AI_CREDENTIAL_MASTER_KEY", master_key)
    monkeypatch.setattr(api_module, "SessionLocal", sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)); client = TestClient(app)
    editable = ("provider", "base_url", "character_model", "world_model", "director_model", "writer_model", "critic_model", "repair_model", "fallback_model", "auto_failover", "max_repair_attempts", "embedding_enabled", "embedding_use_main_connection", "embedding_provider", "embedding_base_url", "embedding_model", "embedding_dimension", "memory_retrieval_mode", "memory_vector_top_k", "memory_rrf_k", "memory_semantic_min_similarity")
    initial = {"provider": "openai_compatible", "base_url": "https://example.test/v1", "writer_model": "writer-v1", "critic_model": "critic-v1", "repair_model": "repair-v1", "embedding_enabled": True, "embedding_use_main_connection": False, "embedding_provider": "openai_compatible", "embedding_base_url": "https://embed.test/v1", "embedding_model": "embed-v1", "embedding_dimension": 2, "memory_retrieval_mode": "HYBRID_RRF", "api_key": "generation-v1", "embedding_api_key": "embedding-v1"}
    assert client.put(f"/projects/{project.id}/model-config", json=initial).status_code == 200
    received = client.get(f"/projects/{project.id}/model-config").json(); first_ciphertexts = {row.purpose.value: row.secret_ciphertext for row in session.scalars(select(ProjectProviderCredential).where(ProjectProviderCredential.project_id == project.id)).all()}
    projected = {field: received.get(field) for field in editable}; projected["writer_model"] = "writer-v2"
    response = client.put(f"/projects/{project.id}/model-config", json=projected)
    assert response.status_code == 200 and response.json()["writer_model"] == "writer-v2"
    assert {row.purpose.value: row.secret_ciphertext for row in session.scalars(select(ProjectProviderCredential).where(ProjectProviderCredential.project_id == project.id)).all()} == first_ciphertexts
    assert "generation-v1" not in str(response.json()) and "embedding-v1" not in str(response.json())
    assert client.put(f"/projects/{project.id}/model-config", json={**projected, "api_key": "generation-v2"}).status_code == 200
    generation = session.scalar(select(ProjectProviderCredential).where(ProjectProviderCredential.project_id == project.id, ProjectProviderCredential.purpose == ProviderCredentialPurpose.GENERATION))
    assert CredentialVault(master_key).decrypt(generation.secret_ciphertext) == "generation-v2"
    assert client.put(f"/projects/{project.id}/model-config", json={**projected, "clear_embedding_api_key": True}).status_code == 200
    assert session.scalar(select(ProjectProviderCredential).where(ProjectProviderCredential.project_id == project.id, ProjectProviderCredential.purpose == ProviderCredentialPurpose.EMBEDDING)) is None


def test_draft_embedding_dimension_mismatch_returns_safe_code(session, monkeypatch):
    project, *_ = seed(session)
    monkeypatch.setattr(api_module, "SessionLocal", sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False))

    class MismatchProvider:
        def __init__(self, *_args, **_kwargs): pass
        def embed(self, _inputs, _model): return EmbeddingResult([[0.0] * 3], "fake", "fake", 3, 0)

    monkeypatch.setattr(api_module, "OpenAICompatibleEmbeddingProvider", MismatchProvider)
    response = TestClient(app).post(f"/projects/{project.id}/model-config/test-embedding", json={"provider": "openai_compatible", "base_url": "https://example.test/v1", "model": "fake", "dimension": 2, "api_key": "temporary-secret"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "EMBEDDING_DIMENSION_MISMATCH"
    assert "temporary-secret" not in str(response.json())


def test_generation_credential_precedes_environment_and_clear_falls_back(session, monkeypatch):
    project, *_ = seed(session); monkeypatch.setenv("AI_CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    from app.api import _update_provider_credential
    _update_provider_credential(session, project.id, ProviderCredentialPurpose.GENERATION, "project-secret", False, __import__("os").environ["AI_CREDENTIAL_MASTER_KEY"]); session.commit()
    settings = Settings(ai_api_key="env-secret")
    assert ProviderCredentialResolver().generation_key(session, project.id, settings) == "project-secret"
    _update_provider_credential(session, project.id, ProviderCredentialPurpose.GENERATION, None, True, __import__("os").environ["AI_CREDENTIAL_MASTER_KEY"]); session.commit()
    assert ProviderCredentialResolver().generation_key(session, project.id, settings) == "env-secret"


@pytest.mark.parametrize("status,code", [(401, "MODEL_AUTH_FAILED"), (403, "MODEL_AUTH_FAILED"), (429, "MODEL_RATE_LIMITED"), (500, "MODEL_UPSTREAM_ERROR")])
def test_openai_embedding_provider_maps_safe_http_errors(status, code):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"error": "secret must not leak"})))
    with pytest.raises(Exception) as error:
        OpenAICompatibleEmbeddingProvider("https://example.test/v1", "secret", client=client).embed(["x"], "model")
    assert getattr(error.value, "code", None) == code


def test_openai_embedding_provider_reorders_batch_and_rejects_malformed_vectors():
    def handler(request):
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = OpenAICompatibleEmbeddingProvider("https://example.test/v1", "secret", client=client).embed(["a", "b"], "model")
    assert result.vectors == [[1.0, 0.0], [0.0, 1.0]]
    malformed = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'{"data":[{"index":0,"embedding":[NaN]}]}', headers={"content-type": "application/json"})))
    with pytest.raises(Exception) as error:
        OpenAICompatibleEmbeddingProvider("https://example.test/v1", "secret", client=malformed).embed(["a"], "model")
    assert getattr(error.value, "code", None) == "EMBEDDING_OUTPUT_INVALID"
