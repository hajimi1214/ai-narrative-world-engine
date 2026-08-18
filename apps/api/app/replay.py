"""Deterministic selective replay orchestration.  It never invokes an AI provider."""
from __future__ import annotations
import hashlib, json
from datetime import datetime
from sqlalchemy import select, func
from copy import deepcopy
from sqlalchemy.orm import Session
from .models import (
    CharacterDecision, CharacterKnowledge, CharacterMemory, ReplaySceneRun,
    ReplaySceneRunStatus, ReplaySessionStatus, RetconApplication,
    RetconApplicationStatus, RetconCognitionInvalidation,
    RetconCognitionInvalidationStatus, RetconReplaySession, Scene, ScenePerformance,
    ScenePerformanceTurn, WorldResolution, PerformanceStatus, SceneExecutionBinding, PerformanceMode, ResolutionStatus, ResolutionOutcome, ResolverMode, ActionVisibility,
)
from .versioning import WorldSnapshotBuilder
from .performance import HeuristicCharacterPerformer, CharacterPerformancePayload, PerformanceActionConstraintChecker, PerformanceObservationRouter
from .character_mind import CharacterContextBuilder, CharacterDecisionConstraintChecker
from .world_resolution import HeuristicWorldResolver, WorldResolutionPayload, WorldResolutionConstraintChecker

class ReplayWorldView:
    """Read-only runtime view; replay never uses current formal state as world truth."""
    def __init__(self, session): self.state = (session.staged_world_state or {}).get("current_world", {})
    def rows(self, key): return self.state.get(key, [])
    def one(self, key, ident): return next((row for row in self.rows(key) if row.get("id") == ident), None)
    def character(self, ident): return self.one("characters", ident)
    def entity(self, ident):
        entity = deepcopy(self.one("world_entities", ident))
        if not entity: return None
        for fact in self.state.get("staged_facts", []):
            if fact.get("subject_type") == "ENTITY" and fact.get("subject_id") == ident:
                profile = entity.setdefault("profile", {}); profile[fact.get("predicate")] = fact.get("value")
        return entity
    def fact(self, subject_type, subject_id, predicate):
        values = [fact.get("value") for fact in self.state.get("staged_facts", []) if fact.get("subject_type") == subject_type and fact.get("subject_id") == subject_id and fact.get("predicate") == predicate]
        if values: return values[-1]
        entity = self.one("world_entities", subject_id) if subject_type == "ENTITY" else None
        return (entity.get("profile") or {}).get(predicate) if entity else None
    def canon(self): return self.rows("canon_facts")
    def apply_facts(self, facts):
        state = deepcopy(self.state)
        state.setdefault("staged_facts", []).extend(facts)
        self.state = state
        return state

class ReplayCharacterContextBuilder:
    def build(self, db, session, scene, proposal, character_id):
        from .historical import TemporalCharacterCognitionReader
        world = ReplayWorldView(session); character = world.character(character_id)
        if not character: raise ValueError("REPLAY_CHARACTER_STATE_UNAVAILABLE")
        others = [world.character(item) for item in scene.participants or [] if item != character_id and world.character(item)]
        cognition = TemporalCharacterCognitionReader().read(db, session.project_id, character_id, session, scene.sequence)
        location_id = getattr(proposal, "location_id", None)
        location = world.entity(location_id) if location_id else None
        relationships = {key: value for key, value in (character.get("relationships") or {}).items() if key in {row["id"] for row in others}}
        entry = getattr(proposal, "entry_state", {}) or {}; grouped = {"KNOWN": [], "SUSPECTED": [], "FALSE_BELIEF": [], "UNKNOWN": []}
        for row in cognition["knowledge"]: grouped[getattr(getattr(row, "status", "KNOWN"), "value", getattr(row, "status", "KNOWN"))].append({"id": row.id, "proposition": row.proposition, "confidence": row.confidence})
        context = {"project": {"id": session.project_id}, "character": {"id": character_id, "name": character.get("name"), "personality": character.get("personality") or {}, "core_values": character.get("core_values") or [], "boundaries": character.get("boundaries") or [], "goals": character.get("goals") or {}, "current_state": character.get("current_state") or {}, "physical_state": character.get("physical_state") or {}, "emotional_state": character.get("emotional_state") or {}, "relationships": relationships}, "scene": {"proposal_id": proposal.id, "location": location, "visible_context": entry.get("visible_context", {}), "actor_visible_context": entry.get("actor_visible_context", {}).get(character_id, {}), "other_participants": [{"id": row["id"], "name": row.get("name")} for row in others], "active_participant_ids": list(scene.participants or []), "performance_observations": [], "world_observations": [], "self_turn_history": [], "world_affordances": entry.get("world_affordances", [])}, "knowledge": grouped, "memories": [{"memory_id": row.id, "content": row.content} for row in cognition["memories"]], "inventory": list(character.get("inventory") or []), "abilities": list(character.get("abilities") or [])}
        context["fingerprint"] = _fingerprint(context); context["version"] = context["fingerprint"]
        return context

