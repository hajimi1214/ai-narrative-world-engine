"""PostgreSQL-only Phase 16C1 retrieval parity proofs."""
import os
import uuid

import pytest
from sqlalchemy import event, create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.character_mind import CharacterMindViewBuilder
from app.models import (
    Character, CharacterKnowledge, CharacterMemory, CharacterMemoryCueRef,
    CharacterMemorySearchIndex, CharacterKnowledgeSearchIndex, CognitionUsageHead,
    KnowledgeStatus, Project, ProjectCognitionRetrievalIndex, ResearchChunk,
    ResearchChunkLexicalIndex, ResearchDocument, ResearchDocumentRevision,
    ResearchLexicalIndexState, ResearchTermPosting, ResearchTermStat, SceneProposal,
    ProjectModelConfig, MemoryRetrievalMode, CharacterMemoryEmbedding, EmbeddingStatus,
    RetrievalIndexStatus, ResearchSourceTier, ResearchSourceKind,
)
from app.research import ResearchBM25Retriever, ResearchIngestionService, ResearchRetrievalConfig
from app.retrieval_index import CognitionRetrievalProjectionService, ResearchLexicalIndexService, ResearchIndexedBM25Retriever
from app.embeddings import EmbeddingRoute, FakeEmbeddingProvider, memory_content_fingerprint


DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL.startswith("postgresql"), reason="requires PostgreSQL DATABASE_URL")


def cleanup(db, project_id):
    memory_ids = select(CharacterMemory.id).join(Character).where(Character.project_id == project_id)
    knowledge_ids = select(CharacterKnowledge.id).join(Character).where(Character.project_id == project_id)
    for model in (ResearchTermPosting, ResearchTermStat, ResearchChunkLexicalIndex, ResearchLexicalIndexState,
                  CharacterMemoryCueRef, CharacterMemorySearchIndex, CharacterKnowledgeSearchIndex,
                  CognitionUsageHead, ProjectCognitionRetrievalIndex, CharacterMemoryEmbedding, ResearchChunk,
                  ResearchDocumentRevision, ResearchDocument, ProjectModelConfig):
        db.execute(delete(model).where(model.project_id == project_id))
    db.execute(delete(CharacterMemory).where(CharacterMemory.id.in_(memory_ids)))
    db.execute(delete(CharacterKnowledge).where(CharacterKnowledge.id.in_(knowledge_ids)))
    db.execute(delete(Character).where(Character.project_id == project_id))
    db.execute(delete(Project).where(Project.id == project_id))
    db.commit()


def test_postgres_cognition_fast_path_matches_legacy():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="cognition-fast-parity"); db.add(project); db.flush()
        actor = Character(project_id=project.id, name="Actor"); db.add(actor); db.flush()
        db.add_all([
            CharacterKnowledge(character_id=actor.id, proposition="ENTITY door: open = true", status=KnowledgeStatus.KNOWN, confidence=0.9),
            CharacterKnowledge(character_id=actor.id, proposition="ENTITY door: open = false", status=KnowledgeStatus.SUSPECTED, confidence=0.5),
            CharacterMemory(character_id=actor.id, content="The door opened.", importance=0.9, emotional_weight=0.4, confidence=1.0, distortion={"entity_ids": ["door"]}),
        ])
        proposal = SceneProposal(id="c1-fast-proposal", project_id=project.id, location_id="door", participants=[actor.id], entry_state={})
        CognitionRetrievalProjectionService().rebuild(db, project.id); db.commit()
        fast = CharacterMindViewBuilder().build(db, project.id, actor.id, proposal)
        CognitionRetrievalProjectionService().mark_dirty(db, project.id); db.commit()
        legacy = CharacterMindViewBuilder().build(db, project.id, actor.id, proposal)
        assert fast["knowledge"] == legacy["knowledge"]
        assert fast["memories"] == legacy["memories"]
        assert fast["belief_conflicts"] == legacy["belief_conflicts"]
        cleanup(db, project.id)
    engine.dispose()


