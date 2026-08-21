"""Deterministic selective replay orchestration.  It never invokes an AI provider."""
from __future__ import annotations
import hashlib, json
import re
from datetime import datetime
from sqlalchemy import select, func
from copy import deepcopy
from sqlalchemy.orm import Session
from .models import (
    CharacterDecision, CharacterKnowledge, CharacterMemory, ReplaySceneRun,
    ReplaySceneRunStatus, ReplaySessionStatus, RetconApplication,
    RetconApplicationStatus, RetconCognitionInvalidation,
    RetconCognitionInvalidationStatus, RetconReplaySession, Scene, ScenePerformance,
    ScenePerformanceTurn, WorldResolution, PerformanceStatus, SceneExecutionBinding, PerformanceMode, ResolutionStatus, ResolutionOutcome, ResolverMode, ActionVisibility, Project,
)
from .versioning import WorldSnapshotBuilder
from .performance import HeuristicCharacterPerformer, CharacterPerformancePayload, PerformanceActionConstraintChecker, PerformanceObservationRouter
from .character_mind import CharacterContextBuilder, CharacterDecisionConstraintChecker, ReplayCharacterMindViewBuilder
from .world_resolution import HeuristicWorldResolver, WorldResolutionPayload, WorldResolutionConstraintChecker
from .historical import SceneCheckpointService, SceneCheckpointOrigin, CurrentSceneCheckpointResolver, snapshot_fingerprint
from .causal_ledger import CausalLedgerService
from .revision import _record
from .snapshot_storage import ProjectWorldSnapshotHeadService, SnapshotPayloadResolver


class PreservedSceneStateTransitionProjector:
    """Apply only structured PRE -> POST changes onto a new sandbox payload."""
    def project(self, old_pre, old_post, new_pre, invalidated_ids=()):
        result = deepcopy(new_pre)
        invalidated_ids = set(invalidated_ids)
        def walk(before, after, target, path=()):
            if isinstance(before, dict) and isinstance(after, dict) and isinstance(target, dict):
                for key in set(before) | set(after):
                    if key not in before:
                        target[key] = deepcopy(after[key])
                    elif key not in after:
                        target.pop(key, None)
                    elif before[key] != after[key]:
                        if isinstance(before[key], (dict, list)) and isinstance(after[key], type(before[key])) and isinstance(target.get(key), type(before[key])):
                            walk(before[key], after[key], target[key], path + (key,))
                        else:
                            target[key] = deepcopy(after[key])
            elif before != after:
                return deepcopy(after)
            return target
        if isinstance(old_pre.get("project"), dict) and isinstance(old_post.get("project"), dict):
            result["project"] = walk(old_pre["project"], old_post["project"], deepcopy(result.get("project", {})))
        # Snapshot collections are keyed by stable row id.  This prevents an
        # old POST from replacing unrelated state introduced by earlier replay,
        # while retaining additions/removals caused by the preserved scene.
        for key in set(old_pre) | set(old_post):
            if key not in result or not isinstance(old_pre.get(key), list) or not isinstance(old_post.get(key), list):
                continue
            pre_rows = {row.get("id"): row for row in old_pre.get(key, []) if isinstance(row, dict) and row.get("id")}
            post_rows = {row.get("id"): row for row in old_post.get(key, []) if isinstance(row, dict) and row.get("id")}
            result_rows = {row.get("id"): row for row in result.get(key, []) if isinstance(row, dict) and row.get("id")}
            for ident in set(pre_rows) & set(post_rows) & set(result_rows):
                result_rows[ident] = walk(pre_rows[ident], post_rows[ident], result_rows[ident])
            for ident in set(post_rows) - set(pre_rows):
                if ident not in invalidated_ids:
                    result_rows[ident] = deepcopy(post_rows[ident])
            for ident in set(pre_rows) - set(post_rows):
                result_rows.pop(ident, None)
            if key in {"character_knowledge", "character_memories"}:
                result_rows = {ident: row for ident, row in result_rows.items() if ident not in invalidated_ids}
            result[key] = [result_rows[ident] for ident in sorted(result_rows)] + [row for row in result.get(key, []) if not isinstance(row, dict) or not row.get("id")]
        return result


class ReplayCheckpointStateBuilder:
    """Build a WorldSnapshot-compatible effective sandbox payload, pure only."""
    KEYS = ("project", "canon_facts", "world_entities", "characters", "character_knowledge", "character_memories", "reveal_constraints", "story_threads", "story_arcs", "scenes", "chapters")
    def build(self, session):
        state = session.staged_world_state or {}
        payload = {key: deepcopy((state.get("current_world") or {}).get(key, {} if key == "project" else [])) for key in self.KEYS}
        entities = {row.get("id"): row for row in payload["world_entities"] if row.get("id")}
        for fact in state.get("staged_facts", []):
            if fact.get("subject_type") == "ENTITY" and fact.get("subject_id") in entities:
                entities[fact["subject_id"]].setdefault("profile", {})[fact.get("predicate")] = fact.get("value")
        payload["world_entities"] = [entities[key] for key in sorted(entities)]
        cognition = state.get("staged_cognition", {})
        for key, target in (("knowledge", "character_knowledge"), ("memories", "character_memories")):
            rows = {row.get("id"): row for row in payload[target] if row.get("id")}
            for value in cognition.get(key, []):
                row = deepcopy(value); row["id"] = row.get("temp_id")
                rows[row["id"]] = row
            payload[target] = [rows[key] for key in sorted(rows)]
        return payload


