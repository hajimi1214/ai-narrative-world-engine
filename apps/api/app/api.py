from datetime import datetime
import json
from enum import Enum
from typing import Any, Type
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .db import SessionLocal
from .director import DirectorCandidateEngine, DirectorConstraintChecker, DirectorContextBuilder, DirectorModelContextSanitizer, DirectorProposalFactory, HeuristicDirector, LLMDirectorCandidateGenerator, StoryGravityContextBuilder, StoryGravityEngine
from .character_mind import ActiveCharacterCognitionReader, ActorPerceptionSanitizer, CharacterContextBuilder, CharacterDecisionConstraintChecker, CharacterMindViewBuilder, HeuristicCharacterActor
from .ai.errors import MODEL_AUTH_FAILED, MODEL_RATE_LIMITED, MODEL_TIMEOUT, MODEL_OUTPUT_INVALID, ModelProviderError
from .ai.factory import get_model_provider
from .llm_actor import LLMCharacterActor, _extract_single_json_object
from .settings import get_settings
from .models import ActionVisibility, AntiAIBible, CanonFact, Character, CharacterDecision, CharacterDecisionStatus, CharacterKnowledge, CharacterMemory, Chapter, ChapterWriterDraft, ChapterQualityAssessment, WriterDraftStatus, WriterPOVMode, DecisionType, DirectorDecisionLog, PerformanceMode, PerformanceStatus, Project, ProjectTemplate, ProposalStatus, RevealConstraint, Scene, SceneProposal, ScenePerformance, ScenePerformanceTurn, SceneCommit, SceneStateCheckpoint, StoryArc, StoryThread, WorldEntity, WritingBible, WorldResolution, ResolutionStatus, ResolutionOutcome, ResolverMode, WorldRevision, RevisionStatus, WorldSnapshot, SnapshotType, RevisionApplication, ProjectModelConfig, ProjectWorldSnapshotHead, ExecutionTrace, ExecutionStage, ExecutionStatus, RecoveryCandidate, RecoveryCandidateVersion, RecoveryCandidateStatus, RecoveryCandidateType, RecoveryVersionOrigin, RetconRequest, RetconImpactPlan, RetconImpactItem, RetconApplication, RetconApplicationStatus, RetconCognitionInvalidation, RetconCognitionInvalidationStatus, RetconReplaySession, ReplaySceneRun, ReplaySessionStatus, StateDeltaBatch, StateDeltaBatchStatus, StateDeltaItem, TimelineEvent, TimelineEventType, AutonomousWorldRun, AutonomousWorldStep, NarrativeStructureRevision, ResearchDocument, ResearchDocumentRevision, ProjectProviderCredential, ProviderCredentialPurpose, CharacterMemoryEmbedding
from .performance import CharacterPerformancePayload, HeuristicCharacterPerformer, LLMCharacterPerformer, PerformanceActionConstraintChecker, PerformanceCharacterContextBuilder, PerformanceObservationRouter, PerformancePostTurnStateResolver, TurnScheduler, is_quiescent_cycle
from .world_resolution import HeuristicWorldResolver, LLMWorldResolver, WorldResolutionContextBuilder, WorldResolutionConstraintChecker, WorldObservationRouter, WorldResolutionPayload
from .revision import RevisionChangeNormalizer, RevisionCreatePayload, RevisionImpactAnalyzer, RevisionStateFingerprintBuilder, target_fingerprint
from .versioning import WorldSnapshotBuilder, RevisionApplyService
from .model_router import ModelRouter, ProjectModelConfigPayload, ProviderCredentialResolver
from .execution_trace import ExecutionTraceRecorder, RecoveryPolicy, stable_fingerprint
from .recovery import CandidateRepairAgent, RecoveryActionResolver, RecoveryCandidateService, RecoveryContextStaleError, RecoveryEditPayload
from .services import DomainRuleError, activate_anti_ai_bible, activate_writing_bible, update_canon
from .retcon import RetconImpactPlanner, RetconPlanStalenessChecker, CLASSIFICATION_LABELS, semantic_fingerprint
from .retcon_apply import RetconApplyService, RetconPendingReplayGuard, RetconAuthorOverrideResolver, has_pending_replay
from .replay import ReplayService
from .historical import SceneStateCheckpointService, CurrentSceneCheckpointResolver
from .state_delta import StateDeltaCandidateBuilder
from .state_delta_validation import StateDeltaValidator
from .scene_commit import SceneCommitService
from .causal_ledger import CausalLedgerBackfillService, CausalProvenanceQuery
from .autonomy import AutonomousWorldLoopService
from .narrative_structure import NarrativeStructureService
from .runtime import PerformanceRuntimeService, RuntimeFailure, WorldResolutionRuntimeService, persisted_turns
from .writer import WriterDomainError, WriterProjectionAudit, WriterProjectionService
from .quality import QualityAssessmentFreshnessChecker, QualityDomainError, QualityGateService, QualityRepairService, assessment_payload
from .embeddings import CredentialVault, EmbeddingRoute, EmbeddingRouter, MemoryEmbeddingIndexService, OpenAICompatibleEmbeddingProvider, embedding_error_code
from .backup import ProjectBackupService, MAX_ARCHIVE_BYTES
from .research import KnowledgePacketBuilder, ResearchCorpusFingerprintBuilder, ResearchDomainError, ResearchIngestionService
from .planning import ChapterPlanPatchPayload, PlanCreatePayload, PlanFraming, PlanPatchPayload, chapter_task_context, generate_plan, persist_plan, plan_payload, validate_plan_references
from .long_form import LongFormEvaluationService, LongFormWorkflowError
from .creation import generate_creation_directions
from .models import StoryPlan, StoryPlanArc, StoryPlanChapter, StoryPlanStatus, StoryPlanVolume
from .scaling import ProjectHistoryProjectionService
from .formal_state import FormalStateIdentityService
from .retrieval_index import CognitionRetrievalProjectionService, ResearchLexicalIndexService, MemoryANNIndexStatusService
from .routes import research as research_routes
from .routes import scaling as scaling_routes
from .routes import state_history as state_history_routes
from .routes import writer as writer_routes
from .routes import auto_director as auto_director_routes
from .routes import author_guided_volume as author_guided_volume_routes

router = APIRouter()
router.include_router(research_routes.router)
router.include_router(scaling_routes.router)
router.include_router(state_history_routes.router)
router.include_router(writer_routes.router)
router.include_router(auto_director_routes.router)
router.include_router(author_guided_volume_routes.router)

class Payload(BaseModel):
    model_config = ConfigDict(extra="allow")

class PlanningScopeRegeneratePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str | None = Field(default=None, max_length=4000)

class PlanningNodePatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    summary: str | None = None
    theme: str | None = None
    core_question: str | None = None
    major_conflict: str | None = None
    start_state: dict[str, Any] | None = None
    end_state: dict[str, Any] | None = None
    main_thread: str | None = None
    ending_turn: str | None = None
    foreshadowing: list[Any] | None = None
    goal: str | None = None
    turning_points: list[Any] | None = None
    thread_refs: list[Any] | None = None

class AutonomousRunCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_budget: int = Field(gt=0, le=100)
    max_turns_per_scene: int = Field(default=6, gt=0, le=100)
    performance_mode: PerformanceMode = PerformanceMode.HEURISTIC
    resolver_mode: ResolverMode = ResolverMode.HEURISTIC
    config: dict[str, Any] = Field(default_factory=dict)
    client_request_id: str | None = None
    idempotency_key: str | None = None

class RetconRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_revision_id: str
    reason: str = Field(min_length=1, max_length=4000)

class RetconApplyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    explicit_confirmation: bool = False
    author_override: bool = False
    author_override_reason: str | None = None

class StateDeltaDerivePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_resolution_id: str

def retcon_actions(status: str) -> list[str]:
    return {"DRAFT": ["ANALYZE", "ABORT"], "PLANNED": ["REANALYZE", "ABORT", "APPLY"], "STALE": ["REANALYZE", "ABORT"], "APPLIED_PENDING_REPLAY": [], "ROLLED_BACK": [], "ABORTED": []}.get(status, [])

def retcon_item_payload(item: RetconImpactItem, db: Session | None = None) -> dict[str, Any]:
    result = record_dict(item)
    result["classification_label"] = CLASSIFICATION_LABELS.get(item.classification, item.classification)
    if db:
        model = {"CHARACTER_KNOWLEDGE": CharacterKnowledge, "CHARACTER_MEMORY": CharacterMemory, "CHARACTER_DECISION": CharacterDecision, "SCENE_PERFORMANCE_TURN": ScenePerformanceTurn, "WORLD_RESOLUTION": WorldResolution, "SCENE": Scene}.get(item.resource_type)
        row = db.get(model, item.resource_id) if model else None
        if row:
            summary = getattr(row, "proposition", None) or getattr(row, "content", None) or getattr(row, "decision_summary", None) or getattr(row, "observable_action", None) or getattr(row, "outcome_summary", None)
            if item.resource_type == "SCENE": summary = f"场景 {row.sequence}"
            result["display_summary"] = summary
            if item.character_id:
                character = db.get(Character, item.character_id)
                result["display_character_name"] = character.name if character else None
    return result

def retcon_plan_payload(db: Session, plan: RetconImpactPlan) -> dict[str, Any]:
    application = db.scalar(select(RetconApplication).where(RetconApplication.retcon_plan_id == plan.id).order_by(RetconApplication.created_at.desc(), RetconApplication.id.desc()))
    consumed = application is not None and application.status in {RetconApplicationStatus.APPLIED_PENDING_REPLAY, RetconApplicationStatus.REPLAY_COMPLETED, RetconApplicationStatus.ROLLED_BACK}
    stale = False if consumed else RetconPlanStalenessChecker().is_stale(db, plan)
    revision = db.get(WorldRevision, db.get(RetconRequest, plan.retcon_request_id).source_revision_id) if db.get(RetconRequest, plan.retcon_request_id) else None
    requirements = RetconAuthorOverrideResolver().resolve(db, plan.project_id, revision) if revision else {"explicit_confirmation_required": True, "author_override_required": False, "author_override_targets": []}
    return record_dict(plan) | {"status": "STALE" if stale else plan.status, "is_stale": stale, "consumed": consumed, "consumed_by_application_id": application.id if consumed else None, "consumption_status": getattr(application.status, "value", application.status) if consumed else None, "apply_requirements": requirements, "classification_labels": CLASSIFICATION_LABELS}

def retcon_request_payload(db: Session, request: RetconRequest, latest: RetconImpactPlan | None = None) -> dict[str, Any]:
    if latest is None:
        latest = db.scalar(select(RetconImpactPlan).where(RetconImpactPlan.retcon_request_id == request.id).order_by(RetconImpactPlan.version.desc()))
    application = db.scalar(select(RetconApplication).where(RetconApplication.retcon_request_id == request.id).order_by(RetconApplication.created_at.desc(), RetconApplication.id.desc()))
    lifecycle = getattr(application.status, "value", application.status) if application else None
    effective_status = lifecycle or ("STALE" if latest and RetconPlanStalenessChecker().is_stale(db, latest) else request.status)
    return record_dict(request) | {"effective_status": effective_status, "available_actions": retcon_actions(effective_status)}

def retcon_application_payload(db: Session, application: RetconApplication) -> dict[str, Any]:
    invalidations = db.scalars(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.retcon_application_id == application.id).order_by(RetconCognitionInvalidation.created_at, RetconCognitionInvalidation.id)).all()
    return record_dict(application) | {"pending_replay": application.status == RetconApplicationStatus.APPLIED_PENDING_REPLAY, "cognition_invalidations": [record_dict(item) for item in invalidations]}

# Retcon routes are declared before the main CRUD helpers; FastAPI evaluates
# dependency defaults while importing the module.
def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

