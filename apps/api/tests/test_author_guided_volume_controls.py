"""Control the author volume worker without touching the author's database."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AI_PROVIDER"] = "disabled"
os.environ["AI_API_KEY"] = ""
os.environ["AI_EMBEDDING_PROVIDER"] = "disabled"
os.environ["AI_EMBEDDING_API_KEY"] = ""

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.settings import Settings
Settings.model_config["env_file"] = None

import app.api as api_module
from app.auto_director_worker import AutoDirectorWorker
from app.db import Base
from app.main import app
from app.models import (
    AutoDirectorRun, AutoDirectorRunStatus, AutoDirectorStage,
    AutoDirectorStep, AutoDirectorStepStatus, ChapterPlanningWindow,
    ChapterWindowStatus, Project, VolumeContract, VolumeContractStatus,
)


@pytest.fixture
def control_api(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db:
        project = Project(name="隔离卷控件测试", story_seed="仅使用 fake 测试数据")
        db.add(project)
        db.flush()
        volumes = []
        for number in (1, 2):
            volume = VolumeContract(
                project_id=project.id, volume_number=number,
                status=VolumeContractStatus.ACTIVE,
                title=f"测试卷 {number}", fingerprint=f"volume-{number}",
            )
            db.add(volume)
            db.flush()
            window = ChapterPlanningWindow(
                project_id=project.id, volume_id=volume.id,
                start_chapter_number=1, end_chapter_number=5,
                status=ChapterWindowStatus.ACTIVE,
                plan_fingerprint=f"window-{number}",
            )
            db.add(window)
            db.flush()
            volumes.append((volume, window))
        db.commit()
        project_id = project.id
        (first_volume, first_window), (second_volume, second_window) = volumes
        volume_ids = (first_volume.id, second_volume.id)
        window_ids = (first_window.id, second_window.id)
    monkeypatch.setattr(api_module, "SessionLocal", session_factory)
    yield {
        "client": TestClient(app), "Session": session_factory,
        "project_id": project_id, "volume_ids": volume_ids,
        "window_ids": window_ids,
    }
    engine.dispose()


def add_run(context, *, volume_index=0, status=AutoDirectorRunStatus.RUNNING,
            stage=AutoDirectorStage.CHAPTER_WINDOW_EXECUTION, max_retries=2,
            failed_attempt=None):
    with context["Session"]() as db:
        project_id = context["project_id"]
        volume_id = context["volume_ids"][volume_index]
        window_id = context["window_ids"][volume_index]
        run = AutoDirectorRun(
            project_id=project_id,
            idempotency_key=f"control-{volume_index}-{status.value}-{stage.value}",
            run_mode="AUTHOR_GUIDED_VOLUME", status=status, current_stage=stage,
            settings={"max_retries": max_retries},
            context={"volume_id": volume_id, "window_id": window_id, "execute_window": True},
        )
        db.add(run)
        db.flush()
        if failed_attempt is not None:
            step = AutoDirectorStep(
                run_id=run.id, stage=AutoDirectorStage.CHAPTER_WINDOW_PLANNING,
                status=AutoDirectorStepStatus.FAILED, attempt=failed_attempt,
                input_fingerprint=f"failed-{failed_attempt}", error_code="PLAN_PROVIDER_FAILED",
            )
            db.add(step)
            db.flush()
            run.context = {**run.context, "failed_step_id": step.id}
        db.commit()
        return run.id


def test_pause_volume_stops_worker_and_only_reschedules_the_run_window(control_api):
    run_id = add_run(control_api)
    url = f"/projects/{control_api['project_id']}/volumes/{control_api['volume_ids'][0]}/pause"
    response = control_api["client"].post(url, json={"reason": "检查章节走向"})
    assert response.status_code == 200, response.text
    assert response.json()["paused"] is True
    assert response.json()["run"]["id"] == run_id

    with control_api["Session"]() as db:
        run = db.get(AutoDirectorRun, run_id)
        assert run.status == AutoDirectorRunStatus.PAUSED
        assert run.current_stage == AutoDirectorStage.PAUSED
        assert run.context["stop_requested"] is True
        assert run.context["resume_stage"] == AutoDirectorStage.CHAPTER_WINDOW_EXECUTION.value
        paused_window = db.get(ChapterPlanningWindow, control_api["window_ids"][0])
        unrelated_window = db.get(ChapterPlanningWindow, control_api["window_ids"][1])
        assert paused_window.status == ChapterWindowStatus.PLANNING
        assert paused_window.author_note == "检查章节走向"
        assert unrelated_window.status == ChapterWindowStatus.ACTIVE

    # The background worker selects RUNNING rows only, so a subsequent poll
    # cannot advance a paused checkpoint.
    assert AutoDirectorWorker(control_api["Session"], poll_seconds=0).run_once() is False


@pytest.mark.parametrize("action", ["continue", "extend"])
def test_exhausted_volume_checkpoint_blocks_without_changing_volume_or_window(control_api, action):
    run_id = add_run(
        control_api, status=AutoDirectorRunStatus.FAILED,
        stage=AutoDirectorStage.FAILED, max_retries=2, failed_attempt=3,
    )
    volume_id = control_api["volume_ids"][0]
    window_id = control_api["window_ids"][0]
    with control_api["Session"]() as db:
        before_volume = db.get(VolumeContract, volume_id).status
        before_window = db.get(ChapterPlanningWindow, window_id).status

    response = control_api["client"].post(
        f"/projects/{control_api['project_id']}/volumes/{volume_id}/{action}",
        json={"window_size": 8},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MAX_RETRIES_REACHED"

    with control_api["Session"]() as db:
        run = db.get(AutoDirectorRun, run_id)
        assert run.status == AutoDirectorRunStatus.BLOCKED
        assert run.current_stage == AutoDirectorStage.BLOCKED
        assert run.pause_reason == "MAX_RETRIES_REACHED"
        assert db.get(VolumeContract, volume_id).status == before_volume
        assert db.get(ChapterPlanningWindow, window_id).status == before_window
        assert db.get(AutoDirectorStep, run.context["failed_step_id"]).attempt == 3


def test_author_pause_resumes_without_spending_a_retry_and_clears_stop_flag(control_api):
    run_id = add_run(
        control_api, status=AutoDirectorRunStatus.PAUSED,
        stage=AutoDirectorStage.PAUSED,
    )
    with control_api["Session"]() as db:
        run = db.get(AutoDirectorRun, run_id)
        run.context = {**run.context, "resume_stage": AutoDirectorStage.CHAPTER_WINDOW_EXECUTION.value, "stop_requested": True}
        run.pause_reason = "AUTHOR_PAUSED"
        run.settings = {**run.settings, "max_retries": 0}
        db.commit()

    response = control_api["client"].post(
        f"/projects/{control_api['project_id']}/volumes/{control_api['volume_ids'][0]}/continue",
        json={},
    )
    assert response.status_code == 200, response.text
    with control_api["Session"]() as db:
        run = db.get(AutoDirectorRun, run_id)
        assert run.status == AutoDirectorRunStatus.RUNNING
        assert run.current_stage == AutoDirectorStage.CHAPTER_WINDOW_EXECUTION
        assert run.context["stop_requested"] is False
        assert run.pause_reason is None


def test_volume_controls_do_not_retry_a_run_owned_by_another_volume(control_api):
    run_id = add_run(
        control_api, volume_index=0, status=AutoDirectorRunStatus.FAILED,
        stage=AutoDirectorStage.FAILED, max_retries=0, failed_attempt=1,
    )
    response = control_api["client"].post(
        f"/projects/{control_api['project_id']}/volumes/{control_api['volume_ids'][1]}/continue",
        json={},
    )
    assert response.status_code == 200, response.text
    with control_api["Session"]() as db:
        assert db.get(AutoDirectorRun, run_id).status == AutoDirectorRunStatus.FAILED
