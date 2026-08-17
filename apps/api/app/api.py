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
from .ai.errors import MODEL_AUTH_FAILED, MODEL_RATE_LIMITED, MODEL_TIMEOUT, MODEL_OUTPUT_INVALID, ModelProviderError
from .ai.factory import get_model_provider
from .llm_actor import LLMCharacterActor
from .settings import get_settings
from .models import ActionVisibility, AntiAIBible, CanonFact, Character, CharacterDecision, CharacterDecisionStatus, CharacterKnowledge, CharacterMemory, Chapter, DecisionType, DirectorDecisionLog, PerformanceMode, PerformanceStatus, Project, ProjectTemplate, ProposalStatus, RevealConstraint, Scene, SceneProposal, ScenePerformance, ScenePerformanceTurn, StoryArc, StoryThread, WorldEntity, WritingBible, WorldResolution, ResolutionStatus, ResolutionOutcome, ResolverMode, WorldRevision, RevisionStatus, WorldSnapshot, SnapshotType, RevisionApplication, ProjectModelConfig, ExecutionTrace, ExecutionStage, ExecutionStatus
from .performance import HeuristicCharacterPerformer, LLMCharacterPerformer, PerformanceActionConstraintChecker, PerformanceCharacterContextBuilder, PerformanceObservationRouter, TurnScheduler, is_quiescent_cycle
from .world_resolution import HeuristicWorldResolver, LLMWorldResolver, WorldResolutionContextBuilder, WorldResolutionConstraintChecker, WorldObservationRouter, WorldResolutionPayload
from .revision import RevisionChangeNormalizer, RevisionCreatePayload, RevisionImpactAnalyzer, RevisionStateFingerprintBuilder
from .versioning import WorldSnapshotBuilder, RevisionApplyService
from .model_router import ModelRouter, ProjectModelConfigPayload
from .execution_trace import ExecutionTraceRecorder, RecoveryPolicy, stable_fingerprint
from .services import DomainRuleError, activate_anti_ai_bible, activate_writing_bible, update_canon

router = APIRouter()

class Payload(BaseModel):
    model_config = ConfigDict(extra="allow")

def routed_provider(settings, route):
    """Keeps existing test injection seam while production uses the project route."""
    try:
        return get_model_provider(settings, route.provider, route.base_url)
    except TypeError:
        return get_model_provider(settings)

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
    trace = None
    try:
        route = ModelRouter().resolve(db, project_id, settings, "CHARACTER")
        trace = ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="CHARACTER", source_id=character.id, provider=route.provider, model=route.model, input_fingerprint=context["fingerprint"])
        payload, model_result = LLMCharacterActor(routed_provider(settings, route), route.model).decide(actor_view)
    except ModelProviderError as exc:
        ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="CHARACTER", source_id=character.id, status=ExecutionStatus.BLOCKED if exc.code == MODEL_OUTPUT_INVALID else ExecutionStatus.FAILED, error_code=exc.code, upstream_status=exc.upstream_status)
        db.commit()
        status_code = {MODEL_AUTH_FAILED: 503, MODEL_RATE_LIMITED: 429, MODEL_TIMEOUT: 504}.get(exc.code, 502)
        raise HTTPException(status_code=status_code, detail={"code": exc.code, "upstream_status": exc.upstream_status, "message": "Character model request could not be completed."}) from exc
    decision = CharacterDecision(project_id=project_id, scene_proposal_id=proposal.id, character_id=character.id, context_fingerprint=context["fingerprint"], **payload)
    report = CharacterDecisionConstraintChecker().validate(db, context, decision)
    decision.status = CharacterDecisionStatus.VALID if report.valid else CharacterDecisionStatus.REJECTED
    if trace:
        trace.status = ExecutionStatus.SUCCEEDED if report.valid else ExecutionStatus.BLOCKED
        trace.latency_ms = model_result.latency_ms; trace.request_id = model_result.request_id
        trace.output_fingerprint = stable_fingerprint(payload)
        if not report.valid: trace.validation_report = {"issues": report.as_dict().get("issues", [])}
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

