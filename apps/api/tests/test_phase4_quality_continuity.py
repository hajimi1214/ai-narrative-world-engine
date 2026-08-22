from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import CharacterKnowledge, KnowledgeStatus, Project
from app.quality import NovelContinuityQualityChecker
from app.performance import PerformanceActionConstraintChecker, PerformanceActionPayload
from app.models import ActionVisibility


def test_phase4_continuity_checker_catches_task_timeline_location_and_knowledge_errors():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        project = Project(name="continuity")
        db.add(project); db.flush()
        db.add(CharacterKnowledge(character_id="char-a", proposition="秘密", status=KnowledgeStatus.UNKNOWN)); db.commit()
        context = {
            "chapter": type("Chapter", (), {"number": 2})(),
            "prose": "揭示真相",
            "writer_draft": type("Draft", (), {"validation_report": {"task_coverage": [], "task_forbidden_hits": ["揭示真相"]}})(),
            "writer_safe_context": {
                "planning_task": {"must_events": ["接案"], "forbidden_events": ["揭示真相"], "forbidden_reveals": ["揭示真相"]},
                "source_manifest": {"scenes": [
                    {"scene_id": "s1", "world_time": "2041-03-01T10:00:00", "location": "港口", "participants": ["char-a"], "turns": []},
                    {"scene_id": "s2", "world_time": "2041-03-01T10:00:00", "location": "档案馆", "participants": ["char-a"], "turns": [{"actor_character_id": "char-a", "decision": {"character_id": "char-a", "knowledge_used": [{"knowledge_id": "missing", "accepted_statuses": ["KNOWN"]}]}}]},
                    {"scene_id": "s3", "world_time": "2041-03-01T09:00:00", "location": "档案馆", "participants": [], "turns": []},
                ]},
            },
        }
        report = NovelContinuityQualityChecker().evaluate(db, context)
        codes = {item["rule_code"] for item in report["findings"]}
        assert {"PLAN_REQUIRED_EVENT_MISSING", "PLAN_FORBIDDEN_EVENT_PRESENT", "PLAN_FORBIDDEN_REVEAL_PRESENT", "TIMELINE_ORDER_INVALID", "LOCATION_CONFLICT", "KNOWLEDGE_LEAK"}.issubset(codes)


def test_phase4_checker_is_disabled_for_legacy_projects():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        report = NovelContinuityQualityChecker().evaluate(db, {"chapter": type("Chapter", (), {"number": 1})(), "writer_safe_context": {}, "writer_draft": None, "prose": ""})
        assert report["enabled"] is False
        assert report["findings"] == []


def test_scene_beats_must_flow_from_action_to_continuity_gate():
    context = {
        "chapter": type("Chapter", (), {"number": 4})(),
        "prose": "人物检查档案并决定离开。",
        "writer_draft": type("Draft", (), {"validation_report": {"task_coverage": ["检查档案"], "task_forbidden_hits": []}})(),
        "writer_safe_context": {"planning_task": {"must_events": ["检查档案"], "forbidden_events": [], "scene_beats": ["检查档案", "决定离开"]}, "source_manifest": {"scenes": [{"scene_id": "s1", "sequence": 1, "world_time": "2041-01-01T10:00:00", "location": "档案馆", "participants": ["char-a"], "turns": [{"id": "turn-1", "actor_character_id": "char-a", "decision": {"id": "decision-1", "character_id": "char-a"}, "scene_beat_refs": ["检查档案", "决定离开"], "requires_world_resolution": False}]}]}},
    }
    report = NovelContinuityQualityChecker().evaluate(None, context)
    assert not {item["rule_code"] for item in report["findings"]}.intersection({"SCENE_BEAT_NOT_EXECUTED", "SCENE_BEAT_REFERENCE_INVALID"})


def test_scene_beat_action_reference_is_required_and_must_be_planned():
    context = {"character": {"id": "c1"}, "scene": {"other_participants": [], "location": None}, "planning_task": {"scene_beats": ["检查档案"]}, "knowledge": {}, "memories": [], "abilities": [], "inventory": {}}
    action = PerformanceActionPayload(visibility=ActionVisibility.PUBLIC, observable_action="检查", spoken_content=None, requires_world_resolution=False, world_resolution_request=None, disclosure_knowledge_ids=[], scene_beat_refs=[])
    class EmptySession:
        def scalars(self, *_args, **_kwargs):
            return self
        def where(self, *_args, **_kwargs):
            return self
        def all(self):
            return []
    report = PerformanceActionConstraintChecker().validate(EmptySession(), context, type("Proposal", (), {"forbidden_reveals": []})(), type("Decision", (), {"target_character_id": None, "character_id": "c1", "project_id": "p"})(), action)
    assert any(item.code == "SCENE_BEAT_REFERENCE_MISSING" for item in report.issues)
