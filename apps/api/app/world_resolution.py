"""Take-local objective world resolution. Never mutates formal world state."""
import hashlib, json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from .ai.errors import ModelProviderError, MODEL_OUTPUT_INVALID
from .ai.provider import ModelProvider, ModelResult
from .character_mind import _value_id, character_context_fingerprint
from .llm_actor import CHARACTER_SYSTEM_PROMPT, _extract_single_json_object, _validation_diagnostics
from .models import CanonFact, CanonType, Character, CharacterDecision, CharacterKnowledge, PerformanceStatus, ResolutionOutcome, ResolutionStatus, ResolverMode, RevealConstraint, RevealStatus, ScenePerformance, ScenePerformanceTurn, StoryThread, WorldEntity


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
    return {"required_fields": list(WorldResolutionPayload.model_json_schema()["required"]), "outcome_values": [item.value for item in ResolutionOutcome], "world_fact": {"subject_type": ["ENTITY", "CHARACTER", "LOCATION", "SCENE"], "subject_id": "string", "predicate": "string", "value": "JSON value"}, "rules": ["objective_facts are Take-local and not Canon", "use UNRESOLVED with missing_information when facts are insufficient", "do not expose objective_facts or secret canon in observations"]}


def world_context_fingerprint(context: dict[str, Any]) -> str:
    stable = json.dumps(context, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return f"world-context-v1:{hashlib.sha256(stable.encode()).hexdigest()}"


class PerformanceWorldStateBuilder:
    def build(self, session: Session, performance_id: str) -> dict[str, Any]:
        resolutions = session.scalars(select(__import__("app.models", fromlist=["WorldResolution"]).WorldResolution).where(__import__("app.models", fromlist=["WorldResolution"]).WorldResolution.performance_id == performance_id, __import__("app.models", fromlist=["WorldResolution"]).WorldResolution.status == ResolutionStatus.VALID).order_by(__import__("app.models", fromlist=["WorldResolution"]).WorldResolution.created_at, __import__("app.models", fromlist=["WorldResolution"]).WorldResolution.id)).all()
        state: dict[str, dict[str, Any]] = {}
        for resolution in resolutions:
            for fact in resolution.objective_facts:
                key = f"{fact['subject_type']}:{fact['subject_id']}:{fact['predicate']}"
                state[key] = fact
        return {"facts": list(sorted(state.values(), key=lambda item: (item["subject_type"], item["subject_id"], item["predicate"])))}


class WorldResolutionContextBuilder:
    def build(self, session: Session, performance: ScenePerformance, turn: ScenePerformanceTurn, proposal: Any, request: dict[str, Any]) -> dict[str, Any]:
        actor = session.get(Character, turn.actor_character_id)
        location = session.get(WorldEntity, proposal.location_id) if proposal.location_id else None
        target_entity = session.get(WorldEntity, request.get("target_entity_id")) if request.get("target_entity_id") else None
        state = PerformanceWorldStateBuilder().build(session, performance.id)
        entities = [item for item in (location, target_entity) if item]
        canon = session.scalars(select(CanonFact).where(CanonFact.project_id == performance.project_id).order_by(CanonFact.id)).all()
        relevant_ids = {item.id for item in entities}
        def canon_relevant(fact):
            data = fact.data or {}
            refs = {data.get("entity_id"), data.get("location_id")} | set(data.get("entity_ids", []))
            refs.discard(None)
            return fact.fact_type in {CanonType.CORE_CANON, CanonType.WORLD_FACT} or bool(refs & relevant_ids) or bool(data.get("global_director_required"))
        related = [fact for fact in canon if canon_relevant(fact)]
        context = {"request": request, "attempt": {"actor_character_id": turn.actor_character_id, "observable_action": turn.observable_action, "target_entity_id": request.get("target_entity_id"), "target_character_id": request.get("target_character_id")}, "location": {"id": location.id, "name": location.name, "profile": location.profile} if location else None, "target_entity": {"id": target_entity.id, "name": target_entity.name, "profile": target_entity.profile} if target_entity else None, "actor": {"id": actor.id, "physical_state": actor.physical_state, "abilities": actor.abilities, "inventory": actor.inventory} if actor else None, "canon": [{"id": fact.id, "proposition": fact.proposition, "fact_type": fact.fact_type.value, "locked": fact.locked, "data": fact.data} for fact in related], "take_state": state, "current_world_time": session.get(__import__("app.models", fromlist=["Project"]).Project, performance.project_id).current_world_time.isoformat() if session.get(__import__("app.models", fromlist=["Project"]).Project, performance.project_id).current_world_time else None}
        context["fingerprint"] = world_context_fingerprint(context)
        return context


class HeuristicWorldResolver:
    def resolve(self, context: dict[str, Any]) -> tuple[dict[str, Any], ModelResult | None]:
        request = context["request"]; entity = context.get("target_entity") or context.get("location")
        if not entity: return {"outcome": "UNRESOLVED", "outcome_summary": "No resolvable entity is present.", "objective_facts": [], "actor_observation": None, "public_observation": None, "canon_fact_ids_used": [], "world_entity_ids_used": [], "resolution_basis_summary": None, "missing_information": ["target entity"]}, None
        profile = entity.get("profile") or {}
        if request.get("kind") == "INSPECT":
            inspectable = profile.get("inspectable") if isinstance(profile, dict) else None
            if inspectable is None: return {"outcome": "UNRESOLVED", "outcome_summary": "The entity has no structured inspectable information.", "objective_facts": [], "actor_observation": None, "public_observation": None, "canon_fact_ids_used": [], "world_entity_ids_used": [entity["id"]], "resolution_basis_summary": None, "missing_information": ["inspectable profile"]}, None
            return {"outcome": "SUCCESS", "outcome_summary": "The inspection attempt completed.", "objective_facts": [{"subject_type": "ENTITY", "subject_id": entity["id"], "predicate": "inspection.result", "value": inspectable}], "actor_observation": str(inspectable), "public_observation": "The character examines the entity.", "canon_fact_ids_used": [], "world_entity_ids_used": [entity["id"]], "resolution_basis_summary": "The entity exposes an explicitly structured inspectable profile.", "missing_information": []}, None
        if request.get("kind") == "INTERACT":
            locked = profile.get("locked") if isinstance(profile, dict) else None
            if locked is True: return {"outcome": "FAILURE", "outcome_summary": "The interaction did not change the entity.", "objective_facts": [{"subject_type": "ENTITY", "subject_id": entity["id"], "predicate": "locked", "value": True}], "actor_observation": "The mechanism resists the attempt.", "public_observation": "The attempted interaction produces no visible change.", "canon_fact_ids_used": [], "world_entity_ids_used": [entity["id"]], "resolution_basis_summary": "The entity profile explicitly marks it locked.", "missing_information": []}, None
        return {"outcome": "UNRESOLVED", "outcome_summary": "The supplied world state cannot determine this result.", "objective_facts": [], "actor_observation": None, "public_observation": None, "canon_fact_ids_used": [], "world_entity_ids_used": [entity["id"]], "resolution_basis_summary": None, "missing_information": ["structured resolution rule"]}, None


class LLMWorldResolver:
    system_prompt = """You are the objective world resolution engine. You are not a novelist, director, or character. Resolve only what objectively follows from supplied world state, entities, rules, and attempted action. Do not choose dramatic outcomes, invent facts, or protect a protagonist. If information is insufficient, return UNRESOLVED and list missing_information. Separate objective facts from character observations. Return only the required JSON."""
    def __init__(self, provider: ModelProvider, model: str): self.provider, self.model = provider, model
    def resolve(self, context: dict[str, Any]) -> tuple[dict[str, Any], ModelResult]:
        messages = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": json.dumps({"world_context": context, "output_contract": world_resolution_contract(), "instruction": "Return exactly one JSON object conforming to the output contract."}, ensure_ascii=True, sort_keys=True)}]
        first = self.provider.generate(messages, self.model)
        try: return WorldResolutionPayload.model_validate(_extract_single_json_object(first.content)).model_dump(mode="json"), first
        except (ValidationError, ModelProviderError) as error:
            diagnostics = _validation_diagnostics(error) if isinstance(error, ValidationError) else [{"path": "$", "type": "invalid_output", "message": "Output does not match contract."}]
        second = self.provider.generate(messages + [{"role": "assistant", "content": first.content}, {"role": "user", "content": json.dumps({"validation_errors": diagnostics, "instruction": "Repair exactly one valid JSON object without inventing facts."})}], self.model)
        return WorldResolutionPayload.model_validate(_extract_single_json_object(second.content)).model_dump(mode="json"), second


class WorldObservationRouter:
    def recipients(self, performance: ScenePerformance, turn: ScenePerformanceTurn, resolution: Any) -> list[str]:
        active = set(performance.active_participant_ids or [])
        recipients = {turn.actor_character_id} if resolution.actor_observation else set()
        if resolution.public_observation: recipients.update(active)
        return sorted(recipients & active)


class WorldResolutionConstraintChecker:
    def validate(self, session: Session, context: dict[str, Any], payload: WorldResolutionPayload, project_id: str) -> dict[str, Any]:
        issues = []
        add = lambda code, message, ids=None: issues.append({"code": code, "severity": "BLOCKING", "message": message, "related_entity_ids": ids or []})
        allowed_entities = {item["id"] for item in (context.get("target_entity"), context.get("location")) if item}
        for entity_id in payload.world_entity_ids_used:
            entity = session.get(WorldEntity, entity_id)
            if not entity or entity.project_id != project_id: add("INVALID_ENTITY_REFERENCE", "World entity is missing or belongs to another project.", [entity_id])
            if entity_id not in allowed_entities: add("INVALID_ENTITY_REFERENCE", "World entity is not in the resolver context.", [entity_id])
        allowed_canon = {item["id"] for item in context.get("canon", [])}
        for canon_id in payload.canon_fact_ids_used:
            fact = session.get(CanonFact, canon_id)
            if not fact or fact.project_id != project_id or canon_id not in allowed_canon: add("INVALID_CANON_REFERENCE", "Canon reference is unavailable to this resolver.", [canon_id])
        valid_subjects = allowed_entities | {context["attempt"]["actor_character_id"]}
        for fact in payload.objective_facts:
            if fact.subject_type in {"ENTITY", "LOCATION", "CHARACTER"} and fact.subject_id not in valid_subjects: add("INVALID_FACT_SUBJECT", "Objective fact subject is not in the current resolution scope.", [fact.subject_id])
            if fact.predicate.startswith(("project.", "story_thread.", "chapter.", "canon.")): add("UNSUPPORTED_FACT_SCOPE", "Resolver cannot mutate formal project/story/canon scope.", [fact.predicate])
        for canon in context.get("canon", []):
            data = canon.get("data") or {}
            if not (canon.get("locked") and {"subject_id", "predicate", "value"}.issubset(data)):
                continue
            for fact in payload.objective_facts:
                if fact.subject_id == data["subject_id"] and fact.predicate == data["predicate"] and fact.value != data["value"]:
                    add("CANON_CONTRADICTION", "Objective fact conflicts with locked structured Canon.", [canon["id"]])
        locked_secrets = session.scalars(select(CanonFact).join(RevealConstraint, RevealConstraint.canon_fact_id == CanonFact.id).where(RevealConstraint.project_id == project_id, RevealConstraint.status == RevealStatus.LOCKED)).all()
        observation = "\n".join(item for item in (payload.actor_observation, payload.public_observation) if item)
        for secret in locked_secrets:
            if secret.proposition and secret.proposition in observation:
                add("OBSERVATION_LEAK", "Observation exposes a locked secret proposition.", [secret.id])
        if payload.outcome == ResolutionOutcome.UNRESOLVED and not payload.missing_information: add("MISSING_INFORMATION_REQUIRED", "UNRESOLVED requires missing_information.")
        return {"valid": not issues, "issues": issues}
