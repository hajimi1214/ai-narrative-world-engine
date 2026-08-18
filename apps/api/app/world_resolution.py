"""Take-local objective world resolution. Never mutates formal world state."""
import hashlib
import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from .ai.provider import ModelProvider, ModelResult
from .llm_actor import _extract_single_json_object, _validation_diagnostics
from .models import CanonFact, CanonType, Character, ResolutionOutcome, ResolutionStatus, RevealConstraint, RevealStatus, ScenePerformance, ScenePerformanceTurn, WorldEntity, WorldResolution


class WorldFactPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_type: Literal["ENTITY", "CHARACTER", "LOCATION", "SCENE"]
    subject_id: str
    predicate: str
    value: Any


class WorldResolutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: ResolutionOutcome
    outcome_summary: str
    objective_facts: list[WorldFactPayload]
    actor_observation: str | None
    public_observation: str | None
    canon_fact_ids_used: list[str]
    world_entity_ids_used: list[str]
    resolution_basis_summary: str | None
    missing_information: list[str]


def world_resolution_contract() -> dict[str, Any]:
    return {"fields": {"outcome": {"type": "enum", "values": [item.value for item in ResolutionOutcome]}, "outcome_summary": "string", "objective_facts": "WorldFact[]", "actor_observation": "string|null", "public_observation": "string|null", "canon_fact_ids_used": "string[]", "world_entity_ids_used": "string[]", "resolution_basis_summary": "string|null", "missing_information": "string[]"}, "WorldFact": {"subject_type": ["ENTITY", "CHARACTER", "LOCATION", "SCENE"], "subject_id": "string", "predicate": "string", "value": "JSON value"}, "rules": ["All fields are required.", "Use [] for empty lists and null for absent optional text.", "Do not add fields.", "UNRESOLVED requires missing_information and no objective facts."]}


def world_context_fingerprint(context: dict[str, Any]) -> str:
    payload = {key: value for key, value in context.items() if key != "fingerprint"}
    stable = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return f"world-context-v1:{hashlib.sha256(stable.encode()).hexdigest()}"


def _entity_view(entity: WorldEntity | None) -> dict[str, Any] | None:
    return {"id": entity.id, "name": entity.name, "profile": entity.profile} if entity else None


class PerformanceWorldStateBuilder:
    def build(self, session: Session, performance_id: str) -> dict[str, Any]:
        rows = session.execute(select(WorldResolution, ScenePerformanceTurn.sequence).outerjoin(ScenePerformanceTurn, WorldResolution.performance_turn_id == ScenePerformanceTurn.id).where(WorldResolution.performance_id == performance_id, WorldResolution.status == ResolutionStatus.VALID).order_by(ScenePerformanceTurn.sequence, WorldResolution.created_at, WorldResolution.id)).all()
        state: dict[str, dict[str, Any]] = {}
        for resolution, _ in rows:
            for fact in resolution.objective_facts:
                state[f"{fact['subject_type']}:{fact['subject_id']}:{fact['predicate']}"] = fact
        return {"facts": sorted(state.values(), key=lambda item: (item["subject_type"], item["subject_id"], item["predicate"]))}