class ReplayResourceMapper:
    """Deterministic execution-lineage mapper; never guesses from prose."""
    def map(self, db, application, scene_ids):
        from .models import RetconImpactItem
        result = {sid: {"decision_ids": [], "turn_ids": [], "resolution_ids": [], "knowledge_ids": [], "memory_ids": []} for sid in scene_ids}
        if not scene_ids: return result
        items = db.scalars(select(RetconImpactItem).where(RetconImpactItem.plan_id == application.retcon_plan_id)).all()
        ownership = {"CHARACTER_DECISION": {}, "SCENE_PERFORMANCE_TURN": {}, "WORLD_RESOLUTION": {}}
        pairs = {sid: [] for sid in scene_ids}
        for scene_id in scene_ids:
            bindings = db.scalars(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == scene_id, SceneExecutionBinding.active.is_(True))).all()
            expected = any(item.scene_id == scene_id and item.resource_type in ownership for item in items)
            if len(bindings) > 1: raise ValueError("REPLAY_RESOURCE_MAPPING_AMBIGUOUS")
            if expected and not bindings: raise ValueError("REPLAY_EXECUTION_BINDING_MISSING")
            if not bindings: continue
            turns = db.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == bindings[0].performance_id).order_by(ScenePerformanceTurn.sequence, ScenePerformanceTurn.id)).all()
            for turn in turns:
                ownership["SCENE_PERFORMANCE_TURN"].setdefault(turn.id, []).append(scene_id)
                ownership["CHARACTER_DECISION"].setdefault(turn.character_decision_id, []).append(scene_id)
                resolutions = db.scalars(select(WorldResolution).where(WorldResolution.performance_turn_id == turn.id).order_by(WorldResolution.id)).all()
                for resolution in resolutions: ownership["WORLD_RESOLUTION"].setdefault(resolution.id, []).append(scene_id)
                pairs[scene_id].append({"decision_id": turn.character_decision_id, "turn_id": turn.id, "resolution_ids": [row.id for row in resolutions]})
        for item in items:
            if item.resource_type == "CHARACTER_KNOWLEDGE":
                row = db.get(CharacterKnowledge, item.resource_id); structured = getattr(row, "source", None); candidates = [sid for sid in scene_ids if sid == structured]
            elif item.resource_type == "CHARACTER_MEMORY":
                row = db.get(CharacterMemory, item.resource_id); structured = getattr(row, "source_scene", None); candidates = [sid for sid in scene_ids if sid == structured]
            elif item.resource_type in ownership:
                candidates = ownership[item.resource_type].get(item.resource_id, [])
            else:
                candidates = [sid for sid in scene_ids if item.scene_id == sid or any(node.get("type") == "SCENE" and node.get("id") == sid for node in (item.dependency_path or []))]
            if not candidates and item.resource_type in {"CHARACTER_KNOWLEDGE", "CHARACTER_MEMORY"}:
                if not item.scene_id or item.scene_id not in scene_ids: continue
                candidates = [sid for sid in scene_ids if item.scene_id == sid or any(node.get("type") == "SCENE" and node.get("id") == sid for node in (item.dependency_path or []))]
                if len(candidates) != 1: raise ValueError("COGNITION_REPLAY_COVERAGE_UNRESOLVED")
            if len(candidates) != 1 and item.resource_type in {"CHARACTER_DECISION", "SCENE_PERFORMANCE_TURN", "WORLD_RESOLUTION"}:
                raise ValueError("REPLAY_RESOURCE_SCENE_UNRESOLVED" if not candidates else "REPLAY_RESOURCE_MAPPING_AMBIGUOUS")
            if candidates:
                key = {"CHARACTER_DECISION":"decision_ids","SCENE_PERFORMANCE_TURN":"turn_ids","WORLD_RESOLUTION":"resolution_ids","CHARACTER_KNOWLEDGE":"knowledge_ids","CHARACTER_MEMORY":"memory_ids"}.get(item.resource_type)
                if key: result[candidates[0]][key].append(item.resource_id)
        for sid in scene_ids:
            result[sid]["execution_pairs"] = sorted(pairs[sid], key=lambda x: (next((t.sequence for t in db.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.id == x["turn_id"])).all()), 0), x["turn_id"]))
            for key in result[sid]:
                if key != "execution_pairs": result[sid][key] = sorted(set(result[sid][key]))
        return result

