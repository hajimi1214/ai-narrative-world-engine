"""Phase 16C1 derived-index invariants on the portable reference backend."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.character_mind import ActiveCharacterCognitionReader, CharacterMemoryRetriever
from app.models import CausalEdgeKind, CausalLink, CausalRelationType, CausalResourceType, Character, CharacterKnowledge, CharacterMemory, CharacterMemoryCueRef, CharacterMemorySearchIndex, CharacterKnowledgeSearchIndex, CognitionUsageHead, KnowledgeStatus, Project, ResearchChunkLexicalIndex, ResearchTermPosting, ResearchTermStat, Scene, SceneStatus
from app.research import ResearchIngestionService
from app.retrieval_index import (
    CognitionRetrievalIndexAudit,
    CognitionRetrievalProjectionService,
    ResearchLexicalIndexAudit,
    ResearchLexicalIndexService,
    CurrentCharacterCognitionFastRetriever,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda connection, _record: (
        connection.create_function("least", -1, min),
        connection.create_function("greatest", -1, max),
    ))
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def cognition_fixture(db):
    project = Project(name="Retrieval")
    db.add(project); db.flush()
    actor = Character(project_id=project.id, name="Actor")
    db.add(actor); db.flush()
    knowledge = CharacterKnowledge(character_id=actor.id, proposition="ENTITY door: open = true", status=KnowledgeStatus.KNOWN, confidence=0.9)
    memory = CharacterMemory(character_id=actor.id, content="The door opened.", importance=0.8, emotional_weight=0.2, confidence=1.0, distortion={"entity_ids": ["door"]})
    db.add_all([knowledge, memory]); db.flush()
    return project, actor, knowledge, memory


def test_cognition_projection_rebuilds_as_derived_sqlite_state(session):
    project, _actor, knowledge, memory = cognition_fixture(session)
    state = CognitionRetrievalProjectionService().rebuild(session, project.id)
    assert state.status.value == "READY"
    assert state.indexed_knowledge_count == state.indexed_memory_count == 1
    assert CognitionRetrievalIndexAudit().audit(session, project.id)["valid"]
    # SQLite is deliberately the reference implementation, never the fast path.
    assert not CognitionRetrievalProjectionService().fast_path_available(session, project.id)


def test_cognition_audit_detects_derived_column_tamper(session):
    project, _actor, _knowledge, _memory = cognition_fixture(session)
    CognitionRetrievalProjectionService().rebuild(session, project.id)
    from app.models import CharacterKnowledgeSearchIndex
    row = session.scalar(select(CharacterKnowledgeSearchIndex).where(CharacterKnowledgeSearchIndex.project_id == project.id))
    row.subject_id = "tampered"
    with pytest.raises(ValueError, match="COGNITION_RETRIEVAL_INDEX_INTEGRITY_INVALID"):
        CognitionRetrievalIndexAudit().audit(session, project.id)


@pytest.mark.parametrize("model,field,value", [
    (CharacterKnowledgeSearchIndex, "predicate", "tampered"),
    (CharacterKnowledgeSearchIndex, "value_fingerprint", "tampered"),
    (CharacterMemorySearchIndex, "emotional_weight", 99.0),
    (CharacterMemorySearchIndex, "source_bucket", "scene:tampered"),
])
def test_cognition_audit_detects_all_derived_column_tamper(session, model, field, value):
    project, _actor, _knowledge, _memory = cognition_fixture(session)
    CognitionRetrievalProjectionService().rebuild(session, project.id)
    row = session.scalar(select(model).where(model.project_id == project.id))
    setattr(row, field, value)
    with pytest.raises(ValueError, match="COGNITION_RETRIEVAL_INDEX_INTEGRITY_INVALID"):
        CognitionRetrievalIndexAudit().audit(session, project.id)


def test_cognition_audit_detects_cue_and_usage_tamper(session):
    project, actor, knowledge, memory = cognition_fixture(session)
    CognitionRetrievalProjectionService().rebuild(session, project.id)
    cue = session.scalar(select(CharacterMemoryCueRef).where(CharacterMemoryCueRef.project_id == project.id))
    cue.cue_value = "tampered"
    with pytest.raises(ValueError, match="COGNITION_RETRIEVAL_INDEX_INTEGRITY_INVALID"):
        CognitionRetrievalIndexAudit().audit(session, project.id)
    session.rollback(); CognitionRetrievalProjectionService().rebuild(session, project.id)
    head = CognitionUsageHead(project_id=project.id, resource_type="CHARACTER_MEMORY", resource_id=memory.id, usage_count=9, latest_sequence=3, usage_fingerprint="bad")
    session.add(head)
    with pytest.raises(ValueError, match="COGNITION_RETRIEVAL_INDEX_INTEGRITY_INVALID"):
        CognitionRetrievalIndexAudit().audit(session, project.id)


def test_cognition_incremental_usage_head_never_recounts_historical_links(session):
    """A normal append reads only its causal links, even after a large prefix."""
    project, actor, _knowledge, memory = cognition_fixture(session)
    first = Scene(project_id=project.id, sequence=1, status=SceneStatus.OCCURRED, history_status="ACTIVE")
    session.add(first); session.flush()
    historical = [
        CausalLink(
            project_id=project.id,
            cause_type=CausalResourceType.CHARACTER_MEMORY,
            cause_id=memory.id,
            effect_type=CausalResourceType.CHARACTER_DECISION,
            effect_id=f"historical-decision-{index}",
            edge_kind=CausalEdgeKind.CAUSAL,
            relation_type=CausalRelationType.MEMORY_INFORMED_DECISION,
            scene_id=first.id,
            sequence=1,
            source_key=f"historical-memory-usage-{index}",
            link_fingerprint=f"historical-memory-usage-fp-{index}",
        )
        for index in range(10_000)
    ]
    session.add_all(historical); session.flush()
    CognitionRetrievalProjectionService().rebuild(session, project.id)
    second = Scene(project_id=project.id, sequence=2, status=SceneStatus.OCCURRED, history_status="ACTIVE")
    session.add(second); session.flush()
    session.add(CausalLink(
        project_id=project.id,
        cause_type=CausalResourceType.CHARACTER_MEMORY,
        cause_id=memory.id,
        effect_type=CausalResourceType.CHARACTER_DECISION,
        effect_id="new-decision",
        edge_kind=CausalEdgeKind.CAUSAL,
        relation_type=CausalRelationType.MEMORY_INFORMED_DECISION,
        scene_id=second.id,
        sequence=2,
        source_key="new-memory-usage",
        link_fingerprint="new-memory-usage-fp",
    ))
    session.flush()
    statements = []

    def receive(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement.lower())

    from sqlalchemy import event
    event.listen(session.bind, "before_cursor_execute", receive)
    try:
        service = CognitionRetrievalProjectionService()
        service.sync_after_scene_commit(session, project.id, second.id, second.sequence)
        session.flush()
    finally:
        event.remove(session.bind, "before_cursor_execute", receive)
    head = session.scalar(select(CognitionUsageHead).where(
        CognitionUsageHead.project_id == project.id,
        CognitionUsageHead.resource_id == memory.id,
    ))
    assert head.usage_count == 10_001
    causal_queries = [statement for statement in statements if "causal_links" in statement]
    assert causal_queries
    assert all("count(" not in statement and "max(" not in statement for statement in causal_queries)
    assert all("scene_id" in statement for statement in causal_queries)
    service.sync_after_scene_commit(session, project.id, second.id, second.sequence)
    assert head.usage_count == 10_001
    assert CognitionRetrievalIndexAudit().audit(session, project.id)["valid"]


def test_fast_memory_payload_preserves_source_diversity_and_happened_at_ties(session):
    """The fast scalar ranking must retain every frozen tie-breaker."""
    project, actor, _knowledge, _memory = cognition_fixture(session)
    source = Scene(
        project_id=project.id, sequence=1, status=SceneStatus.OCCURRED,
        history_status="ACTIVE", location="archive", participants=[actor.id],
    )
    session.add(source); session.flush()
    base = datetime(2030, 1, 1)
    added = [
        CharacterMemory(
            character_id=actor.id, source_scene=source.id, content=f"tie {index}",
            importance=0.5, emotional_weight=0, confidence=1,
            happened_at=base + timedelta(days=offset), distortion={},
        )
        for index, offset in enumerate((3, 1, 2, 4))
    ]
    session.add_all(added); session.flush()
    CognitionRetrievalProjectionService().rebuild(session, project.id)
    cues = {"entity_ids": (), "participant_ids": (), "thread_ids": (), "item_ids": (), "location_ids": ()}
    legacy = CharacterMemoryRetriever().retrieve(
        session, project.id,
        ActiveCharacterCognitionReader().memories(session, project.id, actor.id),
        cues,
    )
    fast = CurrentCharacterCognitionFastRetriever().memories(session, project.id, actor.id, cues)
    assert [item["memory_id"] for item in fast] == [item["memory_id"] for item in legacy]


def test_research_ingestion_builds_derived_lexical_state_and_audit(session):
    project = Project(name="Research")
    session.add(project); session.flush()
    ResearchIngestionService().ingest(session, project.id, title="Notes", content="Steam engine workshop")
    state = ResearchLexicalIndexService()._state(session, project.id)
    assert state and state.status.value == "READY" and state.active_chunk_count == 1
    assert ResearchLexicalIndexAudit().audit(session, project.id)["valid"]
    assert not ResearchLexicalIndexService().fast_path_available(session, project.id)


def test_research_audit_detects_term_stat_tamper(session):
    project = Project(name="Research")
    session.add(project); session.flush()
    ResearchIngestionService().ingest(session, project.id, title="Notes", content="Steam engine workshop")
    from app.models import ResearchTermStat
    row = session.scalar(select(ResearchTermStat).where(ResearchTermStat.project_id == project.id))
    row.document_frequency += 1
    with pytest.raises(ValueError, match="RESEARCH_LEXICAL_INDEX_INTEGRITY_INVALID"):
        ResearchLexicalIndexAudit().audit(session, project.id)


@pytest.mark.parametrize("model,field,value", [
    (ResearchTermPosting, "term_frequency", 99),
    (ResearchChunkLexicalIndex, "token_count", 99),
    (ResearchChunkLexicalIndex, "index_fingerprint", "tampered"),
])
def test_research_audit_detects_derived_column_tamper(session, model, field, value):
    project = Project(name="Research")
    session.add(project); session.flush()
    ResearchIngestionService().ingest(session, project.id, title="Notes", content="Steam engine workshop")
    row = session.scalar(select(model).where(model.project_id == project.id))
    setattr(row, field, value)
    with pytest.raises(ValueError, match="RESEARCH_LEXICAL_INDEX_INTEGRITY_INVALID"):
        ResearchLexicalIndexAudit().audit(session, project.id)