class WorldResolutionContextBuilder:
    def build(self, session: Session, performance: ScenePerformance, turn: ScenePerformanceTurn, proposal: Any, request: dict[str, Any]) -> dict[str, Any]:
        actor = session.get(Character, turn.actor_character_id)
        location = session.get(WorldEntity, proposal.location_id) if proposal.location_id else None
        target_entity = session.get(WorldEntity, request.get("target_entity_id")) if request.get("target_entity_id") else None
        target_character = session.get(Character, request.get("target_character_id")) if request.get("target_character_id") else None
        active = set(performance.active_participant_ids or [])
        if not target_character or target_character.project_id != performance.project_id or target_character.id not in active:
            target_character = None
        project = session.get(__import__("app.models", fromlist=["Project"]).Project, performance.project_id)
        scope = {"project_id": performance.project_id, "performance_id": performance.id, "performance_turn_id": turn.id, "actor_character_id": turn.actor_character_id, "target_character_id": target_character.id if target_character else None, "location_id": location.id if location else None}
        allowed_entities = {item.id for item in (location, target_entity) if item and item.project_id == performance.project_id}
        scope_ids = allowed_entities | {turn.actor_character_id} | ({target_character.id} if target_character else set())
        canon = session.scalars(select(CanonFact).where(CanonFact.project_id == performance.project_id).order_by(CanonFact.id)).all()
        def relevant(fact: CanonFact) -> bool:
            data = fact.data or {}
            refs = {data.get("entity_id"), data.get("location_id"), data.get("character_id"), data.get("subject_id")} | set(data.get("entity_ids", [])) | set(data.get("character_ids", []))
            refs.discard(None)
            if fact.fact_type == CanonType.SECRET_CANON:
                return bool(refs & scope_ids) or bool(data.get("global_world_rule"))
            return bool(refs & scope_ids) or bool(data.get("global_world_rule"))
        context = {"scope": scope, "project": {"current_world_time": project.current_world_time.isoformat() if project and project.current_world_time else None}, "request": request, "attempt": {"actor_character_id": turn.actor_character_id, "observable_action": turn.observable_action, "target_entity_id": request.get("target_entity_id"), "target_character_id": request.get("target_character_id")}, "location": _entity_view(location), "target_entity": _entity_view(target_entity), "target_character": {"id": target_character.id, "current_state": target_character.current_state, "physical_state": target_character.physical_state, "abilities": target_character.abilities, "inventory": target_character.inventory} if target_character else None, "actor": {"id": actor.id, "current_state": actor.current_state, "physical_state": actor.physical_state, "abilities": actor.abilities, "inventory": actor.inventory} if actor else None, "allowed_world_entity_ids": sorted(allowed_entities), "canon": [{"id": item.id, "proposition": item.proposition, "fact_type": item.fact_type.value, "locked": item.locked, "data": item.data} for item in canon if relevant(item)], "take_state": PerformanceWorldStateBuilder().build(session, performance.id)}
        canon_by_id = {item.id: item for item in canon}
        forbidden_ids, forbidden_propositions = set(), set()
        for value in proposal.forbidden_reveals or []:
            fact = canon_by_id.get(value)
            if fact:
                forbidden_ids.add(fact.id); forbidden_propositions.add(fact.proposition)
            else:
                forbidden_propositions.add(value)
        context["forbidden_canon_ids"] = sorted(forbidden_ids)
        context["forbidden_propositions"] = sorted(forbidden_propositions)
        resolver_view = WorldContextSanitizer().sanitize(context)
        context["fingerprint"] = world_context_fingerprint({"resolver_view": resolver_view, "constraints": {"forbidden_canon_ids": context["forbidden_canon_ids"], "forbidden_propositions": context["forbidden_propositions"], "locked_structured_canon": sorted([{"id": item["id"], "data": WorldContextSanitizer()._clean(item["data"])} for item in context["canon"] if item["locked"] and isinstance(item["data"], dict) and {"subject_id", "predicate", "value"}.issubset(item["data"])], key=lambda item: item["id"])}})
        return context


class WorldContextSanitizer:
    def sanitize(self, context: dict[str, Any]) -> dict[str, Any]:
        allowed = ("scope", "project", "request", "attempt", "location", "target_entity", "target_character", "actor", "allowed_world_entity_ids", "canon", "take_state")
        return {key: self._clean(context[key]) for key in allowed if key in context}

    def _clean(self, value: Any) -> Any:
        blocked = {"director_only", "writer_only", "author_only", "narrative_only", "director_reasoning_summary", "scene_goal", "planned_pressure", "expected_progress", "possible_outcomes", "story_goal", "chapter_goal"}
        if isinstance(value, dict):
            return {key: self._clean(item) for key, item in value.items() if key not in blocked}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        return value


