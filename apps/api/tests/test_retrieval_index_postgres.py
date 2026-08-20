"""PostgreSQL-only Phase 16C1 retrieval parity proofs."""
import os
import threading
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
    ProjectModelConfig, MemoryRetrievalMode, MemoryVectorSearchMode, CharacterMemoryEmbedding, EmbeddingStatus,
    RetrievalIndexStatus, ResearchSourceTier, ResearchSourceKind, WorldEntity, EntityType, Scene,
)
from app.research import ResearchBM25Retriever, ResearchIngestionService, ResearchRetrievalConfig
from app.retrieval_index import CognitionRetrievalProjectionService, ResearchLexicalIndexService, ResearchIndexedBM25Retriever, CharacterMemoryANNSemanticRetriever, CurrentCharacterCognitionFastRetriever, MemoryANNIndexStatusService
from app.embeddings import EmbeddingRoute, FakeEmbeddingProvider, memory_content_fingerprint


DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL.startswith("postgresql"), reason="requires PostgreSQL DATABASE_URL")


def cleanup(db, project_id):
    memory_ids = select(CharacterMemory.id).join(Character).where(Character.project_id == project_id)
    knowledge_ids = select(CharacterKnowledge.id).join(Character).where(Character.project_id == project_id)
    for model in (ResearchTermPosting, ResearchTermStat, ResearchChunkLexicalIndex, ResearchLexicalIndexState,
                  CharacterMemoryCueRef, CharacterMemorySearchIndex, CharacterKnowledgeSearchIndex,
                  CognitionUsageHead, ProjectCognitionRetrievalIndex, CharacterMemoryEmbedding, ResearchChunk,
                  ResearchDocumentRevision, ResearchDocument, ProjectModelConfig, WorldEntity, SceneProposal, Scene):
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


def test_postgres_fast_and_legacy_hybrid_send_byte_exact_cjk_semantic_query(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="cue-parity"); db.add(project); db.flush()
        actor = Character(project_id=project.id, name="沈砚", goals={"goal": "寻人"}, emotional_state={"mood": "忧"})
        peer = Character(project_id=project.id, name="顾清辞")
        location = WorldEntity(project_id=project.id, entity_type=EntityType.LOCATION, name="长安酒楼")
        db.add_all([actor, peer, location]); db.flush()
        memory = CharacterMemory(character_id=actor.id, content="故人离去", importance=.5, emotional_weight=.2, confidence=1, distortion={})
        db.add(memory); db.add(ProjectModelConfig(project_id=project.id, embedding_enabled=True, embedding_use_main_connection=False, embedding_provider="openai_compatible", embedding_base_url="https://example.test", embedding_model="fake", embedding_dimension=2, memory_retrieval_mode=MemoryRetrievalMode.HYBRID_RRF))
        CognitionRetrievalProjectionService().rebuild(db, project.id)
        route = EmbeddingRoute(True, "openai_compatible", "https://example.test", "fake", 2, "key", "TEST", "cue-cfg")
        db.add(CharacterMemoryEmbedding(project_id=project.id, character_id=actor.id, memory_id=memory.id, embedding_config_fingerprint="cue-cfg", provider="fake", model="fake", dimension=2, content_fingerprint=memory_content_fingerprint(memory.content), status=EmbeddingStatus.READY, embedding=[1.0, 0.0]))
        db.commit(); received = []
        class RecordingProvider:
            def embed(self, inputs, _model):
                received.extend(inputs)
                from app.embeddings import EmbeddingResult
                return EmbeddingResult([[1.0, 0.0] for _ in inputs], "fake", "fake", 2, 0)
        monkeypatch.setattr("app.embeddings.EmbeddingRouter.resolve", lambda *_args, **_kwargs: route)
        proposal = SceneProposal(id="cue-parity-proposal", project_id=project.id, location_id=location.id, participants=[actor.id, peer.id], entry_state={"visible_context": {"situation": "故人再次不告而别"}})
        builder = CharacterMindViewBuilder(embedding_provider_factory=lambda _route: RecordingProvider())
        CognitionRetrievalProjectionService().mark_dirty(db, project.id); db.commit()
        legacy = builder.build(db, project.id, actor.id, proposal)
        CognitionRetrievalProjectionService().rebuild(db, project.id); db.commit()
        fast = builder.build(db, project.id, actor.id, proposal)
        assert len(received) == 2 and received[0] == received[1]
        assert "长安酒楼" in received[1] and "沈砚" in received[1] and "顾清辞" in received[1] and "故人再次不告而别" in received[1]
        assert legacy["memories"] == fast["memories"]
        cleanup(db, project.id)
    engine.dispose()


