"""Phase 16C1 derived-index invariants on the portable reference backend."""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Character, CharacterKnowledge, CharacterMemory, KnowledgeStatus, Project
from app.research import ResearchIngestionService
from app.retrieval_index import (
    CognitionRetrievalIndexAudit,
    CognitionRetrievalProjectionService,
    ResearchLexicalIndexAudit,
    ResearchLexicalIndexService,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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
