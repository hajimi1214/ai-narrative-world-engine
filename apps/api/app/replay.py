"""Deterministic selective replay orchestration.  It never invokes an AI provider."""
from __future__ import annotations
import hashlib, json
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import (
    CharacterDecision, CharacterKnowledge, CharacterMemory, ReplaySceneRun,
    ReplaySceneRunStatus, ReplaySessionStatus, RetconApplication,
    RetconApplicationStatus, RetconCognitionInvalidation,
    RetconCognitionInvalidationStatus, RetconReplaySession, Scene, ScenePerformance,
    ScenePerformanceTurn, WorldResolution, PerformanceStatus,
)
from .versioning import WorldSnapshotBuilder

def _fingerprint(value):
    return "replay-input-v1:" + hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()

class CurrentSceneHistoryResolver:
    def resolve(self, db: Session, project_id: str, sequence: int):
        return db.scalar(select(Scene).where(Scene.project_id == project_id, Scene.sequence == sequence, Scene.history_status == "ACTIVE").order_by(Scene.id.desc()))

class PreservedSceneValidator:
    def validate(self, db: Session, scene: Scene):
        participants = scene.participants or []
        available = {row.id for row in db.query(__import__("app.models", fromlist=["Character"]).Character).filter_by(project_id=scene.project_id).all()}
        missing = sorted(set(participants) - available)
        if missing:
            return "REPLAY_ESCALATED", {"code": "PARTICIPANT_UNAVAILABLE", "participants": missing}
        return "PRESERVED", {"code": "PRESERVED_PREREQUISITES_VALID"}

class SelectiveReplayQueue:
    def build(self, db: Session, application: RetconApplication):
        summary = application.replay_summary or {}
        replay_ids = set(summary.get("replay_scene_ids", []))
        preserved = summary.get("preserved_scene_ranges", [])
        scenes = db.scalars(select(Scene).where(Scene.project_id == application.project_id, Scene.history_status == "ACTIVE").order_by(Scene.sequence, Scene.id)).all()
        queue = []
        from .models import RetconImpactItem
        items = db.scalars(select(RetconImpactItem).where(RetconImpactItem.plan_id == application.retcon_plan_id)).all()
        resources = {}
        for item in items:
            scene_id = item.scene_id
            if not scene_id:
                for node in item.dependency_path or []:
                    if node.get("type") == "SCENE": scene_id = node.get("id"); break
            if scene_id: resources.setdefault(scene_id, {"decision_ids": [], "turn_ids": [], "resolution_ids": [], "cognition_resource_ids": []})
            if scene_id:
                key = "cognition_resource_ids" if item.resource_type in {"CHARACTER_KNOWLEDGE", "CHARACTER_MEMORY"} else {"CHARACTER_DECISION": "decision_ids", "SCENE_PERFORMANCE_TURN": "turn_ids", "WORLD_RESOLUTION": "resolution_ids"}.get(item.resource_type)
                if key: resources[scene_id][key].append(item.resource_id)
        for scene in scenes:
            keep = any(r.get("sequence_start", 0) <= scene.sequence <= r.get("sequence_end", 0) for r in preserved)
            if scene.id in replay_ids:
                queue.append({"sequence": scene.sequence, "scene_id": scene.id, "mode": "REPLAY", "reason": "INITIAL_PLAN", "dynamic_expansion_reason": None, **resources.get(scene.id, {})})
            elif keep:
                queue.append({"sequence": scene.sequence, "scene_id": scene.id, "mode": "VALIDATE_PRESERVED", "reason": "PRESERVED_HISTORY", "dynamic_expansion_reason": None, **resources.get(scene.id, {})})
        return sorted(queue, key=lambda item: (item["sequence"], item["scene_id"]))