def test_postgres_fast_formal_ownership_blocks_corrupt_derived_owners():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="owner-integrity"); db.add(project); db.flush()
        actor_a = Character(project_id=project.id, name="A"); actor_b = Character(project_id=project.id, name="B")
        db.add_all([actor_a, actor_b]); db.flush()
        knowledge_b = CharacterKnowledge(character_id=actor_b.id, proposition="ENTITY door: open = true", status=KnowledgeStatus.KNOWN, confidence=1)
        memory_b = CharacterMemory(character_id=actor_b.id, content="B secret", importance=1, emotional_weight=0, confidence=1, distortion={})
        db.add_all([knowledge_b, memory_b]); db.flush(); CognitionRetrievalProjectionService().rebuild(db, project.id)
        knowledge_index = db.scalar(select(CharacterKnowledgeSearchIndex).where(CharacterKnowledgeSearchIndex.knowledge_id == knowledge_b.id)); memory_index = db.scalar(select(CharacterMemorySearchIndex).where(CharacterMemorySearchIndex.memory_id == memory_b.id))
        knowledge_index.character_id = actor_a.id; memory_index.character_id = actor_a.id
        db.add(ProjectModelConfig(project_id=project.id, embedding_enabled=True, embedding_use_main_connection=False, embedding_provider="openai_compatible", embedding_base_url="https://example.test", embedding_model="fake", embedding_dimension=2, memory_retrieval_mode=MemoryRetrievalMode.HYBRID_RRF))
        db.add(CharacterMemoryEmbedding(project_id=project.id, character_id=actor_a.id, memory_id=memory_b.id, embedding_config_fingerprint="owner-cfg", provider="fake", model="fake", dimension=2, content_fingerprint=memory_content_fingerprint(memory_b.content), status=EmbeddingStatus.READY, embedding=[1.0, 0.0]))
        db.commit()
        from app.retrieval_index import CurrentCharacterCognitionFastRetriever
        fast = CurrentCharacterCognitionFastRetriever(); cues = {"entity_ids": (), "participant_ids": (), "thread_ids": (), "item_ids": (), "location_ids": ()}
        knowledge, _ = fast.knowledge(db, project.id, actor_a.id, cues)
        deterministic = fast.memories(db, project.id, actor_a.id, cues)
        config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == project.id))
        hybrid = fast.hybrid_memories(db, project.id, actor_a.id, cues, [1.0, 0.0], "owner-cfg", config)
        assert knowledge_b.id not in [row["knowledge_id"] for row in knowledge]
        assert memory_b.id not in [row["memory_id"] for row in deterministic]
        assert memory_b.id not in [row["memory_id"] for row in hybrid]
        cleanup(db, project.id)
    engine.dispose()


def test_postgres_cognition_rebuild_and_sync_share_project_lock():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="cognition-rebuild-sync"); db.add(project); db.flush()
        actor = Character(project_id=project.id, name="Actor"); db.add(actor); db.flush()
        scene = Scene(project_id=project.id, sequence=1, location="room", participants=[actor.id], history_status="ACTIVE")
        db.add(scene); db.flush()
        db.add(CharacterMemory(character_id=actor.id, content="scene memory", importance=.5, emotional_weight=0, confidence=1, source_scene=scene.id, distortion={}))
        CognitionRetrievalProjectionService().rebuild(db, project.id); db.commit(); project_id, scene_id = project.id, scene.id
    barrier = threading.Barrier(2); errors = []
    def rebuild():
        try:
            with Session() as db:
                barrier.wait(); CognitionRetrievalProjectionService().rebuild(db, project_id); db.commit()
        except Exception as exc: errors.append(exc)
    def sync():
        try:
            with Session() as db:
                barrier.wait(); CognitionRetrievalProjectionService().sync_after_scene_commit(db, project_id, scene_id, 1); db.commit()
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=rebuild), threading.Thread(target=sync)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert not errors
    with Session() as db:
        assert CognitionRetrievalProjectionService().fast_path_available(db, project_id)
        assert __import__("app.retrieval_index", fromlist=["CognitionRetrievalIndexAudit"]).CognitionRetrievalIndexAudit().audit(db, project_id)["valid"]
        cleanup(db, project_id)
    engine.dispose()


def test_postgres_research_rebuild_and_sync_share_project_lock():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="research-rebuild-sync"); db.add(project); db.flush()
        document = ResearchIngestionService().ingest(db, project.id, title="Notes", content="steam engine workshop").document
        db.commit(); project_id, document_id = project.id, document.id
    barrier = threading.Barrier(2); errors = []
    def rebuild():
        try:
            with Session() as db:
                barrier.wait(); ResearchLexicalIndexService().rebuild(db, project_id); db.commit()
        except Exception as exc: errors.append(exc)
    def sync():
        try:
            with Session() as db:
                barrier.wait(); ResearchLexicalIndexService().sync_after_ingestion(db, project_id, document_id); db.commit()
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=rebuild), threading.Thread(target=sync)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert not errors
    with Session() as db:
        assert ResearchLexicalIndexService().fast_path_available(db, project_id)
        assert __import__("app.retrieval_index", fromlist=["ResearchLexicalIndexAudit"]).ResearchLexicalIndexAudit().audit(db, project_id)["valid"]
        cleanup(db, project_id)
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