@router.post("/projects/{project_id}/revisions", status_code=status.HTTP_201_CREATED)
def create_revision(project_id: str, payload: RevisionCreatePayload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    # Creation validates only target existence/ownership; patch semantics are preview-only.
    try: RevisionChangeNormalizer().normalize(db, project_id, payload.changes)
    except ValueError as exc: raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    revision = WorldRevision(project_id=project_id, title=payload.title, description=payload.description, status=RevisionStatus.DRAFT, change_set=[item.model_dump() for item in payload.changes])
    db.add(revision); db.commit(); db.refresh(revision)
    return record_dict(revision)

@router.get("/projects/{project_id}/revisions")
def list_revisions(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [record_dict(item) for item in db.scalars(select(WorldRevision).where(WorldRevision.project_id == project_id).order_by(WorldRevision.created_at.desc(), WorldRevision.id)).all()]

@router.get("/projects/{project_id}/revisions/{revision_id}")
def get_revision(project_id: str, revision_id: str, db: Session = Depends(get_db)):
    revision = db.get(WorldRevision, revision_id)
    if not revision or revision.project_id != project_id: raise HTTPException(status_code=404, detail="World Revision not found")
    value = record_dict(revision); value["is_stale"] = bool(revision.base_state_fingerprint and revision.base_state_fingerprint != RevisionStateFingerprintBuilder().build(db, project_id))
    return value

@router.post("/projects/{project_id}/revisions/{revision_id}/preview")
def preview_revision(project_id: str, revision_id: str, db: Session = Depends(get_db)):
    revision = db.get(WorldRevision, revision_id)
    if not revision or revision.project_id != project_id: raise HTTPException(status_code=404, detail="World Revision not found")
    if revision.status == RevisionStatus.CANCELLED: raise HTTPException(status_code=409, detail={"code":"REVISION_CANCELLED"})
    try:
        changes = RevisionChangeNormalizer().normalize(db, project_id, [__import__("app.revision", fromlist=["RevisionChangePayload"]).RevisionChangePayload.model_validate(item) for item in revision.change_set])
    except ValueError as exc: raise HTTPException(status_code=409, detail={"code":str(exc)}) from exc
    revision.normalized_changes = changes; revision.impact_report = RevisionImpactAnalyzer().analyze(db, project_id, changes); revision.base_state_fingerprint = RevisionStateFingerprintBuilder().build(db, project_id); revision.status = RevisionStatus.PREVIEWED
    db.add(revision); db.commit(); db.refresh(revision)
    return record_dict(revision)

@router.post("/projects/{project_id}/revisions/{revision_id}/cancel")
def cancel_revision(project_id: str, revision_id: str, db: Session = Depends(get_db)):
    revision = db.get(WorldRevision, revision_id)
    if not revision or revision.project_id != project_id: raise HTTPException(status_code=404, detail="World Revision not found")
    revision.status = RevisionStatus.CANCELLED; db.add(revision); db.commit(); db.refresh(revision)
    return record_dict(revision)

@router.post("/projects/{project_id}/snapshots", status_code=status.HTTP_201_CREATED)
def create_snapshot(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db,project_id)
    try: kind=SnapshotType(payload.model_dump().get("snapshot_type","BASELINE"))
    except ValueError: raise HTTPException(status_code=400,detail={"code":"INVALID_SNAPSHOT_TYPE"})
    snap=WorldSnapshotBuilder().create(db,project_id,kind); db.commit(); db.refresh(snap); return record_dict(snap)
@router.get("/projects/{project_id}/snapshots")
def list_snapshots(project_id:str,db:Session=Depends(get_db)):
    require_project(db,project_id); return [record_dict(x) for x in db.scalars(select(WorldSnapshot).where(WorldSnapshot.project_id==project_id).order_by(WorldSnapshot.created_at.desc(),WorldSnapshot.id)).all()]
@router.get("/projects/{project_id}/snapshots/{snapshot_id}")
def get_snapshot(project_id:str,snapshot_id:str,db:Session=Depends(get_db)):
    item=db.get(WorldSnapshot,snapshot_id)
    if not item or item.project_id!=project_id: raise HTTPException(status_code=404,detail="World Snapshot not found")
    return record_dict(item)
@router.post("/projects/{project_id}/revisions/{revision_id}/apply")
def apply_revision(project_id:str,revision_id:str,payload:Payload,db:Session=Depends(get_db)):
    revision=db.get(WorldRevision,revision_id)
    if not revision or revision.project_id!=project_id: raise HTTPException(status_code=404,detail="World Revision not found")
    service = RevisionApplyService()
    override = bool(payload.model_dump().get("author_override")); reason = payload.model_dump().get("author_override_reason")
    try:
        prepared = service.preflight(db, project_id, revision, override, reason)
    except ValueError as exc:
        db.rollback()
        if str(exc) == "REVISION_STALE":
            revision = db.get(WorldRevision, revision_id)
            revision.status = RevisionStatus.STALE
            db.commit()
        raise HTTPException(status_code=409, detail={"code": str(exc), "requires_repreview": str(exc) == "TARGET_STATE_STALE"}) from exc
    try:
        with db.begin_nested(): app=service.apply(db,project_id,revision,override,reason,prepared)
        db.commit(); db.refresh(app); return {"application":record_dict(app),"revision":record_dict(revision),"impact_report":revision.impact_report,"pending_retcon":bool(revision.impact_report.get("summary",{}).get("high") or revision.impact_report.get("summary",{}).get("critical") or revision.impact_report.get("summary",{}).get("manual_review"))}
    except ValueError as exc:
        db.rollback(); raise HTTPException(status_code=409,detail={"code":str(exc)}) from exc
@router.post("/projects/{project_id}/revision-applications/{application_id}/rollback")
def rollback_revision(project_id:str,application_id:str,db:Session=Depends(get_db)):
    app=db.get(RevisionApplication,application_id)
    if not app or app.project_id!=project_id: raise HTTPException(status_code=404,detail="Revision Application not found")
    if str(app.status.value if hasattr(app.status,"value") else app.status)!="APPLIED": raise HTTPException(status_code=409,detail={"code":"APPLICATION_NOT_APPLIED"})
    try:
        with db.begin_nested(): RevisionApplyService().rollback(db,project_id,app)
        db.commit(); db.refresh(app); return record_dict(app)
    except ValueError as exc: db.rollback(); raise HTTPException(status_code=409,detail={"code":str(exc)}) from exc
@router.get("/projects/{project_id}/model-config")
def get_model_config(project_id:str,db:Session=Depends(get_db)):
    require_project(db,project_id); item=db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id==project_id)); return record_dict(item) if item else None
