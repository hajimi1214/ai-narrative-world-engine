"""Deterministic selective replay orchestration.  It never invokes an AI provider."""
from __future__ import annotations
import hashlib, json
from datetime import datetime
from sqlalchemy import select
from copy import deepcopy
from sqlalchemy.orm import Session
from .models import (
    CharacterDecision, CharacterKnowledge, CharacterMemory, ReplaySceneRun,
    ReplaySceneRunStatus, ReplaySessionStatus, RetconApplication,
    RetconApplicationStatus, RetconCognitionInvalidation,
    RetconCognitionInvalidationStatus, RetconReplaySession, Scene, ScenePerformance,
    ScenePerformanceTurn, WorldResolution, PerformanceStatus,
)
from .versioning import WorldSnapshotBuilder
from .performance import HeuristicCharacterPerformer, CharacterPerformancePayload, PerformanceActionConstraintChecker
from .character_mind import CharacterContextBuilder, CharacterDecisionConstraintChecker
from .world_resolution import HeuristicWorldResolver

def _fingerprint(value):
    return "replay-input-v1:" + hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()

class CurrentSceneHistoryResolver:
    def resolve(self, db: Session, project_id: str, sequence: int):
        rows = db.scalars(select(Scene).where(Scene.project_id == project_id, Scene.sequence == sequence, Scene.history_status == "ACTIVE")).all()
        if len(rows) > 1:
            raise ValueError("CURRENT_HISTORY_AMBIGUOUS")
        return rows[0] if rows else None

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
        queue = SelectiveReplayQueue().build(db, app)
        from .models import WorldRevision, SceneStateCheckpoint
        revision = db.get(WorldRevision, app.source_revision_id)
        earliest = next((item for item in queue if item["mode"] == "REPLAY"), None)
        if earliest is None:
            payload, fingerprint = WorldSnapshotBuilder().build(db, project_id)
            session = RetconReplaySession(project_id=project_id, retcon_application_id=app.id, status=ReplaySessionStatus.READY, baseline_snapshot_id=None, baseline_fingerprint=fingerprint, queue=queue, current_sequence=None, staged_world_state={"baseline": payload, "current_world": deepcopy(payload), "staged_facts": [], "staged_cognition": {}, "scene_results": {}})
            db.add(session); db.flush(); return session
        checkpoint = db.scalar(select(SceneStateCheckpoint).where(SceneStateCheckpoint.project_id == project_id, SceneStateCheckpoint.scene_id == earliest["scene_id"]))
        if not checkpoint or checkpoint.capture_protocol_version < 2:
            self._fail("HISTORICAL_BASELINE_UNAVAILABLE")
        from .models import WorldSnapshot
        snapshot = db.get(WorldSnapshot, checkpoint.pre_snapshot_id)
        if not snapshot: self._fail("HISTORICAL_BASELINE_UNAVAILABLE")
        from .historical import ReplayBaselineBuilder
        payload, fingerprint = ReplayBaselineBuilder().build(db, project_id, earliest["scene_id"], revision)
        staged = {"baseline": payload, "current_world": deepcopy(payload), "staged_facts": [], "staged_cognition": {}, "scene_results": {}}
        session = RetconReplaySession(project_id=project_id, retcon_application_id=app.id, status=ReplaySessionStatus.READY, baseline_snapshot_id=snapshot.id, baseline_fingerprint=fingerprint, queue=queue, current_sequence=queue[0]["sequence"] if queue else None, staged_world_state=staged)
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
                queue = deepcopy(session.queue); queue[session.cursor] = {**queue[session.cursor], "mode": "REPLAY", "reason": "DYNAMIC_EXPANSION", "dynamic_expansion_reason": report}; session.queue = queue
                run.status = ReplaySceneRunStatus.VALIDATED; run.validation_report = {"result": "REPLAY_ESCALATED", **report}; session.status = ReplaySessionStatus.RUNNING; session.failure_report = {"code": "DYNAMIC_REPLAY_EXPANSION", "reason": report}; db.flush(); return run
            run.status = ReplaySceneRunStatus.VALIDATED; run.validation_report = report
        else:
            queue_item = session.queue[session.cursor]
            situation = {"sequence": scene.sequence, "location": scene.location, "participants": list(scene.participants or []), "intent": scene.intent}
            staged_decisions = []
            staged_outcome = {"objective_facts": [], "outcome": "UNRESOLVED", "source": "HEURISTIC_REPLAY"}
            for old_id in queue_item.get("decision_ids", []):
                old = db.get(CharacterDecision, old_id)
                if not old: continue
                proposal = db.get(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal, old.scene_proposal_id)
                if not proposal: continue
                context = CharacterContextBuilder().build(db, session.project_id, old.character_id, proposal)
                temporal = __import__("app.historical", fromlist=["TemporalCharacterCognitionReader"]).TemporalCharacterCognitionReader().read(db, session.project_id, old.character_id, session, scene.sequence)
                context["knowledge"] = {"KNOWN": [{"id": row.id, "proposition": row.proposition} for row in temporal["knowledge"]], "SUSPECTED": [], "FALSE_BELIEF": [], "UNKNOWN": []}
                context["memories"] = [{"memory_id": row.id, "content": row.content} for row in temporal["memories"]]
                output, _ = HeuristicCharacterPerformer().perform(context)
                candidate = CharacterDecision(project_id=session.project_id, scene_proposal_id=proposal.id, character_id=old.character_id, context_fingerprint=context["fingerprint"], **output["decision"])
                decision_report = CharacterDecisionConstraintChecker().validate(db, context, candidate)
                if not decision_report.valid:
                    run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "REPLAY_DECISION_CONSTRAINT_FAILED", "report": decision_report.as_dict()}; session.status = ReplaySessionStatus.BLOCKED; self._fail("REPLAY_DECISION_CONSTRAINT_FAILED")
                parsed = CharacterPerformancePayload.model_validate(output)
                action_report = PerformanceActionConstraintChecker().validate(db, context, proposal, candidate, parsed.action, list(scene.participants or []))
                if not action_report.valid:
                    run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "REPLAY_ACTION_CONSTRAINT_FAILED", "report": action_report.as_dict()}; session.status = ReplaySessionStatus.BLOCKED; self._fail("REPLAY_ACTION_CONSTRAINT_FAILED")
                staged_decisions.append({"replay_of_id": old_id, "character_id": old.character_id, "decision": output["decision"], "action": output["action"], "context_fingerprint": _fingerprint(context)})
                if output["action"].get("requires_world_resolution"):
                    request = output["action"].get("world_resolution_request") or {}
                    entity = next((e for e in (session.staged_world_state or {}).get("current_world", {}).get("world_entities", []) if e.get("id") == request.get("target_entity_id")), None)
                    world_context = {"request": request, "target_entity": entity, "location": entity, "allowed_world_entity_ids": [entity["id"]] if entity else [], "canon": [], "scope": {"location_id": entity["id"] if entity else None, "actor_character_id": old.character_id, "target_character_id": None, "performance_id": scene.id}, "forbidden_canon_ids": [], "forbidden_propositions": []}
                    staged_outcome, _ = HeuristicWorldResolver().resolve(world_context)
            session.staged_world_state = dict(session.staged_world_state or {}) | {str(scene.sequence): {"situation": situation, "decisions": staged_decisions, "resolution": staged_outcome}}
            run.validation_report = {"code": "REPLAY_VALIDATED", "deterministic": True, "staged": True}
            run.status = ReplaySceneRunStatus.VALIDATED
        run.completed_at = datetime.utcnow(); session.cursor += 1; session.current_sequence = session.queue[session.cursor]["sequence"] if session.cursor < len(session.queue) else None; session.status = ReplaySessionStatus.RUNNING; db.flush(); return run

    def commit(self, db: Session, session: RetconReplaySession, explicit_confirmation: bool):
        if not explicit_confirmation: self._fail("EXPLICIT_CONFIRMATION_REQUIRED")
        if session.status != ReplaySessionStatus.RUNNING or session.cursor < len(session.queue): self._fail("REPLAY_NOT_VALIDATED")
        app = db.get(RetconApplication, session.retcon_application_id)
        session.pre_commit_snapshot_id = WorldSnapshotBuilder().create(db, session.project_id, __import__("app.models", fromlist=["SnapshotType"]).SnapshotType.PRE_REPLAY_COMMIT).id
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
        replacement_by_old = {run.original_scene_id: run.replacement_scene_id for run in runs if run.replacement_scene_id}
        # A rebuild is complete only when the affected resource was covered by a replayed scene.
        for inv in db.scalars(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.retcon_application_id == app.id, RetconCognitionInvalidation.status == RetconCognitionInvalidationStatus.ACTIVE)).all():
            covered = any(inv.resource_id in ((item.get("knowledge_ids", []) + item.get("memory_ids", []) + item.get("cognition_resource_ids", []))) for item in session.queue) or bool(replacement_by_old)
            if not covered:
                self._fail("COGNITION_REBUILD_INCOMPLETE")
            # The replacement may be deliberately absent: no new observation means the character now knows less.
            inv.status = RetconCognitionInvalidationStatus.RESOLVED
        app.status = RetconApplicationStatus.REPLAY_COMPLETED
        session.status = ReplaySessionStatus.COMPLETED; session.completed_at = datetime.utcnow()
        session.post_commit_snapshot_id = WorldSnapshotBuilder().create(db, session.project_id, __import__("app.models", fromlist=["SnapshotType"]).SnapshotType.POST_REPLAY_COMMIT).id
        db.flush(); return session
