import enum
import uuid
from datetime import datetime
from typing import Any
from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON as SAJSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

JSON = SAJSON

def new_id() -> str:
    return str(uuid.uuid4())

class ProjectStatus(str, enum.Enum): DRAFT = "DRAFT"; ACTIVE = "ACTIVE"; ARCHIVED = "ARCHIVED"
class CreationMode(str, enum.Enum): AUTONOMOUS = "AUTONOMOUS"; GUIDED = "GUIDED"; PERFORMANCE = "PERFORMANCE"
class CanonType(str, enum.Enum): TEMPORARY = "TEMPORARY"; WORLD_FACT = "WORLD_FACT"; CORE_CANON = "CORE_CANON"; SECRET_CANON = "SECRET_CANON"
class EntityType(str, enum.Enum): CITY = "CITY"; LOCATION = "LOCATION"; SECT = "SECT"; FACTION = "FACTION"; COUNTRY = "COUNTRY"; ITEM = "ITEM"; SYSTEM = "SYSTEM"; HISTORY = "HISTORY"; CUSTOM = "CUSTOM"
class KnowledgeStatus(str, enum.Enum): KNOWN = "KNOWN"; SUSPECTED = "SUSPECTED"; FALSE_BELIEF = "FALSE_BELIEF"; UNKNOWN = "UNKNOWN"
class ThreadStatus(str, enum.Enum): OPEN = "OPEN"; PAUSED = "PAUSED"; RESOLVED = "RESOLVED"; ABANDONED = "ABANDONED"
class SceneStatus(str, enum.Enum): PLANNED = "PLANNED"; OCCURRED = "OCCURRED"; VOID = "VOID"
class ProposalType(str, enum.Enum): CONTINUE_THREAD = "CONTINUE_THREAD"; CHARACTER_DRIVEN = "CHARACTER_DRIVEN"; CONSEQUENCE = "CONSEQUENCE"; REVEAL = "REVEAL"; ESCALATION = "ESCALATION"; RELATIONSHIP = "RELATIONSHIP"; TRANSITION = "TRANSITION"; NEW_THREAD = "NEW_THREAD"
class ProposalStatus(str, enum.Enum): DRAFT = "DRAFT"; VALID = "VALID"; REJECTED = "REJECTED"; APPROVED = "APPROVED"; EXECUTED = "EXECUTED"
class RevealStatus(str, enum.Enum): LOCKED = "LOCKED"; AVAILABLE = "AVAILABLE"; REVEALED = "REVEALED"
class DecisionType(str, enum.Enum): DRY_RUN = "DRY_RUN"; APPROVE = "APPROVE"; REJECT = "REJECT"
class CharacterDecisionType(str, enum.Enum): ACT = "ACT"; WAIT = "WAIT"; ASK = "ASK"; INVESTIGATE = "INVESTIGATE"; CONFRONT = "CONFRONT"; WITHDRAW = "WITHDRAW"; REFUSE = "REFUSE"; HELP = "HELP"; HIDE = "HIDE"; NEGOTIATE = "NEGOTIATE"; OBSERVE = "OBSERVE"; CUSTOM = "CUSTOM"
class CharacterDecisionStatus(str, enum.Enum): DRAFT = "DRAFT"; VALID = "VALID"; REJECTED = "REJECTED"; SUPERSEDED = "SUPERSEDED"
class PerformanceMode(str, enum.Enum): HEURISTIC = "HEURISTIC"; LLM = "LLM"
class PerformanceStatus(str, enum.Enum): READY = "READY"; RUNNING = "RUNNING"; AWAITING_WORLD = "AWAITING_WORLD"; PAUSED = "PAUSED"; COMPLETED = "COMPLETED"; INVALIDATED = "INVALIDATED"; FAILED = "FAILED"
class ActionVisibility(str, enum.Enum): PUBLIC = "PUBLIC"; TARGETED = "TARGETED"; COVERT = "COVERT"; PRIVATE = "PRIVATE"
class ResolverMode(str, enum.Enum): HEURISTIC = "HEURISTIC"; LLM = "LLM"
class ResolutionStatus(str, enum.Enum): VALID = "VALID"; REJECTED = "REJECTED"; UNRESOLVED = "UNRESOLVED"
class ResolutionOutcome(str, enum.Enum): SUCCESS = "SUCCESS"; PARTIAL = "PARTIAL"; FAILURE = "FAILURE"; NO_EFFECT = "NO_EFFECT"; INTERRUPTED = "INTERRUPTED"; UNRESOLVED = "UNRESOLVED"
class RevisionStatus(str, enum.Enum): DRAFT = "DRAFT"; PREVIEWED = "PREVIEWED"; STALE = "STALE"; CANCELLED = "CANCELLED"; APPLIED = "APPLIED"; ROLLED_BACK = "ROLLED_BACK"
class SnapshotType(str, enum.Enum): BASELINE="BASELINE"; PRE_REVISION="PRE_REVISION"; POST_REVISION="POST_REVISION"; ROLLBACK_POINT="ROLLBACK_POINT"
class RevisionApplicationStatus(str, enum.Enum): PENDING="PENDING"; APPLIED="APPLIED"; FAILED="FAILED"; ROLLED_BACK="ROLLED_BACK"
class RetconApplicationStatus(str, enum.Enum): PENDING="PENDING"; APPLIED_PENDING_REPLAY="APPLIED_PENDING_REPLAY"; FAILED="FAILED"; ROLLED_BACK="ROLLED_BACK"
class RetconCognitionInvalidationStatus(str, enum.Enum): ACTIVE="ACTIVE"; RESOLVED="RESOLVED"; ROLLED_BACK="ROLLED_BACK"
class ExecutionStage(str, enum.Enum): CHARACTER_ACTOR="CHARACTER_ACTOR"; WORLD_RESOLVER="WORLD_RESOLVER"; DIRECTOR="DIRECTOR"; REPAIR="REPAIR"; REVISION_APPLY="REVISION_APPLY"; REVISION_ROLLBACK="REVISION_ROLLBACK"; WRITER="WRITER"; CRITIC="CRITIC"
class ExecutionStatus(str, enum.Enum): STARTED="STARTED"; SUCCEEDED="SUCCEEDED"; FAILED="FAILED"; BLOCKED="BLOCKED"
class RecoveryCandidateStatus(str, enum.Enum): OPEN="OPEN"; VALIDATED="VALIDATED"; ADOPTED="ADOPTED"; STALE="STALE"; ABORTED="ABORTED"
class RecoveryCandidateType(str, enum.Enum): CHARACTER_DECISION="CHARACTER_DECISION"; CHARACTER_PERFORMANCE="CHARACTER_PERFORMANCE"; WORLD_RESOLUTION="WORLD_RESOLUTION"
class RecoveryVersionOrigin(str, enum.Enum): ORIGINAL="ORIGINAL"; MANUAL_EDIT="MANUAL_EDIT"; AI_REPAIR="AI_REPAIR"

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[ProjectStatus] = mapped_column(Enum(ProjectStatus), default=ProjectStatus.DRAFT, nullable=False)
    creation_mode: Mapped[CreationMode] = mapped_column(Enum(CreationMode), default=CreationMode.AUTONOMOUS, nullable=False)
    story_seed: Mapped[str | None] = mapped_column(Text)
    target_chapter_words: Mapped[int] = mapped_column(Integer, default=3000, nullable=False)
    min_chapter_words: Mapped[int | None] = mapped_column(Integer)
    max_chapter_words: Mapped[int | None] = mapped_column(Integer)
    autonomy_settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    research_settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    current_world_time: Mapped[datetime | None] = mapped_column(DateTime)