@router.post("/projects/{project_id}/retcon/requests", status_code=status.HTTP_201_CREATED)
def create_retcon_request(project_id: str, payload: RetconRequestPayload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    revision = db.get(WorldRevision, payload.source_revision_id)
    if not revision or revision.project_id != project_id:
        raise HTTPException(status_code=409, detail={"code": "CROSS_PROJECT_REFERENCE" if revision else "TARGET_NOT_FOUND"})
    if revision.status != RevisionStatus.PREVIEWED:
        raise HTTPException(status_code=409, detail={"code": "SOURCE_REVISION_STALE"})
    request = RetconRequest(project_id=project_id, source_revision_id=revision.id, reason=payload.reason, status="DRAFT", current_plan_version=0)
    db.add(request); db.commit(); db.refresh(request)
    return retcon_request_payload(db, request)

@router.get("/projects/{project_id}/retcon/requests")
def list_retcon_requests(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    requests = db.scalars(select(RetconRequest).where(RetconRequest.project_id == project_id).order_by(RetconRequest.created_at.desc(), RetconRequest.id)).all()
    result = []
    for request in requests:
        latest = db.scalar(select(RetconImpactPlan).where(RetconImpactPlan.retcon_request_id == request.id).order_by(RetconImpactPlan.version.desc()))
        result.append(retcon_request_payload(db, request, latest) | {"latest_plan": retcon_plan_payload(db, latest) if latest else None})
    return result

@router.get("/projects/{project_id}/retcon/requests/{request_id}")
def get_retcon_request(project_id: str, request_id: str, db: Session = Depends(get_db)):
    request = db.get(RetconRequest, request_id)
    if not request or request.project_id != project_id: raise HTTPException(status_code=404, detail="Retcon request not found")
    plans = db.scalars(select(RetconImpactPlan).where(RetconImpactPlan.retcon_request_id == request.id).order_by(RetconImpactPlan.version.desc())).all()
    return retcon_request_payload(db, request, plans[0] if plans else None) | {"plans": [retcon_plan_payload(db, plan) for plan in plans]}

@router.post("/projects/{project_id}/retcon/requests/{request_id}/analyze")
def analyze_retcon_request(project_id: str, request_id: str, db: Session = Depends(get_db)):
    request = db.get(RetconRequest, request_id)
    if not request or request.project_id != project_id: raise HTTPException(status_code=404, detail="Retcon request not found")
    if request.status == "ABORTED": raise HTTPException(status_code=409, detail={"code": "RETCON_REQUEST_ABORTED"})
    if request.status == "APPLIED_PENDING_REPLAY": raise HTTPException(status_code=409, detail={"code": "RETCON_ALREADY_APPLIED"})
    if request.status == "ROLLED_BACK": raise HTTPException(status_code=409, detail={"code": "RETCON_REQUEST_ROLLED_BACK"})
    revision = db.get(WorldRevision, request.source_revision_id)
    if not revision or revision.project_id != project_id: raise HTTPException(status_code=409, detail={"code": "CROSS_PROJECT_REFERENCE"})
    current_basis = RevisionStateFingerprintBuilder().build(db, project_id)
    if revision.status != RevisionStatus.PREVIEWED or revision.base_state_fingerprint != current_basis:
        request.status = "STALE"; db.commit()
        raise HTTPException(status_code=409, detail={"code": "SOURCE_REVISION_STALE"})
    target_models = {"CANON_FACT": CanonFact, "WORLD_ENTITY": WorldEntity, "CHARACTER": Character}
    for change in revision.normalized_changes or []:
        target = db.get(target_models.get(change.get("target_type")), change.get("target_id")) if change.get("target_type") in target_models else None
        if not target or target.project_id != project_id:
            raise HTTPException(status_code=409, detail={"code": "CROSS_PROJECT_REFERENCE" if target else "TARGET_NOT_FOUND"})
    plan, items = RetconImpactPlanner().analyze(db, request, revision)
    db.add(plan); db.flush()
    for item in items: item.plan_id = plan.id; db.add(item)
    request.current_plan_version = plan.version; request.status = "PLANNED"
    db.commit(); db.refresh(plan)
    return {"request": retcon_request_payload(db, request, plan), "plan": retcon_plan_payload(db, plan), "items": [retcon_item_payload(item, db) for item in items]}

@router.get("/projects/{project_id}/retcon/plans/{plan_id}")
def get_retcon_plan(project_id: str, plan_id: str, db: Session = Depends(get_db)):
    plan = db.get(RetconImpactPlan, plan_id)
    if not plan or plan.project_id != project_id: raise HTTPException(status_code=404, detail="Retcon plan not found")
    items = db.scalars(select(RetconImpactItem).where(RetconImpactItem.plan_id == plan.id).order_by(RetconImpactItem.resource_type, RetconImpactItem.resource_id)).all()
    return {"plan": retcon_plan_payload(db, plan), "items": [retcon_item_payload(item, db) for item in items]}

@router.post("/projects/{project_id}/retcon/requests/{request_id}/abort")
def abort_retcon_request(project_id: str, request_id: str, db: Session = Depends(get_db)):
    request = db.get(RetconRequest, request_id)
    if not request or request.project_id != project_id: raise HTTPException(status_code=404, detail="Retcon request not found")
    if request.status == "APPLIED_PENDING_REPLAY": raise HTTPException(status_code=409, detail={"code": "RETCON_ALREADY_APPLIED"})
    if request.status == "ROLLED_BACK": raise HTTPException(status_code=409, detail={"code": "RETCON_REQUEST_ROLLED_BACK"})
    if request.status not in {"DRAFT", "PLANNED", "STALE"}: raise HTTPException(status_code=409, detail={"code": "RETCON_REQUEST_NOT_ABORTABLE"})
    request.status = "ABORTED"; db.commit(); db.refresh(request)
    return retcon_request_payload(db, request)

@router.post("/projects/{project_id}/retcon/requests/{request_id}/apply")
def apply_retcon(project_id: str, request_id: str, payload: RetconApplyPayload, db: Session = Depends(get_db)):
    request = db.scalar(select(RetconRequest).where(RetconRequest.id == request_id, RetconRequest.project_id == project_id).with_for_update())
    if not request or request.project_id != project_id:
        raise HTTPException(status_code=404, detail="Retcon request not found")
    plan = db.scalar(select(RetconImpactPlan).where(RetconImpactPlan.id == payload.plan_id).with_for_update())
    revision = db.scalar(select(WorldRevision).where(WorldRevision.id == request.source_revision_id).with_for_update())
    if not plan or plan.retcon_request_id != request_id or plan.project_id != project_id:
        raise HTTPException(status_code=409, detail={"code": "CROSS_PROJECT_REFERENCE" if plan else "RETCON_PLAN_NOT_FOUND"})
    if not revision or revision.project_id != project_id:
        raise HTTPException(status_code=409, detail={"code": "CROSS_PROJECT_REFERENCE"})
    try:
        application, revision_application, invalidations = RetconApplyService().apply(
            db, project_id, request, plan, revision, payload.explicit_confirmation,
            payload.author_override, payload.author_override_reason,
        )
        ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.REVISION_APPLY, source_type="WORLD_REVISION", source_id=revision.id, status=ExecutionStatus.SUCCEEDED, input_fingerprint=plan.basis_fingerprint, output_fingerprint=application.post_apply_world_fingerprint)
        db.commit(); db.refresh(application)
        return {"application": retcon_application_payload(db, application), "revision_application": record_dict(revision_application), "revision": record_dict(revision), "cognition_invalidations": [record_dict(item) for item in invalidations], "replay_summary": application.replay_summary}
    except IntegrityError as exc:
        db.rollback()
        constraint = str(getattr(exc, "orig", exc))
        code = "RETCON_ALREADY_APPLIED" if "uq_retcon_application_request" in constraint or db.scalar(select(RetconApplication.id).where(RetconApplication.retcon_request_id == request_id)) else "RETCON_APPLY_INTEGRITY_ERROR"
        raise HTTPException(status_code=409, detail={"code": code}) from exc
    except ValueError as exc:
        db.rollback()
        code = str(exc)
        trace_status = ExecutionStatus.FAILED if code in {"APPLY_RESULT_MISMATCH", "INVALID_TARGET_STATE", "COGNITION_TARGET_NOT_FOUND"} else ExecutionStatus.BLOCKED
        ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.REVISION_APPLY, source_type="WORLD_REVISION", source_id=request.source_revision_id, status=trace_status, error_code=code, input_fingerprint=getattr(revision, "base_state_fingerprint", None))
        db.commit()
        raise HTTPException(status_code=409, detail={"code": code}) from exc

@router.get("/projects/{project_id}/retcon/applications")
def list_retcon_applications(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    rows = db.scalars(select(RetconApplication).where(RetconApplication.project_id == project_id).order_by(RetconApplication.created_at.desc(), RetconApplication.id.desc())).all()
    return [retcon_application_payload(db, row) for row in rows]

@router.get("/projects/{project_id}/retcon/applications/{application_id}")
def get_retcon_application(project_id: str, application_id: str, db: Session = Depends(get_db)):
    row = db.get(RetconApplication, application_id)
    if not row or row.project_id != project_id:
        raise HTTPException(status_code=404, detail="Retcon application not found")
    return retcon_application_payload(db, row)

@router.post("/projects/{project_id}/retcon/applications/{application_id}/rollback")
def rollback_retcon(project_id: str, application_id: str, db: Session = Depends(get_db)):
    row = db.get(RetconApplication, application_id)
    if not row or row.project_id != project_id:
        raise HTTPException(status_code=404, detail="Retcon application not found")
    active_replay = db.scalar(select(RetconReplaySession).where(RetconReplaySession.retcon_application_id == row.id, RetconReplaySession.status.in_([ReplaySessionStatus.READY, ReplaySessionStatus.RUNNING, ReplaySessionStatus.BLOCKED])))
    if active_replay:
        raise HTTPException(status_code=409, detail={"code": "REPLAY_SESSION_ACTIVE"})
    try:
        application = RetconApplyService().rollback(db, project_id, row)
        ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.REVISION_ROLLBACK, source_type="RETCON_APPLICATION", source_id=row.id, status=ExecutionStatus.SUCCEEDED, output_fingerprint=application.post_apply_world_fingerprint)
        db.commit(); db.refresh(application)
        return retcon_application_payload(db, application)
    except ValueError as exc:
        db.rollback()
        code = "RETCON_ROLLBACK_STALE" if str(exc) in {"ROLLBACK_TARGET_STALE", "ROLLBACK_RESULT_MISMATCH"} else str(exc)
        ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.REVISION_ROLLBACK, source_type="RETCON_APPLICATION", source_id=application_id, status=ExecutionStatus.BLOCKED, error_code=code)
        db.commit()
        raise HTTPException(status_code=409, detail={"code": code}) from exc

class ReplayCommitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    explicit_confirmation: bool = False

def replay_session_payload(db: Session, session: RetconReplaySession) -> dict[str, Any]:
    runs = db.scalars(select(ReplaySceneRun).where(ReplaySceneRun.replay_session_id == session.id).order_by(ReplaySceneRun.original_sequence, ReplaySceneRun.id)).all()
    return record_dict(session) | {"queue": session.queue, "runs": [record_dict(run) for run in runs]}

@router.post("/projects/{project_id}/retcon/applications/{application_id}/replay-sessions", status_code=status.HTTP_201_CREATED)
def create_replay_session(project_id: str, application_id: str, db: Session = Depends(get_db)):
    try:
        session = ReplayService().create_session(db, project_id, application_id)
        db.commit(); db.refresh(session)
        return replay_session_payload(db, session)
    except ValueError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc

@router.get("/projects/{project_id}/retcon/replay-sessions/{session_id}")
def get_replay_session(project_id: str, session_id: str, db: Session = Depends(get_db)):
    session = db.get(RetconReplaySession, session_id)
    if not session or session.project_id != project_id: raise HTTPException(status_code=404, detail="Replay session not found")
    return replay_session_payload(db, session)

@router.post("/projects/{project_id}/retcon/replay-sessions/{session_id}/step")
def step_replay_session(project_id: str, session_id: str, db: Session = Depends(get_db)):
    session = db.get(RetconReplaySession, session_id)
    if not session or session.project_id != project_id: raise HTTPException(status_code=404, detail="Replay session not found")
    try:
        ReplayService().step(db, session); db.commit(); db.refresh(session); return replay_session_payload(db, session)
    except ValueError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc

@router.post("/projects/{project_id}/retcon/replay-sessions/{session_id}/commit")
def commit_replay_session(project_id: str, session_id: str, payload: ReplayCommitPayload, db: Session = Depends(get_db)):
    session = db.get(RetconReplaySession, session_id)
    if not session or session.project_id != project_id: raise HTTPException(status_code=404, detail="Replay session not found")
    try:
        ReplayService().commit(db, session, payload.explicit_confirmation); db.commit(); db.refresh(session); return replay_session_payload(db, session)
    except IntegrityError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": "REPLAY_COMMIT_INTEGRITY_ERROR"}) from exc
    except RuntimeError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": "REPLAY_COMMIT_FAILED"}) from exc
    except ValueError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc

@router.post("/projects/{project_id}/retcon/replay-sessions/{session_id}/abort")
def abort_replay_session(project_id: str, session_id: str, db: Session = Depends(get_db)):
    session = db.get(RetconReplaySession, session_id)
    if not session or session.project_id != project_id: raise HTTPException(status_code=404, detail="Replay session not found")
    if session.status == ReplaySessionStatus.COMPLETED: raise HTTPException(status_code=409, detail={"code": "REPLAY_SESSION_COMPLETED"})
    session.status = ReplaySessionStatus.ABORTED; session.staged_world_state = {}; db.query(ReplaySceneRun).filter(ReplaySceneRun.replay_session_id == session.id).delete(synchronize_session=False); db.commit(); db.refresh(session)
    return replay_session_payload(db, session)

def routed_provider(settings, route, db=None, project_id=None):
    key = ProviderCredentialResolver().generation_key(db, project_id, settings) if db is not None and project_id else None
    config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == project_id)) if db is not None and project_id else None
    timeout = config.request_timeout_seconds if config else None
    retries = config.max_retries if config else 0
    rate_limit = config.rate_limit_per_minute if config else 0
    try:
        return get_model_provider(settings, route.provider, route.base_url, key, timeout_seconds=timeout, max_retries=retries, rate_limit_per_minute=rate_limit)
    except TypeError as exc:
        # Preserve compatibility with injected test/fake factories from the
        # frozen phases, whose contract has three positional arguments.
        if "positional" not in str(exc) and "argument" not in str(exc):
            raise
        return get_model_provider(settings, route.provider, route.base_url)

def ensure_replay_not_pending(db: Session, project_id: str) -> None:
    try:
        RetconPendingReplayGuard().assert_progression_allowed(db, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc

def primary_issue(report):
    issues = report.get("issues", []) if isinstance(report, dict) else report.as_dict().get("issues", [])
    codes = sorted(str(item.get("code")) for item in issues if item.get("blocking", True) and item.get("code"))
    return codes[0] if codes else "VALIDATION_BLOCKED"

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

def writer_draft_payload(draft: ChapterWriterDraft, *, include_content: bool = False) -> dict[str, Any]:
    value = {"id": draft.id, "project_id": draft.project_id, "chapter_id": draft.chapter_id, "version": draft.version, "status": serialize(draft.status), "origin": serialize(draft.origin), "source_quality_assessment_id": draft.source_quality_assessment_id, "client_request_id": draft.client_request_id, "request_fingerprint": draft.request_fingerprint, "chapter_structure_fingerprint": draft.chapter_structure_fingerprint, "chapter_source_fingerprint": draft.chapter_source_fingerprint, "writer_context_fingerprint": draft.writer_context_fingerprint, "source_structure_status": draft.source_structure_status, "source_scene_ids": draft.source_scene_ids, "writing_bible_id": draft.writing_bible_id, "writing_bible_version": draft.writing_bible_version, "writing_bible_fingerprint": draft.writing_bible_fingerprint, "pov_mode": serialize(draft.pov_mode), "pov_character_id": draft.pov_character_id, "provider": draft.provider, "model": draft.model, "model_request_id": draft.model_request_id, "prompt_fingerprint": draft.prompt_fingerprint, "title_candidate": draft.title_candidate, "content_fingerprint": draft.content_fingerprint, "word_count": draft.word_count, "scene_coverage": draft.scene_coverage, "source_refs": draft.source_refs, "validation_report": draft.validation_report, "parent_draft_id": draft.parent_draft_id, "supersedes_draft_id": draft.supersedes_draft_id, "created_at": serialize(draft.created_at), "completed_at": serialize(draft.completed_at), "adopted_at": serialize(draft.adopted_at), "stale_at": serialize(draft.stale_at)}
    if include_content:
        value["content"] = draft.content
        value["prose"] = draft.content
        value["chapter_title"] = draft.title_candidate
    return value

def require_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project: raise HTTPException(status_code=404, detail="Project not found")
    return project

def ensure_autonomous_run_idle(db: Session, project_id: str) -> None:
    if db.scalar(select(AutonomousWorldRun.id).where(AutonomousWorldRun.project_id == project_id, AutonomousWorldRun.active.is_(True))):
        raise HTTPException(status_code=409, detail={"code": "AUTONOMY_RUN_ACTIVE"})

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

FORMAL_MUTATION_MODELS = {Character, WorldEntity, CanonFact, StoryThread, StoryArc, Scene, Chapter}

def guard_formal_mutation(db: Session, project_id: str, model: Type) -> None:
    if model in FORMAL_MUTATION_MODELS:
        ensure_replay_not_pending(db, project_id)


def _quality_chapter(db: Session, project_id: str, chapter_id: str) -> Chapter:
    require_project(db, project_id)
    chapter = db.get(Chapter, chapter_id)
    if not chapter or chapter.project_id != project_id:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return chapter


def _quality_provider(db: Session, project_id: str, role: str):
    settings = get_settings()
    route = ModelRouter().resolve(db, project_id, settings, role)
    try:
        provider = routed_provider(settings, route, db, project_id)
    except ModelProviderError as provider_error:
        deferred_error = provider_error
        class DeferredQualityProvider:
            name = route.provider
            def generate(self, messages, model):
                raise deferred_error
        provider = DeferredQualityProvider()
    return provider, route.model, settings


@router.post("/projects/{project_id}/chapters/{chapter_id}/quality/preview")
def quality_preview(project_id: str, chapter_id: str, payload: Payload | None = None, db: Session = Depends(get_db)):
    _quality_chapter(db, project_id, chapter_id)
    try:
        return QualityGateService().preview(db, chapter_id, payload.model_dump() if payload else {})
    except QualityDomainError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": exc.code, "detail": exc.detail}) from exc


@router.post("/projects/{project_id}/chapters/{chapter_id}/quality/assess")
def quality_assess(project_id: str, chapter_id: str, payload: Payload | None = None, db: Session = Depends(get_db)):
    _quality_chapter(db, project_id, chapter_id)
    values = payload.model_dump() if payload else {}
    if values.get("idempotency_key") and not values.get("client_request_id"):
        values["client_request_id"] = values["idempotency_key"]
    provider, model, settings = _quality_provider(db, project_id, "CRITIC")
    try:
        assessment = QualityGateService().assess(db, chapter_id, values, provider=provider, model=model, settings=settings)
        db.commit(); db.refresh(assessment)
    except QualityDomainError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": exc.code, "detail": exc.detail}) from exc
    if serialize(assessment.status) == "FAILED":
        raise HTTPException(status_code=409, detail={"code": (assessment.decision_reason_codes or ["QUALITY_ASSESSMENT_FAILED"])[0], "assessment_id": assessment.id})
    return assessment_payload(db, assessment, include_findings=True)


@router.get("/projects/{project_id}/chapters/{chapter_id}/quality/assessments")
def quality_assessments(project_id: str, chapter_id: str, db: Session = Depends(get_db)):
    _quality_chapter(db, project_id, chapter_id)
    rows = db.scalars(select(ChapterQualityAssessment).where(ChapterQualityAssessment.project_id == project_id, ChapterQualityAssessment.chapter_id == chapter_id).order_by(ChapterQualityAssessment.version.desc(), ChapterQualityAssessment.id.desc())).all()
    return [assessment_payload(db, item) for item in rows]


@router.get("/projects/{project_id}/chapters/{chapter_id}/quality/assessments/{assessment_id}")
def quality_assessment_detail(project_id: str, chapter_id: str, assessment_id: str, db: Session = Depends(get_db)):
    assessment = db.get(ChapterQualityAssessment, assessment_id)
    if not assessment or assessment.project_id != project_id or assessment.chapter_id != chapter_id:
        raise HTTPException(status_code=404, detail="Quality assessment not found")
    return assessment_payload(db, assessment, include_findings=True)


@router.get("/projects/{project_id}/chapters/{chapter_id}/quality")
def quality_current(project_id: str, chapter_id: str, db: Session = Depends(get_db)):
    chapter = _quality_chapter(db, project_id, chapter_id)
    current_draft_id = chapter.current_writer_draft_id
    current_content_fp = chapter.writer_content_fingerprint
    assessment = db.get(ChapterQualityAssessment, chapter.current_quality_assessment_id) if chapter.current_quality_assessment_id else None
    if not assessment or assessment.writer_draft_id != current_draft_id or assessment.content_fingerprint != current_content_fp:
        assessment = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.chapter_id == chapter.id, ChapterQualityAssessment.writer_draft_id == current_draft_id, ChapterQualityAssessment.content_fingerprint == current_content_fp).order_by(ChapterQualityAssessment.version.desc(), ChapterQualityAssessment.id.desc()))
    freshness = QualityAssessmentFreshnessChecker().check(db, assessment, require_current=True) if assessment else {"fresh": False, "current": False, "reasons": ["QUALITY_NOT_ASSESSED"]}
    stored_status = serialize(assessment.status) if assessment else (chapter.quality_status or "NOT_ASSESSED")
    effective_status = stored_status if assessment and freshness["fresh"] else "STALE" if assessment else "NOT_ASSESSED"
    return {"chapter_id": chapter.id, "chapter_status": chapter.status, "quality_status": chapter.quality_status, "stored_status": stored_status, "effective_status": effective_status, "quality_content_fingerprint": chapter.quality_content_fingerprint, "quality_approved_at": serialize(chapter.quality_approved_at), "current_assessment": assessment_payload(db, assessment) if assessment else None, "stale": bool(assessment and not freshness["fresh"]), "freshness_reasons": freshness["reasons"], "repair_available": bool(assessment and serialize(assessment.status) in {"REPAIR_REQUIRED", "BLOCKED"})}


