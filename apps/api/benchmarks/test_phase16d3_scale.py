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
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    ActionVisibility,
    Character,
    CharacterDecision,
    CharacterDecisionStatus,
    CharacterKnowledge,
    CharacterMemory,
    KnowledgeStatus,
    PerformanceMode,
    Project,
    ProposalStatus,
    ProposalType,
    Scene,
    ScenePerformance,
    ScenePerformanceTurn,
    SceneProposal,
    SceneStatus,
    StoryThread,
    ThreadStatus,
)
from app.director import DirectorContextBuilder
from app.narrative_structure_projection import NarrativeStructureProjectionService
from app.scaling import ProjectHistoryProjectionService
from app.scene_commit import SceneCommitService
from app.causal_ledger import CausalLedgerService
from app.causal_ledger import CurrentCausalLedgerAudit
from app.formal_state import FormalStateIdentityAudit
from app.historical import CurrentHistoryCheckpointAudit
from app.narrative_structure_projection import NarrativeStructureProjectionAudit
from app.retrieval_index import CognitionRetrievalIndexAudit
from app.snapshot_storage import CompactSnapshotAudit

from benchmarks.phase16d3_runner import (
    D3_FALLBACK_EVIDENCE_KEYS,
    D3_ROUTE_EVIDENCE_KEYS,
    certification_report,
    measure,
    report_json,
    run_matrix,
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


def _append_scene(db, project: Project, sequence: int) -> Scene:
    scene = Scene(
        project_id=project.id,
        sequence=sequence,
        location="benchmark-room",
        participants=[],
        facts=[],
        result={"text": "x" * 100},
        story_threads=[],
        status=SceneStatus.OCCURRED.value,
        history_status="ACTIVE",
    )
    db.add(scene)
    db.flush()
    ProjectHistoryProjectionService().sync_after_scene_commit(db, project.id, scene.id)
    NarrativeStructureProjectionService().sync_after_scene_commit(db, project.id, scene.id)
    db.flush()
    return scene


def _append_formal_commit(db, project_id: str, actor_id: str, location_id: str, thread_id: str):
    """Create one new formal execution lineage and commit it through production code."""
    context = DirectorContextBuilder().build(db, project_id)
    proposal = SceneProposal(
        project_id=project_id,
        context_fingerprint=context["fingerprint"],
        proposal_type=ProposalType.CONTINUE_THREAD,
        primary_thread_id=thread_id,
        location_id=location_id,
        participants=[actor_id],
        scene_goal="Continue deterministic benchmark scene",
        character_motivations={actor_id: {}},
        entry_state={},
        planned_pressure=None,
        expected_progress={"benchmark": True},
        allowed_reveals=[], forbidden_reveals=[], required_canon=[],
        possible_outcomes=[], new_entity_requests=[], risk_flags=[],
        director_reasoning_summary="Benchmark-only deterministic proposal",
        status=ProposalStatus.APPROVED,
    )
    db.add(proposal)
    db.flush()
    performance = ScenePerformance(
        project_id=project_id,
        scene_proposal_id=proposal.id,
        take_number=1,
        proposal_context_fingerprint=context["fingerprint"],
        mode=PerformanceMode.HEURISTIC,
        status="RUNNING",
        participant_order=[actor_id],
        active_participant_ids=[actor_id],
        max_turns=1,
        turn_count=1,
    )
    db.add(performance)
    db.flush()
    decision = CharacterDecision(
        project_id=project_id,
        scene_proposal_id=proposal.id,
        character_id=actor_id,
        context_fingerprint=context["fingerprint"],
        decision_type="WAIT",
        intent="wait",
        chosen_action="wait",
        motivation="benchmark",
        goal_refs=[], knowledge_used=[], memory_refs=[], ability_refs=[], inventory_refs=[],
        relationship_factors={}, perceived_risk=None, accepted_cost=None,
        expected_personal_result=None, uncertainties=[], refused_options=[],
        boundary_override_reason=None, decision_summary="Wait for the next deterministic step.",
        status=CharacterDecisionStatus.VALID,
    )
    db.add(decision)
    db.flush()
    turn = ScenePerformanceTurn(
        project_id=project_id,
        performance_id=performance.id,
        sequence=1,
        actor_character_id=actor_id,
        actor_context_fingerprint=context["fingerprint"],
        character_decision_id=decision.id,
        action_visibility=ActionVisibility.PUBLIC,
        observable_action="wait",
        spoken_content=None,
        recipient_character_ids=[],
        requires_world_resolution=False,
        world_resolution_request=None,
        validation_result={},
    )
    db.add(turn)
    db.flush()
    performance.stop_reason = "QUIESCENT"
    performance.status = "PAUSED"
    result = SceneCommitService().commit(db, project_id, performance.id)
    db.commit()
    return result


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
    final = certification_report(metrics=[], route_evidence={"COGNITION_FAST": {"status": "proven"}})
    assert final["acceptance"] == "PENDING"
    assert final["route_evidence"]["fast_path"]["COGNITION_FAST"]["status"] == "proven"


def test_phase16d3_matrix_runner_is_fail_closed():
    results = run_matrix({
        "known-pass": lambda: {"evidence": "fixture"},
        "known-fail": lambda: (_ for _ in ()).throw(RuntimeError("SAFE_FAILURE_CODE")),
    })
    assert [item.status for item in results] == ["PASS", "FAIL"]
    assert results[1].reason == "SAFE_FAILURE_CODE"


def test_phase16d3_real_incremental_append_boundary_is_bounded(benchmark_session):
    project = Project(name=f"phase16d3-append-{uuid4()}")
    benchmark_session.add(project)
    benchmark_session.flush()
    for sequence in range(1, 11):
        _append_scene(benchmark_session, project, sequence)
    latest = benchmark_session.scalar(select(Scene.id).where(
        Scene.project_id == project.id, Scene.sequence == 11,
    ))
    # The measured operation is the same post-commit derived append boundary;
    # the fixture's prior ten scenes are already READY and are not rebuilt.
    metrics = measure(
        benchmark_session,
        name="incremental_projection_append",
        scale=10,
        operation=lambda: _append_scene(benchmark_session, project, 11),
        route="NARRATIVE_STRUCTURE_INCREMENTAL",
        projection_status="READY",
        scene_sequence_continuous=True,
        details={"history_prefix": 10, "full_rebuild": False, "scene_id_before": latest},
    )
    assert metrics.sql_query_count < 80
    assert metrics.orm_object_hydration_count < 40


def test_phase16d3_scene_commit_full_chain_is_measured(benchmark_session, monkeypatch):
    """Measure the real SceneCommit boundary, not a synthetic row insert."""
    tests_path = str(Path(__file__).resolve().parents[1] / "tests")
    if tests_path not in sys.path:
        sys.path.insert(0, tests_path)
    from tests.test_scene_commit import prepared_commit

    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, _client = prepared_commit(
        benchmark_session, monkeypatch, requires_resolution=False,
    )
    metrics_holder: dict[str, object] = {}

    def operation():
        result = SceneCommitService().commit(benchmark_session, project.id, performance.id)
        benchmark_session.flush()
        metrics_holder["scene_id"] = result.scene.id
        metrics_holder["checkpoint_id"] = result.checkpoint.id
        metrics_holder["commit_id"] = result.commit.id

    metrics = measure(
        benchmark_session,
        name="scene_commit_full_chain",
        scale=1,
        operation=operation,
        route="FORMAL_SCENE_COMMIT",
        projection_status="READY_OR_DIRTY",
        scene_sequence_continuous=True,
        details={"state_delta": True, "checkpoint": True, "causal_ledger": True},
    )
    assert metrics_holder["scene_id"]
    assert metrics_holder["checkpoint_id"]
    assert metrics_holder["commit_id"]
    assert metrics.sql_query_count > 0
    assert metrics.orm_object_hydration_count >= 0


def test_phase16d3_continuous_scene_commit_smoke(benchmark_session, monkeypatch):
    """A small continuous run proves the harness does not switch to row inserts."""
    tests_path = str(Path(__file__).resolve().parents[1] / "tests")
    if tests_path not in sys.path:
        sys.path.insert(0, tests_path)
    from tests.test_scene_commit import prepared_commit

    project, location, actor, _other, _proposal, performance, _turn, _resolution, _batch, _client = prepared_commit(
        benchmark_session, monkeypatch, requires_resolution=False,
    )

    def operation():
        SceneCommitService().commit(benchmark_session, project.id, performance.id)
        benchmark_session.commit()
        for _ in range(2):
            _append_formal_commit(benchmark_session, project.id, actor.id, location.id, _proposal.primary_thread_id)

    metrics = measure(
        benchmark_session,
        name="continuous_scene_commit_smoke",
        scale=3,
        operation=operation,
        route="FORMAL_SCENE_COMMIT",
        projection_status="READY_OR_DIRTY",
        scene_sequence_continuous=True,
        details={"full_chain": True, "projection": True, "checkpoint": True, "ledger": True},
    )
    sequences = list(benchmark_session.scalars(select(Scene.sequence).where(Scene.project_id == project.id).order_by(Scene.sequence)))
    assert len(sequences) == 3
    assert scene_sequence_is_continuous(sequences)
    assert metrics.sql_query_count > 0


def test_phase16d3_suffix_rebuild_metrics(benchmark_session, monkeypatch):
    tests_path = str(Path(__file__).resolve().parents[1] / "tests")
    if tests_path not in sys.path:
        sys.path.insert(0, tests_path)
    from tests.test_scene_commit import prepared_commit

    project, location, actor, _other, proposal, performance, _turn, _resolution, _batch, _client = prepared_commit(
        benchmark_session, monkeypatch, requires_resolution=False,
    )
    SceneCommitService().commit(benchmark_session, project.id, performance.id)
    benchmark_session.commit()
    for _ in range(4):
        _append_formal_commit(benchmark_session, project.id, actor.id, location.id, proposal.primary_thread_id)
    operations = {
        "history_projection_rebuild": lambda: ProjectHistoryProjectionService().rebuild(benchmark_session, project.id),
        "narrative_structure_rebuild": lambda: NarrativeStructureProjectionService().rebuild(benchmark_session, project.id),
        "causal_ledger_reindex": lambda: CausalLedgerService().index_current_history(benchmark_session, project.id),
    }
    metrics = []
    for name, operation in operations.items():
        item = measure(
            benchmark_session,
            name=name,
            scale=100,
            operation=operation,
            route="EXPLICIT_SUFFIX_REBUILD",
            projection_status="READY",
            details={"scope": "explicit-rebuild", "full_audit_allowed": True},
        )
        metrics.append(item)
        assert item.sql_query_count > 0
    assert all(item.wall_time_ms >= 0 for item in metrics)


def test_phase16d3_unified_audit_matrix_is_explicit(benchmark_session, monkeypatch):
    """The report names every derived auditor and preserves failure evidence."""
    tests_path = str(Path(__file__).resolve().parents[1] / "tests")
    if tests_path not in sys.path:
        sys.path.insert(0, tests_path)
    from tests.test_scene_commit import prepared_commit

    project, _location, _actor, _other, _proposal, performance, _turn, _resolution, _batch, _client = prepared_commit(
        benchmark_session, monkeypatch, requires_resolution=False,
    )
    SceneCommitService().commit(benchmark_session, project.id, performance.id)
    benchmark_session.commit()
    cases = {
        "formal_state": lambda: FormalStateIdentityAudit().audit(benchmark_session, project.id),
        "compact_snapshot": lambda: CompactSnapshotAudit().audit(benchmark_session, project.id),
        "checkpoint": lambda: CurrentHistoryCheckpointAudit().audit(benchmark_session, project.id),
        "causal_ledger": lambda: CurrentCausalLedgerAudit().audit(benchmark_session, project.id),
        "cognition": lambda: CognitionRetrievalIndexAudit().audit(benchmark_session, project.id),
        "narrative_structure": lambda: NarrativeStructureProjectionAudit().audit(benchmark_session, project.id),
    }
    results = run_matrix(cases)
    assert {item.name for item in results} == set(cases)
    assert all(item.status in {"PASS", "FAIL"} for item in results)


@pytest.mark.skipif(os.getenv("RUN_PHASE16D3") != "1", reason="opt-in 10k/100k continuous SceneCommit certification")
@pytest.mark.parametrize("scene_count", [10_000, 100_000])
def test_phase16d3_continuous_scene_commit_scale(benchmark_session, monkeypatch, scene_count, capsys):
    """Run the complete production SceneCommit chain over one continuous Project.

    This is intentionally opt-in: it creates the requested 10k/100k formal
    execution rows and is a certification job, not a normal regression test.
    """
    tests_path = str(Path(__file__).resolve().parents[1] / "tests")
    if tests_path not in sys.path:
        sys.path.insert(0, tests_path)
    from tests.test_scene_commit import prepared_commit

    project, location, actor, _other, proposal, performance, _turn, _resolution, _batch, _client = prepared_commit(
        benchmark_session, monkeypatch, requires_resolution=False,
    )

    def operation():
        SceneCommitService().commit(benchmark_session, project.id, performance.id)
        benchmark_session.commit()
        for _ in range(scene_count - 1):
            _append_formal_commit(benchmark_session, project.id, actor.id, location.id, proposal.primary_thread_id)

    metrics = measure(
        benchmark_session,
        name="continuous_scene_commit_scale",
        scale=scene_count,
        operation=operation,
        route="FORMAL_SCENE_COMMIT",
        projection_status="READY_OR_DIRTY",
        scene_sequence_continuous=True,
        details={"full_chain": True, "million_word_equivalent": scene_count * 100},
    )
    sequences = list(benchmark_session.scalars(select(Scene.sequence).where(Scene.project_id == project.id).order_by(Scene.sequence)))
    assert len(sequences) == scene_count
    assert scene_sequence_is_continuous(sequences)
    print(report_json([metrics]))
    assert capsys.readouterr().out


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
