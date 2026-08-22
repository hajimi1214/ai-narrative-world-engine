from sqlalchemy import select
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db import Base
from app.models import Project

from app.embeddings import (
    EmbeddingResult,
    EmbeddingRoute,
    ResearchChunkEmbeddingAudit,
    ResearchChunkEmbeddingIndexService,
    ResearchChunkSemanticRetriever,
)
from app.models import EmbeddingStatus, ProjectModelConfig, ResearchChunkEmbedding
from app.research import ResearchBM25Retriever, ResearchConfig, ResearchIngestionService


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


@pytest.fixture()
def project(session):
    value = Project(name="Research", research_settings={})
    session.add(value); session.flush()
    return value


class FakeEmbeddingProvider:
    def __init__(self, vectors=None, error=None):
        self.vectors = vectors or {}
        self.error = error
        self.calls = 0

    def embed(self, inputs, model):
        self.calls += 1
        if self.error:
            raise self.error
        return EmbeddingResult(
            [self.vectors.get(value, [1.0, 0.0]) for value in inputs],
            "fake", model, 2, 0, "request-1",
        )


def route(fingerprint="research-test"):
    return EmbeddingRoute(True, "openai_compatible", "https://example.test/v1", "embed-v1", 2, "secret", "TEST", fingerprint)


def ingest(session, project, content):
    return ResearchIngestionService().ingest(session, project.id, title="Notes", content=content)


def test_research_chunk_index_persists_and_audits(session, project, monkeypatch):
    result = ingest(session, project, "蒸汽机推动了工坊生产。")
    provider = FakeEmbeddingProvider()
    monkeypatch.setattr("app.embeddings.EmbeddingRouter.resolve_research", lambda *_args, **_kwargs: route())
    indexed = ResearchChunkEmbeddingIndexService(provider_factory=lambda _route: provider).index_chunks(session, project.id)
    row = session.scalar(select(ResearchChunkEmbedding).where(ResearchChunkEmbedding.chunk_id == result.chunks[0].id))
    assert indexed["indexed"] == 1 and row.status == EmbeddingStatus.READY
    assert ResearchChunkEmbeddingAudit().audit(session, row.id)["valid"]
    assert provider.calls == 1


def test_research_chunk_content_change_reindexes_and_failure_retries(session, project, monkeypatch):
    result = ingest(session, project, "旧资料")
    monkeypatch.setattr("app.embeddings.EmbeddingRouter.resolve_research", lambda *_args, **_kwargs: route("retry-route"))
    failed = ResearchChunkEmbeddingIndexService(provider_factory=lambda _route: FakeEmbeddingProvider(error=TimeoutError())).index_chunks(session, project.id)
    row = session.scalar(select(ResearchChunkEmbedding).where(ResearchChunkEmbedding.chunk_id == result.chunks[0].id))
    assert failed["failed"] == 1 and row.status == EmbeddingStatus.FAILED and row.attempt_count == 1
    retried = ResearchChunkEmbeddingIndexService(provider_factory=lambda _route: FakeEmbeddingProvider()).index_chunks(session, project.id)
    assert retried["indexed"] == 1 and row.status == EmbeddingStatus.READY and row.attempt_count == 2
    result.chunks[0].content = "新资料"
    retried_again = ResearchChunkEmbeddingIndexService(provider_factory=lambda _route: FakeEmbeddingProvider()).index_chunks(session, project.id)
    assert retried_again["indexed"] == 1 and row.content_fingerprint == ResearchChunkEmbeddingIndexService._content_fingerprint("新资料")


def test_research_semantic_retrieval_filters_project_and_stale_content(session, project, monkeypatch):
    result = ingest(session, project, "目标资料")
    other = type(project)(name="Other")
    session.add(other); session.flush()
    foreign = ingest(session, other, "不应返回")
    for item, owner in ((result.chunks[0], project), (foreign.chunks[0], other)):
        session.add(ResearchChunkEmbedding(project_id=owner.id, document_id=item.document_id, revision_id=item.revision_id, chunk_id=item.id, embedding_config_fingerprint="semantic", provider="fake", model="fake", dimension=2, content_fingerprint=ResearchChunkEmbeddingIndexService._content_fingerprint(item.content), status=EmbeddingStatus.READY, embedding=[1.0, 0.0]))
    session.flush()
    found = ResearchChunkSemanticRetriever().retrieve(session, project.id, [1.0, 0.0], "semantic")
    assert [item[0] for item in found] == [result.chunks[0].id]
    result.chunks[0].content = "内容已变化"
    assert not ResearchChunkSemanticRetriever().retrieve(session, project.id, [1.0, 0.0], "semantic")


def test_research_search_uses_semantic_recall_and_rrf(session, project, monkeypatch):
    target = ingest(session, project, "关于机械动力的隐秘记录")
    lexical = ingest(session, project, "蒸汽机的公开说明")
    cfg = ProjectModelConfig(project_id=project.id, provider="openai_compatible", base_url="https://example.test/v1", embedding_enabled=True, embedding_use_main_connection=True, embedding_model="embed-v1", embedding_dimension=2)
    session.add(cfg); session.flush()
    monkeypatch.setattr("app.embeddings.EmbeddingRouter.resolve_research", lambda *_args, **_kwargs: route("hybrid-route"))
    indexer = ResearchChunkEmbeddingIndexService(provider_factory=lambda _route: FakeEmbeddingProvider()).index_chunks(session, project.id)
    assert indexer["indexed"] == 2
    retriever = ResearchBM25Retriever(embedding_provider_factory=lambda _route: FakeEmbeddingProvider())
    hits = retriever.search(session, project.id, "完全不同的查询", config=ResearchConfig(top_k=2))
    assert retriever.last_route == "HYBRID_RRF"
    assert target.chunks[0].id in {item.chunk_id for item in hits}
    assert any("SEMANTIC_VECTOR" in item.retrieval_channels for item in hits)
    mixed = retriever.search(session, project.id, "蒸汽机", config=ResearchConfig(top_k=2))
    assert any(set(item.retrieval_channels) == {"LEXICAL_BM25", "SEMANTIC_VECTOR"} for item in mixed)


def test_research_search_falls_back_to_bm25_on_embedding_failure(session, project, monkeypatch):
    ingest(session, project, "蒸汽机的公开说明")
    cfg = ProjectModelConfig(project_id=project.id, provider="openai_compatible", base_url="https://example.test/v1", embedding_enabled=True, embedding_use_main_connection=True, embedding_model="embed-v1", embedding_dimension=2)
    session.add(cfg); session.flush()
    monkeypatch.setattr("app.embeddings.EmbeddingRouter.resolve_research", lambda *_args, **_kwargs: route("fallback-route"))
    retriever = ResearchBM25Retriever(embedding_provider_factory=lambda _route: FakeEmbeddingProvider(error=TimeoutError()))
    hits = retriever.search(session, project.id, "蒸汽机")
    assert hits and retriever.last_route == "LEXICAL_FALLBACK"
    assert retriever.last_fallback_reason in {"MODEL_TIMEOUT", "EMBEDDING_INDEX_FAILED", "TimeoutError"}