def test_postgres_hybrid_current_fast_path_does_not_call_legacy_reader(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="hybrid-fast"); db.add(project); db.flush()
        actor = Character(project_id=project.id, name="Actor"); db.add(actor); db.flush()
        target = CharacterMemory(character_id=actor.id, content="semantic target", importance=.1, emotional_weight=0, confidence=1, distortion={})
        other = CharacterMemory(character_id=actor.id, content="ordinary", importance=.9, emotional_weight=0, confidence=1, distortion={})
        db.add_all([target, other]); db.flush()
        db.add(ProjectModelConfig(project_id=project.id, embedding_enabled=True, embedding_use_main_connection=False, embedding_provider="openai_compatible", embedding_base_url="https://example.test", embedding_model="fake", embedding_dimension=2, memory_retrieval_mode=MemoryRetrievalMode.HYBRID_RRF))
        CognitionRetrievalProjectionService().rebuild(db, project.id)
        route = EmbeddingRoute(True, "openai_compatible", "https://example.test", "fake", 2, "key", "TEST", "hybrid-cfg")
        for memory, vector in ((target, [1.0, 0.0]), (other, [0.0, 1.0])):
            db.add(CharacterMemoryEmbedding(project_id=project.id, character_id=actor.id, memory_id=memory.id, embedding_config_fingerprint=route.embedding_config_fingerprint, provider="fake", model="fake", dimension=2, content_fingerprint=memory_content_fingerprint(memory.content), status=EmbeddingStatus.READY, embedding=vector))
        db.commit()
        monkeypatch.setattr("app.embeddings.EmbeddingRouter.resolve", lambda *_args, **_kwargs: route)
        monkeypatch.setattr("app.character_mind.ActiveCharacterCognitionReader.knowledge", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LEGACY_PATH_USED")))
        monkeypatch.setattr("app.character_mind.ActiveCharacterCognitionReader.memories", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LEGACY_PATH_USED")))
        proposal = SceneProposal(id="hybrid-fast-proposal", project_id=project.id, location_id="room", participants=[actor.id], entry_state={})
        result = CharacterMindViewBuilder(embedding_provider_factory=lambda _route: FakeEmbeddingProvider({"{\"location_ids\":[\"room\"],\"participant_ids\":[\"" + actor.id + "\"]}": [1.0, 0.0]})).build(db, project.id, actor.id, proposal)
        assert {item["memory_id"] for item in result["memories"]} == {target.id, other.id}
        cleanup(db, project.id)
    engine.dispose()


def test_postgres_hybrid_semantic_rescues_memory_outside_deterministic_top12():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="hybrid-semantic-rescue"); db.add(project); db.flush()
        actor = Character(project_id=project.id, name="Actor"); db.add(actor); db.flush()
        target = CharacterMemory(character_id=actor.id, content="father never returned", importance=0.0, emotional_weight=0.0, confidence=0.0, distortion={})
        others = [CharacterMemory(character_id=actor.id, content=f"ordinary {index}", importance=1.0, emotional_weight=0.0, confidence=1.0, distortion={}) for index in range(20)]
        db.add_all([target, *others]); db.flush()
        config = ProjectModelConfig(project_id=project.id, embedding_enabled=True, embedding_use_main_connection=False, embedding_provider="openai_compatible", embedding_base_url="https://example.test", embedding_model="fake", embedding_dimension=2, memory_retrieval_mode=MemoryRetrievalMode.HYBRID_RRF, memory_vector_top_k=20, memory_rrf_k=60)
        db.add(config); CognitionRetrievalProjectionService().rebuild(db, project.id)
        for memory in [target, *others]:
            vector = [1.0, 0.0] if memory.id == target.id else [0.0, 1.0]
            db.add(CharacterMemoryEmbedding(project_id=project.id, character_id=actor.id, memory_id=memory.id, embedding_config_fingerprint="rescue-cfg", provider="fake", model="fake", dimension=2, content_fingerprint=memory_content_fingerprint(memory.content), status=EmbeddingStatus.READY, embedding=vector))
        db.commit()
        from app.retrieval_index import CurrentCharacterCognitionFastRetriever
        fast = CurrentCharacterCognitionFastRetriever()
        deterministic = fast.memories(db, project.id, actor.id, {"entity_ids": (), "participant_ids": (), "thread_ids": (), "item_ids": (), "location_ids": ()})
        hybrid = fast.hybrid_memories(db, project.id, actor.id, {"entity_ids": (), "participant_ids": (), "thread_ids": (), "item_ids": (), "location_ids": ()}, [1.0, 0.0], "rescue-cfg", config)
        assert target.id not in [item["memory_id"] for item in deterministic]
        assert target.id in [item["memory_id"] for item in hybrid]
        cleanup(db, project.id)
    engine.dispose()


def test_postgres_research_indexed_bm25_matches_legacy():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="research-fast-parity"); db.add(project); db.flush()
        ResearchIngestionService().ingest(db, project.id, title="Steam", content="Steam engine drives the workshop.")
        ResearchIngestionService().ingest(db, project.id, title="CJK", content="长安酒楼供应葡萄酒。")
        db.commit(); assert ResearchLexicalIndexService().fast_path_available(db, project.id)
        fast = ResearchBM25Retriever().search(db, project.id, "steam engine")
        ResearchLexicalIndexService().mark_dirty(db, project.id); db.commit()
        legacy = ResearchBM25Retriever().search(db, project.id, "steam engine")
        assert [(item.chunk_id, item.score) for item in fast] == [(item.chunk_id, item.score) for item in legacy]
        cleanup(db, project.id)
    engine.dispose()


