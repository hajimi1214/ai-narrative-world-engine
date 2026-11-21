from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.ai.errors import MODEL_AUTH_FAILED, MODEL_PROVIDER_NOT_CONFIGURED, MODEL_RATE_LIMITED, MODEL_TIMEOUT, MODEL_UPSTREAM_ERROR
from app.author_recovery import LostRunClaim, recovery_payload, utc_now
from app.auto_director_worker import AutoDirectorWorker
from app.models import AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage, Project
from test_author_guided_volume import _setup


@pytest.fixture
def active_run(monkeypatch):
    client, sessions, project_id = _setup(monkeypatch)
    created = client.post(
        f"/projects/{project_id}/author-guided-volume/runs",
        json={"idempotency_key": "recovery-worker", "continuous_chapter_progression": True},
    )
    assert created.status_code == 201, created.text
    return client, sessions, project_id, created.json()["id"]


def test_only_one_worker_claims_and_stale_lease_can_be_recovered(active_run):
    client, sessions, project_id, run_id = active_run
    first, second = AutoDirectorWorker(sessions), AutoDirectorWorker(sessions)
    with sessions() as db:
        db.get(AutoDirectorRun, run_id).settings = {"target_words_per_chapter": 1500}
        db.commit()
    stale_claim = first._claim()
    assert stale_claim and second._claim() is None

    with sessions() as db:
        run = db.get(AutoDirectorRun, run_id)
        assert run.worker_claim_token == stale_claim[1]
        run.worker_lease_until = utc_now() - timedelta(seconds=1)
        db.commit()

    new_claim = second._claim()
    assert new_claim and new_claim[0] == run_id and new_claim[1] != stale_claim[1]
    with sessions() as db:
        assert recovery_payload(db.get(AutoDirectorRun, run_id))["reserved_tokens"] == 3000
        with pytest.raises(LostRunClaim):
            first._fence(db, *stale_claim)

    paused = client.post(f"/projects/{project_id}/author-guided-volume/runs/{run_id}/pause", json={"reason": "AUTHOR_PAUSED"})
    assert paused.status_code == 200, paused.text
    with sessions() as db:
        assert db.get(AutoDirectorRun, run_id).worker_claim_token is None
        with pytest.raises(LostRunClaim):
            second._fence(db, *new_claim)


@pytest.mark.parametrize("code,status,expected", [
    (MODEL_RATE_LIMITED, 429, True),
    (MODEL_TIMEOUT, None, True),
    (MODEL_UPSTREAM_ERROR, 503, True),
    (MODEL_UPSTREAM_ERROR, 400, False),
    (MODEL_AUTH_FAILED, 401, False),
])
def test_provider_failure_is_retried_only_when_temporary(active_run, code, status, expected):
    _, sessions, _, run_id = active_run
    worker = AutoDirectorWorker(sessions)
    with sessions() as db:
        run = db.get(AutoDirectorRun, run_id)
        run.settings = {"max_retries": 2, "operational_token_budget": 10000}
        worker._handle_failure(run, code=code, stage="QUALITY_REVIEW", upstream_status=status, step_id="review-1")
        if expected:
            assert run.status == AutoDirectorRunStatus.RUNNING
            assert run.recovery_attempts == 1 and run.next_retry_at > utc_now()
            assert recovery_payload(run)["state"] == "WAITING_RETRY"
        elif code == MODEL_AUTH_FAILED:
            assert run.status == AutoDirectorRunStatus.BLOCKED
            assert recovery_payload(run)["state"] == "NEEDS_AUTHOR"
        else:
            assert run.status == AutoDirectorRunStatus.FAILED
            assert recovery_payload(run)["state"] == "NEEDS_AUTHOR"


def test_retry_exhaustion_stops_and_requires_author_action(active_run):
    _, sessions, _, run_id = active_run
    worker = AutoDirectorWorker(sessions)
    with sessions() as db:
        run = db.get(AutoDirectorRun, run_id)
        run.settings = {"max_retries": 1, "operational_token_budget": 1000}
        worker._handle_failure(run, code=MODEL_RATE_LIMITED, stage="QUALITY_REVIEW", step_id="review-1")
        assert run.recovery_attempts == 1 and run.next_retry_at
        run.next_retry_at = None
        worker._handle_failure(run, code=MODEL_RATE_LIMITED, stage="QUALITY_REVIEW", step_id="review-1")
        assert run.recovery_attempts == 1
        assert run.status == AutoDirectorRunStatus.BLOCKED
        assert run.pause_reason == "AUTO_RETRY_EXHAUSTED"
        assert recovery_payload(run)["state"] == "NEEDS_AUTHOR"


def test_budget_includes_uncertain_usage_reserve_and_stops_at_saved_checkpoint(active_run):
    _, sessions, _, run_id = active_run
    worker = AutoDirectorWorker(sessions)
    with sessions() as db:
        run = db.get(AutoDirectorRun, run_id)
        run.settings = {"operational_token_budget": 110}
        run.total_tokens = 90
        run.context = {"recovery_reserved_tokens": 20}
        run.current_stage = AutoDirectorStage.QUALITY_REVIEW
        run.checkpoint_stage = AutoDirectorStage.CHAPTER_EXECUTION.value
        run.checkpoint_saved_at = datetime.utcnow()
        worker._handle_failure(run, code=MODEL_TIMEOUT, stage="QUALITY_REVIEW", step_id="review-timeout")
        assert run.status == AutoDirectorRunStatus.PAUSED
        assert run.pause_reason == "TOKEN_BUDGET_EXCEEDED"
        assert run.recovery_usage_uncertain
        state = recovery_payload(run)
        assert state["usage_uncertain"] and state["last_checkpoint"]["stage"] == AutoDirectorStage.CHAPTER_EXECUTION.value
        assert run.current_stage == AutoDirectorStage.PAUSED