def _fingerprint(value):
    return "replay-input-v1:" + hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()

class CurrentSceneHistoryResolver:
    def resolve(self, db: Session, project_id: str, sequence: int):
        rows = db.scalars(select(Scene).where(Scene.project_id == project_id, Scene.sequence == sequence, Scene.history_status == "ACTIVE")).all()
        if len(rows) > 1:
            raise ValueError("CURRENT_HISTORY_AMBIGUOUS")
        return rows[0] if rows else None

class PreservedSceneValidator:
    def validate(self, db: Session, scene: Scene, world=None):
        participants = scene.participants or []
        available = {row.get("id") for row in world.rows("characters")} if world else {row.id for row in db.query(__import__("app.models", fromlist=["Character"]).Character).filter_by(project_id=scene.project_id).all()}
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
        eligible = [scene for scene in scenes if scene.id in replay_ids or any(r.get("sequence_start", 0) <= scene.sequence <= r.get("sequence_end", 0) for r in preserved)]
        scene_ids = [scene.id for scene in eligible]
        mapped = ReplayResourceMapper().map(db, application, scene_ids)
        for scene in eligible:
            keep = any(r.get("sequence_start", 0) <= scene.sequence <= r.get("sequence_end", 0) for r in preserved)
            if scene.id in replay_ids:
                queue.append({"sequence": scene.sequence, "scene_id": scene.id, "mode": "REPLAY", "reason": "INITIAL_PLAN", "dynamic_expansion_reason": None, **mapped.get(scene.id, {"decision_ids": [], "turn_ids": [], "resolution_ids": [], "knowledge_ids": [], "memory_ids": []})})
            elif keep:
                queue.append({"sequence": scene.sequence, "scene_id": scene.id, "mode": "VALIDATE_PRESERVED", "reason": "PRESERVED_HISTORY", "dynamic_expansion_reason": None, **mapped.get(scene.id, {"decision_ids": [], "turn_ids": [], "resolution_ids": [], "knowledge_ids": [], "memory_ids": []})})
        return sorted(queue, key=lambda item: (item["sequence"], item["scene_id"]))