@router.put("/projects/{project_id}/model-config")
def put_model_config(project_id:str,payload:Payload,db:Session=Depends(get_db)):
    require_project(db,project_id)
    try: values=ProjectModelConfigPayload.model_validate(payload.model_dump()).model_dump()
    except Exception as exc: raise HTTPException(status_code=400,detail={"code":"INVALID_MODEL_CONFIG"}) from exc
    item=db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id==project_id))
    if item: return record_dict(update_record(db,item,values))
    return record_dict(create_record(db,ProjectModelConfig,values,project_id))
def _trace_payload(trace):
    value = record_dict(trace)
    value["available_actions"] = RecoveryPolicy.resolve(value.get("error_code"))[2]
    return value

@router.get("/projects/{project_id}/execution-traces")
def list_execution_traces(project_id:str,stage:str|None=None,status_filter:str|None=None,source_type:str|None=None,source_id:str|None=None,limit:int=50,db:Session=Depends(get_db)):
    query=select(ExecutionTrace).where(ExecutionTrace.project_id==project_id)
    if stage: query=query.where(ExecutionTrace.stage==stage)
    if status_filter: query=query.where(ExecutionTrace.status==status_filter)
    if source_type: query=query.where(ExecutionTrace.source_type==source_type)
    if source_id: query=query.where(ExecutionTrace.source_id==source_id)
    return [_trace_payload(x) for x in db.scalars(query.order_by(ExecutionTrace.created_at.desc(),ExecutionTrace.id).limit(min(max(limit, 1), 200))).all()]
@router.get("/projects/{project_id}/execution-traces/{trace_id}")
def get_execution_trace(project_id:str,trace_id:str,db:Session=Depends(get_db)):
    item=db.get(ExecutionTrace,trace_id)
    if not item or item.project_id!=project_id: raise HTTPException(status_code=404,detail="Execution Trace not found")
    return _trace_payload(item)