def test_postgres_research_tag_filter_uses_indexed_bm25():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="research-tag-fast"); db.add(project); db.flush()
        ResearchIngestionService().ingest(db, project.id, title="Wine", content="长安酒楼供应葡萄酒", source_metadata={"tags": ["wine"]})
        ResearchIngestionService().ingest(db, project.id, title="Iron", content="铁匠铺供应铁器", source_metadata={"tags": ["metal"]})
        db.commit()
        indexed = ResearchIndexedBM25Retriever().search(db, project.id, "供应", filters={"tags": ["wine"]}, config=ResearchRetrievalConfig())
        ResearchLexicalIndexService().mark_dirty(db, project.id); db.commit()
        legacy = ResearchBM25Retriever().search(db, project.id, "供应", filters={"tags": ["wine"]})
        assert [item.chunk_id for item in indexed] == [item.chunk_id for item in legacy]
        cleanup(db, project.id)
    engine.dispose()


def test_postgres_ready_top_level_research_invokes_indexed_retriever(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="research-invocation"); db.add(project); db.flush()
        ResearchIngestionService().ingest(db, project.id, title="Steam", content="Steam engine drives workshop")
        db.commit(); called = {"value": False}
        original = ResearchIndexedBM25Retriever.search
        def tracked(*args, **kwargs):
            called["value"] = True
            return original(*args, **kwargs)
        monkeypatch.setattr(ResearchIndexedBM25Retriever, "search", tracked)
        assert ResearchBM25Retriever().search(db, project.id, "steam engine")
        assert called["value"]
        cleanup(db, project.id)
    engine.dispose()