@router.post("/projects/{project_id}/chapters/{chapter_id}/quality/assessments/{assessment_id}/approve")
def quality_approve(project_id: str, chapter_id: str, assessment_id: str, db: Session = Depends(get_db)):
    assessment = db.get(ChapterQualityAssessment, assessment_id)
    if not assessment or assessment.project_id != project_id or assessment.chapter_id != chapter_id:
        raise HTTPException(status_code=404, detail="Quality assessment not found")
    try:
        chapter = QualityGateService().approve(db, assessment_id); db.commit(); db.refresh(chapter); return record_dict(chapter)
    except QualityDomainError as exc:
        if exc.code == "QUALITY_SOURCE_CHANGED": db.commit()
        else: db.rollback()
        raise HTTPException(status_code=409, detail={"code": exc.code, "detail": exc.detail}) from exc


@router.post("/projects/{project_id}/chapters/{chapter_id}/quality/assessments/{assessment_id}/repair")
def quality_repair(project_id: str, chapter_id: str, assessment_id: str, payload: Payload | None = None, db: Session = Depends(get_db)):
    assessment = db.get(ChapterQualityAssessment, assessment_id)
    if not assessment or assessment.project_id != project_id or assessment.chapter_id != chapter_id:
        raise HTTPException(status_code=404, detail="Quality assessment not found")
    repair_provider, repair_model, settings = _quality_provider(db, project_id, "REPAIR")
    critic_provider, critic_model, _ = _quality_provider(db, project_id, "CRITIC")
    try:
        draft, child = QualityRepairService().repair(db, assessment_id, payload.model_dump() if payload else {}, repair_provider=repair_provider, repair_model=repair_model, critic_provider=critic_provider, critic_model=critic_model, settings=settings)
        db.commit(); db.refresh(draft)
    except QualityDomainError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": exc.code, "detail": exc.detail}) from exc
    return {"draft": writer_draft_payload(draft, include_content=True), "assessment": assessment_payload(db, child, include_findings=True) if child else None}


@router.post("/projects/{project_id}/chapters/{chapter_id}/quality/repairs/{draft_id}/adopt")
def quality_repair_adopt(project_id: str, chapter_id: str, draft_id: str, db: Session = Depends(get_db)):
    draft = db.get(ChapterWriterDraft, draft_id)
    if not draft or draft.project_id != project_id or draft.chapter_id != chapter_id:
        raise HTTPException(status_code=404, detail="Quality repair draft not found")
    try:
        chapter = QualityRepairService().adopt(db, draft_id); db.commit(); db.refresh(chapter); return record_dict(chapter)
    except QualityDomainError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": exc.code, "detail": exc.detail}) from exc

@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    return [record_dict(item) for item in db.scalars(select(Project).order_by(Project.created_at.desc())).all()]

@router.get("/projects/{project_id}/backup/export")
def export_project_backup(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    archive = ProjectBackupService().export(db, project_id)
    body = json.dumps(archive, ensure_ascii=False, separators=(",", ":"))
    return Response(content=body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="novel-backup-{project_id}.json"',
        "X-Backup-Fingerprint": archive["fingerprint"],
    })

@router.get("/projects/{project_id}/backup/status")
def project_backup_status(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    snapshots = db.scalars(select(WorldSnapshot).where(WorldSnapshot.project_id == project_id).order_by(WorldSnapshot.created_at.desc(), WorldSnapshot.id.desc()).limit(10)).all()
    snapshot_count = db.scalar(select(func.count(WorldSnapshot.id)).where(WorldSnapshot.project_id == project_id)) or 0
    head = db.scalar(select(ProjectWorldSnapshotHead).where(ProjectWorldSnapshotHead.project_id == project_id))
    return {"safe": True, "snapshot_count": int(snapshot_count), "last_snapshot_at": serialize(snapshots[0].created_at) if snapshots else None, "current_checkpoint": bool(head), "message": "作品数据保存在本机，导出备份后可随时恢复副本。"}

def _restore_archive(values: dict[str, Any], db: Session):
    archive = values.get("archive") if isinstance(values.get("archive"), dict) else values
    if len(json.dumps(archive, ensure_ascii=False, default=str)) > MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail={"code": "BACKUP_TOO_LARGE"})
    try:
        project = ProjectBackupService().restore(db, archive, name=values.get("name"))
        db.commit(); db.refresh(project)
        return {"project": record_dict(project), "message": "已创建恢复副本，原小说未被覆盖。"}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc

@router.post("/projects/import", status_code=status.HTTP_201_CREATED)
def import_project_backup(payload: Payload, db: Session = Depends(get_db)):
    return _restore_archive(payload.model_dump(), db)

@router.post("/projects/{project_id}/backup/restore", status_code=status.HTTP_201_CREATED)
def restore_project_backup(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return _restore_archive(payload.model_dump(), db)

@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(payload: Payload, db: Session = Depends(get_db)):
    return record_dict(create_record(db, Project, payload.model_dump()))

@router.post("/projects/{project_id}/creation-directions")
def creation_directions(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    try:
        result, model_result = generate_creation_directions(db, project_id, payload.model_dump())
        return result
    except ModelProviderError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc)}) from exc

@router.get("/projects/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)):
    return record_dict(require_project(db, project_id))

@router.patch("/projects/{project_id}")
def patch_project(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    values = payload.model_dump()
    if "current_world_time" in values:
        ensure_replay_not_pending(db, project_id)
    return record_dict(update_record(db, require_project(db, project_id), values))


@router.get("/projects/{project_id}/planning/plans")
def list_story_plans(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [plan_payload(db, item) for item in db.scalars(select(StoryPlan).where(StoryPlan.project_id == project_id).order_by(StoryPlan.version.desc())).all()]


@router.get("/projects/{project_id}/planning/plan")
def get_story_plan(project_id: str, version: int | None = Query(default=None, ge=1), db: Session = Depends(get_db)):
    require_project(db, project_id)
    query = select(StoryPlan).where(StoryPlan.project_id == project_id)
    plan = db.scalar(query.where(StoryPlan.version == version) if version else query.order_by(StoryPlan.version.desc()))
    if not plan:
        return {"plan": None}
    return plan_payload(db, plan)


@router.post("/projects/{project_id}/planning/plans", status_code=status.HTTP_201_CREATED)
def create_story_plan(project_id: str, payload: PlanCreatePayload, db: Session = Depends(get_db)):
    project = require_project(db, project_id)
    plan = persist_plan(db, project, payload.model_dump())
    db.commit(); db.refresh(plan)
    return plan_payload(db, plan)


@router.post("/projects/{project_id}/planning/generate", status_code=status.HTTP_201_CREATED)
def generate_story_plan(project_id: str, payload: PlanCreatePayload, db: Session = Depends(get_db)):
    project = require_project(db, project_id)
    values = payload.model_dump()
    try:
        generated, result = generate_plan(db, project, values["framing"], values.get("premise"), values.get("style_guide") or {}, values.get("anti_ai_rules") or {})
        generated.setdefault("framing", values["framing"]); generated.setdefault("premise", values.get("premise")); generated.setdefault("style_guide", values.get("style_guide") or {}); generated.setdefault("anti_ai_rules", values.get("anti_ai_rules") or {})
        generated["framing"] = values["framing"] | (generated.get("framing") or {})
        errors = validate_plan_references(db, project_id, generated)
        if errors:
            raise HTTPException(status_code=422, detail={"code": "PLAN_REFERENCE_INVALID", "errors": errors})
        plan = persist_plan(db, project, generated, provider=result.provider, model=result.model, request_id=result.request_id, report={"latency_ms": result.latency_ms, "direction_options": generated.get("direction_options", [])})
        db.commit(); db.refresh(plan)
        return plan_payload(db, plan)
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        code = getattr(exc, "code", None) or getattr(exc, "error_code", None) or "PLAN_GENERATION_FAILED"
        raise HTTPException(status_code=502, detail={"code": code, "message": str(exc)}) from exc


@router.patch("/projects/{project_id}/planning/plans/{plan_id}")
def patch_story_plan(project_id: str, plan_id: str, payload: PlanPatchPayload, db: Session = Depends(get_db)):
    plan = db.get(StoryPlan, plan_id)
    if not plan or plan.project_id != project_id:
        raise HTTPException(status_code=404, detail="Story plan not found")
    if plan.status in (StoryPlanStatus.ARCHIVED, StoryPlanStatus.APPROVED):
        raise HTTPException(status_code=409, detail={"code": "PLAN_IMMUTABLE"})
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(plan, key, value)
    db.commit(); db.refresh(plan)
    return plan_payload(db, plan)


@router.patch("/projects/{project_id}/planning/plans/{plan_id}/volumes/{volume_number}")
def patch_story_plan_volume(project_id: str, plan_id: str, volume_number: int, payload: PlanningNodePatchPayload, db: Session = Depends(get_db)):
    plan = db.get(StoryPlan, plan_id)
    if not plan or plan.project_id != project_id:
        raise HTTPException(status_code=404, detail="Story plan not found")
    if plan.status in (StoryPlanStatus.ARCHIVED, StoryPlanStatus.APPROVED):
        raise HTTPException(status_code=409, detail={"code": "PLAN_IMMUTABLE", "message": "已审批规划不可直接修改，请从该版本局部重做"})
    volume = db.scalar(select(StoryPlanVolume).where(StoryPlanVolume.plan_id == plan_id, StoryPlanVolume.number == volume_number))
    if not volume:
        raise HTTPException(status_code=404, detail="Volume plan not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(volume, key, value)
    db.commit(); db.refresh(volume)
    return plan_payload(db, plan)


@router.patch("/projects/{project_id}/planning/plans/{plan_id}/arcs/{arc_number}")
def patch_story_plan_arc(project_id: str, plan_id: str, arc_number: int, payload: PlanningNodePatchPayload, db: Session = Depends(get_db)):
    plan = db.get(StoryPlan, plan_id)
    if not plan or plan.project_id != project_id:
        raise HTTPException(status_code=404, detail="Story plan not found")
    if plan.status in (StoryPlanStatus.ARCHIVED, StoryPlanStatus.APPROVED):
        raise HTTPException(status_code=409, detail={"code": "PLAN_IMMUTABLE", "message": "已审批规划不可直接修改，请从该版本局部重做"})
    arc = db.scalar(select(StoryPlanArc).where(StoryPlanArc.plan_id == plan_id, StoryPlanArc.number == arc_number))
    if not arc:
        raise HTTPException(status_code=404, detail="Arc plan not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(arc, key, value)
    db.commit(); db.refresh(arc)
    return plan_payload(db, plan)


@router.post("/projects/{project_id}/planning/plans/{plan_id}/volumes/{volume_number}/regenerate", status_code=status.HTTP_201_CREATED)
def regenerate_story_plan_volume(project_id: str, plan_id: str, volume_number: int, payload: PlanningScopeRegeneratePayload | None = None, db: Session = Depends(get_db)):
    return _regenerate_story_plan_scope(project_id, plan_id, db, volume_number=volume_number, instruction=(payload.instruction if payload else None))


@router.post("/projects/{project_id}/planning/plans/{plan_id}/chapters/{chapter_id}/regenerate", status_code=status.HTTP_201_CREATED)
def regenerate_story_plan_chapter(project_id: str, plan_id: str, chapter_id: str, payload: PlanningScopeRegeneratePayload | None = None, db: Session = Depends(get_db)):
    chapter = db.get(StoryPlanChapter, chapter_id)
    if not chapter or chapter.project_id != project_id or chapter.plan_id != plan_id:
        raise HTTPException(status_code=404, detail="Chapter plan not found")
    return _regenerate_story_plan_scope(project_id, plan_id, db, chapter_number=chapter.number, instruction=(payload.instruction if payload else None))


def _regenerate_story_plan_scope(project_id: str, plan_id: str, db: Session, *, volume_number: int | None = None, chapter_number: int | None = None, instruction: str | None = None):
    source = db.get(StoryPlan, plan_id)
    project = require_project(db, project_id)
    if not source or source.project_id != project_id:
        raise HTTPException(status_code=404, detail="Story plan not found")
    source_data = plan_payload(db, source)
    target_chapters = [item for item in source_data["chapters"] if (volume_number is None or item["volume_number"] == volume_number) and (chapter_number is None or item["number"] == chapter_number)]
    if not target_chapters:
        raise HTTPException(status_code=404, detail="Planning scope not found")
    if any(item.get("locked") for item in target_chapters):
        raise HTTPException(status_code=409, detail={"code": "PLANNING_SCOPE_LOCKED", "chapters": [item["number"] for item in target_chapters if item.get("locked")]})
    try:
        generated, result = generate_plan(db, project, source.framing or {}, source.premise, source.style_guide or {}, source.anti_ai_rules or {})
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"code": "PLAN_SCOPE_GENERATION_FAILED", "message": str(exc)}) from exc
    merged = {"framing": source_data.get("framing") or {}, "premise": source_data.get("premise"), "macro_plan": source_data.get("macro_plan") or {}, "style_guide": source_data.get("style_guide") or {}, "anti_ai_rules": source_data.get("anti_ai_rules") or {}, "volumes": source_data.get("volumes") or [], "arcs": source_data.get("arcs") or [], "chapters": source_data.get("chapters") or []}
    if volume_number is not None:
        generated_volumes = {item.get("number"): item for item in generated.get("volumes", [])}
        if volume_number in generated_volumes:
            merged["volumes"] = [generated_volumes.get(item.get("number"), item) if item.get("number") == volume_number else item for item in merged["volumes"]]
        generated_arcs = {item.get("number"): item for item in generated.get("arcs", []) if item.get("volume_number") == volume_number}
        merged["arcs"] = [generated_arcs.get(item.get("number"), item) if item.get("volume_number") == volume_number else item for item in merged["arcs"]]
    generated_chapters = {item.get("number"): item for item in generated.get("chapters", [])}
    selected_numbers = {item["number"] for item in target_chapters}
    merged["chapters"] = [generated_chapters.get(item.get("number"), item) if item.get("number") in selected_numbers and not item.get("locked") else item for item in merged["chapters"]]
    if instruction:
        merged["generation_instruction"] = instruction
    new_plan = persist_plan(db, project, merged, provider=getattr(result, "provider", None), model=getattr(result, "model", None), request_id=getattr(result, "request_id", None), report={"scope": "volume" if volume_number is not None else "chapter", "source_plan_id": source.id, "instruction": instruction})
    db.commit(); db.refresh(new_plan)
    affected = sorted({item["number"] for item in merged["chapters"] if item["number"] in selected_numbers or (volume_number is not None and item["volume_number"] == volume_number and item["number"] >= min(selected_numbers))})
    return {"plan": plan_payload(db, new_plan), "impact": {"source_plan_id": source.id, "new_plan_id": new_plan.id, "scope": "volume" if volume_number is not None else "chapter", "affected_chapters": affected, "affected_volumes": [volume_number] if volume_number is not None else sorted({item["volume_number"] for item in target_chapters}), "affected_foreshadowing": sorted({str(value) for item in target_chapters for value in (item.get("foreshadow_create") or []) + (item.get("foreshadow_payoff") or [])}), "affected_threads": sorted({str(value) for item in source_data.get("arcs", []) if volume_number is None or item.get("volume_number") == volume_number for value in item.get("thread_refs", [])})}}


@router.patch("/projects/{project_id}/planning/plans/{plan_id}/chapters/{chapter_id}")
def patch_story_plan_chapter(project_id: str, plan_id: str, chapter_id: str, payload: ChapterPlanPatchPayload, db: Session = Depends(get_db)):
    chapter = db.get(StoryPlanChapter, chapter_id)
    if not chapter or chapter.project_id != project_id or chapter.plan_id != plan_id:
        raise HTTPException(status_code=404, detail="Chapter plan not found")
    if chapter.locked or db.get(StoryPlan, plan_id).status == StoryPlanStatus.APPROVED:
        raise HTTPException(status_code=409, detail={"code": "CHAPTER_PLAN_LOCKED" if chapter.locked else "PLAN_IMMUTABLE"})
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(chapter, key, value)
    db.commit(); db.refresh(chapter)
    return {column.name: getattr(chapter, column.name).value if hasattr(getattr(chapter, column.name), "value") else getattr(chapter, column.name) for column in chapter.__table__.columns}


@router.post("/projects/{project_id}/planning/plans/{plan_id}/approve")
def approve_story_plan(project_id: str, plan_id: str, db: Session = Depends(get_db)):
    plan = db.get(StoryPlan, plan_id)
    if not plan or plan.project_id != project_id:
        raise HTTPException(status_code=404, detail="Story plan not found")
    chapters = db.scalars(select(StoryPlanChapter).where(StoryPlanChapter.plan_id == plan.id).order_by(StoryPlanChapter.number)).all()
    if not chapters:
        raise HTTPException(status_code=409, detail={"code": "PLAN_HAS_NO_CHAPTERS"})
    missing = [chapter.number for chapter in chapters if not chapter.objective or not chapter.conflict or not chapter.end_state]
    if missing:
        raise HTTPException(status_code=409, detail={"code": "CHAPTER_TASK_INCOMPLETE", "chapters": missing})
    beat_missing = [chapter.number for chapter in chapters if not 3 <= len(chapter.scene_beats or []) <= 6]
    if beat_missing:
        raise HTTPException(status_code=409, detail={"code": "SCENE_BEATS_INCOMPLETE", "chapters": beat_missing, "required": "3-6"})
    plan.status = StoryPlanStatus.APPROVED
    db.commit(); db.refresh(plan)
    return plan_payload(db, plan)


@router.get("/projects/{project_id}/chapters/{chapter_id}/planning-task")
def get_chapter_planning_task(project_id: str, chapter_id: str, db: Session = Depends(get_db)):
    chapter = db.get(Chapter, chapter_id)
    if not chapter or chapter.project_id != project_id:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return {"chapter_id": chapter.id, "chapter_number": chapter.number, "task": chapter_task_context(db, project_id, chapter.number)}


@router.get("/projects/{project_id}/long-form/next")
def long_form_next_chapter(project_id: str, db: Session = Depends(get_db)):
    try:
        return LongFormEvaluationService().next_chapter(db, project_id)
    except LongFormWorkflowError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "detail": exc.detail}) from exc


@router.get("/projects/{project_id}/long-form/evaluation")
def long_form_evaluation(project_id: str, db: Session = Depends(get_db)):
    try:
        return LongFormEvaluationService().evaluate(db, project_id)
    except LongFormWorkflowError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "detail": exc.detail}) from exc

