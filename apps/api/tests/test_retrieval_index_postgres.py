"""PostgreSQL-only Phase 16C1 retrieval parity proofs."""
import os

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.character_mind import CharacterMindViewBuilder
from app.models import (
    Character, CharacterKnowledge, CharacterMemory, CharacterMemoryCueRef,
    CharacterMemorySearchIndex, CharacterKnowledgeSearchIndex, CognitionUsageHead,
    KnowledgeStatus, Project, ProjectCognitionRetrievalIndex, ResearchChunk,
    ResearchChunkLexicalIndex, ResearchDocument, ResearchDocumentRevision,
    ResearchLexicalIndexState, ResearchTermPosting, ResearchTermStat, SceneProposal,
)
from app.research import ResearchBM25Retriever, ResearchIngestionService
from app.retrieval_index import CognitionRetrievalProjectionService, ResearchLexicalIndexService


DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL.startswith("postgresql"), reason="requires PostgreSQL DATABASE_URL")


def cleanup(db, project_id):
    memory_ids = select(CharacterMemory.id).join(Character).where(Character.project_id == project_id)
    knowledge_ids = select(CharacterKnowledge.id).join(Character).where(Character.project_id == project_id)
    for model in (ResearchTermPosting, ResearchTermStat, ResearchChunkLexicalIndex, ResearchLexicalIndexState,
                  CharacterMemoryCueRef, CharacterMemorySearchIndex, CharacterKnowledgeSearchIndex,
                  CognitionUsageHead, ProjectCognitionRetrievalIndex, ResearchChunk,
                  ResearchDocumentRevision, ResearchDocument):
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