class ReplayService:
    def _fail(self, code):
        raise ValueError(code)

    def create_session(self, db: Session, project_id: str, application_id: str):
        app = db.get(RetconApplication, application_id)
        if not app or app.project_id != project_id:
            self._fail("RETCON_APPLICATION_NOT_FOUND")
        if app.status != RetconApplicationStatus.APPLIED_PENDING_REPLAY:
            self._fail("RETCON_REPLAY_NOT_PENDING")
        existing = db.scalar(select(RetconReplaySession).where(RetconReplaySession.retcon_application_id == app.id))
        if existing:
            self._fail("REPLAY_SESSION_ALREADY_EXISTS")
        from .models import RevisionApplication, WorldSnapshot
        revision_application = db.get(RevisionApplication, app.revision_application_id) if app.revision_application_id else None
        snapshot = db.get(WorldSnapshot, revision_application.pre_snapshot_id) if revision_application else None
        _, current_fingerprint = WorldSnapshotBuilder().build(db, project_id)
        fingerprint = snapshot.state_fingerprint if snapshot else current_fingerprint
        queue = SelectiveReplayQueue().build(db, app)
        from .historical import ReplayBaselineBuilder
        for item in queue:
            if item["mode"] == "REPLAY":
                try: ReplayBaselineBuilder().build(db, project_id, item["scene_id"])
                except ValueError: self._fail("HISTORICAL_BASELINE_UNAVAILABLE")
        session = RetconReplaySession(project_id=project_id, retcon_application_id=app.id, status=ReplaySessionStatus.READY, baseline_snapshot_id=snapshot.id if snapshot else None, baseline_fingerprint=fingerprint, queue=queue, current_sequence=queue[0]["sequence"] if queue else None)
        db.add(session); db.flush()
        return session

    def step(self, db: Session, session: RetconReplaySession):
        if session.status in {ReplaySessionStatus.BLOCKED, ReplaySessionStatus.COMPLETED, ReplaySessionStatus.ABORTED}:
            self._fail("REPLAY_SESSION_NOT_RUNNABLE")
        if session.cursor >= len(session.queue):
            session.status = ReplaySessionStatus.RUNNING
            return None
        item = session.queue[session.cursor]
        scene = db.get(Scene, item["scene_id"])
        if not scene or scene.project_id != session.project_id:
            session.status = ReplaySessionStatus.BLOCKED; session.failure_report = {"code": "REPLAY_SCENE_NOT_FOUND"}; self._fail("REPLAY_SCENE_NOT_FOUND")
        run = ReplaySceneRun(project_id=session.project_id, replay_session_id=session.id, original_scene_id=scene.id, original_sequence=scene.sequence, mode=item["mode"], status=ReplaySceneRunStatus.RUNNING, input_fingerprint=_fingerprint({"scene": scene.id, "sequence": scene.sequence, "baseline": session.baseline_fingerprint}), started_at=datetime.utcnow())
        db.add(run); db.flush()
        if item["mode"] == "VALIDATE_PRESERVED":
            result, report = PreservedSceneValidator().validate(db, scene)
            if result == "REPLAY_ESCALATED":
                item["mode"] = "REPLAY"; item["reason"] = "DYNAMIC_EXPANSION"; item["dynamic_expansion_reason"] = report
                run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = report; session.status = ReplaySessionStatus.RUNNING; session.failure_report = {"code": "DYNAMIC_REPLAY_EXPANSION", "reason": report}; db.flush(); return run
            run.status = ReplaySceneRunStatus.VALIDATED; run.validation_report = report
        else:
            queue_item = session.queue[session.cursor]
            situation = {"sequence": scene.sequence, "location": scene.location, "participants": list(scene.participants or []), "intent": scene.intent}
            staged_decisions = [{"replay_of_id": old_id, "character_id": db.get(CharacterDecision, old_id).character_id if db.get(CharacterDecision, old_id) else None, "chosen_action": "observe and reassess the situation", "context_fingerprint": _fingerprint({"session": session.id, "sequence": scene.sequence, "character": old_id})} for old_id in queue_item.get("decision_ids", [])]
            staged_outcome = {"objective_facts": [], "outcome": "UNRESOLVED", "source": "HEURISTIC_REPLAY"}
            session.staged_world_state = dict(session.staged_world_state or {}) | {str(scene.sequence): {"situation": situation, "decisions": staged_decisions, "resolution": staged_outcome}}
            run.validation_report = {"code": "REPLAY_VALIDATED", "deterministic": True, "staged": True}
            run.status = ReplaySceneRunStatus.VALIDATED
        run.completed_at = datetime.utcnow(); session.cursor += 1; session.current_sequence = session.queue[session.cursor]["sequence"] if session.cursor < len(session.queue) else None; session.status = ReplaySessionStatus.RUNNING; db.flush(); return run

    def commit(self, db: Session, session: RetconReplaySession, explicit_confirmation: bool):
        if not explicit_confirmation: self._fail("EXPLICIT_CONFIRMATION_REQUIRED")
        if session.status != ReplaySessionStatus.RUNNING or session.cursor < len(session.queue): self._fail("REPLAY_NOT_VALIDATED")
        runs = db.scalars(select(ReplaySceneRun).where(ReplaySceneRun.replay_session_id == session.id)).all()
        for run in runs:
            if not run.replacement_scene_id:
                old = db.get(Scene, run.original_scene_id)
                staged = (session.staged_world_state or {}).get(str(run.original_sequence), {})
                if old:
                    new = Scene(project_id=old.project_id, sequence=old.sequence, world_time=old.world_time, location=old.location, participants=list(old.participants or []), intent=old.intent, facts=list(staged.get("resolution", {}).get("objective_facts", [])), result=dict(staged.get("resolution", {})), summary="Deterministic replay scene", story_threads=list(old.story_threads or []), status=old.status, history_status="ACTIVE")
                    db.add(new); db.flush(); run.replacement_scene_id = new.id
            old = db.get(Scene, run.original_scene_id); new = db.get(Scene, run.replacement_scene_id) if run.replacement_scene_id else None
            if old and new:
                old.history_status = "SUPERSEDED"; old.superseded_by_scene_id = new.id; run.status = ReplaySceneRunStatus.COMMITTED
        app = db.get(RetconApplication, session.retcon_application_id)
        replacement_by_old = {run.original_scene_id: run.replacement_scene_id for run in runs if run.replacement_scene_id}
        for inv in db.scalars(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.retcon_application_id == app.id, RetconCognitionInvalidation.status == RetconCognitionInvalidationStatus.ACTIVE)).all():
            replacement_scene = next(iter(replacement_by_old.values()), None)
            if inv.resource_type == "KNOWLEDGE":
                old = db.get(CharacterKnowledge, inv.resource_id)
                if old:
                    new = CharacterKnowledge(character_id=old.character_id, proposition=old.proposition, status=old.status, source=replacement_scene or old.source, confidence=old.confidence, replay_session_id=session.id, replay_of_id=old.id)
                    db.add(new)
            else:
                old = db.get(CharacterMemory, inv.resource_id)
                if old:
                    new = CharacterMemory(character_id=old.character_id, content=old.content, importance=old.importance, emotional_weight=old.emotional_weight, confidence=old.confidence, distortion=dict(old.distortion or {}), source_scene=replacement_scene or old.source_scene, happened_at=old.happened_at, replay_session_id=session.id, replay_of_id=old.id)
                    db.add(new)
        for inv in db.scalars(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.retcon_application_id == app.id, RetconCognitionInvalidation.status == RetconCognitionInvalidationStatus.ACTIVE)).all(): inv.status = RetconCognitionInvalidationStatus.RESOLVED
        app.status = "REPLAY_COMPLETED"; session.status = ReplaySessionStatus.COMPLETED; session.completed_at = datetime.utcnow(); db.flush(); return session