@router.get("/projects/{project_id}/snapshot")
def project_snapshot(project_id: str, db: Session = Depends(get_db)):
    project = require_project(db, project_id)
    characters = db.scalars(select(Character).where(Character.project_id == project_id, Character.active.is_(True))).all()
    character_ids = [item.id for item in characters]
    reader = ActiveCharacterCognitionReader()
    knowledge = [item for character in characters for item in reader.knowledge(db, project_id, character.id)] if character_ids else []
    memories = [item for character in characters for item in reader.memories(db, project_id, character.id)] if character_ids else []
    return {
        "project": record_dict(project),
        "active_writing_bible": next((record_dict(item) for item in db.scalars(select(WritingBible).where(WritingBible.project_id == project_id, WritingBible.active.is_(True))).all()), None),
        "active_anti_ai_bible": next((record_dict(item) for item in db.scalars(select(AntiAIBible).where(AntiAIBible.project_id == project_id, AntiAIBible.active.is_(True))).all()), None),
        "canon": [record_dict(item) for item in db.scalars(select(CanonFact).where(CanonFact.project_id == project_id)).all()],
        "active_characters": [record_dict(item) for item in characters],
        "character_states": [{"character_id": item.id, "current_state": serialize(item.current_state), "physical_state": serialize(item.physical_state), "emotional_state": serialize(item.emotional_state), "goals": serialize(item.goals)} for item in characters],
        "character_knowledge_summary": [{"id": item.id, "character_id": item.character_id, "proposition": item.proposition, "status": item.status.value, "confidence": item.confidence} for item in knowledge],
        "character_memory_summary": [{"id": item.id, "character_id": item.character_id, "content": item.content, "importance": item.importance, "confidence": item.confidence} for item in memories],
        "world_entities": [record_dict(item) for item in db.scalars(select(WorldEntity).where(WorldEntity.project_id == project_id, WorldEntity.active.is_(True))).all()],
        "active_story_threads": [record_dict(item) for item in db.scalars(select(StoryThread).where(StoryThread.project_id == project_id, StoryThread.status.in_(["OPEN", "PAUSED"]))).all()],
        "current_story_arc": next((record_dict(item) for item in db.scalars(select(StoryArc).where(StoryArc.project_id == project_id, StoryArc.status == "ACTIVE").order_by(StoryArc.id.desc())).all()), None),
        "recent_scenes": [record_dict(item) for item in db.scalars(select(Scene).where(Scene.project_id == project_id, Scene.history_status == "ACTIVE").order_by(Scene.sequence.desc()).limit(20)).all()],
    }