class ProjectTemplate(TimestampMixin, Base):
    __tablename__ = "project_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    story_dna: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    writing_defaults: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    anti_ai_defaults: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    autonomy_defaults: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    research_defaults: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

class WritingBible(TimestampMixin, Base):
    __tablename__ = "writing_bibles"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

class AntiAIBible(TimestampMixin, Base):
    __tablename__ = "anti_ai_bibles"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    disabled_expressions: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    warning_expressions: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    frequency_limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    writing_principles: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    future_risk_labels: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)

class CanonFact(TimestampMixin, Base):
    __tablename__ = "canon_facts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    fact_type: Mapped[CanonType] = mapped_column(Enum(CanonType), nullable=False)
    proposition: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

class WorldEntity(TimestampMixin, Base):
    __tablename__ = "world_entities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    entity_type: Mapped[EntityType] = mapped_column(Enum(EntityType), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

class Character(TimestampMixin, Base):
    __tablename__ = "characters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    personality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    core_values: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    boundaries: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    goals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    current_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    physical_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    emotional_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    abilities: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    voice_profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    relationships: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    inventory: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    secrets: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    narrative_relevance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

class CharacterKnowledge(Base):
    __tablename__ = "character_knowledge"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id"), nullable=False)
    proposition: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[KnowledgeStatus] = mapped_column(Enum(KnowledgeStatus), nullable=False)
    source: Mapped[str | None] = mapped_column(String(200))
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class CharacterMemory(Base):
    __tablename__ = "character_memories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    emotional_weight: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    distortion: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source_scene: Mapped[str | None] = mapped_column(String(36))
    happened_at: Mapped[datetime | None] = mapped_column(DateTime)