def _performance_payload(performance: ScenePerformance, turns: list[ScenePerformanceTurn], db: Session):
    value = record_dict(performance)
    value["next_actor_id"] = TurnScheduler().next_actor(performance, turns)
    value["turns"] = []
    for turn in turns:
        item = record_dict(turn)
        decision = db.get(CharacterDecision, turn.character_decision_id)
        item["decision"] = {"decision_type": getattr(decision.decision_type, "value", decision.decision_type), "intent": decision.intent, "chosen_action": decision.chosen_action, "motivation": decision.motivation, "status": getattr(decision.status, "value", decision.status)} if decision else None
        value["turns"].append(item)
    value["world_resolutions"] = [record_dict(item) for item in db.scalars(select(WorldResolution).where(WorldResolution.performance_id == performance.id).order_by(WorldResolution.created_at, WorldResolution.id)).all()]
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
    resume_actor = None
    if turns and turns[-1].requires_world_resolution:
        resolution = db.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == turns[-1].id, WorldResolution.status == ResolutionStatus.VALID))
        if resolution:
            resume_actor = turns[-1].actor_character_id
    actor_id = resume_actor or TurnScheduler().next_actor(performance, turns, previous_decision.target_character_id if previous_decision else None)
    if not actor_id: performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "INSUFFICIENT_ACTIVE_PARTICIPANTS"; db.add(performance); db.commit(); raise HTTPException(status_code=409, detail={"code": performance.stop_reason})
    context = PerformanceCharacterContextBuilder().build(db, project_id, actor_id, proposal, performance.id, turns)
    actor_view = __import__("app.character_mind", fromlist=["ActorPerceptionSanitizer"]).ActorPerceptionSanitizer().sanitize(context)
    trace = None
    try:
        if performance.mode == PerformanceMode.HEURISTIC: raw_payload, result = HeuristicCharacterPerformer().perform(context)
        else:
            settings = get_settings(); route = ModelRouter().resolve(db, project_id, settings, "CHARACTER")
            trace = ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="SCENE_PERFORMANCE", source_id=performance.id, provider=route.provider, model=route.model, input_fingerprint=context["fingerprint"])
            raw_payload, result = LLMCharacterPerformer(routed_provider(settings, route), route.model).perform(actor_view)
    except ModelProviderError as exc:
        ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="SCENE_PERFORMANCE", source_id=performance.id, status=ExecutionStatus.BLOCKED if exc.code == MODEL_OUTPUT_INVALID else ExecutionStatus.FAILED, error_code=exc.code, upstream_status=exc.upstream_status)
        db.commit()
        error_status = {"MODEL_AUTH_FAILED": 503, "MODEL_RATE_LIMITED": 429, "MODEL_TIMEOUT": 504}.get(exc.code, 502)
        raise HTTPException(status_code=error_status, detail={"code": exc.code, "upstream_status": exc.upstream_status}) from exc
    decision_data = raw_payload["decision"]
    decision = CharacterDecision(project_id=project_id, scene_proposal_id=proposal.id, character_id=actor_id, context_fingerprint=context["fingerprint"], **decision_data)
    decision_report = CharacterDecisionConstraintChecker().validate(db, context, decision)
    action = __import__("app.performance", fromlist=["PerformanceActionPayload"]).PerformanceActionPayload.model_validate(raw_payload["action"])
    action_report = PerformanceActionConstraintChecker().validate(db, context, proposal, decision, action, performance.active_participant_ids)
    valid = decision_report.valid and action_report.valid
    if trace:
        trace.status = ExecutionStatus.SUCCEEDED if valid else ExecutionStatus.BLOCKED
        trace.latency_ms = result.latency_ms; trace.request_id = result.request_id
        trace.output_fingerprint = stable_fingerprint(raw_payload)
        if not valid: trace.validation_report = {"decision": decision_report.as_dict(), "action": action_report.as_dict()}
    decision.status = CharacterDecisionStatus.VALID if valid else CharacterDecisionStatus.REJECTED
    db.add(decision); db.flush()
    recipients = PerformanceObservationRouter().recipients(action.visibility, [item for item in performance.participant_order if item in performance.active_participant_ids], actor_id, action.target_character_id)
    turn = ScenePerformanceTurn(project_id=project_id, performance_id=performance.id, sequence=performance.turn_count + 1, actor_character_id=actor_id, actor_context_fingerprint=context["fingerprint"], character_decision_id=decision.id, action_visibility=action.visibility, observable_action=action.observable_action if valid else None, spoken_content=action.spoken_content if valid else None, recipient_character_ids=recipients if valid else [], requires_world_resolution=action.requires_world_resolution if valid else False, world_resolution_request=action.world_resolution_request.model_dump(mode="json") if valid and action.world_resolution_request else None, validation_result={"decision": decision_report.as_dict(), "action": action_report.as_dict()})
    db.add(turn); performance.turn_count += 1; performance.status = PerformanceStatus.RUNNING
    if not valid: performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "CHARACTER_DECISION_REJECTED"
    elif action.requires_world_resolution: performance.status = PerformanceStatus.AWAITING_WORLD
    elif getattr(decision.decision_type, "value", decision.decision_type) == "WITHDRAW":
        performance.active_participant_ids = [item for item in performance.active_participant_ids if item != actor_id]
        if len(performance.active_participant_ids) < 2: performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "INSUFFICIENT_ACTIVE_PARTICIPANTS"
    if performance.status == PerformanceStatus.RUNNING and is_quiescent_cycle(performance, turns + [turn], db): performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "QUIESCENT"
    if performance.turn_count >= performance.max_turns and performance.status == PerformanceStatus.RUNNING: performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "TURN_LIMIT"
    db.add(performance); db.commit(); db.refresh(turn); db.refresh(performance)
    return {"performance": _performance_payload(performance, turns + [turn], db), "turn": record_dict(turn), "decision": record_dict(decision), "validation_report": {"decision": decision_report.as_dict(), "action": action_report.as_dict()}, "model_metadata": {"provider": result.provider, "model": result.model, "latency_ms": result.latency_ms, "request_id": result.request_id} if result else None}