def test_postgres_ann_unsupported_dimension_uses_exact_parity():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="ann-unsupported"); db.add(project); db.flush()
        actor = Character(project_id=project.id, name="Actor"); db.add(actor); db.flush()
        memories = [CharacterMemory(character_id=actor.id, content=f"memory-{index}", importance=.1, emotional_weight=0, confidence=1, distortion={}) for index in range(14)]
        db.add_all(memories); db.flush(); CognitionRetrievalProjectionService().rebuild(db, project.id)
        config = ProjectModelConfig(project_id=project.id, embedding_enabled=True, embedding_dimension=2, memory_retrieval_mode=MemoryRetrievalMode.HYBRID_RRF, memory_vector_search_mode=MemoryVectorSearchMode.ANN)
        db.add(config)
        for index, memory in enumerate(memories):
            db.add(CharacterMemoryEmbedding(project_id=project.id, character_id=actor.id, memory_id=memory.id, embedding_config_fingerprint="ann-unsupported", provider="fake", model="fake", dimension=2, content_fingerprint=memory_content_fingerprint(memory.content), status=EmbeddingStatus.READY, embedding=[1.0, float(index) / 100]))
        db.commit(); cues = {"entity_ids": (), "participant_ids": (), "thread_ids": (), "item_ids": (), "location_ids": ()}
        assert MemoryANNIndexStatusService().status(db, project.id)["effective_mode"] == "EXACT"
        fast = CurrentCharacterCognitionFastRetriever()
        ann = fast.hybrid_memories(db, project.id, actor.id, cues, [1.0, 0.0], "ann-unsupported", config)
        config.memory_vector_search_mode = MemoryVectorSearchMode.EXACT; db.commit()
        exact = fast.hybrid_memories(db, project.id, actor.id, cues, [1.0, 0.0], "ann-unsupported", config)
        assert ann == exact
        cleanup(db, project.id)
    engine.dispose()


def test_postgres_ann_halfvec_discovers_bounded_candidates_and_reranks():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True); Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="ann-halfvec"); db.add(project); db.flush()
        actor = Character(project_id=project.id, name="Actor"); db.add(actor); db.flush()
        memories = [CharacterMemory(character_id=actor.id, content=f"memory-{index}", importance=.1, emotional_weight=0, confidence=1, distortion={}) for index in range(14)]
        db.add_all(memories); db.flush(); CognitionRetrievalProjectionService().rebuild(db, project.id)
        config = ProjectModelConfig(project_id=project.id, embedding_enabled=True, embedding_dimension=3072, memory_retrieval_mode=MemoryRetrievalMode.HYBRID_RRF, memory_vector_search_mode=MemoryVectorSearchMode.ANN)
        db.add(config)
        for index, memory in enumerate(memories):
            vector = [0.0] * 3072; vector[index] = 1.0
            db.add(CharacterMemoryEmbedding(project_id=project.id, character_id=actor.id, memory_id=memory.id, embedding_config_fingerprint="ann-halfvec", provider="fake", model="fake", dimension=3072, content_fingerprint=memory_content_fingerprint(memory.content), status=EmbeddingStatus.READY, embedding=vector))
        db.commit(); status = MemoryANNIndexStatusService().status(db, project.id)
        hidden = CurrentCharacterCognitionFastRetriever()._hidden(project.id, actor.id, "MEMORY", CharacterMemory.id)
        ids = CharacterMemoryANNSemanticRetriever().retrieve(db, project.id, actor.id, [1.0] + [0.0] * 3071, "ann-halfvec", 12, None, config, hidden)
        assert status["index_kind"] == "HALFVEC" and status["effective_mode"] == "ANN"
        assert ids is not None and len(ids) == 12 and ids[0] == memories[0].id
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
        db.commit(); seen = []; statements = []
        original = __import__("app.research", fromlist=["KnowledgeTokenizer"]).KnowledgeTokenizer.tokenize
        monkeypatch.setattr("app.research.KnowledgeTokenizer.tokenize", lambda _self, text: (seen.append(text), original(_self, text))[1])
        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", capture)
        result = ResearchIndexedBM25Retriever().search(db, project.id, "needle", filters={}, config=ResearchRetrievalConfig())
        event.remove(engine, "before_cursor_execute", capture)
        assert [item.content for item in result] == ["needle"]
        assert seen == ["needle"]
        normalized = " ".join(statements).lower()
        assert "count(" not in normalized
        assert "sum(" not in normalized
        assert "research_term_stat" in normalized
        cleanup(db, project.id)
    engine.dispose()