class ReplayCheckpointFormalizer:
    """Purely converts sandbox boundary identities to formal current history."""
    def formalize(self, payload, sequence, *, include_current, replacements, knowledge_by_temp, memory_by_temp, invalidated_ids=()):
        result = deepcopy(payload); invalidated = set(invalidated_ids)
        replacement_rows = {old_id: (old_sequence, _record(scene)) for old_id, (old_sequence, scene) in replacements.items()}
        rows = []
        for row in result.get("scenes", []):
            replacement = replacement_rows.get(row.get("id"))
            if replacement and replacement[0] < sequence:
                rows.append(deepcopy(replacement[1]))
            elif replacement and replacement[0] == sequence and include_current:
                rows.append(deepcopy(replacement[1]))
            elif not replacement:
                rows.append(row)
        seen = {row.get("id") for row in rows}
        for _old_id, (old_sequence, row) in replacement_rows.items():
            if (old_sequence < sequence or (include_current and old_sequence == sequence)) and row.get("id") not in seen:
                rows.append(deepcopy(row))
        result["scenes"] = sorted(rows, key=lambda row: (row.get("sequence", 0), row.get("id", "")))
        for key, mapping in (("character_knowledge", knowledge_by_temp), ("character_memories", memory_by_temp)):
            result[key] = [deepcopy(mapping.get(row.get("id"), row)) for row in result.get(key, []) if row.get("id") not in invalidated]
        return result

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
        mind = ReplayCharacterMindViewBuilder().build(db, session, scene, proposal, character_id)
        location_id = getattr(proposal, "location_id", None)
        location = world.entity(location_id) if location_id else None
        relationships = {key: value for key, value in (character.get("relationships") or {}).items() if key in {row["id"] for row in others}}
        entry = getattr(proposal, "entry_state", {}) or {}; grouped = {"KNOWN": [], "SUSPECTED": [], "FALSE_BELIEF": [], "UNKNOWN": []}
        for row in mind["knowledge"]: grouped[row["status"]].append(row)
        context = {"project": {"id": session.project_id}, "character": {"id": character_id, "name": character.get("name"), "personality": character.get("personality") or {}, "core_values": character.get("core_values") or [], "boundaries": character.get("boundaries") or [], "goals": character.get("goals") or {}, "current_state": character.get("current_state") or {}, "physical_state": character.get("physical_state") or {}, "emotional_state": character.get("emotional_state") or {}, "relationships": relationships}, "scene": {"proposal_id": proposal.id, "location": location, "visible_context": entry.get("visible_context", {}), "actor_visible_context": entry.get("actor_visible_context", {}).get(character_id, {}), "other_participants": [{"id": row["id"], "name": row.get("name")} for row in others], "active_participant_ids": list(scene.participants or []), "performance_observations": [], "world_observations": [], "self_turn_history": [], "world_affordances": entry.get("world_affordances", [])}, "knowledge": grouped, "memories": mind["memories"], "inventory": list(character.get("inventory") or []), "abilities": list(character.get("abilities") or []), "belief_conflicts": mind["belief_conflicts"]}
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
        def path_scenes(item):
            return sorted({node.get("id") for node in (item.dependency_path or []) if node.get("type") == "SCENE" and node.get("id") in scene_ids})

        def cognition_candidates(item, row, source_field):
            # Source lineage is authoritative.  Only fall back when it is absent,
            # never by combining unrelated hints into an ambiguous guess.
            structured = getattr(row, source_field, None) if row else None
            if structured:
                return [structured] if structured in scene_ids else []
            if item.scene_id:
                return [item.scene_id] if item.scene_id in scene_ids else []
            return path_scenes(item)

        for item in items:
            if item.resource_type == "CHARACTER_KNOWLEDGE":
                row = db.get(CharacterKnowledge, item.resource_id)
                candidates = cognition_candidates(item, row, "source")
            elif item.resource_type == "CHARACTER_MEMORY":
                row = db.get(CharacterMemory, item.resource_id)
                candidates = cognition_candidates(item, row, "source_scene")
            elif item.resource_type in ownership:
                candidates = ownership[item.resource_type].get(item.resource_id, [])
            else:
                candidates = [sid for sid in scene_ids if item.scene_id == sid or any(node.get("type") == "SCENE" and node.get("id") == sid for node in (item.dependency_path or []))]
            if item.resource_type in {"CHARACTER_KNOWLEDGE", "CHARACTER_MEMORY"} and len(candidates) != 1:
                raise ValueError("COGNITION_REPLAY_COVERAGE_UNRESOLVED" if not candidates else "COGNITION_REPLAY_COVERAGE_AMBIGUOUS")
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

class ReplayCognitionReplacementMatcher:
    """Match only explicit structured provenance; never compare prose semantically."""
    _fact = re.compile(r"^(ENTITY|CHARACTER|LOCATION|SCENE) ([^:]+): ([^ ]+) = (.+)$")

    def knowledge(self, old: CharacterKnowledge, candidate: dict, scene_id: str) -> bool:
        identity = candidate.get("fact_identity") or {}
        if old.character_id != candidate.get("character_id") or old.source != scene_id:
            return False
        match = self._fact.match(old.proposition or "")
        if not match:
            return False
        try:
            old_value = json.loads(match.group(4))
        except (TypeError, json.JSONDecodeError):
            old_value = match.group(4)
        return {"subject_type": match.group(1), "subject_id": match.group(2), "predicate": match.group(3), "value": old_value} == identity

    def memory(self, old: CharacterMemory, candidate: dict, scene_id: str) -> bool:
        # Legacy memories have no structured turn/resolution provenance.  Do not
        # infer replacement from text or source_scene alone.
        return False

def _fingerprint(value):
    return "replay-input-v1:" + hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()

def _cognition_fingerprint(row):
    if isinstance(row, CharacterKnowledge):
        value = {"type": "KNOWLEDGE", "id": row.id, "character_id": row.character_id, "proposition": row.proposition, "status": getattr(row.status, "value", row.status), "source": row.source, "confidence": row.confidence}
    else:
        value = {"type": "MEMORY", "id": row.id, "character_id": row.character_id, "content": row.content, "source_scene": row.source_scene, "importance": row.importance, "emotional_weight": row.emotional_weight, "confidence": row.confidence}
    return _fingerprint(value)

class CurrentSceneHistoryResolver:
    def resolve(self, db: Session, project_id: str, sequence: int):
        rows = db.scalars(select(Scene).where(Scene.project_id == project_id, Scene.sequence == sequence, Scene.history_status == "ACTIVE")).all()
        if len(rows) > 1:
            raise ValueError("CURRENT_HISTORY_AMBIGUOUS")
        return rows[0] if rows else None

