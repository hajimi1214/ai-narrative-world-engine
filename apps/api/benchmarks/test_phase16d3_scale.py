"""Phase 16D3 scale proofs.

The smoke proof runs in the normal test suite.  The large corpus proof is
opt-in because it intentionally creates 10k/100k rows and belongs in a
dedicated benchmark job, not every developer test run::

    RUN_PHASE16D3=1 pytest apps/api/benchmarks/test_phase16d3_scale.py -q -s

The benchmark is read-only with respect to production semantics: fixture rows
are isolated in an in-memory database and the measured operation only reads
the current head and projection status.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    Character,
    CharacterKnowledge,
    CharacterMemory,
    KnowledgeStatus,
    Project,
    Scene,
    SceneStatus,
    StoryThread,
    ThreadStatus,
)
from app.narrative_structure_projection import NarrativeStructureProjectionService
from app.scaling import ProjectHistoryProjectionService

from benchmarks.phase16d3_runner import (
    D3_FALLBACK_EVIDENCE_KEYS,
    D3_ROUTE_EVIDENCE_KEYS,
    measure,
    report_json,
    route_evidence_report,
    scene_sequence_is_continuous,
)


@pytest.fixture()
def benchmark_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db
    engine.dispose()


def _fixture(db, size: int) -> tuple[Project, str]:
    project = Project(name=f"phase16d3-{uuid4()}")
    db.add(project)
    db.flush()
    character_ids = [str(uuid4()) for _ in range(4)]
    thread_ids = [str(uuid4()) for _ in range(4)]
    db.bulk_insert_mappings(Character, [
        {
            "id": character_id, "project_id": project.id, "name": f"Character {index}",
            "goals": {"goal": f"goal-{index}"}, "current_state": {"location_id": "benchmark-room"},
            "active": True,
        }
        for index, character_id in enumerate(character_ids)
    ])
    db.bulk_insert_mappings(StoryThread, [
        {
            "id": thread_id, "project_id": project.id, "title": f"Thread {index}",
            "type": "BENCHMARK", "status": ThreadStatus.OPEN.value, "state": {},
        }
        for index, thread_id in enumerate(thread_ids)
    ])
    db.bulk_insert_mappings(CharacterKnowledge, [
        {
            "id": str(uuid4()), "character_id": character_id,
            "proposition": f"ENTITY benchmark-{index}: active = true",
            "status": KnowledgeStatus.KNOWN.value, "confidence": 1.0,
        }
        for index, character_id in enumerate(character_ids)
    ])
    db.bulk_insert_mappings(CharacterMemory, [
        {
            "id": str(uuid4()), "character_id": character_id,
            "content": f"Benchmark memory {index}", "importance": 0.5,
            "emotional_weight": 0.0, "confidence": 1.0, "distortion": {},
        }
        for index, character_id in enumerate(character_ids)
    ])
    db.bulk_insert_mappings(Scene, [
        {
            "id": str(uuid4()), "project_id": project.id, "sequence": sequence,
            "location": "benchmark-room", "participants": character_ids[:2], "facts": [],
            "result": {"text": "x" * 100}, "story_threads": thread_ids[:2],
            "status": SceneStatus.OCCURRED.value, "history_status": "ACTIVE",
        }
        for sequence in range(1, size + 1)
    ])
    db.commit()
    latest_id = db.scalar(select(Scene.id).where(Scene.project_id == project.id).order_by(Scene.sequence.desc()).limit(1))
    assert latest_id
    return project, latest_id


def _bounded_read(db, project_id: str) -> dict[str, object]:
    """The current-head/projection read used as the D3 append boundary probe."""
    projection = ProjectHistoryProjectionService().status(db, project_id)
    structure = NarrativeStructureProjectionService().status(db, project_id)
    latest = db.scalar(select(Scene.sequence).where(
        Scene.project_id == project_id,
        Scene.history_status == "ACTIVE",
    ).order_by(Scene.sequence.desc()).limit(1)) or 0
    return {"projection": projection, "structure": structure, "latest": latest}


def test_phase16d3_smoke_emits_metrics_and_sequence_proof(benchmark_session, capsys):
    project, _ = _fixture(benchmark_session, 100)
    sequences = list(benchmark_session.scalars(
        select(Scene.sequence).where(Scene.project_id == project.id).order_by(Scene.sequence),
    ))
    metrics = measure(
        benchmark_session,
        name="bounded_current_head_read",
        scale=len(sequences),
        operation=lambda: _bounded_read(benchmark_session, project.id),
        route="CURRENT_HEAD_BOUNDED_READ",
        projection_status="MISSING",
        audit_valid=None,
        scene_sequence_continuous=scene_sequence_is_continuous(sequences),
        details={"formal_mutation": False, "fixture_rows": len(sequences)},
    )
    assert metrics.scene_sequence_continuous is True
    assert metrics.sql_query_count <= 8
    assert metrics.orm_object_hydration_count == 0
    assert metrics.route == "CURRENT_HEAD_BOUNDED_READ"
    print(report_json([metrics]))
    assert capsys.readouterr().out


def test_phase16d3_route_report_is_fail_closed():
    report = route_evidence_report({"COGNITION_FAST": {"status": "proven"}})
    assert report["fast_path"]["COGNITION_FAST"]["status"] == "proven"
    assert all(report["fast_path"][key]["status"] == "pending" for key in D3_ROUTE_EVIDENCE_KEYS if key != "COGNITION_FAST")
    assert all(report["fallback"][key]["status"] == "pending" for key in D3_FALLBACK_EVIDENCE_KEYS)


@pytest.mark.skipif(os.getenv("RUN_PHASE16D3") != "1", reason="opt-in million-word/10k-100k benchmark")
@pytest.mark.parametrize("size", [10_000, 100_000])
def test_phase16d3_scale_read_is_bounded(benchmark_session, size, capsys):
    project, _ = _fixture(benchmark_session, size)
    metrics = measure(
        benchmark_session,
        name="bounded_current_head_read",
        scale=size,
        operation=lambda: _bounded_read(benchmark_session, project.id),
        route="CURRENT_HEAD_BOUNDED_READ",
        projection_status="MISSING",
        scene_sequence_continuous=True,
        details={"fixture_rows": size, "history_payload_chars": size * 100},
    )
    assert metrics.sql_query_count <= 8
    assert metrics.orm_object_hydration_count == 0
    print(report_json([metrics]))
    assert capsys.readouterr().out