@router.post("/projects/{project_id}/performances/{performance_id}/world/resolve", status_code=status.HTTP_201_CREATED)
def resolve_world(project_id: str, performance_id: str, payload: Payload, db: Session = Depends(get_db)):
    performance = db.get(ScenePerformance, performance_id)
    if not performance or performance.project_id != project_id:
        raise HTTPException(status_code=404, detail="Scene Performance not found")
    if performance.status != PerformanceStatus.AWAITING_WORLD:
        raise HTTPException(status_code=409, detail={"code": "PERFORMANCE_NOT_AWAITING_WORLD"})
    proposal = db.get(SceneProposal, performance.scene_proposal_id)
    director_context = DirectorContextBuilder().build(db, project_id)
    if not proposal or proposal.context_fingerprint != performance.proposal_context_fingerprint or proposal.context_fingerprint != director_context["fingerprint"]:
        performance.status = PerformanceStatus.INVALIDATED; performance.stop_reason = "STALE_PERFORMANCE"; db.add(performance); db.commit()
        raise HTTPException(status_code=409, detail={"code": "STALE_PERFORMANCE"})
    turns = db.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == performance.id).order_by(ScenePerformanceTurn.sequence.desc())).all()
    turn = next((item for item in turns if item.requires_world_resolution and (not db.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == item.id)) or db.scalar(select(WorldResolution.status).where(WorldResolution.performance_turn_id == item.id)).value in {"UNRESOLVED", "REJECTED"})), None)
    if not turn or not turn.world_resolution_request:
        raise HTTPException(status_code=409, detail={"code": "NO_PENDING_WORLD_REQUEST"})
    request = turn.world_resolution_request
    if request.get("target_character_id"):
        target = db.get(Character, request["target_character_id"])
        if not target or target.project_id != project_id:
            raise HTTPException(status_code=409, detail={"code": "CROSS_PROJECT_REFERENCE"})
        if target.id not in (performance.active_participant_ids or []):
            raise HTTPException(status_code=409, detail={"code": "INVALID_TARGET"})
    if request.get("target_entity_id"):
        target_entity = db.get(WorldEntity, request["target_entity_id"])
        if not target_entity or target_entity.project_id != project_id:
            raise HTTPException(status_code=409, detail={"code": "CROSS_PROJECT_REFERENCE" if target_entity else "INVALID_ENTITY_REFERENCE"})
    context = WorldResolutionContextBuilder().build(db, performance, turn, proposal, turn.world_resolution_request)
    context_fingerprint = context["fingerprint"]
    trace = None
    requested_mode = payload.model_dump().get("mode") or performance.mode.value
    try:
        mode = ResolverMode(requested_mode)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "INVALID_RESOLVER_MODE"})
    try:
        if mode == ResolverMode.HEURISTIC:
            raw, model_result = HeuristicWorldResolver().resolve(context)
        else:
            settings = get_settings()
            route = ModelRouter().resolve(db, project_id, settings, "WORLD")
            trace = ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.WORLD_RESOLVER, source_type="SCENE_PERFORMANCE_TURN", source_id=turn.id, provider=route.provider, model=route.model, input_fingerprint=context_fingerprint)
            raw, model_result = LLMWorldResolver(routed_provider(settings, route), route.model).resolve(context)
        world_payload = WorldResolutionPayload.model_validate(raw)
    except ModelProviderError as exc:
        ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.WORLD_RESOLVER, source_type="SCENE_PERFORMANCE_TURN", source_id=turn.id, status=ExecutionStatus.BLOCKED if exc.code == MODEL_OUTPUT_INVALID else ExecutionStatus.FAILED, error_code=exc.code, upstream_status=exc.upstream_status)
        db.commit()
        error_status = {MODEL_AUTH_FAILED: 503, MODEL_RATE_LIMITED: 429, MODEL_TIMEOUT: 504}.get(exc.code, 502)
        raise HTTPException(status_code=error_status, detail={"code": exc.code, "upstream_status": exc.upstream_status}) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"code": MODEL_OUTPUT_INVALID, "message": "World resolver output was invalid."}) from exc
    context_after = WorldResolutionContextBuilder().build(db, performance, turn, proposal, turn.world_resolution_request)
    if context_after["fingerprint"] != context_fingerprint:
        raise HTTPException(status_code=409, detail={"code": "WORLD_CONTEXT_STALE"})
    report = WorldResolutionConstraintChecker().validate(db, context_after, world_payload, project_id)
    if trace:
        trace.status = ExecutionStatus.SUCCEEDED if report["valid"] and world_payload.outcome != ResolutionOutcome.UNRESOLVED else ExecutionStatus.BLOCKED
        trace.latency_ms = model_result.latency_ms; trace.request_id = model_result.request_id
        trace.output_fingerprint = stable_fingerprint(world_payload.model_dump(mode="json"))
        if trace.status == ExecutionStatus.BLOCKED:
            trace.error_code = "WORLD_INFORMATION_MISSING" if world_payload.outcome == ResolutionOutcome.UNRESOLVED else None
            trace.validation_report = {"issues": report.get("issues", [])}
            trace.retryable, trace.repairable, _ = RecoveryPolicy.resolve(trace.error_code)
    resolution_status = ResolutionStatus.VALID if report["valid"] and world_payload.outcome != ResolutionOutcome.UNRESOLVED else (ResolutionStatus.UNRESOLVED if report["valid"] else ResolutionStatus.REJECTED)
    resolution = db.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == turn.id))
    if resolution and resolution.status == ResolutionStatus.VALID:
        raise HTTPException(status_code=409, detail={"code": "WORLD_ALREADY_RESOLVED"})
    values = dict(project_id=project_id, performance_id=performance.id, performance_turn_id=turn.id, resolver_mode=mode, world_context_fingerprint=context_fingerprint, status=resolution_status, **world_payload.model_dump(mode="json"))
    if resolution:
        for key, value in values.items():
            if key not in {"project_id", "performance_id", "performance_turn_id"}: setattr(resolution, key, value)
    else:
        resolution = WorldResolution(**values)
    resolution.recipient_character_ids = WorldObservationRouter().recipients(performance, turn, resolution)
    db.add(resolution)
    if resolution_status == ResolutionStatus.VALID:
        performance.status = PerformanceStatus.RUNNING; performance.stop_reason = None
    elif resolution_status == ResolutionStatus.UNRESOLVED:
        performance.status = PerformanceStatus.AWAITING_WORLD; performance.stop_reason = "WORLD_INFORMATION_MISSING"
    else:
        performance.status = PerformanceStatus.AWAITING_WORLD; performance.stop_reason = "WORLD_RESOLUTION_REJECTED"
    db.add(performance); db.commit(); db.refresh(resolution); db.refresh(performance)
    return {"performance": _performance_payload(performance, list(reversed(turns)), db), "resolution": record_dict(resolution), "validation_report": report, "model_metadata": {"provider": model_result.provider, "model": model_result.model, "latency_ms": model_result.latency_ms, "request_id": model_result.request_id} if model_result else None}