def project_routes(prefix: str, model: Type, allow_update: bool = True):
    @router.get(f"/projects/{{project_id}}/{prefix}")
    def list_items(project_id: str, db: Session = Depends(get_db)):
        require_project(db, project_id)
        return [record_dict(item) for item in db.scalars(select(model).where(model.project_id == project_id)).all()]
    @router.post(f"/projects/{{project_id}}/{prefix}", status_code=status.HTTP_201_CREATED)
    def add_item(project_id: str, payload: Payload, db: Session = Depends(get_db)):
        require_project(db, project_id)
        guard_formal_mutation(db, project_id, model)
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
            guard_formal_mutation(db, item.project_id, model)
            return record_dict(update_record(db, item, payload.model_dump()))
    @router.delete(f"/{prefix}/{{item_id}}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_item(item_id: str, db: Session = Depends(get_db)):
        item = db.get(model, item_id)
        if not item: raise HTTPException(status_code=404, detail="Resource not found")
        guard_formal_mutation(db, item.project_id, model)
        db.delete(item); db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

for path, model, allow_update in [("characters", Character, True), ("world-entities", WorldEntity, True), ("canon", CanonFact, False), ("story-threads", StoryThread, True), ("scenes", Scene, True), ("chapters", Chapter, True), ("story-arcs", StoryArc, True)]:
    project_routes(path, model, allow_update)

@router.patch("/canon/{fact_id}")
def patch_canon(fact_id: str, payload: Payload, db: Session = Depends(get_db)):
    fact = db.get(CanonFact, fact_id)
    if not fact: raise HTTPException(status_code=404, detail="Canon fact not found")
    guard_formal_mutation(db, fact.project_id, CanonFact)
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
    character = db.get(Character, character_id)
    if not character: raise HTTPException(status_code=404, detail="Character not found")
    ensure_replay_not_pending(db, character.project_id)
    return record_dict(create_record(db, CharacterKnowledge, payload.model_dump() | {"character_id": character_id}))

@router.get("/characters/{character_id}/memories")
def character_memories(character_id: str, db: Session = Depends(get_db)):
    if not db.get(Character, character_id): raise HTTPException(status_code=404, detail="Character not found")
    return [record_dict(item) for item in db.scalars(select(CharacterMemory).where(CharacterMemory.character_id == character_id)).all()]

@router.post("/characters/{character_id}/memories", status_code=status.HTTP_201_CREATED)
def create_character_memory(character_id: str, payload: Payload, db: Session = Depends(get_db)):
    character = db.get(Character, character_id)
    if not character: raise HTTPException(status_code=404, detail="Character not found")
    ensure_replay_not_pending(db, character.project_id)
    return record_dict(create_record(db, CharacterMemory, payload.model_dump() | {"character_id": character_id}))


@router.get("/projects/{project_id}/characters/{character_id}/agent")
def character_agent_profile(project_id: str, character_id: str, db: Session = Depends(get_db)):
    character = db.get(Character, character_id)
    if not character or character.project_id != project_id:
        raise HTTPException(status_code=404, detail="Character not found")
    return {"agent_id": character.id, "character_id": character.id, "enabled": character.agent_enabled, "protocol_version": "character-agent-v1", "profile": character.agent_profile or {}, "model_route": "CHARACTER", "model_policy": "project unified generation model"}


@router.patch("/projects/{project_id}/characters/{character_id}/agent")
def patch_character_agent_profile(project_id: str, character_id: str, payload: Payload, db: Session = Depends(get_db)):
    character = db.get(Character, character_id)
    if not character or character.project_id != project_id:
        raise HTTPException(status_code=404, detail="Character not found")
    ensure_replay_not_pending(db, project_id)
    values = payload.model_dump()
    if "enabled" in values:
        character.agent_enabled = bool(values["enabled"])
    if "profile" in values:
        if not isinstance(values["profile"], dict):
            raise HTTPException(status_code=422, detail={"code": "AGENT_PROFILE_INVALID"})
        character.agent_profile = values["profile"]
    db.add(character); db.commit(); db.refresh(character)
    return {"agent_id": character.id, "character_id": character.id, "enabled": character.agent_enabled, "protocol_version": "character-agent-v1", "profile": character.agent_profile or {}, "model_route": "CHARACTER", "model_policy": "project unified generation model"}

@router.get("/projects/{project_id}/characters/{character_id}/mind")
def character_mind_view(project_id: str, character_id: str, proposal_id: str | None = Query(default=None), db: Session = Depends(get_db)):
    """Read-only subjective recall for one character and one explicit Scene context."""
    require_project(db, project_id)
    character = db.get(Character, character_id)
    if not character or character.project_id != project_id:
        raise HTTPException(status_code=404, detail="Character not found")
    proposal = db.get(SceneProposal, proposal_id) if proposal_id else next((item for item in db.scalars(
        select(SceneProposal).where(SceneProposal.project_id == project_id).order_by(SceneProposal.created_at.desc(), SceneProposal.id.desc())
    ).all() if character_id in (item.participants or [])), None)
    if not proposal or proposal.project_id != project_id:
        raise HTTPException(status_code=404, detail="Scene Proposal not found")
    if character_id not in (proposal.participants or []):
        raise HTTPException(status_code=409, detail={"code": "NOT_SCENE_PARTICIPANT", "message": "Character is not a participant in this Scene Proposal."})
    return CharacterMindViewBuilder().build(db, project_id, character_id, proposal)

def context_summary(context: dict[str, Any]) -> dict[str, Any]:
    return {"version": context["version"], "fingerprint": context["fingerprint"], "project": context["project"], "current_sequence": context.get("current_sequence", 0), "current_history_fingerprint": context.get("current_history_fingerprint"), "current_story_arc": context["current_story_arc"], "active_story_threads": context["active_story_threads"], "paused_story_threads": context["paused_story_threads"], "active_characters": context["active_characters"], "recent_scene_count": len(context["recent_scenes"]), "world_entity_count": len(context["world_entities"]), "canon_count": len(context["canon"])}

@router.post("/projects/{project_id}/director/dry-run", status_code=status.HTTP_201_CREATED)
def director_dry_run(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    ensure_replay_not_pending(db, project_id)
    if db.scalar(select(AutonomousWorldRun).where(AutonomousWorldRun.project_id == project_id, AutonomousWorldRun.active.is_(True))):
        raise HTTPException(status_code=409, detail={"code": "AUTONOMY_RUN_ACTIVE"})
    context = DirectorContextBuilder().build(db, project_id)
    gravity_context = StoryGravityContextBuilder().build(db, project_id)
    gravity = StoryGravityEngine().build(gravity_context)
    engine = DirectorCandidateEngine()
    candidates = engine.generate(gravity_context, gravity)
    checker = DirectorConstraintChecker()
    selected = None
    report = None
    for candidate in candidates:
        transient = SceneProposal(project_id=project_id, context_fingerprint=context["fingerprint"], **DirectorProposalFactory().create(project_id, gravity_context, gravity, candidate))
        candidate_report = checker.validate(db, context, transient)
        if candidate_report.valid:
            selected, report = candidate, candidate_report
            break
    if not selected or not report:
        raise HTTPException(status_code=409, detail={"code": "NO_VALID_DIRECTOR_CANDIDATE"})
    proposal = SceneProposal(project_id=project_id, context_fingerprint=context["fingerprint"], **DirectorProposalFactory().create(project_id, gravity_context, gravity, selected))
    proposal.status = ProposalStatus.VALID
    db.add(proposal)
    log = DirectorDecisionLog(project_id=project_id, context_version=context["version"], proposal_id=proposal.id, decision_type=DecisionType.DRY_RUN, brief_reason=proposal.director_reasoning_summary, validation_result=report.as_dict())
    db.add(log); db.commit(); db.refresh(proposal)
    return {"context_summary": context_summary(context), "gravity_summary": gravity.as_dict(), "selected_candidate": selected.as_dict(), "candidate_seeds": [candidate.as_dict() for candidate in engine.top_diverse(candidates)], "proposal": record_dict(proposal), "validation_report": report.as_dict()}

@router.get("/projects/{project_id}/director/gravity")
def director_gravity(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    gravity_context = StoryGravityContextBuilder().build(db, project_id)
    gravity = StoryGravityEngine().build(gravity_context)
    candidates = DirectorCandidateEngine().generate(gravity_context, gravity)
    return {**gravity.as_dict(), "ranked_candidate_seeds": [candidate.as_dict() for candidate in DirectorCandidateEngine().top_diverse(candidates)]}


@router.post("/projects/{project_id}/autonomous-runs", status_code=status.HTTP_201_CREATED)
@router.post("/projects/{project_id}/autonomy/runs", status_code=status.HTTP_201_CREATED)
def create_autonomous_run(project_id: str, payload: AutonomousRunCreatePayload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    try:
        run = AutonomousWorldLoopService().create_run(db, project_id, scene_budget=payload.scene_budget, max_turns_per_scene=payload.max_turns_per_scene, performance_mode=payload.performance_mode.value, resolver_mode=payload.resolver_mode.value, config=payload.config, client_request_id=payload.client_request_id or payload.idempotency_key)
        db.commit(); db.refresh(run)
        return AutonomousWorldLoopService.run_payload(run)
    except LookupError as exc:
        db.rollback(); raise HTTPException(status_code=404, detail={"code": str(exc)}) from exc
    except (ValueError, IntegrityError) as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc


@router.get("/projects/{project_id}/autonomous-runs")
@router.get("/projects/{project_id}/autonomy/runs")
def list_autonomous_runs(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [AutonomousWorldLoopService.run_payload(run) for run in db.scalars(select(AutonomousWorldRun).where(AutonomousWorldRun.project_id == project_id).order_by(AutonomousWorldRun.created_at.desc(), AutonomousWorldRun.id.desc())).all()]


@router.get("/projects/{project_id}/autonomous-runs/{run_id}")
@router.get("/projects/{project_id}/autonomy/runs/{run_id}")
def get_autonomous_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    run = db.get(AutonomousWorldRun, run_id)
    if not run or run.project_id != project_id: raise HTTPException(status_code=404, detail="Autonomous run not found")
    return AutonomousWorldLoopService().get_status(db, run_id)


@router.get("/projects/{project_id}/autonomous-runs/{run_id}/steps")
@router.get("/projects/{project_id}/autonomy/runs/{run_id}/steps")
def list_autonomous_steps(project_id: str, run_id: str, db: Session = Depends(get_db)):
    run = db.get(AutonomousWorldRun, run_id)
    if not run or run.project_id != project_id: raise HTTPException(status_code=404, detail="Autonomous run not found")
    return [AutonomousWorldLoopService.step_payload(step) for step in db.scalars(select(AutonomousWorldStep).where(AutonomousWorldStep.run_id == run_id).order_by(AutonomousWorldStep.ordinal)).all()]


@router.post("/projects/{project_id}/autonomous-runs/{run_id}/advance")
@router.post("/projects/{project_id}/autonomy/runs/{run_id}/advance")
def advance_autonomous_run(project_id: str, run_id: str, payload: Payload, db: Session = Depends(get_db)):
    run = db.get(AutonomousWorldRun, run_id)
    if not run or run.project_id != project_id: raise HTTPException(status_code=404, detail="Autonomous run not found")
    try:
        values = payload.model_dump(); limit = int(values.get("max_scenes", 1)); request_key = str(values.get("idempotency_key", values.get("request_key", "default"))); offset = int(values.get("request_offset", 0))
        result = AutonomousWorldLoopService().advance(db, run_id, max_scenes=limit, request_key=request_key, request_offset=offset); db.commit(); return result
    except LookupError as exc:
        db.rollback(); raise HTTPException(status_code=404, detail={"code": str(exc)}) from exc
    except ValueError as exc:
        db.rollback(); raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc


@router.post("/projects/{project_id}/autonomous-runs/{run_id}/pause")
@router.post("/projects/{project_id}/autonomy/runs/{run_id}/pause")
def pause_autonomous_run(project_id: str, run_id: str, payload: Payload | None = None, db: Session = Depends(get_db)):
    run = db.get(AutonomousWorldRun, run_id)
    if not run or run.project_id != project_id: raise HTTPException(status_code=404, detail="Autonomous run not found")
    result = AutonomousWorldLoopService().pause(db, run_id, str((payload.model_dump() if payload else {}).get("reason", "USER_PAUSED"))); db.commit(); return AutonomousWorldLoopService.run_payload(result)


@router.post("/projects/{project_id}/autonomous-runs/{run_id}/resume")
@router.post("/projects/{project_id}/autonomy/runs/{run_id}/resume")
def resume_autonomous_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    run = db.get(AutonomousWorldRun, run_id)
    if not run or run.project_id != project_id: raise HTTPException(status_code=404, detail="Autonomous run not found")
    try:
        result = AutonomousWorldLoopService().resume(db, run_id); db.commit(); return AutonomousWorldLoopService.run_payload(result)
    except ValueError as exc:
        if str(exc) == "AUTONOMY_BASELINE_CHANGED":
            # The service has intentionally persisted a terminal baseline
            # transition; do not erase it while mapping to HTTP 409.
            db.commit()
        else:
            db.rollback()
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc


@router.post("/projects/{project_id}/autonomous-runs/{run_id}/cancel")
@router.post("/projects/{project_id}/autonomy/runs/{run_id}/cancel")
def cancel_autonomous_run(project_id: str, run_id: str, db: Session = Depends(get_db)):
    run = db.get(AutonomousWorldRun, run_id)
    if not run or run.project_id != project_id: raise HTTPException(status_code=404, detail="Autonomous run not found")
    result = AutonomousWorldLoopService().cancel(db, run_id); db.commit(); return AutonomousWorldLoopService.run_payload(result)

@router.post("/projects/{project_id}/director/ai-dry-run")
def director_ai_dry_run(project_id: str, db: Session = Depends(get_db)):
    """Ask an optional model for a candidate only; no proposal or world write occurs."""
    require_project(db, project_id)
    ensure_replay_not_pending(db, project_id)
    context = DirectorContextBuilder().build(db, project_id)
    gravity_context = StoryGravityContextBuilder().build(db, project_id)
    gravity = StoryGravityEngine().build(gravity_context)
    settings = get_settings(); route = ModelRouter().resolve(db, project_id, settings, "DIRECTOR")
    trace = ExecutionTraceRecorder().start(db, project_id=project_id, stage=ExecutionStage.DIRECTOR, source_type="DIRECTOR", source_id=project_id, provider=route.provider, model=route.model, input_fingerprint=context["fingerprint"])
    try:
        provider = routed_provider(settings, route, db, project_id)
        candidate = LLMDirectorCandidateGenerator().generate(provider, route.model, gravity_context, gravity)
        errors = LLMDirectorCandidateGenerator().validate_references(candidate, gravity_context)
        if errors:
            ExecutionTraceRecorder().block(trace, errors[0], validation_report={"issues": [{"code": error} for error in errors]}); db.commit()
            raise HTTPException(status_code=409, detail={"code": errors[0]})
        ExecutionTraceRecorder().succeed(trace, output_fingerprint=stable_fingerprint(candidate.model_dump(mode="json"))); db.commit()
        return {"candidate": candidate.model_dump(mode="json"), "gravity_fingerprint": gravity.gravity_fingerprint, "context_fingerprint": context["fingerprint"], "authority": "CANDIDATE_ONLY"}
    except HTTPException:
        raise
    except ModelProviderError as exc:
        ExecutionTraceRecorder().fail(trace, exc.code, upstream_status=exc.upstream_status); db.commit()
        raise HTTPException(status_code={MODEL_AUTH_FAILED: 503, MODEL_RATE_LIMITED: 429, MODEL_TIMEOUT: 504}.get(exc.code, 502), detail={"code": exc.code}) from exc
    except ValueError as exc:
        ExecutionTraceRecorder().block(trace, MODEL_OUTPUT_INVALID, validation_report={"code": str(exc)}); db.commit()
        raise HTTPException(status_code=502, detail={"code": MODEL_OUTPUT_INVALID}) from exc

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
    ensure_autonomous_run_idle(db, project_id)
    ensure_replay_not_pending(db, project_id)
    proposal = db.get(SceneProposal, proposal_id)
    if not proposal or proposal.project_id != project_id: raise HTTPException(status_code=404, detail="Scene Proposal not found")
    context = DirectorContextBuilder().build(db, project_id)
    if proposal.context_fingerprint != context["fingerprint"]:
        raise HTTPException(status_code=409, detail={"code": "STALE_PROPOSAL", "message": "World state changed after this proposal was generated. Run Director again."})
    gravity = StoryGravityEngine().build(StoryGravityContextBuilder().build(db, project_id))
    proposal_gravity = (proposal.entry_state or {}).get("director_meta", {}).get("gravity_fingerprint")
    if proposal_gravity and proposal_gravity != gravity.gravity_fingerprint:
        raise HTTPException(status_code=409, detail={"code": "STALE_STORY_GRAVITY", "message": "Story Gravity changed after this proposal was generated. Run Director again."})
    report = DirectorConstraintChecker().validate(db, context, proposal)
    if not report.valid: raise HTTPException(status_code=409, detail={"message": "Blocking validation issues prevent approval.", "validation_report": report.as_dict()})
    proposal.status = ProposalStatus.APPROVED; db.add(proposal); db.add(DirectorDecisionLog(project_id=project_id, context_version=context["version"], proposal_id=proposal.id, decision_type=DecisionType.APPROVE, brief_reason="Proposal approved after constraint validation.", validation_result=report.as_dict())); db.commit(); db.refresh(proposal)
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
    ensure_replay_not_pending(db, project_id)
    proposal = db.get(SceneProposal, proposal_id)
    character = db.get(Character, character_id)
    if not proposal or proposal.project_id != project_id: raise HTTPException(status_code=404, detail="Scene Proposal not found")
    if not character or character.project_id != project_id: raise HTTPException(status_code=404, detail="Character not found")
    if not character.agent_enabled: raise HTTPException(status_code=409, detail={"code": "CHARACTER_AGENT_DISABLED", "message": "This character's independent Agent is disabled."})
    if character_id not in proposal.participants: raise HTTPException(status_code=409, detail={"code": "NOT_SCENE_PARTICIPANT", "message": "Character is not a participant in this Scene Proposal."})
    director_context = DirectorContextBuilder().build(db, project_id)
    if proposal.context_fingerprint != director_context["fingerprint"]: raise HTTPException(status_code=409, detail={"code": "STALE_SCENE_PROPOSAL", "message": "Scene Proposal is stale. Run Director again."})
    return proposal, character


@router.get("/projects/{project_id}/characters/{character_id}/agent/context")
def character_agent_context(project_id: str, character_id: str, proposal_id: str, db: Session = Depends(get_db)):
    """Return exactly the redacted view that the character Agent may receive."""
    proposal, character = require_character_simulation_inputs(db, project_id, proposal_id, character_id)
    context = CharacterContextBuilder().build(db, project_id, character_id, proposal)
    actor_view = ActorPerceptionSanitizer().sanitize(context)
    return {"agent_id": character.id, "proposal_id": proposal.id, "context_fingerprint": context["fingerprint"], "actor_view": actor_view, "redaction_policy": {"director_only": "removed", "secrets": "never included", "unknown_canon": "opaque IDs only", "world_mutation": "forbidden"}}

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
        trace = ExecutionTraceRecorder().start(db, project_id=project_id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="CHARACTER", source_id=character.id, provider=route.provider, model=route.model, input_fingerprint=context["fingerprint"])
        payload, model_result = LLMCharacterActor(routed_provider(settings, route, db, project_id), route.model).decide(actor_view)
    except ModelProviderError as exc:
        if trace: (ExecutionTraceRecorder().block if exc.code == MODEL_OUTPUT_INVALID else ExecutionTraceRecorder().fail)(trace, exc.code, upstream_status=exc.upstream_status)
        db.commit()
        status_code = {MODEL_AUTH_FAILED: 503, MODEL_RATE_LIMITED: 429, MODEL_TIMEOUT: 504}.get(exc.code, 502)
        raise HTTPException(status_code=status_code, detail={"code": exc.code, "upstream_status": exc.upstream_status, "message": "Character model request could not be completed."}) from exc
    decision = CharacterDecision(project_id=project_id, scene_proposal_id=proposal.id, character_id=character.id, context_fingerprint=context["fingerprint"], **payload)
    report = CharacterDecisionConstraintChecker().validate(db, context, decision)
    decision.status = CharacterDecisionStatus.VALID if report.valid else CharacterDecisionStatus.REJECTED
    if trace:
        if report.valid: ExecutionTraceRecorder().succeed(trace, latency_ms=model_result.latency_ms, request_id=model_result.request_id, output_fingerprint=stable_fingerprint(payload))
        else:
            code = primary_issue(report); safe_report = report.as_dict()
            ExecutionTraceRecorder().block(trace, code, validation_report=safe_report, latency_ms=model_result.latency_ms, request_id=model_result.request_id)
            RecoveryCandidateService().create(db, project_id=project_id, trace=trace, candidate_type="CHARACTER_DECISION", payload=payload, context_fingerprint=context["fingerprint"], locator={"project_id": project_id, "proposal_id": proposal.id, "character_id": character.id}, error_code=code, validation_report=safe_report, stage=ExecutionStage.CHARACTER_ACTOR.value, source_type="CHARACTER", source_id=character.id)
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
    if revision.status == RevisionStatus.APPLIED: raise HTTPException(status_code=409, detail={"code":"REVISION_ALREADY_APPLIED"})
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
    if revision.status == RevisionStatus.APPLIED: raise HTTPException(status_code=409, detail={"code":"REVISION_ALREADY_APPLIED"})
    if revision.status == RevisionStatus.CANCELLED: return record_dict(revision)
    revision.status = RevisionStatus.CANCELLED; db.add(revision); db.commit(); db.refresh(revision)
    return record_dict(revision)

@router.post("/projects/{project_id}/snapshots", status_code=status.HTTP_201_CREATED)
def create_snapshot(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db,project_id)
    try: kind=SnapshotType(payload.model_dump().get("snapshot_type","BASELINE"))
    except ValueError: raise HTTPException(status_code=400,detail={"code":"INVALID_SNAPSHOT_TYPE"})
    if kind != SnapshotType.BASELINE: raise HTTPException(status_code=409,detail={"code":"SYSTEM_SNAPSHOT_TYPE_FORBIDDEN"})
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
    ensure_autonomous_run_idle(db, project_id)
    ensure_replay_not_pending(db, project_id)
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
            ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.REVISION_APPLY, source_type="WORLD_REVISION", source_id=revision_id, status=ExecutionStatus.BLOCKED, error_code="REVISION_STALE")
            db.commit()
        else:
            revision = db.get(WorldRevision, revision_id)
            ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.REVISION_APPLY, source_type="WORLD_REVISION", source_id=revision_id, status=ExecutionStatus.BLOCKED, error_code=str(exc), input_fingerprint=getattr(revision, "base_state_fingerprint", None))
            db.commit()
        raise HTTPException(status_code=409, detail={"code": str(exc), "requires_repreview": str(exc) == "TARGET_STATE_STALE"}) from exc
    try:
        with db.begin_nested(): app=service.apply(db,project_id,revision,override,reason,prepared)
        ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.REVISION_APPLY, source_type="WORLD_REVISION", source_id=revision.id, status=ExecutionStatus.SUCCEEDED, input_fingerprint=revision.base_state_fingerprint, output_fingerprint=db.get(WorldSnapshot, app.post_snapshot_id).state_fingerprint)
        db.commit(); db.refresh(app); return {"application":record_dict(app),"revision":record_dict(revision),"impact_report":revision.impact_report,"pending_retcon":bool(revision.impact_report.get("summary",{}).get("high") or revision.impact_report.get("summary",{}).get("critical") or revision.impact_report.get("summary",{}).get("manual_review"))}
    except ValueError as exc:
        db.rollback()
        ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.REVISION_APPLY, source_type="WORLD_REVISION", source_id=revision_id, status=ExecutionStatus.FAILED, error_code=str(exc))
        db.commit()
        raise HTTPException(status_code=409,detail={"code":str(exc)}) from exc
@router.post("/projects/{project_id}/revision-applications/{application_id}/rollback")
def rollback_revision(project_id:str,application_id:str,db:Session=Depends(get_db)):
    app=db.get(RevisionApplication,application_id)
    if not app or app.project_id!=project_id: raise HTTPException(status_code=404,detail="Revision Application not found")
    if str(app.status.value if hasattr(app.status,"value") else app.status)!="APPLIED": raise HTTPException(status_code=409,detail={"code":"APPLICATION_NOT_APPLIED"})
    try:
        with db.begin_nested(): RevisionApplyService().rollback(db,project_id,app)
        rollback_snapshot = db.scalar(select(WorldSnapshot).where(WorldSnapshot.project_id == project_id, WorldSnapshot.snapshot_type == SnapshotType.ROLLBACK_POINT).order_by(WorldSnapshot.created_at.desc(), WorldSnapshot.id.desc()))
        ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.REVISION_ROLLBACK, source_type="REVISION_APPLICATION", source_id=app.id, status=ExecutionStatus.SUCCEEDED, output_fingerprint=rollback_snapshot.state_fingerprint if rollback_snapshot else None)
        db.commit(); db.refresh(app); return record_dict(app)
    except ValueError as exc:
        db.rollback()
        ExecutionTraceRecorder().create(db, project_id=project_id, stage=ExecutionStage.REVISION_ROLLBACK, source_type="REVISION_APPLICATION", source_id=application_id, status=ExecutionStatus.BLOCKED, error_code=str(exc))
        db.commit()
        raise HTTPException(status_code=409,detail={"code":str(exc)}) from exc
