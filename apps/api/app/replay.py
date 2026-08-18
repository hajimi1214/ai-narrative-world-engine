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
    RetconCognitionInvalidationStatus, RetconReplaySession, Scene,
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
        scenes = db.scalars(select(Scene).where(Scene.project_id == application.project_id).order_by(Scene.sequence, Scene.id)).all()
        queue = []
        for scene in scenes:
            keep = any(r.get("sequence_start", 0) <= scene.sequence <= r.get("sequence_end", 0) for r in preserved)
            if scene.id in replay_ids:
                queue.append({"sequence": scene.sequence, "scene_id": scene.id, "mode": "REPLAY", "reason": "INITIAL_PLAN", "dynamic_expansion_reason": None})
            elif keep:
                queue.append({"sequence": scene.sequence, "scene_id": scene.id, "mode": "VALIDATE_PRESERVED", "reason": "PRESERVED_HISTORY", "dynamic_expansion_reason": None})
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
                run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = report; session.status = ReplaySessionStatus.BLOCKED; session.failure_report = report; db.flush(); self._fail("REPLAY_PRESERVED_ESCALATED")
            run.status = ReplaySceneRunStatus.VALIDATED; run.validation_report = report
        else:
            replacement = Scene(project_id=scene.project_id, sequence=scene.sequence, world_time=scene.world_time, location=scene.location, participants=list(scene.participants or []), intent=scene.intent, facts=list(scene.facts or []), result=dict(scene.result or {}), summary=scene.summary, story_threads=list(scene.story_threads or []), status=scene.status, history_status="STAGED")
            db.add(replacement); db.flush(); run.replacement_scene_id = replacement.id; run.status = ReplaySceneRunStatus.VALIDATED; run.validation_report = {"code": "REPLAY_VALIDATED", "deterministic": True}
        run.completed_at = datetime.utcnow(); session.cursor += 1; session.current_sequence = session.queue[session.cursor]["sequence"] if session.cursor < len(session.queue) else None; session.status = ReplaySessionStatus.RUNNING; db.flush(); return run

    def commit(self, db: Session, session: RetconReplaySession, explicit_confirmation: bool):
        if not explicit_confirmation: self._fail("EXPLICIT_CONFIRMATION_REQUIRED")
        if session.status != ReplaySessionStatus.RUNNING or session.cursor < len(session.queue): self._fail("REPLAY_NOT_VALIDATED")
        runs = db.scalars(select(ReplaySceneRun).where(ReplaySceneRun.replay_session_id == session.id)).all()
        for run in runs:
            if run.replacement_scene_id:
                old = db.get(Scene, run.original_scene_id); new = db.get(Scene, run.replacement_scene_id)
                if old and new:
                    old.history_status = "SUPERSEDED"; old.superseded_by_scene_id = new.id; new.history_status = "ACTIVE"; run.status = ReplaySceneRunStatus.COMMITTED
        app = db.get(RetconApplication, session.retcon_application_id)
        for inv in db.scalars(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.retcon_application_id == app.id, RetconCognitionInvalidation.status == RetconCognitionInvalidationStatus.ACTIVE)).all(): inv.status = RetconCognitionInvalidationStatus.RESOLVED
        app.status = "REPLAY_COMPLETED"; session.status = ReplaySessionStatus.COMPLETED; session.completed_at = datetime.utcnow(); db.flush(); return session
