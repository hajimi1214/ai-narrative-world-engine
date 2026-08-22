"""Opt-in real Retcon -> Replay -> Revision certification."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import RetconReplaySession, Scene
from benchmarks.phase16d3_runner import measure, scene_sequence_is_continuous


pytestmark = pytest.mark.skipif(os.getenv("RUN_PHASE16D3") != "1", reason="opt-in real history rewrite certification")


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def test_phase16d3_real_retcon_revision_replay_full_chain(session, monkeypatch):
    tests_path = str(Path(__file__).resolve().parents[1] / "tests")
    if tests_path not in sys.path:
        sys.path.insert(0, tests_path)
    from tests.test_retcon_replay import historical_replay_world

    project, scene, _actor, _proposal, client, application_id = historical_replay_world(session, monkeypatch)
    created = client.post(f"/projects/{project.id}/retcon/applications/{application_id}/replay-sessions")
    assert created.status_code == 201, created.text
    replay_id = created.json()["id"]
    holder = {}

    def operation():
        while True:
            state = client.get(f"/projects/{project.id}/retcon/replay-sessions/{replay_id}").json()
            if state["cursor"] >= len(state["queue"]):
                break
            response = client.post(f"/projects/{project.id}/retcon/replay-sessions/{replay_id}/step")
            assert response.status_code == 200, response.text
        committed = client.post(
            f"/projects/{project.id}/retcon/replay-sessions/{replay_id}/commit",
            json={"explicit_confirmation": True},
        )
        assert committed.status_code == 200, committed.text
        holder["status"] = committed.json()["status"]

    metrics = measure(
        session,
        name="retcon_revision_replay_full_chain",
        scale=1,
        operation=operation,
        route="RETCON_REPLAY_SUFFIX_REBUILD",
        projection_status="READY_OR_DIRTY",
        details={"revision_apply": True, "retcon_apply": True, "replay_commit": True},
    )
    assert holder["status"] == "COMPLETED"
    sequences = list(session.scalars(select(Scene.sequence).where(Scene.project_id == project.id, Scene.history_status == "ACTIVE").order_by(Scene.sequence)))
    assert sequences == list(range(min(sequences), max(sequences) + 1))
    replay_status = session.scalar(select(RetconReplaySession).where(RetconReplaySession.id == replay_id)).status
    assert getattr(replay_status, "value", replay_status) == "COMPLETED"
    assert metrics.sql_query_count > 0