@router.get("/projects/{project_id}/model-config")
def get_model_config(project_id:str,db:Session=Depends(get_db)):
    require_project(db,project_id); item=db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id==project_id)); return _model_config_payload(db, item)

@router.get("/projects/{project_id}/model-config/usage")
def get_model_config_usage(project_id: str, limit: int = Query(default=40, ge=1, le=200), db: Session = Depends(get_db)):
    """Return safe, aggregate model-call telemetry without credentials or prompt content."""
    require_project(db, project_id)
    rows = db.scalars(select(ExecutionTrace).where(ExecutionTrace.project_id == project_id).order_by(ExecutionTrace.created_at.desc(), ExecutionTrace.id.desc()).limit(limit)).all()
    by_role: dict[str, dict[str, Any]] = {}
    for row in rows:
        role = str(row.stage.value if hasattr(row.stage, "value") else row.stage)
        bucket = by_role.setdefault(role, {"calls": 0, "succeeded": 0, "failed": 0, "blocked": 0, "latency_ms_total": 0, "last_error": None})
        bucket["calls"] += 1
        status_value = str(row.status.value if hasattr(row.status, "value") else row.status)
        if status_value == "SUCCEEDED": bucket["succeeded"] += 1
        elif status_value == "FAILED": bucket["failed"] += 1
        elif status_value == "BLOCKED": bucket["blocked"] += 1
        bucket["latency_ms_total"] += row.latency_ms or 0
        if row.error_code and bucket["last_error"] is None: bucket["last_error"] = row.error_code
    for bucket in by_role.values():
        bucket["latency_ms_avg"] = round(bucket["latency_ms_total"] / max(1, bucket["calls"]))
    return {"summary": by_role, "recent": [_trace_payload(row, db) for row in rows]}
@router.put("/projects/{project_id}/model-config")
def put_model_config(project_id:str,payload:Payload,db:Session=Depends(get_db)):
    require_project(db,project_id)
    try: values=ProjectModelConfigPayload.model_validate(payload.model_dump()).model_dump()
    except Exception as exc: raise HTTPException(status_code=400,detail={"code":"INVALID_MODEL_CONFIG"}) from exc
    item=db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id==project_id))
    api_key, embedding_key = values.pop("api_key", None), values.pop("embedding_api_key", None)
    clear_api, clear_embedding = values.pop("clear_api_key", False), values.pop("clear_embedding_api_key", False)
    if item:
        for key, value in values.items():
            if key not in {"id", "project_id", "created_at", "updated_at"}: setattr(item, key, value)
    else:
        item = ProjectModelConfig(project_id=project_id, **values); db.add(item)
    db.flush()
    import os
    vault_key = os.getenv("AI_CREDENTIAL_MASTER_KEY")
    if api_key is not None or clear_api: _update_provider_credential(db, project_id, ProviderCredentialPurpose.GENERATION, api_key, clear_api, vault_key)
    if embedding_key is not None or clear_embedding: _update_provider_credential(db, project_id, ProviderCredentialPurpose.EMBEDDING, embedding_key, clear_embedding, vault_key)
    db.commit(); db.refresh(item); return _model_config_payload(db, item)


def _model_config_payload(db, item):
    if not item: return None
    value = record_dict(item)
    for secret in ("api_key", "embedding_api_key", "secret_ciphertext"):
        value.pop(secret, None)
    credentials = db.scalars(select(ProjectProviderCredential).where(ProjectProviderCredential.project_id == item.project_id)).all()
    value["credentials"] = {
        record.purpose.value: {"configured": True, "hint": record.secret_hint, "fingerprint": record.secret_fingerprint}
        for record in credentials
    }
    return value


def _update_provider_credential(db, project_id, purpose, secret, clear, master_key):
    from .embeddings import credential_fingerprint
    current = db.scalar(select(ProjectProviderCredential).where(ProjectProviderCredential.project_id == project_id, ProjectProviderCredential.purpose == purpose))
    if clear:
        if current: db.delete(current)
        return
    if secret is None: return
    try: ciphertext = CredentialVault(master_key).encrypt(secret)
    except ValueError as exc: raise HTTPException(status_code=400, detail={"code": str(exc)}) from exc
    value = {"project_id": project_id, "purpose": purpose, "secret_ciphertext": ciphertext, "secret_fingerprint": credential_fingerprint(secret), "secret_hint": CredentialVault.hint(secret)}
    if current:
        for key, item in value.items():
            if key not in {"id", "project_id", "created_at", "updated_at"}:
                setattr(current, key, item)
    else:
        db.add(ProjectProviderCredential(**value))
    db.flush()


@router.post("/projects/{project_id}/model-config/test-embedding")
def test_embedding_config(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    values = payload.model_dump(); settings = get_settings()
    try:
        draft_provider = values.get("provider") or values.get("embedding_provider")
        draft_url = values.get("base_url") or values.get("embedding_base_url")
        draft_model = values.get("model") or values.get("embedding_model")
        draft_key = values.get("api_key") or values.get("embedding_api_key")
        use_main_connection = values.get("embedding_use_main_connection", values.get("use_main_connection", True))
        if not draft_key:
            if use_main_connection:
                draft_key = ProviderCredentialResolver().generation_key(db, project_id, settings)
            else:
                credential = db.scalar(select(ProjectProviderCredential).where(ProjectProviderCredential.project_id == project_id, ProjectProviderCredential.purpose == ProviderCredentialPurpose.EMBEDDING))
                if credential:
                    import os
                    draft_key = CredentialVault(os.getenv("AI_CREDENTIAL_MASTER_KEY")).decrypt(credential.secret_ciphertext)
                elif settings.ai_embedding_api_key:
                    draft_key = settings.ai_embedding_api_key.get_secret_value()
        if draft_provider and draft_url and draft_model:
            from .embeddings import _fingerprint
            expected_dimension = values.get("dimension") or values.get("embedding_dimension")
            route = EmbeddingRoute(True, draft_provider, draft_url, draft_model, int(expected_dimension or 0), draft_key, "REQUEST" if draft_key else "ENV", _fingerprint((draft_provider, draft_url.rstrip("/").casefold(), draft_model, expected_dimension), "character-memory-embedding-config-v1"))
        else:
            route = EmbeddingRouter().resolve(db, project_id, settings)
        if values.get("model"): route = route.__class__(route.enabled, route.provider, route.base_url, values["model"], route.dimension, values.get("api_key") or route.api_key, "REQUEST" if values.get("api_key") else route.credential_source, route.embedding_config_fingerprint)
        if not route.api_key: raise ValueError("EMBEDDING_CONFIG_INCOMPLETE")
        provider = OpenAICompatibleEmbeddingProvider(route.base_url, route.api_key or "")
        result = provider.embed([values.get("test_text") or values.get("text") or "AI Narrative World Engine embedding test"], route.model)
        if route.dimension and result.dimension != route.dimension: raise ValueError("EMBEDDING_DIMENSION_MISMATCH")
        return {"ok": True, "provider": result.provider, "model": result.model, "dimension": result.dimension, "latency_ms": result.latency_ms, "request_id": result.request_id}
    except Exception as exc:
        code = embedding_error_code(exc, "EMBEDDING_TEST_FAILED")
        safe_codes = {"MODEL_TIMEOUT", "MODEL_RATE_LIMITED", "MODEL_AUTH_FAILED", "MODEL_UPSTREAM_ERROR", "EMBEDDING_OUTPUT_INVALID", "EMBEDDING_CONFIG_INCOMPLETE", "EMBEDDING_DIMENSION_MISMATCH", "MODEL_CREDENTIAL_VAULT_NOT_CONFIGURED", "MODEL_CREDENTIAL_INVALID", "EMBEDDING_TEST_FAILED"}
        if code not in safe_codes: code = "EMBEDDING_TEST_FAILED"
        raise HTTPException(status_code=409, detail={"code": code}) from exc


@router.post("/projects/{project_id}/model-config/test-generation")
def test_generation_config(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    """Test one OpenAI-compatible generation route without mutating project state."""
    require_project(db, project_id)
    values = payload.model_dump()
    settings = get_settings()
    provider_name = values.get("provider") or settings.ai_provider
    base_url = values.get("base_url") or settings.ai_base_url
    model = values.get("model") or values.get("writer_model") or settings.ai_writer_model
    api_key = values.get("api_key") or ProviderCredentialResolver().generation_key(db, project_id, settings)
    try:
        if not provider_name or not base_url or not model or not api_key:
            raise ValueError("MODEL_PROVIDER_NOT_CONFIGURED")
        provider = get_model_provider(settings, provider_name, base_url, api_key)
        result = provider.generate(
            [
                {"role": "system", "content": "Return only a compact JSON object with the key ok set to true."},
                {"role": "user", "content": values.get("test_prompt") or '{"phase":"model_gateway","ok":true}'},
            ],
            model,
        )
        return {"ok": True, "provider": result.provider, "model": result.model, "chars": len(result.content), "latency_ms": result.latency_ms, "request_id": result.request_id}
    except ModelProviderError as exc:
        safe_codes = {MODEL_TIMEOUT, MODEL_RATE_LIMITED, MODEL_AUTH_FAILED, "MODEL_PROVIDER_NOT_CONFIGURED", "MODEL_PROVIDER_UNSUPPORTED", "MODEL_UPSTREAM_ERROR", MODEL_OUTPUT_INVALID}
        code = exc.code if exc.code in safe_codes else "MODEL_UPSTREAM_ERROR"
        raise HTTPException(status_code=409, detail={"code": code}) from exc

@router.post("/model-config/test-generation")
def test_generation_config_preview(payload: Payload):
    """Test a model connection before an author creates a project."""
    values = payload.model_dump()
    settings = get_settings()
    provider_name = values.get("provider") or settings.ai_provider
    base_url = values.get("base_url") or settings.ai_base_url
    model = values.get("model") or settings.ai_writer_model
    api_key = values.get("api_key") or (settings.ai_api_key.get_secret_value() if settings.ai_api_key else None)
    try:
        if not provider_name or not base_url or not model or not api_key:
            raise ValueError("MODEL_PROVIDER_NOT_CONFIGURED")
        provider = get_model_provider(settings, provider_name, base_url, api_key)
        result = provider.generate(
            [
                {"role": "system", "content": "Return only a compact JSON object with the key ok set to true."},
                {"role": "user", "content": values.get("test_prompt") or '{"phase":"model_gateway_preview","ok":true}'},
            ],
            model,
        )
        return {"ok": True, "provider": result.provider, "model": result.model, "chars": len(result.content), "latency_ms": result.latency_ms, "request_id": result.request_id}
    except ModelProviderError as exc:
        safe_codes = {MODEL_TIMEOUT, MODEL_RATE_LIMITED, MODEL_AUTH_FAILED, "MODEL_PROVIDER_NOT_CONFIGURED", "MODEL_PROVIDER_UNSUPPORTED", "MODEL_UPSTREAM_ERROR", MODEL_OUTPUT_INVALID}
        code = exc.code if exc.code in safe_codes else "MODEL_UPSTREAM_ERROR"
        raise HTTPException(status_code=409, detail={"code": code}) from exc
    except ValueError as exc:
        code = str(exc) if str(exc) in {"MODEL_PROVIDER_NOT_CONFIGURED", "MODEL_PROVIDER_UNSUPPORTED"} else "MODEL_PROVIDER_NOT_CONFIGURED"
        raise HTTPException(status_code=409, detail={"code": code}) from exc


def _safe_embedding_error(exc: Exception) -> str:
    code = embedding_error_code(exc, "EMBEDDING_INDEX_FAILED")
    allowed = {"MODEL_TIMEOUT", "MODEL_RATE_LIMITED", "MODEL_AUTH_FAILED", "MODEL_UPSTREAM_ERROR", "EMBEDDING_OUTPUT_INVALID", "EMBEDDING_CONFIG_INCOMPLETE", "EMBEDDING_DIMENSION_MISMATCH", "MODEL_CREDENTIAL_INVALID", "EMBEDDING_INDEX_FAILED"}
    return code if code in allowed else "EMBEDDING_INDEX_FAILED"


@router.post("/projects/{project_id}/memory-embeddings/index-missing")
def index_missing_memory_embeddings(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    try: result = MemoryEmbeddingIndexService().index_memories(db, project_id); db.commit(); return result
    except Exception as exc: db.rollback(); raise HTTPException(status_code=409, detail={"code": _safe_embedding_error(exc)}) from exc


@router.post("/projects/{project_id}/memory-embeddings/rebuild")
def rebuild_memory_embeddings(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    try: result = MemoryEmbeddingIndexService().index_memories(db, project_id, rebuild=True); db.commit(); return result
    except Exception as exc: db.rollback(); raise HTTPException(status_code=409, detail={"code": _safe_embedding_error(exc)}) from exc


@router.get("/projects/{project_id}/memory-embeddings/status")
def memory_embedding_status(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    from .embeddings import MemoryEmbeddingIndexService
    try:
        result = MemoryEmbeddingIndexService().status(db, project_id)
        config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == project_id))
        result["memory_retrieval_mode"] = serialize(config.memory_retrieval_mode) if config else "DETERMINISTIC"
        return result
    except Exception as exc:
        raise HTTPException(status_code=409, detail={"code": _safe_embedding_error(exc)}) from exc
def _trace_payload(trace, db=None):
    value = record_dict(trace)
    candidate = db.scalar(select(RecoveryCandidate).where(RecoveryCandidate.source_trace_id == trace.id)) if db else None
    value["recovery_candidate_id"] = candidate.id if candidate else None
    value["candidate_status"] = candidate.status if candidate else None
    config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == trace.project_id)) if db else None
    attempts = db.scalar(select(func.count(ExecutionTrace.id)).where(ExecutionTrace.stage == ExecutionStage.REPAIR, ExecutionTrace.source_id == (candidate.id if candidate else ""))) if db else 0
    value["available_actions"] = RecoveryActionResolver.resolve(trace.error_code, candidate, attempts or 0, config.max_repair_attempts if config else 1)
    return value

@router.get("/projects/{project_id}/execution-traces")
def list_execution_traces(project_id:str,stage:str|None=None,status_filter:str|None=None,status_value:str|None=Query(None,alias="status"),source_type:str|None=None,source_id:str|None=None,limit:int=50,db:Session=Depends(get_db)):
    query=select(ExecutionTrace).where(ExecutionTrace.project_id==project_id)
    if stage: query=query.where(ExecutionTrace.stage==stage)
    if status_value or status_filter: query=query.where(ExecutionTrace.status==(status_value or status_filter))
    if source_type: query=query.where(ExecutionTrace.source_type==source_type)
    if source_id: query=query.where(ExecutionTrace.source_id==source_id)
    return [_trace_payload(x, db) for x in db.scalars(query.order_by(ExecutionTrace.created_at.desc(),ExecutionTrace.id).limit(min(max(limit, 1), 200))).all()]
@router.get("/projects/{project_id}/execution-traces/{trace_id}")
def get_execution_trace(project_id:str,trace_id:str,db:Session=Depends(get_db)):
    item=db.get(ExecutionTrace,trace_id)
    if not item or item.project_id!=project_id: raise HTTPException(status_code=404,detail="Execution Trace not found")
    return _trace_payload(item, db)

def _candidate_or_404(db, project_id, candidate_id):
    candidate = db.get(RecoveryCandidate, candidate_id)
    if not candidate or candidate.project_id != project_id:
        raise HTTPException(status_code=404, detail="Recovery Candidate not found")
    return candidate

def _candidate_payload(db, candidate):
    version = RecoveryCandidateService().current_version(db, candidate)
    source_trace = db.get(ExecutionTrace, candidate.source_trace_id)
    config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == candidate.project_id)); attempts = db.scalar(select(func.count(ExecutionTrace.id)).where(ExecutionTrace.stage == ExecutionStage.REPAIR, ExecutionTrace.source_id == candidate.id)) or 0
    value = {"candidate": record_dict(candidate), "current_version": record_dict(version), "validation_report": version.validation_report, "available_actions": RecoveryActionResolver.resolve(source_trace.error_code if source_trace else candidate.initial_error_code, candidate, attempts, config.max_repair_attempts if config else 1), "recovery_candidate_id": candidate.id}
    return value