class StoryThread(Base):
    __tablename__ = "story_threads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[ThreadStatus] = mapped_column(Enum(ThreadStatus), default=ThreadStatus.OPEN, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    goal: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

class StoryArc(Base):
    __tablename__ = "story_arcs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    core_question: Mapped[str | None] = mapped_column(Text)
    core_conflict: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE", nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    source_scene_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)

class Scene(Base):
    __tablename__ = "scenes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    world_time: Mapped[datetime | None] = mapped_column(DateTime)
    location: Mapped[str | None] = mapped_column(String(200))
    participants: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    intent: Mapped[str | None] = mapped_column(Text)
    facts: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    story_threads: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[SceneStatus] = mapped_column(Enum(SceneStatus), default=SceneStatus.PLANNED, nullable=False)

class Chapter(Base):
    __tablename__ = "chapters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    source_scene_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quality_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="DRAFT", nullable=False)

class RevealConstraint(TimestampMixin, Base):
    __tablename__ = "reveal_constraints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    canon_fact_id: Mapped[str] = mapped_column(ForeignKey("canon_facts.id"), nullable=False)
    status: Mapped[RevealStatus] = mapped_column(Enum(RevealStatus), default=RevealStatus.LOCKED, nullable=False)
    minimum_condition: Mapped[str | None] = mapped_column(Text)
    allowed_character_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

class SceneProposal(TimestampMixin, Base):
    __tablename__ = "scene_proposals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    context_fingerprint: Mapped[str] = mapped_column(String(100), nullable=False)
    proposal_type: Mapped[ProposalType] = mapped_column(Enum(ProposalType), nullable=False)
    primary_thread_id: Mapped[str | None] = mapped_column(ForeignKey("story_threads.id"))
    location_id: Mapped[str | None] = mapped_column(String(36))
    proposed_location: Mapped[str | None] = mapped_column(String(200))
    participants: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    scene_goal: Mapped[str] = mapped_column(Text, nullable=False)
    character_motivations: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    entry_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    planned_pressure: Mapped[str | None] = mapped_column(Text)
    expected_progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    allowed_reveals: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    forbidden_reveals: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    required_canon: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    possible_outcomes: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    new_entity_requests: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    risk_flags: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    director_reasoning_summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ProposalStatus] = mapped_column(Enum(ProposalStatus), default=ProposalStatus.DRAFT, nullable=False)

