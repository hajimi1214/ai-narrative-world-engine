"""Real D3 fault-isolation matrix for derived SceneCommit stages."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.causal_ledger import CausalLedgerService
from app.db import Base
from app.models import Scene, SceneCommit
from app.narrative_structure_projection import NarrativeStructureProjectionService
from app.retrieval_index import CognitionRetrievalProjectionService
from app.scene_commit import SceneCommitService


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def _prepared(session, monkeypatch):
    tests_path = str(Path(__file__).resolve().parents[1] / "tests")
    if tests_path not in sys.path:
        sys.path.insert(0, tests_path)
    from tests.test_scene_commit import prepared_commit
    return prepared_commit(session, monkeypatch, requires_resolution=False)


def test_phase16d3_fault_matrix_projection_isolated(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, *_ = _prepared(session, monkeypatch)

    def fail_structure(*_args, **_kwargs):
        raise RuntimeError("STRUCTURE_PROJECTION_INJECTED")

    monkeypatch.setattr(NarrativeStructureProjectionService, "_append_structure", fail_structure)
    result = SceneCommitService().commit(session, project.id, performance.id)
    session.commit()
    assert result.scene.history_status == "ACTIVE"
    status = NarrativeStructureProjectionService().status(session, project.id)
    assert status["status"] == "DIRTY"


def test_phase16d3_fault_matrix_retrieval_isolated(session, monkeypatch):
    project, _location, _actor, _other, _proposal, performance, *_ = _prepared(session, monkeypatch)

    def fail_retrieval(*_args, **_kwargs):
        raise RuntimeError("COGNITION_INDEX_INJECTED")

    monkeypatch.setattr(CognitionRetrievalProjectionService, "sync_after_scene_commit", fail_retrieval)
    result = SceneCommitService().commit(session, project.id, performance.id)
    session.commit()
    assert result.scene.history_status == "ACTIVE"
    status = CognitionRetrievalProjectionService().status(session, project.id)
    assert status["status"] == "DIRTY"


@pytest.mark.parametrize("stage, expected_scene_count", [
    ("checkpoint", 0),
    ("ledger", 0),
])
def test_phase16d3_fault_matrix_atomic_boundaries(session, monkeypatch, stage, expected_scene_count):
    project, _location, _actor, _other, _proposal, performance, *_ = _prepared(session, monkeypatch)
    if stage == "checkpoint":
        monkeypatch.setattr(SceneCommitService, "failure_injector", staticmethod(
            lambda marker: (_ for _ in ()).throw(RuntimeError("CHECKPOINT_MATERIALIZATION_INJECTED"))
            if marker == "AFTER_SCENE_COMMIT_MATERIALIZATION" else None,
        ))
    else:
        monkeypatch.setattr(CausalLedgerService, "failure_injector", staticmethod(
            lambda marker: (_ for _ in ()).throw(RuntimeError("CAUSAL_LEDGER_INJECTED"))
            if marker == "AFTER_CAUSAL_LEDGER_SYNC" else None,
        ))
    with pytest.raises(RuntimeError):
        SceneCommitService().commit(session, project.id, performance.id)
    session.rollback()
    assert session.scalar(select(SceneCommit).where(SceneCommit.project_id == project.id)) is None
    assert session.scalar(select(Scene).where(Scene.project_id == project.id)) is None
    assert session.query(Scene).filter(Scene.project_id == project.id).count() == expected_scene_count