@router.get("/projects/{project_id}/recovery-candidates")
def list_recovery_candidates(project_id: str, status_filter: str | None = None, candidate_type: str | None = None, source_trace_id: str | None = None, db: Session = Depends(get_db)):
    query = select(RecoveryCandidate).where(RecoveryCandidate.project_id == project_id)
    if status_filter: query = query.where(RecoveryCandidate.status == status_filter)
    if candidate_type: query = query.where(RecoveryCandidate.candidate_type == candidate_type)
    if source_trace_id: query = query.where(RecoveryCandidate.source_trace_id == source_trace_id)
    return [_candidate_payload(db, item) for item in db.scalars(query.order_by(RecoveryCandidate.created_at.desc(), RecoveryCandidate.id)).all()]

@router.get("/projects/{project_id}/recovery-candidates/{candidate_id}")
def get_recovery_candidate(project_id: str, candidate_id: str, db: Session = Depends(get_db)):
    return _candidate_payload(db, _candidate_or_404(db, project_id, candidate_id))

@router.get("/projects/{project_id}/recovery-candidates/{candidate_id}/versions")
def list_recovery_versions(project_id: str, candidate_id: str, db: Session = Depends(get_db)):
    candidate = _candidate_or_404(db, project_id, candidate_id)
    return [record_dict(item) for item in db.scalars(select(RecoveryCandidateVersion).where(RecoveryCandidateVersion.candidate_id == candidate.id).order_by(RecoveryCandidateVersion.version_number)).all()]

@router.post("/projects/{project_id}/recovery-candidates/{candidate_id}/edit")
def edit_recovery_candidate(project_id: str, candidate_id: str, payload: RecoveryEditPayload, db: Session = Depends(get_db)):
    candidate = _candidate_or_404(db, project_id, candidate_id)
    if candidate.initial_error_code == "WORLD_INFORMATION_MISSING": raise HTTPException(status_code=409, detail={"code": "WORLD_FACT_REQUIRED"})
    if candidate.status in {RecoveryCandidateStatus.ADOPTED.value, RecoveryCandidateStatus.ABORTED.value, RecoveryCandidateStatus.STALE.value}: raise HTTPException(status_code=409, detail={"code": "RECOVERY_CANDIDATE_NOT_EDITABLE"})
    try: version = RecoveryCandidateService().edit(db, candidate, payload.base_version, payload.changes)
    except ValueError as exc: raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    db.commit(); db.refresh(candidate); db.refresh(version); return _candidate_payload(db, candidate)

@router.post("/projects/{project_id}/recovery-candidates/{candidate_id}/ai-repair")
def repair_recovery_candidate(project_id: str, candidate_id: str, db: Session = Depends(get_db)):
    candidate = _candidate_or_404(db, project_id, candidate_id)
    if candidate.initial_error_code == "WORLD_INFORMATION_MISSING": raise HTTPException(status_code=409, detail={"code": "WORLD_FACT_REQUIRED"})
    if candidate.status != RecoveryCandidateStatus.OPEN.value: raise HTTPException(status_code=409, detail={"code": "RECOVERY_CANDIDATE_NOT_OPEN"})
    config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == project_id)); max_attempts = config.max_repair_attempts if config else 1
    previous_repairs = db.scalar(select(func.count(ExecutionTrace.id)).where(ExecutionTrace.project_id == project_id, ExecutionTrace.stage == ExecutionStage.REPAIR, ExecutionTrace.source_id == candidate.id)) or 0
    if max_attempts <= 0 or previous_repairs >= max_attempts: raise HTTPException(status_code=409, detail={"code": "REPAIR_ATTEMPT_LIMIT"})
    service = RecoveryCandidateService()
    try:
        context, sanitized = service.safe_rebuild(db, candidate)
    except RecoveryContextStaleError as exc:
        candidate.status = RecoveryCandidateStatus.STALE.value; db.commit()
        raise HTTPException(status_code=409, detail={"code": "RECOVERY_CONTEXT_STALE"}) from exc
    if context.get("fingerprint") != candidate.context_fingerprint:
        candidate.status = RecoveryCandidateStatus.STALE.value; db.commit(); raise HTTPException(status_code=409, detail={"code": "RECOVERY_CONTEXT_STALE"})
    current = service.current_version(db, candidate); parent = db.get(ExecutionTrace, candidate.source_trace_id)
    last_repair = db.scalar(select(ExecutionTrace).where(ExecutionTrace.project_id == project_id, ExecutionTrace.stage == ExecutionStage.REPAIR, ExecutionTrace.source_id == candidate.id).order_by(ExecutionTrace.created_at.desc(), ExecutionTrace.id.desc()))
    settings = get_settings(); route = ModelRouter().resolve(db, project_id, settings, "REPAIR")
    trace = ExecutionTraceRecorder().start(db, project_id=project_id, stage=ExecutionStage.REPAIR, source_type="RECOVERY_CANDIDATE", source_id=candidate.id, provider=route.provider, model=route.model, input_fingerprint=candidate.context_fingerprint, attempt_number=previous_repairs + 1, parent_trace_id=(last_repair.id if last_repair else parent.id))
    try:
        _, repaired, result = CandidateRepairAgent().repair(routed_provider(settings, route, db, project_id), route.model, candidate.candidate_type, sanitized, current.payload, current.validation_report)
        safe_payload = service._payload(candidate.candidate_type, repaired)
    except ModelProviderError as exc:
        ExecutionTraceRecorder().fail(trace, exc.code, upstream_status=exc.upstream_status); db.commit(); raise HTTPException(status_code={MODEL_AUTH_FAILED: 503, MODEL_RATE_LIMITED: 429, MODEL_TIMEOUT: 504}.get(exc.code, 502), detail={"code": exc.code, "upstream_status": exc.upstream_status}) from exc
    except Exception as exc:
        ExecutionTraceRecorder().block(trace, MODEL_OUTPUT_INVALID, validation_report={"issues": [{"path": "$", "message": "Repair output did not match the candidate contract."}]}); db.commit(); raise HTTPException(status_code=502, detail={"code": MODEL_OUTPUT_INVALID}) from exc
    validation = service.validate(db, candidate, safe_payload)
    valid, report = validation.constraint_valid, validation.validation_report
    if validation.context_stale:
        ExecutionTraceRecorder().block(trace, "RECOVERY_CONTEXT_STALE", validation_report=report); db.commit(); raise HTTPException(status_code=409, detail={"code": "RECOVERY_CONTEXT_STALE"})
    number = candidate.current_version_number + 1
    version = RecoveryCandidateVersion(candidate_id=candidate.id, version_number=number, origin=RecoveryVersionOrigin.AI_REPAIR.value, parent_version_id=current.id, payload=safe_payload, payload_fingerprint=target_fingerprint(safe_payload), schema_valid=True, constraint_valid=valid, validation_report=report, repair_trace_id=trace.id)
    db.add(version); candidate.current_version_number = number; candidate.status = RecoveryCandidateStatus.VALIDATED.value if valid else RecoveryCandidateStatus.OPEN.value
    if valid: ExecutionTraceRecorder().succeed(trace, latency_ms=result.latency_ms, request_id=result.request_id, output_fingerprint=stable_fingerprint(safe_payload))
    else: ExecutionTraceRecorder().block(trace, primary_issue(report), validation_report=report, latency_ms=result.latency_ms, request_id=result.request_id)
    db.commit(); db.refresh(candidate); return _candidate_payload(db, candidate)

@router.post("/projects/{project_id}/recovery-candidates/{candidate_id}/abort")
def abort_recovery_candidate(project_id: str, candidate_id: str, db: Session = Depends(get_db)):
    candidate = _candidate_or_404(db, project_id, candidate_id)
    if candidate.status == RecoveryCandidateStatus.ADOPTED.value: raise HTTPException(status_code=409, detail={"code": "RECOVERY_ALREADY_ADOPTED"})
    candidate.status = RecoveryCandidateStatus.ABORTED.value; db.commit(); return _candidate_payload(db, candidate)