class PreservedSceneValidator:
    def validate(self, db: Session, scene: Scene, world=None, replay_session=None):
        participants = scene.participants or []
        available = {row.get("id") for row in world.rows("characters")} if world else {row.id for row in db.query(__import__("app.models", fromlist=["Character"]).Character).filter_by(project_id=scene.project_id).all()}
        missing = sorted(set(participants) - available)
        if missing:
            return "REPLAY_ESCALATED", {"code": "PARTICIPANT_UNAVAILABLE", "participants": missing}
        binding = db.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == scene.id, SceneExecutionBinding.active.is_(True)))
        proposal = None
        if binding:
            performance = db.get(ScenePerformance, binding.performance_id)
            proposal = db.get(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal, performance.scene_proposal_id) if performance else None
        prerequisites = ((proposal.entry_state if proposal else {}) or {}).get("replay_prerequisites", {})
        view = world
        for required in prerequisites.get("required_entity_facts", []):
            if view.fact("ENTITY", required.get("entity_id"), required.get("predicate")) != required.get("expected"):
                return "REPLAY_ESCALATED", {"code": "STRUCTURED_PREREQUISITE_FAILED", "entity_id": required.get("entity_id"), "predicate": required.get("predicate")}
        for entity_id in prerequisites.get("required_active_entities", []):
            entity = view.entity(entity_id)
            if not entity or not entity.get("active", False): return "REPLAY_ESCALATED", {"code": "ENTITY_UNAVAILABLE", "entity_id": entity_id}
        location_id = prerequisites.get("location_id")
        if location_id:
            location = view.entity(location_id)
            if not location or not location.get("active", False):
                return "REPLAY_ESCALATED", {"code": "LOCATION_UNAVAILABLE", "location_id": location_id}
        for character_id in prerequisites.get("required_active_characters", []):
            character = view.character(character_id)
            if not character or not character.get("active", False): return "REPLAY_ESCALATED", {"code": "CHARACTER_UNAVAILABLE", "character_id": character_id}
        canon_ids = {row.get("id") for row in view.canon()}
        for canon_id in prerequisites.get("required_canon_fact_ids", []):
            if canon_id not in canon_ids:
                return "REPLAY_ESCALATED", {"code": "CANON_PREREQUISITE_FAILED", "canon_fact_id": canon_id}
        for required in prerequisites.get("required_inventory", []):
            character = view.character(required.get("character_id")) or {}
            if required.get("item") not in (character.get("inventory") or []):
                return "REPLAY_ESCALATED", {"code": "INVENTORY_PREREQUISITE_FAILED", "character_id": required.get("character_id")}
        if prerequisites.get("required_knowledge"):
            if replay_session is None:
                return "REPLAY_ESCALATED", {"code": "KNOWLEDGE_PREREQUISITE_UNAVAILABLE"}
            from .historical import TemporalCharacterCognitionReader
            for required in prerequisites["required_knowledge"]:
                rows = TemporalCharacterCognitionReader().read(db, scene.project_id, required.get("character_id"), replay_session, scene.sequence)["knowledge"]
                if required.get("knowledge_id") not in {row.id for row in rows}:
                    return "REPLAY_ESCALATED", {"code": "KNOWLEDGE_PREREQUISITE_FAILED", "character_id": required.get("character_id"), "knowledge_id": required.get("knowledge_id")}
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
        from .models import WorldRevision
        revision = db.get(WorldRevision, app.source_revision_id)
        boundary_id = (app.replay_summary or {}).get("earliest_affected_scene_id")
        boundary = db.get(Scene, boundary_id) if boundary_id else None
        if boundary and boundary.history_status == "ACTIVE":
            try: boundary_checkpoint = CurrentSceneCheckpointResolver().current(db, project_id, boundary.id)
            except ValueError: self._fail("HISTORICAL_BASELINE_UNAVAILABLE")
            if boundary_checkpoint.capture_protocol_version < 2: self._fail("HISTORICAL_BASELINE_UNAVAILABLE")
        queue = SelectiveReplayQueue().build(db, app)
        earliest = next((item for item in queue if item["mode"] == "REPLAY"), None)
        if earliest is None:
            payload, fingerprint = WorldSnapshotBuilder().build(db, project_id)
            session = RetconReplaySession(project_id=project_id, retcon_application_id=app.id, status=ReplaySessionStatus.READY, baseline_snapshot_id=None, baseline_fingerprint=fingerprint, queue=queue, current_sequence=None, staged_world_state={"baseline": payload, "current_world": deepcopy(payload), "staged_facts": [], "staged_cognition": {}, "scene_results": {}})
            db.add(session); db.flush(); return session
        try: checkpoint = CurrentSceneCheckpointResolver().current(db, project_id, earliest["scene_id"])
        except ValueError: self._fail("HISTORICAL_BASELINE_UNAVAILABLE")
        if checkpoint.capture_protocol_version < 2:
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
        # Replay checkpoint boundaries are staged only.  Formal snapshots are
        # created later in the same transaction as history materialization.
        state_before = deepcopy(session.staged_world_state or {})
        boundary = state_before.setdefault("scene_checkpoints", {}).setdefault(scene.id, {"sequence": scene.sequence, "mode": item["mode"]})
        if "pre_payload" not in boundary:
            boundary["pre_payload"] = ReplayCheckpointStateBuilder().build(session)
            boundary["pre_fingerprint"] = snapshot_fingerprint(boundary["pre_payload"])
        session.staged_world_state = state_before
        run = ReplaySceneRun(project_id=session.project_id, replay_session_id=session.id, original_scene_id=scene.id, original_sequence=scene.sequence, mode=item["mode"], status=ReplaySceneRunStatus.RUNNING, input_fingerprint=_fingerprint({"scene": scene.id, "sequence": scene.sequence, "baseline": session.baseline_fingerprint}), started_at=datetime.utcnow())
        db.add(run); db.flush()
        if item["mode"] == "VALIDATE_PRESERVED":
            result, report = PreservedSceneValidator().validate(db, scene, ReplayWorldView(session), session)
            if result == "REPLAY_ESCALATED":
                queue = deepcopy(session.queue)
                promoted = dict(queue[session.cursor])
                pairs = list(promoted.get("execution_pairs", []))
                binding = db.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == scene.id, SceneExecutionBinding.active.is_(True)))
                if binding and (not pairs or any(not pair.get("decision_id") or not pair.get("turn_id") for pair in pairs)):
                    run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "DYNAMIC_REPLAY_EXECUTION_INCOMPLETE"}; session.status = ReplaySessionStatus.BLOCKED; session.failure_report = run.validation_report; db.flush(); return run
                if binding:
                    promoted["decision_ids"] = sorted({pair["decision_id"] for pair in pairs})
                    promoted["turn_ids"] = sorted({pair["turn_id"] for pair in pairs})
                    promoted["resolution_ids"] = sorted({resolution_id for pair in pairs for resolution_id in pair.get("resolution_ids", [])})
                dynamic = []
                for row in db.scalars(select(CharacterKnowledge).join(__import__("app.models", fromlist=["Character"]).Character, CharacterKnowledge.character_id == __import__("app.models", fromlist=["Character"]).Character.id).where(CharacterKnowledge.source == scene.id, __import__("app.models", fromlist=["Character"]).Character.project_id == session.project_id)).all():
                    dynamic.append({"resource_type": "KNOWLEDGE", "resource_id": row.id, "character_id": row.character_id, "scene_id": scene.id, "sequence": scene.sequence, "fingerprint": _cognition_fingerprint(row)})
                for row in db.scalars(select(CharacterMemory).join(__import__("app.models", fromlist=["Character"]).Character, CharacterMemory.character_id == __import__("app.models", fromlist=["Character"]).Character.id).where(CharacterMemory.source_scene == scene.id, __import__("app.models", fromlist=["Character"]).Character.project_id == session.project_id)).all():
                    dynamic.append({"resource_type": "MEMORY", "resource_id": row.id, "character_id": row.character_id, "scene_id": scene.id, "sequence": scene.sequence, "fingerprint": _cognition_fingerprint(row)})
                promoted["knowledge_ids"] = sorted(set(promoted.get("knowledge_ids", [])) | {item["resource_id"] for item in dynamic if item["resource_type"] == "KNOWLEDGE"})
                promoted["memory_ids"] = sorted(set(promoted.get("memory_ids", [])) | {item["resource_id"] for item in dynamic if item["resource_type"] == "MEMORY"})
                promoted.update({"mode": "REPLAY", "reason": "DYNAMIC_EXPANSION", "dynamic_expansion_reason": report})
                queue[session.cursor] = promoted; session.queue = queue
                state = deepcopy(session.staged_world_state); staged_invalidations = state.setdefault("dynamic_cognition_invalidations", [])
                known = {(item["resource_type"], item["resource_id"]) for item in staged_invalidations}
                staged_invalidations.extend(item for item in dynamic if (item["resource_type"], item["resource_id"]) not in known)
                session.staged_world_state = state
                run.status = ReplaySceneRunStatus.VALIDATED; run.validation_report = {"result": "REPLAY_ESCALATED", **report}; session.status = ReplaySessionStatus.RUNNING; session.failure_report = None; db.flush(); return run
            # The unchanged historical transition is projected over the new
            # sandbox PRE, never copied as an old POST replacement.
            try:
                old_checkpoint = CurrentSceneCheckpointResolver().current(db, session.project_id, scene.id)
            except ValueError:
                old_checkpoint = None
            # A pre-7D legacy preserved scene can lack a checkpoint.  Preserve
            # its sandbox state rather than fabricating a historical delta.
            if old_checkpoint:
                old_pre = db.get(__import__("app.models", fromlist=["WorldSnapshot"]).WorldSnapshot, old_checkpoint.pre_snapshot_id)
                old_post = db.get(__import__("app.models", fromlist=["WorldSnapshot"]).WorldSnapshot, old_checkpoint.post_snapshot_id)
                state = deepcopy(session.staged_world_state or {})
                invalidated = set(db.scalars(select(RetconCognitionInvalidation.resource_id).where(RetconCognitionInvalidation.project_id == session.project_id, RetconCognitionInvalidation.status != RetconCognitionInvalidationStatus.ROLLED_BACK)).all())
                invalidated.update(value["resource_id"] for value in state.get("dynamic_cognition_invalidations", []))
                resolver = SnapshotPayloadResolver()
                state["current_world"] = PreservedSceneStateTransitionProjector().project(
                    resolver.materialize(db, old_pre),
                    resolver.materialize(db, old_post),
                    state.get("current_world", {}),
                    invalidated,
                )
                scene_row = _record(scene)
                scene_row["status"] = "OCCURRED"
                scene_rows = {row.get("id"): row for row in state["current_world"].get("scenes", []) if row.get("id")}
                scene_rows[scene.id] = scene_row
                state["current_world"]["scenes"] = sorted(scene_rows.values(), key=lambda row: (row.get("sequence", 0), row.get("id", "")))
                session.staged_world_state = state
            run.status = ReplaySceneRunStatus.VALIDATED; run.validation_report = report
        else:
            queue_item = session.queue[session.cursor]
            situation = {"sequence": scene.sequence, "location": scene.location, "participants": list(scene.participants or []), "story_threads": list(scene.story_threads or []), "intent": scene.intent}
            staged_decisions = []
            staged_turns = []
            staged_resolutions, knowledge, memories = [], [], []
            if queue_item.get("reason") == "DYNAMIC_EXPANSION" and db.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == scene.id, SceneExecutionBinding.active.is_(True))) and not queue_item.get("decision_ids"):
                run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "DYNAMIC_REPLAY_EXECUTION_INCOMPLETE"}; session.status = ReplaySessionStatus.BLOCKED; session.failure_report = run.validation_report; db.flush(); return run
            for old_id in queue_item.get("decision_ids", []):
                old = db.get(CharacterDecision, old_id)
                if not old: continue
                proposal = db.get(__import__("app.models", fromlist=["SceneProposal"]).SceneProposal, old.scene_proposal_id)
                if not proposal: continue
                context = ReplayCharacterContextBuilder().build(db, session, scene, proposal, old.character_id)
                output, _ = HeuristicCharacterPerformer().perform(context)
                candidate = CharacterDecision(project_id=session.project_id, scene_proposal_id=proposal.id, character_id=old.character_id, context_fingerprint=context["fingerprint"], **output["decision"])
                replay_view = ReplayWorldView(session)
                decision_report = CharacterDecisionConstraintChecker().validate(db, context, candidate, replay_view)
                if not decision_report.valid:
                    run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "REPLAY_DECISION_CONSTRAINT_FAILED", "report": decision_report.as_dict()}; session.status = ReplaySessionStatus.BLOCKED; session.failure_report = run.validation_report; db.flush(); return run
                parsed = CharacterPerformancePayload.model_validate(output)
                action_report = PerformanceActionConstraintChecker().validate(db, context, proposal, candidate, parsed.action, list(scene.participants or []), replay_view)
                if not action_report.valid:
                    run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "REPLAY_ACTION_CONSTRAINT_FAILED", "report": action_report.as_dict()}; session.status = ReplaySessionStatus.BLOCKED; session.failure_report = run.validation_report; db.flush(); return run
                decision_temp = f"replay-decision:{session.id}:{scene.id}:{len(staged_decisions)+1}"
                turn_temp = f"replay-turn:{session.id}:{scene.id}:{len(staged_turns)+1}"
                staged_decisions.append({"temp_id": decision_temp, "replay_of_id": old_id, "character_id": old.character_id, "decision": output["decision"], "action": output["action"], "context_fingerprint": _fingerprint(context)})
                pair = next((pair for pair in queue_item.get("execution_pairs", []) if pair["decision_id"] == old_id), {})
                recipients = PerformanceObservationRouter().recipients(ActionVisibility(output["action"]["visibility"]), list(scene.participants or []), old.character_id, output["action"].get("target_character_id"))
                staged_turns.append({"temp_id": turn_temp, "replay_of_id": pair.get("turn_id"), "decision_temp_id": decision_temp, "sequence": len(staged_turns)+1, "actor_character_id": old.character_id, "visibility": output["action"]["visibility"], "observable_action": output["action"].get("observable_action"), "spoken_content": output["action"].get("spoken_content"), "recipient_character_ids": recipients, "requires_world_resolution": output["action"].get("requires_world_resolution", False), "world_resolution_request": output["action"].get("world_resolution_request"), "validation": {"valid": True}})
                for recipient in recipients:
                    if output["action"].get("observable_action"): memories.append({"temp_id": f"replay-memory:{session.id}:{scene.id}:{len(memories)+1}", "character_id": recipient, "content": output["action"]["observable_action"], "importance": .5, "emotional_weight": 0.0, "confidence": 1.0, "source_sequence": scene.sequence, "source_scene": scene.id, "source_turn_temp_id": turn_temp, "source_resolution_temp_id": None, "old_resource_id": None, "reason": "OBSERVED_REPLAY_ACTION"})
                if output["action"].get("requires_world_resolution"):
                    request = output["action"].get("world_resolution_request") or {}
                    view = ReplayWorldView(session); entity = view.entity(request.get("target_entity_id"))
                    world_context = {"request": request, "target_entity": entity, "location": entity, "allowed_world_entity_ids": [entity["id"]] if entity else [], "canon": view.canon(), "scope": {"location_id": entity["id"] if entity else None, "actor_character_id": old.character_id, "target_character_id": None, "performance_id": scene.id}, "forbidden_canon_ids": [], "forbidden_propositions": []}
                    resolved, _ = HeuristicWorldResolver().resolve(world_context)
                    if resolved.get("outcome") == "UNRESOLVED":
                        run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "WORLD_INFORMATION_MISSING", "missing_information": resolved.get("missing_information", [])}; session.status = ReplaySessionStatus.BLOCKED; session.failure_report = run.validation_report; db.flush(); return run
                    if len(pair.get("resolution_ids", [])) > 1:
                        run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "REPLAY_EXECUTION_LINEAGE_AMBIGUOUS"}; session.status = ReplaySessionStatus.BLOCKED; session.failure_report = run.validation_report; db.flush(); return run
                    resolution_payload = WorldResolutionPayload.model_validate(resolved)
                    resolved["recipient_character_ids"] = sorted(({old.character_id} if resolved.get("actor_observation") else set()) | (set(scene.participants or []) if resolved.get("public_observation") else set()))
                    resolved["temp_id"] = f"replay-resolution:{session.id}:{scene.id}:{turn_temp}"; resolved["replay_of_id"] = next(iter(pair.get("resolution_ids", [])), None); resolved["turn_temp_id"] = turn_temp; resolved["resolver_mode"] = "HEURISTIC"; resolved["status"] = "VALID"
                    resolution_report = WorldResolutionConstraintChecker().validate(db, world_context, resolution_payload, session.project_id, replay_view)
                    if not resolution_report["valid"]:
                        run.status = ReplaySceneRunStatus.BLOCKED; run.validation_report = {"code": "REPLAY_RESOLUTION_CONSTRAINT_FAILED", "report": resolution_report}; session.status = ReplaySessionStatus.BLOCKED; return run
                    staged_resolutions.append(resolved)
                    if resolved.get("actor_observation"):
                        for fact in resolved.get("objective_facts", []): knowledge.append({"temp_id": f"replay-knowledge:{session.id}:{scene.id}:{len(knowledge)+1}", "character_id": old.character_id, "status": "KNOWN", "proposition": f"{fact['subject_type']} {fact['subject_id']}: {fact['predicate']} = {json.dumps(fact['value'], sort_keys=True)}", "confidence": 1.0, "source_sequence": scene.sequence, "source_turn_temp_id": turn_temp, "source_resolution_temp_id": resolved["temp_id"], "fact_identity": fact, "old_resource_id": None, "reason": "STRUCTURED_ACTOR_OBSERVATION"})
                    for recipient in resolved["recipient_character_ids"]:
                        observation = resolved.get("actor_observation") if recipient == old.character_id else resolved.get("public_observation")
                        if observation: memories.append({"temp_id": f"replay-memory:{session.id}:{scene.id}:{len(memories)+1}", "character_id": recipient, "content": observation, "importance": .5, "emotional_weight": 0.0, "confidence": 1.0, "source_sequence": scene.sequence, "source_scene": scene.id, "source_turn_temp_id": turn_temp, "source_resolution_temp_id": resolved["temp_id"], "old_resource_id": None, "reason": "REPLAY_OBSERVATION"})
            state = deepcopy(session.staged_world_state); state.setdefault("staged_facts", []).extend(fact for resolution in staged_resolutions for fact in resolution.get("objective_facts", [])); state.setdefault("current_world", {})["staged_facts"] = list(state["staged_facts"]); state.setdefault("staged_cognition", {}).setdefault("knowledge", []).extend(knowledge); state["staged_cognition"].setdefault("memories", []).extend(memories); state[str(scene.sequence)] = {"situation": situation, "decisions": staged_decisions, "turns": staged_turns, "resolutions": staged_resolutions}; state.setdefault("scene_results", {})[scene.id] = {"mode": "REPLAY", "sequence": scene.sequence, "situation": situation, "performance": {"temp_id": f"replay-performance:{session.id}:{scene.id}", "participant_order": list(scene.participants or []), "active_participant_ids": list(scene.participants or []), "mode": "HEURISTIC"}, "decisions": staged_decisions, "turns": staged_turns, "resolutions": staged_resolutions, "knowledge": knowledge, "memories": memories, "validation": {"code": "REPLAY_VALIDATED"}}; session.staged_world_state = state
            run.validation_report = {"code": "REPLAY_VALIDATED", "deterministic": True, "staged": True}
            run.status = ReplaySceneRunStatus.VALIDATED
        state_after = deepcopy(session.staged_world_state or {})
        boundary = state_after.setdefault("scene_checkpoints", {}).setdefault(scene.id, {"sequence": scene.sequence, "mode": item["mode"]})
        boundary["post_payload"] = ReplayCheckpointStateBuilder().build(session)
        boundary["post_fingerprint"] = snapshot_fingerprint(boundary["post_payload"])
        session.staged_world_state = state_after
        run.completed_at = datetime.utcnow(); session.cursor += 1; session.current_sequence = session.queue[session.cursor]["sequence"] if session.cursor < len(session.queue) else None; session.status = ReplaySessionStatus.RUNNING; session.current_fingerprint = _fingerprint({"world": (session.staged_world_state or {}).get("current_world"), "queue": session.queue, "cursor": session.cursor}); db.flush(); return run

    def commit(self, db: Session, session: RetconReplaySession, explicit_confirmation: bool):
        if not explicit_confirmation: self._fail("EXPLICIT_CONFIRMATION_REQUIRED")
        if session.status != ReplaySessionStatus.RUNNING or session.cursor < len(session.queue): self._fail("REPLAY_NOT_VALIDATED")
        # Shares normal SceneCommit serialization before allocating versions.
        if not db.scalar(select(Project).where(Project.id == session.project_id).with_for_update()):
            self._fail("REPLAY_PROJECT_NOT_FOUND")
        app = db.get(RetconApplication, session.retcon_application_id)
        session.pre_commit_snapshot_id = WorldSnapshotBuilder().create(db, session.project_id, __import__("app.models", fromlist=["SnapshotType"]).SnapshotType.PRE_REPLAY_COMMIT).id
        runs = db.scalars(select(ReplaySceneRun).where(ReplaySceneRun.replay_session_id == session.id).order_by(ReplaySceneRun.original_sequence, ReplaySceneRun.completed_at, ReplaySceneRun.id)).all()
        final_runs = {}
        for run in runs:
            if run.mode == "REPLAY" and run.status == ReplaySceneRunStatus.VALIDATED:
                final_runs[run.original_scene_id] = run
        materialized = []
        state = session.staged_world_state or {}
        formal_knowledge_by_temp, formal_memory_by_temp = {}, {}
        for run in final_runs.values():
            old = db.get(Scene, run.original_scene_id)
            staged = state.get("scene_results", {}).get(run.original_scene_id) or state.get(str(run.original_sequence), {})
            if not old:
                self._fail("REPLAY_SCENE_NOT_FOUND")
            resolutions = staged.get("resolutions", [])
            facts = [fact for resolution in resolutions for fact in resolution.get("objective_facts", [])]
            new = Scene(project_id=old.project_id, sequence=old.sequence, world_time=old.world_time, location=old.location, participants=list(old.participants or []), intent=old.intent, facts=facts, result={"resolutions": [{"outcome": value.get("outcome"), "outcome_summary": value.get("outcome_summary"), "objective_facts": value.get("objective_facts", [])} for value in resolutions]}, summary="Deterministic replay scene", story_threads=list(old.story_threads or []), status=__import__("app.models", fromlist=["SceneStatus"]).SceneStatus.OCCURRED, history_status="STAGED")
            db.add(new); db.flush(); run.replacement_scene_id = new.id
            # Cognition is materialized before decisions so later replay scenes
            # can bind their staged references to formal IDs in this same
            # transaction.
            staged_cognition = staged
            for item in staged_cognition.get("knowledge", []):
                old_resource_id = item.get("old_resource_id")
                if not old_resource_id:
                    for resource_id in next((q.get("knowledge_ids", []) for q in session.queue if q.get("scene_id") == run.original_scene_id), []):
                        old_knowledge = db.get(CharacterKnowledge, resource_id)
                        if old_knowledge and ReplayCognitionReplacementMatcher().knowledge(old_knowledge, item, run.original_scene_id):
                            old_resource_id = old_knowledge.id; break
                row = CharacterKnowledge(character_id=item["character_id"], proposition=item["proposition"], status=item["status"], source=new.id, confidence=item["confidence"], replay_session_id=session.id, replay_of_id=old_resource_id)
                db.add(row); db.flush(); run.new_knowledge_ids = list(run.new_knowledge_ids or []) + [row.id]; formal_knowledge_by_temp[item.get("temp_id")] = row.id
            for item in staged_cognition.get("memories", []):
                row = CharacterMemory(character_id=item["character_id"], content=item["content"], importance=item.get("importance", .5), emotional_weight=item.get("emotional_weight", 0.0), confidence=item.get("confidence", 1.0), distortion=item.get("distortion", {}), source_scene=new.id, replay_session_id=session.id, replay_of_id=item.get("old_resource_id"))
                db.add(row); db.flush(); run.new_memory_ids = list(run.new_memory_ids or []) + [row.id]; formal_memory_by_temp[item.get("temp_id")] = row.id
            binding = None
            decisions = staged.get("decisions", [])
            old_binding = db.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == old.id, SceneExecutionBinding.active.is_(True)))
            if old_binding and not decisions:
                self._fail("DYNAMIC_REPLAY_EXECUTION_INCOMPLETE" if next((item for item in session.queue if item.get("scene_id") == old.id), {}).get("reason") == "DYNAMIC_EXPANSION" else "REPLAY_EXECUTION_LINEAGE_INCOMPLETE")
            if decisions:
                old_decision = db.get(CharacterDecision, decisions[0]["replay_of_id"])
                proposal_id = old_decision.scene_proposal_id if old_decision else None
                if not proposal_id:
                    self._fail("REPLAY_EXECUTION_LINEAGE_INCOMPLETE")
                take = (db.scalar(select(func.max(ScenePerformance.take_number)).where(ScenePerformance.scene_proposal_id == proposal_id)) or 0) + 1
                performance_data = staged.get("performance", {})
                performance = ScenePerformance(project_id=session.project_id, scene_proposal_id=proposal_id, take_number=take, proposal_context_fingerprint=_fingerprint({"session": session.id, "scene": new.id}), mode=PerformanceMode.HEURISTIC, status=PerformanceStatus.COMPLETED, participant_order=list(performance_data.get("participant_order", new.participants or [])), active_participant_ids=list(performance_data.get("active_participant_ids", new.participants or [])), max_turns=len(decisions), turn_count=len(decisions))
                db.add(performance); db.flush()
                binding = SceneExecutionBinding(project_id=session.project_id, scene_id=new.id, performance_id=performance.id, replay_session_id=session.id, active=False)
                db.add(binding)
                for idx, item in enumerate(decisions, 1):
                    payload = deepcopy(item["decision"]); action = item["action"]
                    for reference in payload.get("knowledge_used", []) or []:
                        if isinstance(reference, dict) and reference.get("knowledge_id") in formal_knowledge_by_temp:
                            reference["knowledge_id"] = formal_knowledge_by_temp[reference["knowledge_id"]]
                    payload["memory_refs"] = [formal_memory_by_temp.get(reference, reference) if isinstance(reference, str) else {**reference, "memory_id": formal_memory_by_temp.get(reference.get("memory_id"), reference.get("memory_id"))} for reference in (payload.get("memory_refs", []) or [])]
                    for reference in payload.get("knowledge_used", []) or []:
                        if not isinstance(reference, dict):
                            self._fail("REPLAY_COGNITION_REFERENCE_INVALID")
                        cited = db.get(CharacterKnowledge, reference.get("knowledge_id"))
                        accepted = reference.get("accepted_statuses")
                        if not cited or cited.character_id != item["character_id"] or cited.proposition != reference.get("proposition") or (isinstance(accepted, list) and getattr(cited.status, "value", cited.status) not in {getattr(value, "value", value) for value in accepted}):
                            self._fail("REPLAY_COGNITION_REFERENCE_INVALID")
                    for reference in payload.get("memory_refs", []) or []:
                        ident = reference if isinstance(reference, str) else reference.get("memory_id") if isinstance(reference, dict) else None
                        cited = db.get(CharacterMemory, ident)
                        if not cited or cited.character_id != item["character_id"] or (isinstance(reference, dict) and reference.get("content") is not None and cited.content != reference.get("content")):
                            self._fail("REPLAY_COGNITION_REFERENCE_INVALID")
                    decision = CharacterDecision(project_id=session.project_id, scene_proposal_id=proposal_id, character_id=item["character_id"], context_fingerprint=item["context_fingerprint"], replay_session_id=session.id, replay_of_id=item["replay_of_id"], **payload)
                    db.add(decision); db.flush()
                    staged_turn = next((turn for turn in staged.get("turns", []) if turn.get("decision_temp_id") == item.get("temp_id")), None)
                    if not staged_turn:
                        self._fail("REPLAY_EXECUTION_LINEAGE_INCOMPLETE")
                    turn = ScenePerformanceTurn(project_id=session.project_id, performance_id=performance.id, sequence=staged_turn["sequence"], actor_character_id=item["character_id"], actor_context_fingerprint=item["context_fingerprint"], character_decision_id=decision.id, action_visibility=staged_turn.get("visibility", action["visibility"]), observable_action=staged_turn.get("observable_action", action.get("observable_action")), spoken_content=staged_turn.get("spoken_content", action.get("spoken_content")), recipient_character_ids=staged_turn.get("recipient_character_ids", []), requires_world_resolution=staged_turn.get("requires_world_resolution", action.get("requires_world_resolution", False)), world_resolution_request=staged_turn.get("world_resolution_request", action.get("world_resolution_request")), validation_result=staged_turn.get("validation", {"valid": True}), replay_session_id=session.id, replay_of_id=staged_turn.get("replay_of_id"))
                    db.add(turn); db.flush(); run.new_decision_ids = list(run.new_decision_ids or []) + [decision.id]; run.new_turn_ids = list(run.new_turn_ids or []) + [turn.id]
                    staged_resolution = next((value for value in resolutions if value.get("turn_temp_id") == staged_turn["temp_id"]), None)
                    if staged_resolution:
                        resolution = WorldResolution(project_id=session.project_id, performance_id=performance.id, performance_turn_id=turn.id, resolver_mode=ResolverMode.HEURISTIC, world_context_fingerprint=_fingerprint(staged_resolution), status=ResolutionStatus.VALID, outcome=staged_resolution["outcome"], outcome_summary=staged_resolution["outcome_summary"], objective_facts=staged_resolution.get("objective_facts", []), state_effects=staged_resolution.get("state_effects", []), actor_observation=staged_resolution.get("actor_observation"), public_observation=staged_resolution.get("public_observation"), recipient_character_ids=staged_resolution.get("recipient_character_ids", []), canon_fact_ids_used=staged_resolution.get("canon_fact_ids_used", []), world_entity_ids_used=staged_resolution.get("world_entity_ids_used", []), resolution_basis_summary=staged_resolution.get("resolution_basis_summary"), missing_information=staged_resolution.get("missing_information", []), replay_session_id=session.id, replay_of_id=staged_resolution.get("replay_of_id"))
                        db.add(resolution); db.flush(); run.new_resolution_ids = list(run.new_resolution_ids or []) + [resolution.id]
            if old_binding and binding is None:
                self._fail("REPLAY_EXECUTION_LINEAGE_INCOMPLETE")
            materialized.append((run, old, new, binding))

        # Dynamic expansion has only been staged so far.  Materialize its
        # quarantine records inside this same transaction, never during step.
        existing_invalidations = {(row.resource_type, row.resource_id) for row in db.scalars(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.retcon_application_id == app.id)).all()}
        for item in state.get("dynamic_cognition_invalidations", []):
            key = (item["resource_type"], item["resource_id"])
            if key in existing_invalidations:
                continue
            db.add(RetconCognitionInvalidation(project_id=session.project_id, retcon_application_id=app.id, character_id=item["character_id"], resource_type=item["resource_type"], resource_id=item["resource_id"], source_impact_item_id=None, reason="DYNAMIC_REPLAY_EXPANSION", original_semantic_fingerprint=item["fingerprint"], status=RetconCognitionInvalidationStatus.ACTIVE))
            existing_invalidations.add(key)

        # The hook is deliberately after every formal row has been flushed and
        # before either historical timeline is made current.
        db.flush()
        if type(self).failure_injector:
            type(self).failure_injector("AFTER_FORMAL_MATERIALIZATION")

        for _run, old, new, _binding in materialized:
            old.history_status = "SUPERSEDED"; old.superseded_by_scene_id = new.id
            old_binding = db.scalar(select(SceneExecutionBinding).where(SceneExecutionBinding.scene_id == old.id, SceneExecutionBinding.active.is_(True)))
            if old_binding: old_binding.active = False
            try:
                old_checkpoint = CurrentSceneCheckpointResolver().current(db, session.project_id, old.id)
            except ValueError:
                old_checkpoint = None
            if old_checkpoint:
                old_checkpoint.active = False
        db.flush()
        for run, _old, new, binding in materialized:
            new.history_status = "ACTIVE"
            if binding: binding.active = True
            run.status = ReplaySceneRunStatus.COMMITTED
        db.flush()
        # Materialize each staged scene boundary only after its formal current
        # history is active.  No step ever creates a formal snapshot.
        checkpoint_service = SceneCheckpointService()
        boundaries = state.get("scene_checkpoints", {})
        for run in final_runs.values():
            staged = state.get("scene_results", {}).get(run.original_scene_id, {})
            for item, formal_id in zip(staged.get("knowledge", []), run.new_knowledge_ids or []):
                row = _record(db.get(CharacterKnowledge, formal_id)); row.pop("created_at", None); row.pop("updated_at", None); formal_knowledge_by_temp[item.get("temp_id")] = row
            for item, formal_id in zip(staged.get("memories", []), run.new_memory_ids or []):
                row = _record(db.get(CharacterMemory, formal_id)); row.pop("created_at", None); row.pop("updated_at", None); formal_memory_by_temp[item.get("temp_id")] = row
        replacements = {old.id: (old.sequence, new) for _run, old, new, _binding in materialized}
        invalidated_ids = {row.resource_id for row in db.scalars(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.project_id == session.project_id, RetconCognitionInvalidation.status != RetconCognitionInvalidationStatus.ROLLED_BACK)).all()}
        formalizer = ReplayCheckpointFormalizer()
        for run, old, new, _binding in materialized:
            boundary = boundaries.get(old.id)
            if not boundary or "pre_payload" not in boundary or "post_payload" not in boundary:
                self._fail("SCENE_CHECKPOINT_MISSING")
            pre_payload = formalizer.formalize(boundary["pre_payload"], old.sequence, include_current=False, replacements=replacements, knowledge_by_temp=formal_knowledge_by_temp, memory_by_temp=formal_memory_by_temp, invalidated_ids=invalidated_ids)
            post_payload = formalizer.formalize(boundary["post_payload"], old.sequence, include_current=True, replacements=replacements, knowledge_by_temp=formal_knowledge_by_temp, memory_by_temp=formal_memory_by_temp, invalidated_ids=invalidated_ids)
            scene_rows = {row.get("id"): row for row in post_payload.get("scenes", []) if row.get("id") and row.get("id") != old.id}
            scene_rows[new.id] = _record(new)
            post_payload["scenes"] = sorted(scene_rows.values(), key=lambda row: (row.get("sequence", 0), row.get("id", "")))
            checkpoint_service.materialize_from_payloads(db, session.project_id, new, pre_payload, post_payload, origin=SceneCheckpointOrigin.REPLAY_COMMIT, source_replay_session_id=session.id)
        preserved_runs = [run for run in runs if run.mode == "VALIDATE_PRESERVED" and run.status == ReplaySceneRunStatus.VALIDATED and run.validation_report.get("code") == "PRESERVED_PREREQUISITES_VALID"]
        for run in preserved_runs:
            scene = db.get(Scene, run.original_scene_id); boundary = boundaries.get(scene.id) if scene else None
            if not scene or not boundary or "pre_payload" not in boundary or "post_payload" not in boundary:
                self._fail("SCENE_CHECKPOINT_MISSING")
            scene.status = __import__("app.models", fromlist=["SceneStatus"]).SceneStatus.OCCURRED
            db.flush()
            pre_payload = formalizer.formalize(boundary["pre_payload"], scene.sequence, include_current=False, replacements=replacements, knowledge_by_temp=formal_knowledge_by_temp, memory_by_temp=formal_memory_by_temp, invalidated_ids=invalidated_ids)
            post_payload = formalizer.formalize(boundary["post_payload"], scene.sequence, include_current=True, replacements=replacements, knowledge_by_temp=formal_knowledge_by_temp, memory_by_temp=formal_memory_by_temp, invalidated_ids=invalidated_ids)
            post_payload["scenes"] = [_record(scene) if row.get("id") == scene.id else row for row in post_payload.get("scenes", [])]
            checkpoint_service.materialize_from_payloads(db, session.project_id, scene, pre_payload, post_payload, origin=SceneCheckpointOrigin.REPLAY_COMMIT, source_replay_session_id=session.id)
            run.status = ReplaySceneRunStatus.COMMITTED
        db.flush()
        if type(self).failure_injector:
            type(self).failure_injector("AFTER_CHECKPOINT_MATERIALIZATION")
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
        # Provenance becomes final only now; validation remains inside the
        # replay transaction so any damaged boundary rolls everything back.
        checkpoint_model = __import__("app.models", fromlist=["SceneStateCheckpoint"]).SceneStateCheckpoint
        for checkpoint in db.scalars(select(checkpoint_model).where(checkpoint_model.source_replay_session_id == session.id)).all():
            checkpoint_service.validate_integrity(db, checkpoint)
        post_commit_snapshot = WorldSnapshotBuilder().create(
            db, session.project_id, __import__("app.models", fromlist=["SnapshotType"]).SnapshotType.POST_REPLAY_COMMIT
        )
        session.post_commit_snapshot_id = post_commit_snapshot.id
        current_checkpoint = db.scalar(
            select(checkpoint_model).join(Scene, Scene.id == checkpoint_model.scene_id).where(
                checkpoint_model.project_id == session.project_id,
                checkpoint_model.active.is_(True),
                Scene.history_status == "ACTIVE",
                Scene.status == "OCCURRED",
            ).order_by(Scene.sequence.desc(), Scene.id.desc())
        )
        if current_checkpoint:
            from .models import WorldSnapshot
            ProjectWorldSnapshotHeadService().update(
                db,
                session.project_id,
                db.get(WorldSnapshot, current_checkpoint.post_snapshot_id),
                source_type="REPLAY_FINAL",
                source_id=session.id,
                sequence=current_checkpoint.sequence,
            )
        # Ledger records are derived from the now-final current history and
        # remain inside the replay transaction for atomic rollback semantics.
        CausalLedgerService().sync_after_replay_commit(db, session)
        from .scaling import ProjectHistoryProjectionService
        earliest = min((run.original_sequence for run in runs), default=1)
        ProjectHistoryProjectionService().sync_after_replay_commit(db, session.project_id, earliest)
        from .narrative_structure_projection import NarrativeStructureProjectionService
        structure = NarrativeStructureProjectionService()
        structure.sync_after_history_change(db, session.project_id, earliest, "REPLAY_HISTORY_CHANGED")
        structure.rebuild_suffix_after_history_change(
            db, session.project_id, earliest, project_locked=True
        )
        from .retrieval_index import CognitionRetrievalProjectionService
        try:
            with db.begin_nested():
                CognitionRetrievalProjectionService().rebuild(db, session.project_id)
        except Exception:
            with db.begin_nested():
                CognitionRetrievalProjectionService().mark_dirty(db, session.project_id, earliest)
        from .formal_state import FormalStateIdentityService
        FormalStateIdentityService().rebuild_and_anchor(db, session.project_id, source_type="REPLAY_FINAL_V2", source_id=session.id, project_locked=True)
        db.flush(); return session