class ReplayService:
    failure_injector = None
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
        from .models import WorldRevision, SceneStateCheckpoint
        revision = db.get(WorldRevision, app.source_revision_id)
        boundary_id = (app.replay_summary or {}).get("earliest_affected_scene_id")
        boundary = db.get(Scene, boundary_id) if boundary_id else None
        if boundary and boundary.history_status == "ACTIVE":
            boundary_checkpoint = db.scalar(select(SceneStateCheckpoint).where(SceneStateCheckpoint.project_id == project_id, SceneStateCheckpoint.scene_id == boundary.id))
            if not boundary_checkpoint or boundary_checkpoint.capture_protocol_version < 2: self._fail("HISTORICAL_BASELINE_UNAVAILABLE")
        queue = SelectiveReplayQueue().build(db, app)
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
            result, report = PreservedSceneValidator().validate(db, scene, ReplayWorldView(session))
            if result == "REPLAY_ESCALATED":
                queue = deepcopy(session.queue); queue[session.cursor] = {**queue[session.cursor], "mode": "REPLAY", "reason": "DYNAMIC_EXPANSION", "dynamic_expansion_reason": report}; session.queue = queue
                run.status = ReplaySceneRunStatus.VALIDATED; run.validation_report = {"result": "REPLAY_ESCALATED", **report}; session.status = ReplaySessionStatus.RUNNING; session.failure_report = {"code": "DYNAMIC_REPLAY_EXPANSION", "reason": report}; db.flush(); return run
            run.status = ReplaySceneRunStatus.VALIDATED; run.validation_report = report
        else:
            queue_item = session.queue[session.cursor]
            situation = {"sequence": scene.sequence, "location": scene.location, "participants": list(scene.participants or []), "intent": scene.intent}
            staged_decisions = []
            staged_turns = []
            staged_resolutions, knowledge, memories = [], [], []
            for old_id in queue_item.get("decision_ids", []):
                old = db.get(CharacterDecision, old_id)
                if not old: continue
                proposal = db.get(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal, old.scene_proposal_id)
                if not proposal: continue
                context = ReplayCharacterContextBuilder().build(db, session, scene, proposal, old.character_id)
                output, _ = HeuristicCharacterPerformer().perform(context)
                candidate = CharacterDecision(project_id=session.project_id, scene_proposal_id=proposal.id, character_id=old.character_id, context_fingerprint=context["fingerprint"], **output["decision"])
                decision_report = CharacterDecisionConstraintChecker().validate(db, context, candidate)
                if not decision_report.valid:
                    run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "REPLAY_DECISION_CONSTRAINT_FAILED", "report": decision_report.as_dict()}; session.status = ReplaySessionStatus.BLOCKED; session.failure_report = run.validation_report; db.flush(); return run
                parsed = CharacterPerformancePayload.model_validate(output)
                action_report = PerformanceActionConstraintChecker().validate(db, context, proposal, candidate, parsed.action, list(scene.participants or []))
                if not action_report.valid:
                    run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "REPLAY_ACTION_CONSTRAINT_FAILED", "report": action_report.as_dict()}; session.status = ReplaySessionStatus.BLOCKED; session.failure_report = run.validation_report; db.flush(); return run
                decision_temp = f"replay-decision:{session.id}:{scene.id}:{len(staged_decisions)+1}"
                turn_temp = f"replay-turn:{session.id}:{scene.id}:{len(staged_turns)+1}"
                staged_decisions.append({"temp_id": decision_temp, "replay_of_id": old_id, "character_id": old.character_id, "decision": output["decision"], "action": output["action"], "context_fingerprint": _fingerprint(context)})
                pair = next((pair for pair in queue_item.get("execution_pairs", []) if pair["decision_id"] == old_id), {})
                recipients = PerformanceObservationRouter().recipients(ActionVisibility(output["action"]["visibility"]), list(scene.participants or []), old.character_id, output["action"].get("target_character_id"))
                staged_turns.append({"temp_id": turn_temp, "replay_of_id": pair.get("turn_id"), "decision_temp_id": decision_temp, "sequence": len(staged_turns)+1, "actor_character_id": old.character_id, "visibility": output["action"]["visibility"], "observable_action": output["action"].get("observable_action"), "spoken_content": output["action"].get("spoken_content"), "recipient_character_ids": recipients, "requires_world_resolution": output["action"].get("requires_world_resolution", False), "world_resolution_request": output["action"].get("world_resolution_request"), "validation": {"valid": True}})
                for recipient in recipients:
                    if output["action"].get("observable_action"): memories.append({"temp_id": f"replay-memory:{session.id}:{scene.id}:{len(memories)+1}", "character_id": recipient, "content": output["action"]["observable_action"], "importance": .5, "emotional_weight": 0.0, "confidence": 1.0, "source_sequence": scene.sequence, "source_turn_temp_id": turn_temp, "source_resolution_temp_id": None, "old_resource_id": None, "reason": "OBSERVED_REPLAY_ACTION"})
                if output["action"].get("requires_world_resolution"):
                    request = output["action"].get("world_resolution_request") or {}
                    view = ReplayWorldView(session); entity = view.entity(request.get("target_entity_id"))
                    world_context = {"request": request, "target_entity": entity, "location": entity, "allowed_world_entity_ids": [entity["id"]] if entity else [], "canon": view.canon(), "scope": {"location_id": entity["id"] if entity else None, "actor_character_id": old.character_id, "target_character_id": None, "performance_id": scene.id}, "forbidden_canon_ids": [], "forbidden_propositions": []}
                    resolved, _ = HeuristicWorldResolver().resolve(world_context)
                    if resolved.get("outcome") == "UNRESOLVED":
                        run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "WORLD_INFORMATION_MISSING", "missing_information": resolved.get("missing_information", [])}; session.status = ReplaySessionStatus.BLOCKED; session.failure_report = run.validation_report; db.flush(); return run
                    if len(pair.get("resolution_ids", [])) > 1:
                        run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "REPLAY_EXECUTION_LINEAGE_AMBIGUOUS"}; session.status = ReplaySessionStatus.BLOCKED; session.failure_report = run.validation_report; db.flush(); return run
                    resolved["temp_id"] = f"replay-resolution:{session.id}:{scene.id}:{turn_temp}"; resolved["replay_of_id"] = next(iter(pair.get("resolution_ids", [])), None); resolved["turn_temp_id"] = turn_temp; resolved["resolver_mode"] = "HEURISTIC"; resolved["status"] = "VALID"; resolved["recipient_character_ids"] = sorted(({old.character_id} if resolved.get("actor_observation") else set()) | (set(scene.participants or []) if resolved.get("public_observation") else set()))
                    resolution_report = WorldResolutionConstraintChecker().validate(db, world_context, WorldResolutionPayload.model_validate(resolved), session.project_id)
                    if not resolution_report["valid"]:
                        run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "REPLAY_RESOLUTION_CONSTRAINT_FAILED", "report": resolution_report}; session.status = ReplaySessionStatus.BLOCKED; return run
                    staged_resolutions.append(resolved)
                    if resolved.get("actor_observation"):
                        for fact in resolved.get("objective_facts", []): knowledge.append({"temp_id": f"replay-knowledge:{session.id}:{scene.id}:{len(knowledge)+1}", "character_id": old.character_id, "status": "KNOWN", "proposition": f"{fact['subject_type']} {fact['subject_id']}: {fact['predicate']} = {json.dumps(fact['value'], sort_keys=True)}", "confidence": 1.0, "source_sequence": scene.sequence, "source_turn_temp_id": turn_temp, "source_resolution_temp_id": resolved["temp_id"], "old_resource_id": None, "reason": "STRUCTURED_ACTOR_OBSERVATION"})
                    for recipient in resolved["recipient_character_ids"]:
                        observation = resolved.get("actor_observation") if recipient == old.character_id else resolved.get("public_observation")
                        if observation: memories.append({"temp_id": f"replay-memory:{session.id}:{scene.id}:{len(memories)+1}", "character_id": recipient, "content": observation, "importance": .5, "emotional_weight": 0.0, "confidence": 1.0, "source_sequence": scene.sequence, "source_turn_temp_id": turn_temp, "source_resolution_temp_id": resolved["temp_id"], "old_resource_id": None, "reason": "REPLAY_OBSERVATION"})
            state = deepcopy(session.staged_world_state); state.setdefault("staged_facts", []).extend(fact for resolution in staged_resolutions for fact in resolution.get("objective_facts", [])); state.setdefault("current_world", {})["staged_facts"] = list(state["staged_facts"]); state.setdefault("staged_cognition", {}).setdefault("knowledge", []).extend(knowledge); state["staged_cognition"].setdefault("memories", []).extend(memories); state[str(scene.sequence)] = {"situation": situation, "decisions": staged_decisions, "turns": staged_turns, "resolutions": staged_resolutions}; state.setdefault("scene_results", {})[scene.id] = {"mode": "REPLAY", "sequence": scene.sequence, "situation": situation, "performance": {"temp_id": f"replay-performance:{session.id}:{scene.id}", "participant_order": list(scene.participants or []), "active_participant_ids": list(scene.participants or []), "mode": "HEURISTIC"}, "decisions": staged_decisions, "turns": staged_turns, "resolutions": staged_resolutions, "knowledge": knowledge, "memories": memories, "validation": {"code": "REPLAY_VALIDATED"}}; session.staged_world_state = state
            run.validation_report = {"code": "REPLAY_VALIDATED", "deterministic": True, "staged": True}
            run.status = ReplaySceneRunStatus.VALIDATED
        run.completed_at = datetime.utcnow(); session.cursor += 1; session.current_sequence = session.queue[session.cursor]["sequence"] if session.cursor < len(session.queue) else None; session.status = ReplaySessionStatus.RUNNING; session.current_fingerprint = _fingerprint({"world": (session.staged_world_state or {}).get("current_world"), "queue": session.queue, "cursor": session.cursor}); db.flush(); return run

    def commit(self, db: Session, session: RetconReplaySession, explicit_confirmation: bool):
        if not explicit_confirmation: self._fail("EXPLICIT_CONFIRMATION_REQUIRED")
        if session.status != ReplaySessionStatus.RUNNING or session.cursor < len(session.queue): self._fail("REPLAY_NOT_VALIDATED")
        app = db.get(RetconApplication, session.retcon_application_id)
        session.pre_commit_snapshot_id = WorldSnapshotBuilder().create(db, session.project_id, __import__("app.models", fromlist=["SnapshotType"]).SnapshotType.PRE_REPLAY_COMMIT).id
        runs = db.scalars(select(ReplaySceneRun).where(ReplaySceneRun.replay_session_id == session.id).order_by(ReplaySceneRun.original_sequence, ReplaySceneRun.completed_at, ReplaySceneRun.id)).all()
        final_runs = {}
        for run in runs:
            if run.mode == "REPLAY" and run.status == ReplaySceneRunStatus.VALIDATED:
                final_runs[run.original_scene_id] = run
        for run in final_runs.values():
            if not run.replacement_scene_id:
                old = db.get(Scene, run.original_scene_id)
                staged = (session.staged_world_state or {}).get(str(run.original_sequence), {})
                if old:
                    resolutions = staged.get("resolutions", []); facts = [fact for resolution in resolutions for fact in resolution.get("objective_facts", [])]
                    new = Scene(project_id=old.project_id, sequence=old.sequence, world_time=old.world_time, location=old.location, participants=list(old.participants or []), intent=old.intent, facts=facts, result={"resolutions": [{"outcome": value.get("outcome"), "outcome_summary": value.get("outcome_summary"), "objective_facts": value.get("objective_facts", [])} for value in resolutions]}, summary="Deterministic replay scene", story_threads=list(old.story_threads or []), status=old.status, history_status="STAGED")
                    db.add(new); db.flush(); run.replacement_scene_id = new.id
            old = db.get(Scene, run.original_scene_id); new = db.get(Scene, run.replacement_scene_id) if run.replacement_scene_id else None
            if old and new:
                old.history_status = "SUPERSEDED"; old.superseded_by_scene_id = new.id; db.flush(); new.history_status = "ACTIVE"; run.status = ReplaySceneRunStatus.COMMITTED
                staged = (session.staged_world_state or {}).get(str(run.original_sequence), {})
                decisions = staged.get("decisions", [])
                if decisions:
                    proposal_id = db.get(CharacterDecision, decisions[0]["replay_of_id"]).scene_proposal_id if db.get(CharacterDecision, decisions[0]["replay_of_id"]) else None
                    if proposal_id:
                        take = (db.scalar(select(func.max(ScenePerformance.take_number)).where(ScenePerformance.scene_proposal_id == proposal_id)) or 0) + 1
                        performance = ScenePerformance(project_id=session.project_id, scene_proposal_id=proposal_id, take_number=take, proposal_context_fingerprint=_fingerprint({"session": session.id, "scene": new.id}), mode=PerformanceMode.HEURISTIC, status=PerformanceStatus.COMPLETED, participant_order=list(new.participants or []), active_participant_ids=list(new.participants or []), max_turns=len(decisions), turn_count=len(decisions))
                        db.add(performance); db.flush()
                        binding = SceneExecutionBinding(project_id=session.project_id, scene_id=new.id, performance_id=performance.id, replay_session_id=session.id, active=False); db.add(binding)
                        for idx, item in enumerate(decisions, 1):
                            payload = item["decision"]; action = item["action"]
                            decision = CharacterDecision(project_id=session.project_id, scene_proposal_id=proposal_id, character_id=item["character_id"], context_fingerprint=item["context_fingerprint"], replay_session_id=session.id, replay_of_id=item["replay_of_id"], **payload)
                            db.add(decision); db.flush()
                            staged_turn = next((turn for turn in staged.get("turns", []) if turn.get("decision_temp_id") == item.get("temp_id")), None) or {}
                            turn = ScenePerformanceTurn(project_id=session.project_id, performance_id=performance.id, sequence=staged_turn.get("sequence", idx), actor_character_id=item["character_id"], actor_context_fingerprint=item["context_fingerprint"], character_decision_id=decision.id, action_visibility=staged_turn.get("visibility", action["visibility"]), observable_action=staged_turn.get("observable_action", action.get("observable_action")), spoken_content=staged_turn.get("spoken_content", action.get("spoken_content")), recipient_character_ids=staged_turn.get("recipient_character_ids", list(new.participants or [])), requires_world_resolution=staged_turn.get("requires_world_resolution", action.get("requires_world_resolution", False)), world_resolution_request=staged_turn.get("world_resolution_request", action.get("world_resolution_request")), validation_result=staged_turn.get("validation", {"valid": True}), replay_session_id=session.id, replay_of_id=staged_turn.get("replay_of_id"))
                            db.add(turn); db.flush(); run.new_decision_ids = list(run.new_decision_ids or []) + [decision.id]; run.new_turn_ids = list(run.new_turn_ids or []) + [turn.id]
                            staged_resolution = next((value for value in staged.get("resolutions", []) if value.get("turn_temp_id") == staged_turn.get("temp_id")), None)
                            if staged_resolution:
                                resolution = WorldResolution(project_id=session.project_id, performance_id=performance.id, performance_turn_id=turn.id, resolver_mode=ResolverMode.HEURISTIC, world_context_fingerprint=_fingerprint(staged_resolution), status=ResolutionStatus.VALID, outcome=staged_resolution["outcome"], outcome_summary=staged_resolution["outcome_summary"], objective_facts=staged_resolution.get("objective_facts", []), actor_observation=staged_resolution.get("actor_observation"), public_observation=staged_resolution.get("public_observation"), recipient_character_ids=staged_resolution.get("recipient_character_ids", []), canon_fact_ids_used=staged_resolution.get("canon_fact_ids_used", []), world_entity_ids_used=staged_resolution.get("world_entity_ids_used", []), resolution_basis_summary=staged_resolution.get("resolution_basis_summary"), missing_information=staged_resolution.get("missing_information", []), replay_session_id=session.id, replay_of_id=staged_resolution.get("replay_of_id"))
                                db.add(resolution); db.flush(); run.new_resolution_ids = list(run.new_resolution_ids or []) + [resolution.id]
                        for item in staged.get("knowledge", []):
                            row = CharacterKnowledge(character_id=item["character_id"], proposition=item["proposition"], status=item["status"], source=new.id, confidence=item["confidence"], replay_session_id=session.id, replay_of_id=item.get("old_resource_id")); db.add(row); db.flush(); run.new_knowledge_ids = list(run.new_knowledge_ids or []) + [row.id]
                        for item in staged.get("memories", []):
                            row = CharacterMemory(character_id=item["character_id"], content=item["content"], importance=item["importance"], emotional_weight=item["emotional_weight"], confidence=item["confidence"], distortion={}, source_scene=new.id, replay_session_id=session.id, replay_of_id=item.get("old_resource_id")); db.add(row); db.flush(); run.new_memory_ids = list(run.new_memory_ids or []) + [row.id]
                        old_binding = db.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == old.id, SceneExecutionBinding.active.is_(True)))
                        if old_binding: old_binding.active = False
                        db.flush(); binding.active = True
                        if self.failure_injector:
                            self.failure_injector("AFTER_REPLAY_MATERIALIZATION")
        replacement_by_old = {run.original_scene_id: run.replacement_scene_id for run in final_runs.values() if run.replacement_scene_id}
        # A rebuild is complete only when the affected resource was covered by a replayed scene.
        for inv in db.scalars(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.retcon_application_id == app.id, RetconCognitionInvalidation.status == RetconCognitionInvalidationStatus.ACTIVE)).all():
            coverage = next((item for item in session.queue if inv.resource_id in (item.get("knowledge_ids", []) + item.get("memory_ids", [])) and item.get("scene_id") in replacement_by_old), None)
            if not coverage:
                self._fail("COGNITION_REBUILD_INCOMPLETE")
            run = final_runs.get(coverage["scene_id"]); replacement_ids = (run.new_knowledge_ids if inv.resource_type == "KNOWLEDGE" else run.new_memory_ids) if run else []
            replacement_id = next((rid for rid in replacement_ids if (db.get(CharacterKnowledge if inv.resource_type == "KNOWLEDGE" else CharacterMemory, rid).replay_of_id == inv.resource_id)), None)
            inv.resolution_report = {"result": "REPLACED", "replay_scene_id": replacement_by_old[coverage["scene_id"]], "replacement_resource_id": replacement_id} if replacement_id else {"result": "INVALIDATED_WITHOUT_REPLACEMENT", "replay_scene_id": replacement_by_old[coverage["scene_id"]], "reason": "new history no longer gives this character the cognition"}
            inv.status = RetconCognitionInvalidationStatus.RESOLVED
        app.status = RetconApplicationStatus.REPLAY_COMPLETED
        session.status = ReplaySessionStatus.COMPLETED; session.completed_at = datetime.utcnow()
        session.post_commit_snapshot_id = WorldSnapshotBuilder().create(db, session.project_id, __import__("app.models", fromlist=["SnapshotType"]).SnapshotType.POST_REPLAY_COMMIT).id
        db.flush(); return session