@router.post("/projects/{project_id}/recovery-candidates/{candidate_id}/adopt")
def adopt_recovery_candidate(project_id: str, candidate_id: str, db: Session = Depends(get_db)):
    ensure_replay_not_pending(db, project_id)
    candidate = _candidate_or_404(db, project_id, candidate_id)
    if candidate.initial_error_code == "WORLD_INFORMATION_MISSING": raise HTTPException(status_code=409, detail={"code": "WORLD_FACT_REQUIRED"})
    if candidate.status != RecoveryCandidateStatus.VALIDATED.value: raise HTTPException(status_code=409, detail={"code": "RECOVERY_CANDIDATE_NOT_VALIDATED"})
    service = RecoveryCandidateService(); version = service.current_version(db, candidate)
    loc = candidate.context_locator
    if candidate.candidate_type == RecoveryCandidateType.CHARACTER_DECISION.value:
        proposal = db.get(SceneProposal, loc.get("proposal_id")); character = db.get(Character, loc.get("character_id"))
        if not proposal or not character or proposal.project_id != project_id or character.project_id != project_id:
            candidate.status = RecoveryCandidateStatus.STALE.value; db.commit(); raise HTTPException(status_code=409, detail={"code": "RECOVERY_CONTEXT_STALE"})
    elif candidate.candidate_type == RecoveryCandidateType.CHARACTER_PERFORMANCE.value:
        proposal = db.get(SceneProposal, loc.get("proposal_id")); character = db.get(Character, loc.get("actor_character_id")); performance = db.get(ScenePerformance, loc.get("performance_id")); turn = db.get(ScenePerformanceTurn, loc.get("source_turn_id"))
        if not proposal or not character or not performance or not turn or proposal.project_id != project_id or character.project_id != project_id or performance.project_id != project_id or turn.project_id != project_id:
            candidate.status = RecoveryCandidateStatus.STALE.value; db.commit(); raise HTTPException(status_code=409, detail={"code": "RECOVERY_CONTEXT_STALE"})
    validation = service.validate(db, candidate, version.payload)
    if not validation.constraint_valid:
        db.commit()
        raise HTTPException(status_code=409, detail={"code": "RECOVERY_CONTEXT_STALE" if validation.context_stale else "RECOVERY_VALIDATION_FAILED", "validation_report": validation.validation_report})
    if candidate.candidate_type == RecoveryCandidateType.CHARACTER_DECISION.value:
        loc = candidate.context_locator; decision = CharacterDecision(project_id=project_id, scene_proposal_id=loc["proposal_id"], character_id=loc["character_id"], context_fingerprint=candidate.context_fingerprint, status=CharacterDecisionStatus.VALID, **version.payload); db.add(decision); db.flush(); candidate.adopted_resource_type = "CHARACTER_DECISION"; candidate.adopted_resource_id = decision.id
    elif candidate.candidate_type == RecoveryCandidateType.WORLD_RESOLUTION.value:
        loc = candidate.context_locator; resolution = db.get(WorldResolution, loc["world_resolution_id"]); performance = db.get(ScenePerformance, loc["performance_id"]); turn = db.get(ScenePerformanceTurn, loc["performance_turn_id"])
        if not resolution or not performance or not turn or resolution.project_id != project_id or resolution.performance_id != performance.id or resolution.performance_turn_id != turn.id or resolution.status not in {ResolutionStatus.REJECTED, ResolutionStatus.UNRESOLVED} or performance.status != PerformanceStatus.AWAITING_WORLD or not turn.requires_world_resolution or not turn.world_resolution_request or db.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == turn.id, WorldResolution.status == ResolutionStatus.VALID)):
            candidate.status = RecoveryCandidateStatus.STALE.value; db.commit(); raise HTTPException(status_code=409, detail={"code": "RECOVERY_CONTEXT_STALE"})
        for key in ("outcome", "outcome_summary", "objective_facts", "state_effects", "actor_observation", "public_observation", "canon_fact_ids_used", "world_entity_ids_used", "resolution_basis_summary", "missing_information"):
            setattr(resolution, key, version.payload.get(key))
        resolution.status = ResolutionStatus.VALID; resolution.recipient_character_ids = WorldObservationRouter().recipients(performance, turn, resolution); performance.status = PerformanceStatus.RUNNING; performance.stop_reason = None; candidate.adopted_resource_type = "WORLD_RESOLUTION"; candidate.adopted_resource_id = resolution.id
    else:
        loc = candidate.context_locator; turn = db.get(ScenePerformanceTurn, loc["source_turn_id"]); performance = db.get(ScenePerformance, loc["performance_id"]); parsed = CharacterPerformancePayload.model_validate(version.payload)
        if not turn or not performance:
            candidate.status = RecoveryCandidateStatus.STALE.value; db.commit(); raise HTTPException(status_code=409, detail={"code": "RECOVERY_CONTEXT_STALE"})
        latest = db.scalar(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == performance.id).order_by(ScenePerformanceTurn.sequence.desc()))
        resolution = db.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == turn.id, WorldResolution.status == ResolutionStatus.VALID)) if turn else None
        if not turn or turn.performance_id != performance.id or turn.sequence != (latest.sequence if latest else -1) or turn.character_decision_id != loc["source_decision_id"] or performance.status != PerformanceStatus.PAUSED or performance.stop_reason != "CHARACTER_DECISION_REJECTED" or resolution:
            candidate.status = RecoveryCandidateStatus.STALE.value; db.commit(); raise HTTPException(status_code=409, detail={"code": "RECOVERY_CONTEXT_STALE"})
        decision = CharacterDecision(project_id=project_id, scene_proposal_id=loc["proposal_id"], character_id=loc["actor_character_id"], context_fingerprint=candidate.context_fingerprint, status=CharacterDecisionStatus.VALID, **parsed.decision.model_dump(mode="json")); db.add(decision); db.flush(); turn.character_decision_id = decision.id; turn.action_visibility = parsed.action.visibility; turn.observable_action = parsed.action.observable_action; turn.spoken_content = parsed.action.spoken_content; turn.requires_world_resolution = parsed.action.requires_world_resolution; turn.world_resolution_request = parsed.action.world_resolution_request.model_dump(mode="json") if parsed.action.world_resolution_request else None; performance.status = PerformanceStatus.AWAITING_WORLD if parsed.action.requires_world_resolution else PerformanceStatus.RUNNING; performance.stop_reason = None; candidate.adopted_resource_type = "SCENE_PERFORMANCE_TURN"; candidate.adopted_resource_id = turn.id
        turn.recipient_character_ids = PerformanceObservationRouter().recipients(parsed.action.visibility, [item for item in performance.participant_order if item in performance.active_participant_ids], turn.actor_character_id, parsed.action.target_character_id)
        turn.validation_result = {"decision": {"valid": True, "issues": []}, "action": {"valid": True, "issues": []}}
        turns = db.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == performance.id).order_by(ScenePerformanceTurn.sequence)).all()
        PerformancePostTurnStateResolver().apply(performance, turns, turn, decision, parsed.action, db)
    candidate.status = RecoveryCandidateStatus.ADOPTED.value; db.commit(); return _candidate_payload(db, candidate)

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
    ensure_autonomous_run_idle(db, project_id)
    ensure_replay_not_pending(db, project_id)
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
    # The same service is used by AutonomousWorldLoopService.  Keep HTTP
    # concerns here while the persisted turn authority lives in one place.
    ensure_autonomous_run_idle(db, project_id)
    ensure_replay_not_pending(db, project_id)
    performance = db.get(ScenePerformance, performance_id)
    if not performance or performance.project_id != project_id: raise HTTPException(status_code=404, detail="Scene Performance not found")
    proposal = db.get(SceneProposal, performance.scene_proposal_id)
    if not proposal: raise HTTPException(status_code=404, detail="Scene Proposal not found")
    try:
        result = PerformanceRuntimeService().step(db, project_id, performance, proposal, heuristic_performer=HeuristicCharacterPerformer, model_provider_factory=get_model_provider)
    except RuntimeFailure as exc:
        db.commit() if exc.code.startswith("MODEL_") or exc.code == "STALE_PERFORMANCE" else db.rollback()
        raise HTTPException(status_code={"MODEL_TIMEOUT": 504, "MODEL_RATE_LIMITED": 429, "MODEL_AUTH_FAILED": 503}.get(exc.code, 409), detail={"code": exc.code, **(exc.detail or {})}) from exc
    db.add(performance); db.commit(); db.refresh(performance)
    turns = persisted_turns(db, performance.id)
    return {"performance": _performance_payload(performance, turns, db), "turn": result.get("turn") and record_dict(result["turn"]), "decision": result.get("decision") and record_dict(result["decision"]), "validation_report": result.get("validation_report"), "model_metadata": None}
    # Legacy implementation retained below only as an audit reference.
    ensure_autonomous_run_idle(db, project_id)
    ensure_replay_not_pending(db, project_id)
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
            trace = ExecutionTraceRecorder().start(db, project_id=project_id, stage=ExecutionStage.CHARACTER_ACTOR, source_type="SCENE_PERFORMANCE", source_id=performance.id, provider=route.provider, model=route.model, input_fingerprint=context["fingerprint"])
            raw_payload, result = LLMCharacterPerformer(routed_provider(settings, route, db, project_id), route.model).perform(actor_view)
    except ModelProviderError as exc:
        if trace: (ExecutionTraceRecorder().block if exc.code == MODEL_OUTPUT_INVALID else ExecutionTraceRecorder().fail)(trace, exc.code, upstream_status=exc.upstream_status)
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
        if valid: ExecutionTraceRecorder().succeed(trace, latency_ms=result.latency_ms, request_id=result.request_id, output_fingerprint=stable_fingerprint(raw_payload))
        else:
            code = primary_issue({"issues": decision_report.as_dict().get("issues", []) + action_report.as_dict().get("issues", [])}); report_data = {"decision": decision_report.as_dict(), "action": action_report.as_dict()}
            ExecutionTraceRecorder().block(trace, code, validation_report=report_data, latency_ms=result.latency_ms, request_id=result.request_id)
            recovery_performance_data = (code, report_data)
    decision.status = CharacterDecisionStatus.VALID if valid else CharacterDecisionStatus.REJECTED
    db.add(decision); db.flush()
    recipients = PerformanceObservationRouter().recipients(action.visibility, [item for item in performance.participant_order if item in performance.active_participant_ids], actor_id, action.target_character_id)
    turn = ScenePerformanceTurn(project_id=project_id, performance_id=performance.id, sequence=performance.turn_count + 1, actor_character_id=actor_id, actor_context_fingerprint=context["fingerprint"], character_decision_id=decision.id, action_visibility=action.visibility, observable_action=action.observable_action if valid else None, spoken_content=action.spoken_content if valid else None, recipient_character_ids=recipients if valid else [], requires_world_resolution=action.requires_world_resolution if valid else False, world_resolution_request=action.world_resolution_request.model_dump(mode="json") if valid and action.world_resolution_request else None, scene_beat_refs=action.scene_beat_refs if valid else [], validation_result={"decision": decision_report.as_dict(), "action": action_report.as_dict()})
    db.add(turn); db.flush()
    if trace and not valid:
        code, report_data = recovery_performance_data
        RecoveryCandidateService().create(db, project_id=project_id, trace=trace, candidate_type="CHARACTER_PERFORMANCE", payload=raw_payload, context_fingerprint=context["fingerprint"], locator={"project_id": project_id, "proposal_id": proposal.id, "performance_id": performance.id, "actor_character_id": actor_id, "source_turn_id": turn.id, "source_decision_id": decision.id}, error_code=code, validation_report=report_data, stage=ExecutionStage.CHARACTER_ACTOR.value, source_type="SCENE_PERFORMANCE", source_id=performance.id)
    performance.turn_count += 1
    if not valid:
        performance.status = PerformanceStatus.PAUSED; performance.stop_reason = "CHARACTER_DECISION_REJECTED"
    else:
        PerformancePostTurnStateResolver().apply(performance, turns + [turn], turn, decision, action, db)
    db.add(performance); db.commit(); db.refresh(turn); db.refresh(performance)
    return {"performance": _performance_payload(performance, turns + [turn], db), "turn": record_dict(turn), "decision": record_dict(decision), "validation_report": {"decision": decision_report.as_dict(), "action": action_report.as_dict()}, "model_metadata": {"provider": result.provider, "model": result.model, "latency_ms": result.latency_ms, "request_id": result.request_id} if result else None}


@router.post("/projects/{project_id}/performances/{performance_id}/world/resolve", status_code=status.HTTP_201_CREATED)
def resolve_world(project_id: str, performance_id: str, payload: Payload, db: Session = Depends(get_db)):
    ensure_autonomous_run_idle(db, project_id)
    ensure_replay_not_pending(db, project_id)
    performance = db.get(ScenePerformance, performance_id)
    if not performance or performance.project_id != project_id: raise HTTPException(status_code=404, detail="Scene Performance not found")
    proposal = db.get(SceneProposal, performance.scene_proposal_id)
    if not proposal: raise HTTPException(status_code=404, detail="Scene Proposal not found")
    try:
        result = WorldResolutionRuntimeService().resolve(db, project_id, performance, proposal, payload.model_dump().get("mode"), heuristic_resolver=HeuristicWorldResolver, model_provider_factory=get_model_provider)
    except RuntimeFailure as exc:
        db.commit() if exc.code.startswith("MODEL_") else db.rollback()
        raise HTTPException(status_code={"MODEL_TIMEOUT": 504, "MODEL_RATE_LIMITED": 429, "MODEL_AUTH_FAILED": 503}.get(exc.code, 409), detail={"code": exc.code, **(exc.detail or {})}) from exc
    db.add(performance); db.commit(); db.refresh(performance); db.refresh(result["resolution"])
    turns = list(reversed(persisted_turns(db, performance.id)))
    return {"performance": _performance_payload(performance, turns, db), "resolution": record_dict(result["resolution"]), "validation_report": result.get("validation_report"), "model_metadata": None}
    # Legacy implementation retained below only as an audit reference.
    ensure_autonomous_run_idle(db, project_id)
    ensure_replay_not_pending(db, project_id)
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
            trace = ExecutionTraceRecorder().start(db, project_id=project_id, stage=ExecutionStage.WORLD_RESOLVER, source_type="SCENE_PERFORMANCE_TURN", source_id=turn.id, provider=route.provider, model=route.model, input_fingerprint=context_fingerprint)
            raw, model_result = LLMWorldResolver(routed_provider(settings, route, db, project_id), route.model).resolve(context)
        world_payload = WorldResolutionPayload.model_validate(raw)
    except ModelProviderError as exc:
        if trace: (ExecutionTraceRecorder().block if exc.code == MODEL_OUTPUT_INVALID else ExecutionTraceRecorder().fail)(trace, exc.code, upstream_status=exc.upstream_status)
        db.commit()
        error_status = {MODEL_AUTH_FAILED: 503, MODEL_RATE_LIMITED: 429, MODEL_TIMEOUT: 504}.get(exc.code, 502)
        raise HTTPException(status_code=error_status, detail={"code": exc.code, "upstream_status": exc.upstream_status}) from exc
    except Exception as exc:
        if trace:
            ExecutionTraceRecorder().block(trace, MODEL_OUTPUT_INVALID, validation_report={"schema": "World resolver output was invalid."})
            db.commit()
        raise HTTPException(status_code=502, detail={"code": MODEL_OUTPUT_INVALID, "message": "World resolver output was invalid."}) from exc
    context_after = WorldResolutionContextBuilder().build(db, performance, turn, proposal, turn.world_resolution_request)
    if context_after["fingerprint"] != context_fingerprint:
        if trace:
            ExecutionTraceRecorder().block(trace, "WORLD_CONTEXT_STALE")
            db.commit()
        raise HTTPException(status_code=409, detail={"code": "WORLD_CONTEXT_STALE"})
    report = WorldResolutionConstraintChecker().validate(db, context_after, world_payload, project_id)
    if trace:
        if report["valid"] and world_payload.outcome != ResolutionOutcome.UNRESOLVED:
            ExecutionTraceRecorder().succeed(trace, latency_ms=model_result.latency_ms, request_id=model_result.request_id, output_fingerprint=stable_fingerprint(world_payload.model_dump(mode="json")))
        else:
            code = "WORLD_INFORMATION_MISSING" if world_payload.outcome == ResolutionOutcome.UNRESOLVED else primary_issue(report)
            ExecutionTraceRecorder().block(trace, code, validation_report={"issues": report.get("issues", [])}, latency_ms=model_result.latency_ms, request_id=model_result.request_id)
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
    db.flush()
    if trace and (resolution_status != ResolutionStatus.VALID):
        code = "WORLD_INFORMATION_MISSING" if resolution_status == ResolutionStatus.UNRESOLVED else primary_issue(report)
        RecoveryCandidateService().create(db, project_id=project_id, trace=trace, candidate_type="WORLD_RESOLUTION", payload=world_payload.model_dump(mode="json"), context_fingerprint=context_fingerprint, locator={"project_id": project_id, "proposal_id": proposal.id, "performance_id": performance.id, "performance_turn_id": turn.id, "world_resolution_id": resolution.id}, error_code=code, validation_report=report, stage=ExecutionStage.WORLD_RESOLVER.value, source_type="SCENE_PERFORMANCE_TURN", source_id=turn.id)
    if resolution_status == ResolutionStatus.VALID:
        performance.status = PerformanceStatus.RUNNING; performance.stop_reason = None
    elif resolution_status == ResolutionStatus.UNRESOLVED:
        performance.status = PerformanceStatus.AWAITING_WORLD; performance.stop_reason = "WORLD_INFORMATION_MISSING"
    else:
        performance.status = PerformanceStatus.AWAITING_WORLD; performance.stop_reason = "WORLD_RESOLUTION_REJECTED"
    db.add(performance); db.commit(); db.refresh(resolution); db.refresh(performance)
    return {"performance": _performance_payload(performance, list(reversed(turns)), db), "resolution": record_dict(resolution), "validation_report": report, "model_metadata": {"provider": model_result.provider, "model": model_result.model, "latency_ms": model_result.latency_ms, "request_id": model_result.request_id} if model_result else None}