class HeuristicWorldResolver:
    def resolve(self, context: dict[str, Any]) -> tuple[dict[str, Any], ModelResult | None]:
        request, entity = context["request"], context.get("target_entity") or context.get("location")
        if not entity: return _unresolved("No resolvable entity is present.", "target entity"), None
        profile = entity.get("profile") or {}
        if request.get("kind") == "INSPECT" and profile.get("inspectable") is not None:
            value = profile["inspectable"]
            return {"outcome":"SUCCESS", "outcome_summary":"The inspection attempt completed.", "objective_facts":[{"subject_type":"ENTITY", "subject_id":entity["id"], "predicate":"inspection.result", "value":value}], "actor_observation":str(value), "public_observation":"The character examines the entity.", "canon_fact_ids_used":[], "world_entity_ids_used":[entity["id"]], "resolution_basis_summary":"The entity exposes an explicitly structured inspectable profile.", "missing_information":[]}, None
        if request.get("kind") == "INTERACT" and profile.get("locked") is True:
            return {"outcome":"FAILURE", "outcome_summary":"The interaction did not change the entity.", "objective_facts":[{"subject_type":"ENTITY", "subject_id":entity["id"], "predicate":"locked", "value":True}], "actor_observation":"The mechanism resists the attempt.", "public_observation":"The attempted interaction produces no visible change.", "canon_fact_ids_used":[], "world_entity_ids_used":[entity["id"]], "resolution_basis_summary":"The entity profile explicitly marks it locked.", "missing_information":[]}, None
        return _unresolved("The supplied world state cannot determine this result.", "structured resolution rule", [entity["id"]]), None


def _unresolved(summary: str, missing: str, entities: list[str] | None = None) -> dict[str, Any]:
    return {"outcome":"UNRESOLVED", "outcome_summary":summary, "objective_facts":[], "actor_observation":None, "public_observation":None, "canon_fact_ids_used":[], "world_entity_ids_used":entities or [], "resolution_basis_summary":None, "missing_information":[missing]}


class LLMWorldResolver:
    system_prompt = "You are the objective world resolution engine. Do not invent facts or choose dramatic outcomes. Return UNRESOLVED when information is missing. Return only required JSON."
    def __init__(self, provider: ModelProvider, model: str): self.provider, self.model = provider, model
    def resolve(self, context: dict[str, Any]) -> tuple[dict[str, Any], ModelResult]:
        messages = [{"role":"system", "content":self.system_prompt}, {"role":"user", "content":json.dumps({"world_context":WorldContextSanitizer().sanitize(context), "output_contract":world_resolution_contract(), "instruction":"Return exactly one JSON object."}, ensure_ascii=True, sort_keys=True)}]
        first = self.provider.generate(messages, self.model)
        try: return WorldResolutionPayload.model_validate(_extract_single_json_object(first.content)).model_dump(mode="json"), first
        except ValidationError as error: diagnostics = _validation_diagnostics(error)
        second = self.provider.generate(messages + [{"role":"assistant", "content":first.content}, {"role":"user", "content":json.dumps({"validation_errors":diagnostics, "instruction":"Repair exactly one valid JSON object without inventing facts."})}], self.model)
        return WorldResolutionPayload.model_validate(_extract_single_json_object(second.content)).model_dump(mode="json"), second


class WorldObservationRouter:
    def recipients(self, performance: ScenePerformance, turn: ScenePerformanceTurn, resolution: Any) -> list[str]:
        active = set(performance.active_participant_ids or []); recipients = {turn.actor_character_id} if resolution.actor_observation else set()
        if resolution.public_observation: recipients.update(active)
        return sorted(recipients & active)


