"""PostgreSQL-only pgvector and derived-index concurrency proofs."""
import os
import threading

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.embeddings import CharacterMemorySemanticRetriever, EmbeddingRoute, FakeEmbeddingProvider, MemoryEmbeddingIndexService, memory_content_fingerprint
from app.models import Character, CharacterMemory, CharacterMemoryEmbedding, EmbeddingStatus, Project


DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL.startswith("postgresql"), reason="requires PostgreSQL DATABASE_URL")


def _cleanup(Session, project_id):
    with Session() as db:
        db.execute(delete(CharacterMemoryEmbedding).where(CharacterMemoryEmbedding.project_id == project_id))
        character_ids = select(Character.id).where(Character.project_id == project_id)
        db.execute(delete(CharacterMemory).where(CharacterMemory.character_id.in_(character_ids)))
        db.execute(delete(Character).where(Character.project_id == project_id))
        db.execute(delete(Project).where(Project.id == project_id)); db.commit()


def _seed(Session):
    with Session() as db:
        project = Project(name="pg-memory-embedding"); db.add(project); db.flush()
        actor = Character(project_id=project.id, name="vector actor"); db.add(actor); db.flush()
        first = CharacterMemory(character_id=actor.id, content="north bell", importance=.5, emotional_weight=0, distortion={})
        second = CharacterMemory(character_id=actor.id, content="south bell", importance=.5, emotional_weight=0, distortion={})
        db.add_all([first, second]); db.flush()
        for memory, vector in ((first, [1.0, 0.0]), (second, [0.0, 1.0])):
            db.add(CharacterMemoryEmbedding(project_id=project.id, character_id=actor.id, memory_id=memory.id, embedding_config_fingerprint="pg-cfg", provider="fake", model="fake", dimension=2, content_fingerprint=memory_content_fingerprint(memory.content), status=EmbeddingStatus.READY, embedding=vector))
        db.commit(); return project.id, actor.id, first.id, second.id


def test_postgres_pgvector_extension_and_cosine_ordering():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    project_id, actor_id, first_id, second_id = _seed(Session)
    try:
        with Session() as db:
            assert db.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
            ranked = CharacterMemorySemanticRetriever().retrieve(db, project_id, actor_id, [1.0, 0.0], "pg-cfg", [first_id, second_id])
            assert [memory_id for memory_id, _ in ranked] == [first_id, second_id]
    finally:
        _cleanup(Session, project_id); engine.dispose()


def test_postgres_embedding_identity_prevents_duplicate_derived_rows():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    project_id, actor_id, first_id, _ = _seed(Session); barrier = threading.Barrier(2); successes, failures = [], []
    def insert_duplicate():
        try:
            with Session() as db:
                barrier.wait()
                db.add(CharacterMemoryEmbedding(project_id=project_id, character_id=actor_id, memory_id=first_id, embedding_config_fingerprint="concurrent-cfg", provider="fake", model="fake", dimension=2, content_fingerprint=memory_content_fingerprint("north bell"), status=EmbeddingStatus.READY, embedding=[1.0, 0.0]))
                db.commit(); successes.append(True)
        except IntegrityError:
            failures.append(True)
    threads = [threading.Thread(target=insert_duplicate) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    try:
        assert len(successes) == 1 and len(failures) == 1
        with Session() as db:
            rows = db.scalars(select(CharacterMemoryEmbedding).where(CharacterMemoryEmbedding.memory_id == first_id, CharacterMemoryEmbedding.embedding_config_fingerprint == "concurrent-cfg")).all()
            assert len(rows) == 1
    finally:
        _cleanup(Session, project_id); engine.dispose()


def test_postgres_index_service_concurrent_same_memory_is_serialized(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    project_id, actor_id, first_id, _ = _seed(Session)
    route = EmbeddingRoute(True, "openai_compatible", "https://example.test/v1", "fake", 2, "secret", "PROJECT", "service-concurrent-cfg")
    monkeypatch.setattr("app.embeddings.EmbeddingRouter.resolve", lambda *_args, **_kwargs: route)
    barrier = threading.Barrier(2); results, errors = [], []

    def index_once():
        try:
            with Session() as db:
                barrier.wait()
                result = MemoryEmbeddingIndexService(provider_factory=lambda _route: FakeEmbeddingProvider({"north bell": [1.0, 0.0]})).index_memories(db, project_id, memory_ids=[first_id])
                db.commit(); results.append(result)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=index_once) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    try:
        assert not errors
        with Session() as db:
            rows = db.scalars(select(CharacterMemoryEmbedding).where(CharacterMemoryEmbedding.project_id == project_id, CharacterMemoryEmbedding.memory_id == first_id, CharacterMemoryEmbedding.embedding_config_fingerprint == route.embedding_config_fingerprint)).all()
            assert len(rows) == 1 and rows[0].status == EmbeddingStatus.READY and rows[0].attempt_count == 1
            assert sum(item.get("indexed", 0) for item in results) == 1
    finally:
        _cleanup(Session, project_id); engine.dispose()