class DirectorDecisionLog(Base):
    __tablename__ = "director_decision_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    context_version: Mapped[str] = mapped_column(String(100), nullable=False)
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey("scene_proposals.id"))
    decision_type: Mapped[DecisionType] = mapped_column(Enum(DecisionType), nullable=False)
    brief_reason: Mapped[str] = mapped_column(Text, nullable=False)
    validation_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class CharacterDecision(Base):
    __tablename__ = "character_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    scene_proposal_id: Mapped[str] = mapped_column(ForeignKey("scene_proposals.id"), nullable=False)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id"), nullable=False)
    context_fingerprint: Mapped[str] = mapped_column(String(100), nullable=False)
    decision_type: Mapped[CharacterDecisionType] = mapped_column(Enum(CharacterDecisionType), nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    chosen_action: Mapped[str] = mapped_column(Text, nullable=False)
    target_character_id: Mapped[str | None] = mapped_column(String(36))
    target_entity_id: Mapped[str | None] = mapped_column(String(36))
    motivation: Mapped[str] = mapped_column(Text, nullable=False)
    goal_refs: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    knowledge_used: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    memory_refs: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    ability_refs: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    inventory_refs: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    relationship_factors: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    perceived_risk: Mapped[str | None] = mapped_column(Text)
    accepted_cost: Mapped[str | None] = mapped_column(Text)
    expected_personal_result: Mapped[str | None] = mapped_column(Text)
    uncertainties: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    refused_options: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    boundary_override_reason: Mapped[str | None] = mapped_column(Text)
    decision_summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[CharacterDecisionStatus] = mapped_column(Enum(CharacterDecisionStatus), default=CharacterDecisionStatus.DRAFT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class ScenePerformance(TimestampMixin, Base):
    __tablename__ = "scene_performances"
    __table_args__ = (UniqueConstraint("scene_proposal_id", "take_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    scene_proposal_id: Mapped[str] = mapped_column(ForeignKey("scene_proposals.id"), nullable=False)
    take_number: Mapped[int] = mapped_column(Integer, nullable=False)
    proposal_context_fingerprint: Mapped[str] = mapped_column(String(100), nullable=False)
    mode: Mapped[PerformanceMode] = mapped_column(Enum(PerformanceMode), nullable=False)
    status: Mapped[PerformanceStatus] = mapped_column(Enum(PerformanceStatus), default=PerformanceStatus.READY, nullable=False)
    participant_order: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    active_participant_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    max_turns: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(100))

class ScenePerformanceTurn(Base):
    __tablename__ = "scene_performance_turns"
    __table_args__ = (UniqueConstraint("performance_id", "sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    performance_id: Mapped[str] = mapped_column(ForeignKey("scene_performances.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_character_id: Mapped[str] = mapped_column(ForeignKey("characters.id"), nullable=False)
    actor_context_fingerprint: Mapped[str] = mapped_column(String(100), nullable=False)
    character_decision_id: Mapped[str] = mapped_column(ForeignKey("character_decisions.id"), nullable=False)
    action_visibility: Mapped[ActionVisibility] = mapped_column(Enum(ActionVisibility), nullable=False)
    observable_action: Mapped[str | None] = mapped_column(Text)
    spoken_content: Mapped[str | None] = mapped_column(Text)
    recipient_character_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    requires_world_resolution: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    world_resolution_request: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    validation_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class WorldResolution(Base):
    __tablename__ = "world_resolutions"
    __table_args__ = (UniqueConstraint("performance_turn_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    performance_id: Mapped[str] = mapped_column(ForeignKey("scene_performances.id"), nullable=False)
    performance_turn_id: Mapped[str] = mapped_column(ForeignKey("scene_performance_turns.id"), nullable=False)
    resolver_mode: Mapped[ResolverMode] = mapped_column(Enum(ResolverMode), nullable=False)
    world_context_fingerprint: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[ResolutionStatus] = mapped_column(Enum(ResolutionStatus), nullable=False)
    outcome: Mapped[ResolutionOutcome] = mapped_column(Enum(ResolutionOutcome), nullable=False)
    outcome_summary: Mapped[str] = mapped_column(Text, nullable=False)
    objective_facts: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    actor_observation: Mapped[str | None] = mapped_column(Text)
    public_observation: Mapped[str | None] = mapped_column(Text)
    recipient_character_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    canon_fact_ids_used: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    world_entity_ids_used: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    resolution_basis_summary: Mapped[str | None] = mapped_column(Text)
    missing_information: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class WorldRevision(TimestampMixin, Base):
    __tablename__ = "world_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[RevisionStatus] = mapped_column(Enum(RevisionStatus), default=RevisionStatus.DRAFT, nullable=False)
    base_state_fingerprint: Mapped[str | None] = mapped_column(String(100))
    change_set: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    normalized_changes: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    impact_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

class RetconRequest(TimestampMixin, Base):
    __tablename__ = "retcon_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    source_revision_id: Mapped[str] = mapped_column(ForeignKey("world_revisions.id"), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    current_plan_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

class RetconImpactPlan(Base):
    __tablename__ = "retcon_impact_plans"
    __table_args__ = (UniqueConstraint("retcon_request_id", "version", name="uq_retcon_impact_plan_request_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    retcon_request_id: Mapped[str] = mapped_column(ForeignKey("retcon_requests.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_plan_id: Mapped[str | None] = mapped_column(ForeignKey("retcon_impact_plans.id"))
    basis_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="READY", nullable=False)
    earliest_affected_scene_id: Mapped[str | None] = mapped_column(String(36))
    earliest_affected_sequence: Mapped[int | None] = mapped_column(Integer)
    impact_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class RetconImpactItem(Base):
    __tablename__ = "retcon_impact_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(ForeignKey("retcon_impact_plans.id"), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    classification: Mapped[str] = mapped_column(String(40), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    reason_summary: Mapped[str] = mapped_column(Text, nullable=False)
    character_id: Mapped[str | None] = mapped_column(String(36))
    scene_id: Mapped[str | None] = mapped_column(String(36))
    dependency_path: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class RetconApplication(Base):
    __tablename__ = "retcon_applications"
    __table_args__ = (UniqueConstraint("retcon_request_id", name="uq_retcon_application_request"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    retcon_request_id: Mapped[str] = mapped_column(ForeignKey("retcon_requests.id"), nullable=False, index=True)
    retcon_plan_id: Mapped[str] = mapped_column(ForeignKey("retcon_impact_plans.id"), nullable=False)
    source_revision_id: Mapped[str] = mapped_column(ForeignKey("world_revisions.id"), nullable=False)
    revision_application_id: Mapped[str | None] = mapped_column(ForeignKey("revision_applications.id"))
    status: Mapped[RetconApplicationStatus] = mapped_column(String(40), default=RetconApplicationStatus.PENDING, nullable=False)
    plan_basis_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    pre_apply_world_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    post_apply_world_fingerprint: Mapped[str | None] = mapped_column(String(120))
    cognition_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    replay_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    failure_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime)

class RetconCognitionInvalidation(Base):
    __tablename__ = "retcon_cognition_invalidations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    retcon_application_id: Mapped[str] = mapped_column(ForeignKey("retcon_applications.id"), nullable=False, index=True)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id"), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(20), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_impact_item_id: Mapped[str | None] = mapped_column(ForeignKey("retcon_impact_items.id"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    original_semantic_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[RetconCognitionInvalidationStatus] = mapped_column(String(20), default=RetconCognitionInvalidationStatus.ACTIVE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class WorldSnapshot(Base):
    __tablename__="world_snapshots"
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=new_id); project_id: Mapped[str]=mapped_column(ForeignKey("projects.id"),nullable=False)
    snapshot_type: Mapped[SnapshotType]=mapped_column(String(30),nullable=False); schema_version: Mapped[int]=mapped_column(Integer,default=1,nullable=False)
    state_fingerprint: Mapped[str]=mapped_column(String(100),nullable=False); payload: Mapped[dict[str,Any]]=mapped_column(JSON,nullable=False); source_revision_id: Mapped[str|None]=mapped_column(ForeignKey("world_revisions.id")); created_at: Mapped[datetime]=mapped_column(DateTime,server_default=func.now(),nullable=False)
class RevisionApplication(Base):
    __tablename__="revision_applications"
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=new_id); project_id: Mapped[str]=mapped_column(ForeignKey("projects.id"),nullable=False); revision_id: Mapped[str]=mapped_column(ForeignKey("world_revisions.id"),nullable=False)
    status: Mapped[RevisionApplicationStatus]=mapped_column(String(30),default=RevisionApplicationStatus.PENDING,nullable=False); pre_snapshot_id: Mapped[str]=mapped_column(ForeignKey("world_snapshots.id"),nullable=False); post_snapshot_id: Mapped[str|None]=mapped_column(ForeignKey("world_snapshots.id")); expected_base_fingerprint: Mapped[str]=mapped_column(String(100),nullable=False); actual_base_fingerprint: Mapped[str]=mapped_column(String(100),nullable=False); author_override: Mapped[bool]=mapped_column(Boolean,default=False,nullable=False); author_override_reason: Mapped[str|None]=mapped_column(Text); applied_change_count: Mapped[int]=mapped_column(Integer,default=0,nullable=False); error_code: Mapped[str|None]=mapped_column(String(100)); error_summary: Mapped[str|None]=mapped_column(Text); created_at: Mapped[datetime]=mapped_column(DateTime,server_default=func.now(),nullable=False); completed_at: Mapped[datetime|None]=mapped_column(DateTime)
class ProjectModelConfig(TimestampMixin, Base):
    __tablename__="project_model_configs"; __table_args__=(UniqueConstraint("project_id"),)
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=new_id); project_id: Mapped[str]=mapped_column(ForeignKey("projects.id"),nullable=False); provider: Mapped[str|None]=mapped_column(String(100)); base_url: Mapped[str|None]=mapped_column(String(500)); character_model: Mapped[str|None]=mapped_column(String(200)); world_model: Mapped[str|None]=mapped_column(String(200)); director_model: Mapped[str|None]=mapped_column(String(200)); repair_model: Mapped[str|None]=mapped_column(String(200)); writer_model: Mapped[str|None]=mapped_column(String(200)); critic_model: Mapped[str|None]=mapped_column(String(200)); fallback_model: Mapped[str|None]=mapped_column(String(200)); auto_failover: Mapped[bool]=mapped_column(Boolean,default=False,nullable=False); max_repair_attempts: Mapped[int]=mapped_column(Integer,default=1,nullable=False)
class ExecutionTrace(Base):
    __tablename__="execution_traces"
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=new_id); project_id: Mapped[str]=mapped_column(ForeignKey("projects.id"),nullable=False); stage: Mapped[ExecutionStage]=mapped_column(String(50),nullable=False); source_type: Mapped[str|None]=mapped_column(String(100)); source_id: Mapped[str|None]=mapped_column(String(36)); status: Mapped[ExecutionStatus]=mapped_column(String(30),nullable=False); provider: Mapped[str|None]=mapped_column(String(100)); model: Mapped[str|None]=mapped_column(String(200)); input_fingerprint: Mapped[str|None]=mapped_column(String(100)); output_fingerprint: Mapped[str|None]=mapped_column(String(100)); latency_ms: Mapped[int|None]=mapped_column(Integer); request_id: Mapped[str|None]=mapped_column(String(200)); error_type: Mapped[str|None]=mapped_column(String(100)); error_code: Mapped[str|None]=mapped_column(String(100)); upstream_status: Mapped[int|None]=mapped_column(Integer); validation_report: Mapped[dict[str,Any]]=mapped_column(JSON,default=dict,nullable=False); repairable: Mapped[bool]=mapped_column(Boolean,default=False,nullable=False); retryable: Mapped[bool]=mapped_column(Boolean,default=False,nullable=False); attempt_number: Mapped[int]=mapped_column(Integer,default=1,nullable=False); parent_trace_id: Mapped[str|None]=mapped_column(ForeignKey("execution_traces.id")); created_at: Mapped[datetime]=mapped_column(DateTime,server_default=func.now(),nullable=False)

class RecoveryCandidate(TimestampMixin, Base):
    __tablename__ = "recovery_candidates"
    __table_args__ = (UniqueConstraint("source_trace_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    source_trace_id: Mapped[str] = mapped_column(ForeignKey("execution_traces.id"), nullable=False)
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    candidate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(100))
    source_id: Mapped[str | None] = mapped_column(String(36))
    context_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    context_locator: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    initial_error_code: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", nullable=False)
    current_version_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    adopted_resource_type: Mapped[str | None] = mapped_column(String(100))
    adopted_resource_id: Mapped[str | None] = mapped_column(String(36))

class RecoveryCandidateVersion(Base):
    __tablename__ = "recovery_candidate_versions"
    __table_args__ = (UniqueConstraint("candidate_id", "version_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("recovery_candidates.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[str] = mapped_column(String(30), nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(ForeignKey("recovery_candidate_versions.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    constraint_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    repair_trace_id: Mapped[str | None] = mapped_column(ForeignKey("execution_traces.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
