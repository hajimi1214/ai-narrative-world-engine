import json

from app.planning import validate_task_output
from app.writer import WriterPromptBuilder
from app.models import Project, ProjectModelConfig
from app.ai.fake import FakeModelProvider
from app.db import Base
from app.main import app
import app.api as api_module
import app.planning as planning_module
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def test_phase3_task_output_requires_mandatory_events_and_rejects_forbidden_hits():
    task = {"must_events": ["接案", "发现矛盾"], "forbidden_events": ["揭示真相"]}
    missing = validate_task_output({"task_coverage": ["接案"], "task_forbidden_hits": []}, task)
    assert {item["code"] for item in missing} == {"PLAN_REQUIRED_EVENT_MISSING"}
    forbidden = validate_task_output({"task_coverage": ["接案", "发现矛盾"], "task_forbidden_hits": ["揭示真相"]}, task)
    assert {item["code"] for item in forbidden} == {"PLAN_FORBIDDEN_EVENT_PRESENT"}


def test_phase3_output_without_approved_plan_keeps_legacy_writer_contract():
    assert validate_task_output({"prose": "旧项目正文"}, None) == []


def test_writer_prompt_declares_planning_task_as_binding_contract():
    messages = WriterPromptBuilder().build({"planning_task": {"objective": "接受委托", "must_events": ["接案"], "scene_beats": ["来客", "验物", "接案"]}, "source_manifest": {"scenes": []}})
    assert "PLANNING_TASK" in messages[0]["content"]
    assert "task_coverage" in messages[1]["content"]
    assert "scene_beats" in messages[1]["content"]


def _planning_client(monkeypatch, output):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    project = Project(name="Phase 3", story_seed="一名修复师追查失踪记录")
    db.add(project); db.flush()
    db.add(ProjectModelConfig(project_id=project.id, provider="openai_compatible", base_url="https://tokenrhythm.studio/v1", director_model="deepseek-v4-pro")); db.commit()
    monkeypatch.setattr(planning_module, "get_model_provider", lambda *args, **kwargs: FakeModelProvider(json.dumps(output, ensure_ascii=False)))
    monkeypatch.setattr(api_module, "SessionLocal", Session)
    return db, project, TestClient(app)


def test_phase3_scope_edit_regenerate_and_approved_plan_is_immutable(monkeypatch):
    output = {"premise": "记录会反过来改变修复师", "macro_plan": {"core_conflict": "真相与代价"}, "volumes": [{"number": 1, "title": "雾港", "summary": "建立谜团", "theme": "记忆的代价", "core_question": "谁改了记录？", "major_conflict": "档案馆封锁", "start_state": {"case": "open"}, "end_state": {"case": "exposed"}, "main_thread": "追查记录源头", "ending_turn": "发现主角也被篡改", "foreshadowing": ["旧钟"], "start_chapter": 1, "end_chapter": 3}], "arcs": [{"number": 1, "volume_number": 1, "title": "追索", "goal": "找到源头", "summary": "从委托进入主线", "core_question": "记录可信吗？", "start_state": {"case": "open"}, "end_state": {"case": "exposed"}, "turning_points": ["发现矛盾"], "thread_refs": ["主线"]}], "chapters": [{"number": 1, "volume_number": 1, "arc_number": 1, "title": "来客", "summary": "接案", "objective": "接受调查", "conflict": "委托人隐瞒代价", "start_state": {"case": "open"}, "end_state": {"case": "accepted"}, "scene_beats": ["来客", "验物", "接案"], "must_events": ["接案"]}, {"number": 2, "volume_number": 1, "arc_number": 1, "title": "档案", "summary": "发现矛盾", "objective": "确认异常", "conflict": "管理员阻拦", "start_state": {"case": "accepted"}, "end_state": {"case": "complicated"}, "scene_beats": ["入馆", "查档", "撤离"], "must_events": ["发现矛盾"]}]}
    db, project, client = _planning_client(monkeypatch, output)
    payload = {"framing": {"inspiration": "修复师追查记录", "target_chapters": 3, "target_words_per_chapter": 2000}, "premise": output["premise"]}
    generated = client.post(f"/projects/{project.id}/planning/generate", json=payload).json()
    volume = client.patch(f"/projects/{project.id}/planning/plans/{generated['id']}/volumes/1", json={"theme": "被删掉的战争"})
    assert volume.status_code == 200
    chapter_id = generated["chapters"][0]["id"]
    regenerated = client.post(f"/projects/{project.id}/planning/plans/{generated['id']}/chapters/{chapter_id}/regenerate", json={})
    assert regenerated.status_code == 201
    assert regenerated.json()["plan"]["version"] == 2
    assert chapter_id != regenerated.json()["plan"]["chapters"][0]["id"]
    approved = client.post(f"/projects/{project.id}/planning/plans/{regenerated.json()['plan']['id']}/approve")
    assert approved.status_code == 200
    blocked = client.patch(f"/projects/{project.id}/planning/plans/{approved.json()['id']}", json={"premise": "不能静默改"})
    assert blocked.status_code == 409
    db.close()
