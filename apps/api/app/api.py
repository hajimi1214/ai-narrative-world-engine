from datetime import datetime
from enum import Enum
from typing import Any, Type
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import SessionLocal
from .director import DirectorConstraintChecker, DirectorContextBuilder, HeuristicDirector
from .character_mind import ActorPerceptionSanitizer, CharacterContextBuilder, CharacterDecisionConstraintChecker, HeuristicCharacterActor
from .ai.errors import MODEL_AUTH_FAILED, MODEL_RATE_LIMITED, MODEL_TIMEOUT, ModelProviderError
from .ai.factory import get_model_provider
from .llm_actor import LLMCharacterActor
from .settings import get_settings
from .models import ActionVisibility, AntiAIBible, CanonFact, Character, CharacterDecision, CharacterDecisionStatus, CharacterKnowledge, CharacterMemory, Chapter, DecisionType, DirectorDecisionLog, PerformanceMode, PerformanceStatus, Project, ProjectTemplate, ProposalStatus, RevealConstraint, Scene, SceneProposal, ScenePerformance, ScenePerformanceTurn, StoryArc, StoryThread, WorldEntity, WritingBible
from .performance import HeuristicCharacterPerformer, LLMCharacterPerformer, PerformanceActionConstraintChecker, PerformanceCharacterContextBuilder, PerformanceObservationRouter, TurnScheduler
from .services import DomainRuleError, activate_anti_ai_bible, activate_writing_bible, update_canon

router = APIRouter()

class Payload(BaseModel):
    model_config = ConfigDict(extra="allow")

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def serialize(value: Any) -> Any:
    if isinstance(value, Enum): return value.value
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, list): return [serialize(item) for item in value]
    if isinstance(value, dict): return {key: serialize(item) for key, item in value.items()}
    return value

def record_dict(record: Any) -> dict[str, Any]:
    return {column.name: serialize(getattr(record, column.name)) for column in record.__table__.columns}

def require_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project: raise HTTPException(status_code=404, detail="Project not found")
    return project

def create_record(db: Session, model: Type, values: dict[str, Any], project_id: str | None = None):
    if project_id: values["project_id"] = project_id
    record = model(**values)
    db.add(record); db.commit(); db.refresh(record)
    return record

def update_record(db: Session, record: Any, values: dict[str, Any]):
    for key, value in values.items():
        if key not in {"id", "project_id", "created_at", "updated_at"}: setattr(record, key, value)
    db.add(record); db.commit(); db.refresh(record)
    return record

@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    return [record_dict(item) for item in db.scalars(select(Project).order_by(Project.created_at.desc())).all()]

@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(payload: Payload, db: Session = Depends(get_db)):
    return record_dict(create_record(db, Project, payload.model_dump()))

@router.get("/projects/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)):
    return record_dict(require_project(db, project_id))

@router.patch("/projects/{project_id}")
def patch_project(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    return record_dict(update_record(db, require_project(db, project_id), payload.model_dump()))

@router.get("/projects/{project_id}/snapshot")
def project_snapshot(project_id: str, db: Session = Depends(get_db)):
    project = require_project(db, project_id)
    characters = db.scalars(select(Character).where(Character.project_id == project_id, Character.active.is_(True))).all()
    character_ids = [item.id for item in characters]
    knowledge = db.scalars(select(CharacterKnowledge).where(CharacterKnowledge.character_id.in_(character_ids))).all() if character_ids else []
    return {
        "project": record_dict(project),
        "active_writing_bible": next((record_dict(item) for item in db.scalars(select(WritingBible).where(WritingBible.project_id == project_id, WritingBible.active.is_(True))).all()), None),
        "active_anti_ai_bible": next((record_dict(item) for item in db.scalars(select(AntiAIBible).where(AntiAIBible.project_id == project_id, AntiAIBible.active.is_(True))).all()), None),
        "canon": [record_dict(item) for item in db.scalars(select(CanonFact).where(CanonFact.project_id == project_id)).all()],
        "active_characters": [record_dict(item) for item in characters],
        "character_states": [{"character_id": item.id, "current_state": serialize(item.current_state), "physical_state": serialize(item.physical_state), "emotional_state": serialize(item.emotional_state), "goals": serialize(item.goals)} for item in characters],
        "character_knowledge_summary": [{"character_id": item.character_id, "proposition": item.proposition, "status": item.status.value, "confidence": item.confidence} for item in knowledge],
        "world_entities": [record_dict(item) for item in db.scalars(select(WorldEntity).where(WorldEntity.project_id == project_id, WorldEntity.active.is_(True))).all()],
        "active_story_threads": [record_dict(item) for item in db.scalars(select(StoryThread).where(StoryThread.project_id == project_id, StoryThread.status.in_(["OPEN", "PAUSED"]))).all()],
        "current_story_arc": next((record_dict(item) for item in db.scalars(select(StoryArc).where(StoryArc.project_id == project_id, StoryArc.status == "ACTIVE").order_by(StoryArc.id.desc())).all()), None),
        "recent_scenes": [record_dict(item) for item in db.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.sequence.desc()).limit(20)).all()],
    }