class WorldResolutionConstraintChecker:
    def validate(self, session: Session, context: dict[str, Any], payload: WorldResolutionPayload, project_id: str, world_view=None) -> dict[str, Any]:
        issues = []
        def add(code, message, ids=None): issues.append({"code":code, "severity":"BLOCKING", "message":message, "related_entity_ids":ids or []})
        allowed_entities = set(context.get("allowed_world_entity_ids", [])); scope = context["scope"]
        for entity_id in payload.world_entity_ids_used:
            entity = world_view.entity(entity_id) if world_view else session.get(WorldEntity, entity_id)
            if entity and not world_view and entity.project_id != project_id: add("CROSS_PROJECT_REFERENCE", "World entity belongs to another project.", [entity_id])
            else:
                entity_active = entity.get("active", True) if isinstance(entity, dict) else getattr(entity, "active", True)
                if not entity or entity_id not in allowed_entities or entity_active is False: add("INVALID_ENTITY_REFERENCE", "World entity is unavailable to this resolver.", [entity_id])
        allowed_canon = {item["id"] for item in context.get("canon", [])}
        for canon_id in payload.canon_fact_ids_used:
            fact = session.get(CanonFact, canon_id)
            if fact and fact.project_id != project_id: add("CROSS_PROJECT_REFERENCE", "Canon belongs to another project.", [canon_id])
            elif not fact or canon_id not in allowed_canon: add("INVALID_CANON_REFERENCE", "Canon is unavailable to this resolver.", [canon_id])
        for fact in payload.objective_facts:
            expected = {"ENTITY": allowed_entities, "LOCATION": {scope["location_id"]} - {None}, "CHARACTER": {scope["actor_character_id"], scope["target_character_id"]} - {None}, "SCENE": {scope["performance_id"]}}[fact.subject_type]
            if fact.subject_id not in expected: add("INVALID_FACT_SUBJECT", "Objective fact subject is outside the explicit resolution scope.", [fact.subject_id])
            if fact.predicate.startswith(("project.", "story_thread.", "chapter.", "canon.")): add("UNSUPPORTED_FACT_SCOPE", "Resolver cannot mutate formal project/story/canon scope.", [fact.predicate])
        for canon in context.get("canon", []):
            data = canon.get("data") or {}
            if not canon.get("locked") or not {"subject_id", "predicate", "value"}.issubset(data):
                continue
            for fact in payload.objective_facts:
                if fact.subject_id == data["subject_id"] and fact.predicate == data["predicate"] and (not data.get("subject_type") or fact.subject_type == data["subject_type"]) and fact.value != data["value"]:
                    add("CANON_CONTRADICTION", "Objective fact conflicts with locked structured Canon.", [canon["id"]])
        locked = session.scalars(select(CanonFact).join(RevealConstraint, RevealConstraint.canon_fact_id == CanonFact.id).where(RevealConstraint.project_id == project_id, RevealConstraint.status == RevealStatus.LOCKED)).all()
        forbidden_ids = set(context.get("forbidden_canon_ids", []))
        forbidden = set(context.get("forbidden_propositions", []))
        for secret in locked:
            if any(secret.proposition and secret.proposition in (text or "") for text in (payload.actor_observation, payload.public_observation)):
                add("OBSERVATION_LEAK", "Observation exposes a locked secret proposition.", [secret.id])
        for text in (payload.actor_observation, payload.public_observation):
            if any(item and item in (text or "") for item in forbidden):
                add("OBSERVATION_LEAK", "Observation exposes a forbidden reveal.")
        if payload.outcome == ResolutionOutcome.UNRESOLVED and (not payload.missing_information or payload.objective_facts): add("INVALID_RESOLUTION_STATE", "UNRESOLVED requires missing information and no objective facts.")
        if payload.outcome != ResolutionOutcome.UNRESOLVED and payload.missing_information: add("INVALID_RESOLUTION_STATE", "Resolved outcomes cannot retain missing information.")
        return {"valid":not issues, "issues":issues}