def test_postgres_fast_cognition_read_is_bounded_at_100k():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="cognition-100k"); db.add(project); db.flush()
        actor = Character(project_id=project.id, name="Actor"); db.add(actor); db.flush()
        rows = []
        indexes = []
        for _ in range(100_000):
            memory_id = str(uuid.uuid4())
            rows.append({"id": memory_id, "character_id": actor.id, "content": "bounded memory", "importance": .5, "emotional_weight": 0.0, "confidence": 1.0, "distortion": {}})
            indexes.append({"id": str(uuid.uuid4()), "project_id": project.id, "character_id": actor.id, "memory_id": memory_id, "importance": .5, "emotional_weight": 0.0, "confidence": 1.0, "source_bucket": f"memory:{memory_id}", "content_fingerprint": "fp", "index_fingerprint": "index"})
        db.execute(CharacterMemory.__table__.insert(), rows)
        db.execute(CharacterMemorySearchIndex.__table__.insert(), indexes)
        db.add(ProjectCognitionRetrievalIndex(project_id=project.id, protocol_version="character-cognition-search-v1", status=RetrievalIndexStatus.READY, indexed_knowledge_count=0, indexed_memory_count=100_000, usage_head_count=0, built_through_sequence=0, index_fingerprint="state"))
        db.commit(); statements = []
        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", capture)
        from app.retrieval_index import CurrentCharacterCognitionFastRetriever
        result = CurrentCharacterCognitionFastRetriever().memories(db, project.id, actor.id, {"entity_ids": (), "participant_ids": (), "thread_ids": (), "item_ids": (), "location_ids": ()})
        event.remove(engine, "before_cursor_execute", capture)
        assert len(result) == 12
        assert len(statements) <= 4
        cleanup(db, project.id)
    engine.dispose()


def test_postgres_indexed_research_selective_query_avoids_corpus_tokenization(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="research-100k"); db.add(project); db.flush()
        document = ResearchDocument(project_id=project.id, title="Corpus", source_tier=ResearchSourceTier.PROJECT_RESEARCH, source_kind=ResearchSourceKind.MANUAL_TEXT, active=True, source_metadata={})
        db.add(document); db.flush()
        revision = ResearchDocumentRevision(project_id=project.id, document_id=document.id, version=1, active=True, content="synthetic", content_fingerprint="revision-fp", normalized_fingerprint="normalized", ingestion_config={}, ingestion_config_fingerprint="ingestion")
        db.add(revision); db.flush()
        chunks = []
        lexical = []
        postings = []
        for ordinal in range(100_000):
            chunk_id = str(uuid.uuid4()); content = "needle" if ordinal == 0 else "background"
            chunks.append({"id": chunk_id, "project_id": project.id, "document_id": document.id, "revision_id": revision.id, "ordinal": ordinal, "start_offset": ordinal, "end_offset": ordinal + len(content), "content": content, "content_fingerprint": f"chunk-{ordinal}", "token_count": 1, "char_count": len(content), "metadata": {}, "active": True})
            lexical.append({"id": str(uuid.uuid4()), "project_id": project.id, "document_id": document.id, "revision_id": revision.id, "chunk_id": chunk_id, "content_fingerprint": f"chunk-{ordinal}", "token_count": 1, "index_fingerprint": "index"})
            postings.append({"id": str(uuid.uuid4()), "project_id": project.id, "chunk_id": chunk_id, "term": content, "term_frequency": 1})
        db.execute(ResearchChunk.__table__.insert(), chunks)
        db.execute(ResearchChunkLexicalIndex.__table__.insert(), lexical)
        db.execute(ResearchTermPosting.__table__.insert(), postings)
        db.add_all([ResearchTermStat(project_id=project.id, term="needle", document_frequency=1), ResearchTermStat(project_id=project.id, term="background", document_frequency=99_999)])
        db.add(ResearchLexicalIndexState(project_id=project.id, status=RetrievalIndexStatus.READY, protocol_version="research-inverted-index-v1", corpus_fingerprint="corpus", active_chunk_count=100_000, total_token_count=100_000, average_document_length=1.0, posting_count=100_000, term_count=2, index_fingerprint="state"))
        db.commit(); seen = []
        original = __import__("app.research", fromlist=["KnowledgeTokenizer"]).KnowledgeTokenizer.tokenize
        monkeypatch.setattr("app.research.KnowledgeTokenizer.tokenize", lambda _self, text: (seen.append(text), original(_self, text))[1])
        result = ResearchIndexedBM25Retriever().search(db, project.id, "needle", filters={}, config=ResearchRetrievalConfig())
        assert [item.content for item in result] == ["needle"]
        assert seen == ["needle"]
        cleanup(db, project.id)
    engine.dispose()