def project_routes(prefix: str, model: Type, allow_update: bool = True):
    @router.get(f"/projects/{{project_id}}/{prefix}")
    def list_items(project_id: str, db: Session = Depends(get_db)):
        require_project(db, project_id)
        return [record_dict(item) for item in db.scalars(select(model).where(model.project_id == project_id)).all()]
    @router.post(f"/projects/{{project_id}}/{prefix}", status_code=status.HTTP_201_CREATED)
    def add_item(project_id: str, payload: Payload, db: Session = Depends(get_db)):
        require_project(db, project_id)
        return record_dict(create_record(db, model, payload.model_dump(), project_id))
    @router.get(f"/{prefix}/{{item_id}}")
    def get_item(item_id: str, db: Session = Depends(get_db)):
        item = db.get(model, item_id)
        if not item: raise HTTPException(status_code=404, detail="Resource not found")
        return record_dict(item)
    if allow_update:
        @router.patch(f"/{prefix}/{{item_id}}")
        def patch_item(item_id: str, payload: Payload, db: Session = Depends(get_db)):
            item = db.get(model, item_id)
            if not item: raise HTTPException(status_code=404, detail="Resource not found")
            return record_dict(update_record(db, item, payload.model_dump()))
    @router.delete(f"/{prefix}/{{item_id}}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_item(item_id: str, db: Session = Depends(get_db)):
        item = db.get(model, item_id)
        if not item: raise HTTPException(status_code=404, detail="Resource not found")
        db.delete(item); db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

for path, model, allow_update in [("characters", Character, True), ("world-entities", WorldEntity, True), ("canon", CanonFact, False), ("story-threads", StoryThread, True), ("scenes", Scene, True), ("chapters", Chapter, True), ("story-arcs", StoryArc, True)]:
    project_routes(path, model, allow_update)

@router.patch("/canon/{fact_id}")
def patch_canon(fact_id: str, payload: Payload, db: Session = Depends(get_db)):
    fact = db.get(CanonFact, fact_id)
    if not fact: raise HTTPException(status_code=404, detail="Canon fact not found")
    try: return record_dict(update_canon(db, fact, **payload.model_dump()))
    except DomainRuleError as error: raise HTTPException(status_code=409, detail=str(error)) from error

@router.get("/projects/{project_id}/writing-bibles")
def list_writing_bibles(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [record_dict(item) for item in db.scalars(select(WritingBible).where(WritingBible.project_id == project_id).order_by(WritingBible.version)).all()]

@router.post("/projects/{project_id}/writing-bibles", status_code=status.HTTP_201_CREATED)
def create_writing_bible(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    bible = create_record(db, WritingBible, payload.model_dump(), project_id)
    return record_dict(activate_writing_bible(db, bible) if bible.active else bible)

@router.post("/writing-bibles/{bible_id}/activate")
def activate_writing_bible_endpoint(bible_id: str, db: Session = Depends(get_db)):
    bible = db.get(WritingBible, bible_id)
    if not bible: raise HTTPException(status_code=404, detail="Writing Bible not found")
    return record_dict(activate_writing_bible(db, bible))

@router.get("/projects/{project_id}/anti-ai-bibles")
def list_anti_ai_bibles(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [record_dict(item) for item in db.scalars(select(AntiAIBible).where(AntiAIBible.project_id == project_id).order_by(AntiAIBible.version)).all()]

@router.post("/projects/{project_id}/anti-ai-bibles", status_code=status.HTTP_201_CREATED)
def create_anti_ai_bible(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    bible = create_record(db, AntiAIBible, payload.model_dump(), project_id)
    return record_dict(activate_anti_ai_bible(db, bible) if bible.active else bible)

@router.post("/anti-ai-bibles/{bible_id}/activate")
def activate_anti_ai_bible_endpoint(bible_id: str, db: Session = Depends(get_db)):
    bible = db.get(AntiAIBible, bible_id)
    if not bible: raise HTTPException(status_code=404, detail="Anti-AI Bible not found")
    return record_dict(activate_anti_ai_bible(db, bible))

@router.get("/project-templates")
def list_templates(db: Session = Depends(get_db)):
    return [record_dict(item) for item in db.scalars(select(ProjectTemplate)).all()]

@router.post("/project-templates", status_code=status.HTTP_201_CREATED)
def create_template(payload: Payload, db: Session = Depends(get_db)):
    return record_dict(create_record(db, ProjectTemplate, payload.model_dump()))

@router.get("/characters/{character_id}/knowledge")
def character_knowledge(character_id: str, db: Session = Depends(get_db)):
    if not db.get(Character, character_id): raise HTTPException(status_code=404, detail="Character not found")
    return [record_dict(item) for item in db.scalars(select(CharacterKnowledge).where(CharacterKnowledge.character_id == character_id)).all()]

@router.post("/characters/{character_id}/knowledge", status_code=status.HTTP_201_CREATED)
def create_character_knowledge(character_id: str, payload: Payload, db: Session = Depends(get_db)):
    if not db.get(Character, character_id): raise HTTPException(status_code=404, detail="Character not found")
    return record_dict(create_record(db, CharacterKnowledge, payload.model_dump() | {"character_id": character_id}))

@router.get("/characters/{character_id}/memories")
def character_memories(character_id: str, db: Session = Depends(get_db)):
    if not db.get(Character, character_id): raise HTTPException(status_code=404, detail="Character not found")
    return [record_dict(item) for item in db.scalars(select(CharacterMemory).where(CharacterMemory.character_id == character_id)).all()]

@router.post("/characters/{character_id}/memories", status_code=status.HTTP_201_CREATED)
def create_character_memory(character_id: str, payload: Payload, db: Session = Depends(get_db)):
    if not db.get(Character, character_id): raise HTTPException(status_code=404, detail="Character not found")
    return record_dict(create_record(db, CharacterMemory, payload.model_dump() | {"character_id": character_id}))

def context_summary(context: dict[str, Any]) -> dict[str, Any]:
    return {"version": context["version"], "fingerprint": context["fingerprint"], "project": context["project"], "current_story_arc": context["current_story_arc"], "active_story_threads": context["active_story_threads"], "paused_story_threads": context["paused_story_threads"], "active_characters": context["active_characters"], "recent_scene_count": len(context["recent_scenes"]), "world_entity_count": len(context["world_entities"]), "canon_count": len(context["canon"])}

@router.post("/projects/{project_id}/director/dry-run", status_code=status.HTTP_201_CREATED)
def director_dry_run(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    context = DirectorContextBuilder().build(db, project_id)
    proposal = SceneProposal(project_id=project_id, context_fingerprint=context["fingerprint"], **HeuristicDirector().propose(context))
    report = DirectorConstraintChecker().validate(db, context, proposal)
    proposal.status = ProposalStatus.VALID if report.valid else ProposalStatus.REJECTED
    db.add(proposal); db.commit(); db.refresh(proposal)
    log = DirectorDecisionLog(project_id=project_id, context_version=context["version"], proposal_id=proposal.id, decision_type=DecisionType.DRY_RUN, brief_reason=proposal.director_reasoning_summary, validation_result=report.as_dict())
    db.add(log); db.commit()
    return {"context_summary": context_summary(context), "proposal": record_dict(proposal), "validation_report": report.as_dict()}

@router.get("/projects/{project_id}/director/proposals")
def list_director_proposals(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [record_dict(item) for item in db.scalars(select(SceneProposal).where(SceneProposal.project_id == project_id).order_by(SceneProposal.created_at.desc())).all()]

@router.get("/projects/{project_id}/director/proposals/{proposal_id}")
def get_director_proposal(project_id: str, proposal_id: str, db: Session = Depends(get_db)):
    proposal = db.get(SceneProposal, proposal_id)
    if not proposal or proposal.project_id != project_id: raise HTTPException(status_code=404, detail="Scene Proposal not found")
    return record_dict(proposal)

@router.post("/projects/{project_id}/director/proposals/{proposal_id}/approve")
def approve_director_proposal(project_id: str, proposal_id: str, db: Session = Depends(get_db)):
    proposal = db.get(SceneProposal, proposal_id)
    if not proposal or proposal.project_id != project_id: raise HTTPException(status_code=404, detail="Scene Proposal not found")
    context = DirectorContextBuilder().build(db, project_id)
    if proposal.context_fingerprint != context["fingerprint"]:
        raise HTTPException(status_code=409, detail={"code": "STALE_PROPOSAL", "message": "World state changed after this proposal was generated. Run Director again."})
    report = DirectorConstraintChecker().validate(db, context, proposal)
    if not report.valid: raise HTTPException(status_code=409, detail={"message": "Blocking validation issues prevent approval.", "validation_report": report.as_dict()})
    proposal.status = ProposalStatus.APPROVED; db.add(proposal); db.commit(); db.refresh(proposal)
    db.add(DirectorDecisionLog(project_id=project_id, context_version=context["version"], proposal_id=proposal.id, decision_type=DecisionType.APPROVE, brief_reason="Proposal approved after constraint validation.", validation_result=report.as_dict())); db.commit()
    return {"proposal": record_dict(proposal), "validation_report": report.as_dict()}

@router.post("/projects/{project_id}/director/proposals/{proposal_id}/reject")
def reject_director_proposal(project_id: str, proposal_id: str, payload: Payload, db: Session = Depends(get_db)):
    proposal = db.get(SceneProposal, proposal_id)
    if not proposal or proposal.project_id != project_id: raise HTTPException(status_code=404, detail="Scene Proposal not found")
    proposal.status = ProposalStatus.REJECTED; db.add(proposal); db.commit(); db.refresh(proposal)
    reason = payload.model_dump().get("reason", "Proposal rejected by user.")
    db.add(DirectorDecisionLog(project_id=project_id, context_version=proposal.context_fingerprint, proposal_id=proposal.id, decision_type=DecisionType.REJECT, brief_reason=reason, validation_result={})); db.commit()
    return record_dict(proposal)

@router.get("/projects/{project_id}/reveal-constraints")
def list_reveal_constraints(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [record_dict(item) for item in db.scalars(select(RevealConstraint).where(RevealConstraint.project_id == project_id)).all()]

@router.post("/projects/{project_id}/reveal-constraints", status_code=status.HTTP_201_CREATED)
def create_reveal_constraint(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    values = payload.model_dump()
    fact = db.get(CanonFact, values.get("canon_fact_id"))
    if not fact or fact.project_id != project_id: raise HTTPException(status_code=409, detail="canon_fact_id must belong to this project")
    character_ids = values.get("allowed_character_ids", [])
    invalid = [character_id for character_id in character_ids if not db.get(Character, character_id) or db.get(Character, character_id).project_id != project_id]
    if invalid: raise HTTPException(status_code=409, detail={"message": "allowed_character_ids must belong to this project", "invalid_ids": invalid})
    return record_dict(create_record(db, RevealConstraint, values, project_id))

def character_context_summary(context: dict[str, Any]) -> dict[str, Any]:
    return {"fingerprint": context["fingerprint"], "character": context["character"], "scene": context["scene"], "knowledge": context["knowledge"], "memories": context["memories"], "relationships": context["relationships"], "abilities": context["abilities"], "inventory": context["inventory"]}

def require_character_simulation_inputs(db: Session, project_id: str, proposal_id: str, character_id: str) -> tuple[SceneProposal, Character]:
    require_project(db, project_id)
    proposal = db.get(SceneProposal, proposal_id)
    character = db.get(Character, character_id)
    if not proposal or proposal.project_id != project_id: raise HTTPException(status_code=404, detail="Scene Proposal not found")
    if not character or character.project_id != project_id: raise HTTPException(status_code=404, detail="Character not found")
    if character_id not in proposal.participants: raise HTTPException(status_code=409, detail={"code": "NOT_SCENE_PARTICIPANT", "message": "Character is not a participant in this Scene Proposal."})
    director_context = DirectorContextBuilder().build(db, project_id)
    if proposal.context_fingerprint != director_context["fingerprint"]: raise HTTPException(status_code=409, detail={"code": "STALE_SCENE_PROPOSAL", "message": "Scene Proposal is stale. Run Director again."})
    return proposal, character

@router.post("/projects/{project_id}/director/proposals/{proposal_id}/characters/{character_id}/dry-run", status_code=status.HTTP_201_CREATED)
def character_dry_run(project_id: str, proposal_id: str, character_id: str, db: Session = Depends(get_db)):
    proposal, character = require_character_simulation_inputs(db, project_id, proposal_id, character_id)
    context = CharacterContextBuilder().build(db, project_id, character_id, proposal)
    decision = CharacterDecision(project_id=project_id, scene_proposal_id=proposal.id, character_id=character_id, context_fingerprint=context["fingerprint"], **HeuristicCharacterActor().decide(context))
    report = CharacterDecisionConstraintChecker().validate(db, context, decision)
    decision.status = CharacterDecisionStatus.VALID if report.valid else CharacterDecisionStatus.REJECTED
    db.add(decision); db.commit(); db.refresh(decision)
    return {"character_context_summary": character_context_summary(context), "decision": record_dict(decision), "validation_report": report.as_dict()}

@router.post("/projects/{project_id}/director/proposals/{proposal_id}/characters/{character_id}/ai-dry-run", status_code=status.HTTP_201_CREATED)
def character_ai_dry_run(project_id: str, proposal_id: str, character_id: str, db: Session = Depends(get_db)):
    proposal, character = require_character_simulation_inputs(db, project_id, proposal_id, character_id)
    context = CharacterContextBuilder().build(db, project_id, character_id, proposal)
    actor_view = ActorPerceptionSanitizer().sanitize(context)
    settings = get_settings()
    try:
        payload, model_result = LLMCharacterActor(get_model_provider(settings), settings.ai_character_model).decide(actor_view)
    except ModelProviderError as exc:
        status_code = {MODEL_AUTH_FAILED: 503, MODEL_RATE_LIMITED: 429, MODEL_TIMEOUT: 504}.get(exc.code, 502)
        raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": "Character model request could not be completed."}) from exc
    decision = CharacterDecision(project_id=project_id, scene_proposal_id=proposal.id, character_id=character.id, context_fingerprint=context["fingerprint"], **payload)
    report = CharacterDecisionConstraintChecker().validate(db, context, decision)
    decision.status = CharacterDecisionStatus.VALID if report.valid else CharacterDecisionStatus.REJECTED
    db.add(decision); db.commit(); db.refresh(decision)
    return {"character_context_summary": actor_view, "decision": record_dict(decision), "validation_report": report.as_dict(), "model_metadata": {"provider": model_result.provider, "model": model_result.model, "latency_ms": model_result.latency_ms, "request_id": model_result.request_id}}

@router.get("/projects/{project_id}/character-decisions")
def list_character_decisions(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [record_dict(item) for item in db.scalars(select(CharacterDecision).where(CharacterDecision.project_id == project_id).order_by(CharacterDecision.created_at.desc(), CharacterDecision.id.desc())).all()]

@router.get("/projects/{project_id}/character-decisions/{decision_id}")
def get_character_decision(project_id: str, decision_id: str, db: Session = Depends(get_db)):
    decision = db.get(CharacterDecision, decision_id)
    if not decision or decision.project_id != project_id: raise HTTPException(status_code=404, detail="Character Decision not found")
    return record_dict(decision)

def _performance_payload(performance: ScenePerformance, turns: list[ScenePerformanceTurn], db: Session):
    value = record_dict(performance)
    value["next_actor_id"] = TurnScheduler().next_actor(performance, turns)
    value["turns"] = []
    for turn in turns:
        item = record_dict(turn)
        decision = db.get(CharacterDecision, turn.character_decision_id)
        item["decision"] = {"decision_type": getattr(decision.decision_type, "value", decision.decision_type), "intent": decision.intent, "chosen_action": decision.chosen_action, "motivation": decision.motivation, "status": getattr(decision.status, "value", decision.status)} if decision else None
        value["turns"].append(item)
    return value

@router.post("/projects/{project_id}/director/proposals/{proposal_id}/performances", status_code=status.HTTP_201_CREATED)
def create_performance(project_id: str, proposal_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    proposal = db.get(SceneProposal, proposal_id)
    if not proposal or proposal.project_id != project_id: raise HTTPException(status_code=404, detail="Scene Proposal not found")
    if proposal.status != ProposalStatus.APPROVED: raise HTTPException(status_code=409, detail={"code": "PROPOSAL_NOT_APPROVED", "message": "Only APPROVED proposals can be rehearsed."})
    current = DirectorContextBuilder().build(db, project_id)
    if proposal.context_fingerprint != current["fingerprint"]: raise HTTPException(status_code=409, detail={"code": "STALE_PROPOSAL", "message": "Scene Proposal is stale."})
    values = payload.model_dump(); mode = values.get("mode", PerformanceMode.HEURISTIC.value)
    try: mode_enum = PerformanceMode(mode)
    except ValueError: raise HTTPException(status_code=400, detail={"code": "INVALID_PERFORMANCE_MODE"})
    max_turns = max(1, min(int(values.get("max_turns", 6)), 50))
    take = (db.scalar(select(ScenePerformance.take_number).where(ScenePerformance.scene_proposal_id == proposal_id).order_by(ScenePerformance.take_number.desc())) or 0) + 1
    performance = ScenePerformance(project_id=project_id, scene_proposal_id=proposal_id, take_number=take, proposal_context_fingerprint=current["fingerprint"], mode=mode_enum, participant_order=list(proposal.participants), active_participant_ids=list(proposal.participants), max_turns=max_turns, turn_count=0)
    db.add(performance); db.commit(); db.refresh(performance)
    return record_dict(performance)

@router.get("/projects/{project_id}/director/proposals/{proposal_id}/performances")
def list_performances(project_id: str, proposal_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [record_dict(item) for item in db.scalars(select(ScenePerformance).where(ScenePerformance.project_id == project_id, ScenePerformance.scene_proposal_id == proposal_id).order_by(ScenePerformance.take_number)).all()]

@router.get("/projects/{project_id}/performances/{performance_id}")
def get_performance(project_id: str, performance_id: str, db: Session = Depends(get_db)):
    performance = db.get(ScenePerformance, performance_id)
    if not performance or performance.project_id != project_id: raise HTTPException(status_code=404, detail="Scene Performance not found")
    turns = db.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == performance.id).order_by(ScenePerformanceTurn.sequence)).all()
    return _performance_payload(performance, turns, db)

@router.post("/projects/{project_id}/performances/{performance_id}/step", status_code=status.HTTP_201_CREATED)
def performance_step(project_id: str, performance_id: str, db: Session = Depends(get_db)):
    performance = db.get(ScenePerformance, performance_id)
    if not performance or performance.project_id != project_id: raise HTTPException(status_code=404, detail="Scene Performance not found")
    if performance.status in {PerformanceStatus.AWAITING_WORLD, PerformanceStatus.PAUSED, PerformanceStatus.COMPLETED, PerformanceStatus.INVALIDATED, PerformanceStatus.FAILED}: raise HTTPException(status_code=409, detail={"code": "PERFORMANCE_NOT_RUNNABLE", "status": performance.status.value, "stop_reason": performance.stop_reason})
    proposal = db.get(SceneProposal, performance.scene_proposal_id)
    current = DirectorContextBuilder().build(db, project_id)
    if proposal.context_fingerprint != current["fingerprint"] or performance.proposal_context_fingerprint != current["fingerprint"]:
        performance.status = PerformanceStatus.INVALIDATED; performance.stop_reason = "STALE_PERFORMANCE"; db.add(performance); db.commit()
        raise HTTPException(status_code=409, detail={"code": "STALE_PERFORMANCE"})
    turns = db.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == performance.id).order_by(ScenePerformanceTurn.sequence)).all()
    if performance.turn_count >= performance.max_turns:
        performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "TURN_LIMIT"; db.add(performance); db.commit(); raise HTTPException(status_code=409, detail={"code": "TURN_LIMIT"})
    previous_decision = db.get(CharacterDecision, turns[-1].character_decision_id) if turns else None
    actor_id = TurnScheduler().next_actor(performance, turns, previous_decision.target_character_id if previous_decision else None)
    if not actor_id: performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "INSUFFICIENT_ACTIVE_PARTICIPANTS"; db.add(performance); db.commit(); raise HTTPException(status_code=409, detail={"code": performance.stop_reason})
    context = PerformanceCharacterContextBuilder().build(db, project_id, actor_id, proposal, performance.id, turns)
    actor_view = __import__("app.character_mind", fromlist=["ActorPerceptionSanitizer"]).ActorPerceptionSanitizer().sanitize(context)
    try:
        if performance.mode == PerformanceMode.HEURISTIC: raw_payload, result = HeuristicCharacterPerformer().perform(context)
        else: raw_payload, result = LLMCharacterPerformer(get_model_provider(get_settings()), get_settings().ai_character_model).perform(actor_view)
    except ModelProviderError as exc:
        performance.status = PerformanceStatus.FAILED; performance.stop_reason = exc.code; db.add(performance); db.commit(); raise HTTPException(status_code=502, detail={"code": exc.code}) from exc
    decision_data = raw_payload["decision"]
    decision = CharacterDecision(project_id=project_id, scene_proposal_id=proposal.id, character_id=actor_id, context_fingerprint=context["fingerprint"], **decision_data)
    decision_report = CharacterDecisionConstraintChecker().validate(db, context, decision)
    action = __import__("app.performance", fromlist=["PerformanceActionPayload"]).PerformanceActionPayload.model_validate(raw_payload["action"])
    action_report = PerformanceActionConstraintChecker().validate(db, context, proposal, decision, action)
    valid = decision_report.valid and action_report.valid
    decision.status = CharacterDecisionStatus.VALID if valid else CharacterDecisionStatus.REJECTED
    db.add(decision); db.flush()
    recipients = PerformanceObservationRouter().recipients(action.visibility, performance.participant_order, actor_id, action.target_character_id)
    turn = ScenePerformanceTurn(project_id=project_id, performance_id=performance.id, sequence=performance.turn_count + 1, actor_character_id=actor_id, actor_context_fingerprint=context["fingerprint"], character_decision_id=decision.id, action_visibility=action.visibility, observable_action=action.observable_action if valid else None, spoken_content=action.spoken_content if valid else None, recipient_character_ids=recipients if valid else [], requires_world_resolution=action.requires_world_resolution if valid else False, world_resolution_request=action.world_resolution_request.model_dump(mode="json") if valid and action.world_resolution_request else None, validation_result={"decision": decision_report.as_dict(), "action": action_report.as_dict()})
    db.add(turn); performance.turn_count += 1; performance.status = PerformanceStatus.RUNNING
    if not valid: performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "CHARACTER_DECISION_REJECTED"
    elif action.requires_world_resolution: performance.status = PerformanceStatus.AWAITING_WORLD
    elif getattr(decision.decision_type, "value", decision.decision_type) == "WITHDRAW": performance.active_participant_ids = [item for item in performance.active_participant_ids if item != actor_id]
    if performance.turn_count >= performance.max_turns and performance.status == PerformanceStatus.RUNNING: performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "TURN_LIMIT"
    db.add(performance); db.commit(); db.refresh(turn); db.refresh(performance)
    return {"performance": _performance_payload(performance, turns + [turn], db), "turn": record_dict(turn), "decision": record_dict(decision), "validation_report": {"decision": decision_report.as_dict(), "action": action_report.as_dict()}, "model_metadata": {"provider": result.provider, "model": result.model, "latency_ms": result.latency_ms, "request_id": result.request_id} if result else None}
