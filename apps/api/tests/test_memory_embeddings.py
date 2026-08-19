import math

import pytest
from cryptography.fernet import Fernet
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
    FakeEmbeddingProvider,
    MemoryEmbeddingIndexService,
    memory_content_fingerprint,
)
from app.models import Character, CharacterMemory, CharacterMemoryEmbedding, EmbeddingStatus, Project, ProjectProviderCredential


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


def test_structured_semantic_cues_are_stable_and_do_not_use_prose():
    cues = {"thread_ids": ("thread-b", "thread-a"), "location_ids": ("tomb",), "entity_ids": (), "participant_ids": (), "item_ids": ()}
    assert CharacterSemanticCueBuilder().build(cues) == "thread_ids=thread-a,thread-b|location_ids=tomb"


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


def test_semantic_retrieval_enforces_project_character_and_content_eligibility(session):
    project, actor, other, first, second, foreign = seed(session)
    for memory, vector in ((first, [1.0, 0.0]), (second, [0.0, 1.0]), (foreign, [1.0, 0.0])):
        session.add(CharacterMemoryEmbedding(project_id=project.id, character_id=memory.character_id, memory_id=memory.id, embedding_config_fingerprint="cfg", provider="fake", model="fake", dimension=2, content_fingerprint=memory_content_fingerprint(memory.content), status=EmbeddingStatus.READY, embedding=vector))
    session.commit()
    found = CharacterMemorySemanticRetriever().retrieve(session, project.id, actor.id, [1.0, 0.0], "cfg")
    assert [memory_id for memory_id, _ in found] == [first.id, second.id]
    first.content = "changed"; session.commit()
    found_after_change = CharacterMemorySemanticRetriever().retrieve(session, project.id, actor.id, [1.0, 0.0], "cfg")
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
