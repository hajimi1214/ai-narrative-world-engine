import enum
import uuid
from datetime import datetime
from typing import Any
from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON as SAJSON, String, Text, UniqueConstraint, Index, text, func
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
class StateDeltaBatchStatus(str, enum.Enum): CANDIDATE = "CANDIDATE"; VALIDATED = "VALIDATED"; REJECTED = "REJECTED"; APPLIED = "APPLIED"
class StateDeltaTargetType(str, enum.Enum): CHARACTER = "CHARACTER"; WORLD_ENTITY = "WORLD_ENTITY"; STORY_THREAD = "STORY_THREAD"; PROJECT = "PROJECT"
class StateDeltaDomain(str, enum.Enum): CHARACTER_LOCATION = "CHARACTER_LOCATION"; CHARACTER_INVENTORY = "CHARACTER_INVENTORY"; CHARACTER_RELATIONSHIP = "CHARACTER_RELATIONSHIP"; CHARACTER_PHYSICAL_STATE = "CHARACTER_PHYSICAL_STATE"; CHARACTER_EMOTIONAL_STATE = "CHARACTER_EMOTIONAL_STATE"; CHARACTER_CURRENT_STATE = "CHARACTER_CURRENT_STATE"; WORLD_ENTITY_PROFILE = "WORLD_ENTITY_PROFILE"; WORLD_ENTITY_ACTIVE = "WORLD_ENTITY_ACTIVE"; STORY_THREAD_STATE = "STORY_THREAD_STATE"; STORY_THREAD_STATUS = "STORY_THREAD_STATUS"; STORY_THREAD_PROGRESS = "STORY_THREAD_PROGRESS"; WORLD_TIME = "WORLD_TIME"
class StateDeltaOperation(str, enum.Enum): SET = "SET"; ADD = "ADD"; REMOVE = "REMOVE"; UPSERT = "UPSERT"
class SceneCommitStatus(str, enum.Enum): PENDING = "PENDING"; COMMITTED = "COMMITTED"
class RevisionStatus(str, enum.Enum): DRAFT = "DRAFT"; PREVIEWED = "PREVIEWED"; STALE = "STALE"; CANCELLED = "CANCELLED"; APPLIED = "APPLIED"; ROLLED_BACK = "ROLLED_BACK"
class SnapshotType(str, enum.Enum): BASELINE="BASELINE"; PRE_REVISION="PRE_REVISION"; POST_REVISION="POST_REVISION"; ROLLBACK_POINT="ROLLBACK_POINT"; PRE_REPLAY_COMMIT="PRE_REPLAY_COMMIT"; POST_REPLAY_COMMIT="POST_REPLAY_COMMIT"; PRE_SCENE_COMMIT="PRE_SCENE_COMMIT"; POST_SCENE_COMMIT="POST_SCENE_COMMIT"; PRE_SCENE_STATE="PRE_SCENE_STATE"; POST_SCENE_STATE="POST_SCENE_STATE"
class SceneCheckpointOrigin(str, enum.Enum): NORMAL_COMMIT="NORMAL_COMMIT"; REPLAY_COMMIT="REPLAY_COMMIT"; LEGACY="LEGACY"
class RevisionApplicationStatus(str, enum.Enum): PENDING="PENDING"; APPLIED="APPLIED"; FAILED="FAILED"; ROLLED_BACK="ROLLED_BACK"
class RetconApplicationStatus(str, enum.Enum): PENDING="PENDING"; APPLIED_PENDING_REPLAY="APPLIED_PENDING_REPLAY"; REPLAY_COMPLETED="REPLAY_COMPLETED"; FAILED="FAILED"; ROLLED_BACK="ROLLED_BACK"
class RetconCognitionInvalidationStatus(str, enum.Enum): ACTIVE="ACTIVE"; RESOLVED="RESOLVED"; ROLLED_BACK="ROLLED_BACK"
class ReplaySessionStatus(str, enum.Enum): READY="READY"; RUNNING="RUNNING"; BLOCKED="BLOCKED"; COMPLETED="COMPLETED"; ABORTED="ABORTED"
class ReplaySceneRunStatus(str, enum.Enum): PENDING="PENDING"; RUNNING="RUNNING"; VALIDATED="VALIDATED"; BLOCKED="BLOCKED"; COMMITTED="COMMITTED"
class TimelineEventType(str, enum.Enum): SCENE_OCCURRED="SCENE_OCCURRED"; STATE_CHANGE="STATE_CHANGE"; RETCON_APPLIED="RETCON_APPLIED"; REPLAY_COMMITTED="REPLAY_COMMITTED"
class TimelineOrigin(str, enum.Enum): NORMAL_COMMIT="NORMAL_COMMIT"; REPLAY_COMMIT="REPLAY_COMMIT"; RETCON="RETCON"; LEGACY_BACKFILL="LEGACY_BACKFILL"
class CausalResourceType(str, enum.Enum): CANON_FACT="CANON_FACT"; WORLD_ENTITY="WORLD_ENTITY"; CHARACTER_KNOWLEDGE="CHARACTER_KNOWLEDGE"; CHARACTER_MEMORY="CHARACTER_MEMORY"; CHARACTER_DECISION="CHARACTER_DECISION"; SCENE_PERFORMANCE_TURN="SCENE_PERFORMANCE_TURN"; WORLD_RESOLUTION="WORLD_RESOLUTION"; STATE_DELTA_ITEM="STATE_DELTA_ITEM"; TIMELINE_EVENT="TIMELINE_EVENT"; SCENE="SCENE"; RETCON_APPLICATION="RETCON_APPLICATION"; REPLAY_SESSION="REPLAY_SESSION"
class CausalEdgeKind(str, enum.Enum): CAUSAL="CAUSAL"; TEMPORAL="TEMPORAL"; PROVENANCE="PROVENANCE"; STRUCTURAL="STRUCTURAL"
class CausalRelationType(str, enum.Enum): KNOWLEDGE_INFORMED_DECISION="KNOWLEDGE_INFORMED_DECISION"; MEMORY_INFORMED_DECISION="MEMORY_INFORMED_DECISION"; DECISION_PRODUCED_TURN="DECISION_PRODUCED_TURN"; TURN_RESOLVED_BY="TURN_RESOLVED_BY"; RESOLUTION_PRODUCED_STATE_CHANGE="RESOLUTION_PRODUCED_STATE_CHANGE"; STATE_CHANGE_COMMITTED_IN_SCENE="STATE_CHANGE_COMMITTED_IN_SCENE"; SCENE_PRODUCED_KNOWLEDGE="SCENE_PRODUCED_KNOWLEDGE"; SCENE_PRODUCED_MEMORY="SCENE_PRODUCED_MEMORY"; CANON_CONSTRAINED_RESOLUTION="CANON_CONSTRAINED_RESOLUTION"; WORLD_ENTITY_CONTEXT_FOR_RESOLUTION="WORLD_ENTITY_CONTEXT_FOR_RESOLUTION"; SCENE_PRECEDES_SCENE="SCENE_PRECEDES_SCENE"; RETCON_TRIGGERED_REPLAY="RETCON_TRIGGERED_REPLAY"; REPLAY_REPLACED_SCENE="REPLAY_REPLACED_SCENE"
class AutonomousRunStatus(str, enum.Enum): CREATED="CREATED"; RUNNING="RUNNING"; PAUSED="PAUSED"; BLOCKED="BLOCKED"; COMPLETED="COMPLETED"; FAILED="FAILED"; CANCELLED="CANCELLED"
class AutonomousStepStatus(str, enum.Enum): PENDING="PENDING"; RUNNING="RUNNING"; PAUSED="PAUSED"; COMMITTED="COMMITTED"; BLOCKED="BLOCKED"; FAILED="FAILED"; CANCELLED="CANCELLED"
class ChapterStructureStatus(str, enum.Enum): LEGACY="LEGACY"; PROVISIONAL="PROVISIONAL"; SEALED="SEALED"; SUPERSEDED="SUPERSEDED"
class NarrativeArcStatus(str, enum.Enum): OPEN="OPEN"; SEALED="SEALED"; SUPERSEDED="SUPERSEDED"
class NarrativeVolumeStatus(str, enum.Enum): OPEN="OPEN"; SEALED="SEALED"; SUPERSEDED="SUPERSEDED"
class WriterDraftStatus(str, enum.Enum): GENERATING="GENERATING"; VALIDATED="VALIDATED"; ADOPTED="ADOPTED"; REJECTED="REJECTED"; FAILED="FAILED"; STALE="STALE"; SUPERSEDED="SUPERSEDED"
class WriterDraftOrigin(str, enum.Enum): WRITER="WRITER"; QUALITY_REPAIR="QUALITY_REPAIR"
class QualityAssessmentStatus(str, enum.Enum): RUNNING="RUNNING"; PASS="PASS"; REPAIR_REQUIRED="REPAIR_REQUIRED"; BLOCKED="BLOCKED"; FAILED="FAILED"; STALE="STALE"; SUPERSEDED="SUPERSEDED"
class QualityFindingSeverity(str, enum.Enum): BLOCKING="BLOCKING"; MAJOR="MAJOR"; MINOR="MINOR"; INFO="INFO"
class QualityFindingSource(str, enum.Enum): DETERMINISTIC="DETERMINISTIC"; CRITIC="CRITIC"
class WriterPOVMode(str, enum.Enum): FIRST_PERSON="FIRST_PERSON"; THIRD_PERSON_LIMITED="THIRD_PERSON_LIMITED"; THIRD_PERSON_OMNISCIENT="THIRD_PERSON_OMNISCIENT"; OBJECTIVE="OBJECTIVE"
class ExecutionStage(str, enum.Enum): CHARACTER_ACTOR="CHARACTER_ACTOR"; WORLD_RESOLVER="WORLD_RESOLVER"; DIRECTOR="DIRECTOR"; REPAIR="REPAIR"; REVISION_APPLY="REVISION_APPLY"; REVISION_ROLLBACK="REVISION_ROLLBACK"; SCENE_COMMIT="SCENE_COMMIT"; AUTONOMOUS_LOOP="AUTONOMOUS_LOOP"; WRITER="WRITER"; CRITIC="CRITIC"
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
    replay_session_id: Mapped[str | None] = mapped_column(String(36))
    replay_of_id: Mapped[str | None] = mapped_column(String(36))
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
    replay_session_id: Mapped[str | None] = mapped_column(String(36))
    replay_of_id: Mapped[str | None] = mapped_column(String(36))
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
    __table_args__ = (Index("uq_scene_active_sequence", "project_id", "sequence", unique=True, postgresql_where=text("history_status = 'ACTIVE'"), sqlite_where=text("history_status = 'ACTIVE'")),)
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
    history_status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    superseded_by_scene_id: Mapped[str | None] = mapped_column(String(36))

class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (Index("uq_chapter_project_active_number", "project_id", "number", unique=True, postgresql_where=text("active = true"), sqlite_where=text("active = 1")),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    source_scene_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quality_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="DRAFT", nullable=False)
    structure_revision_id: Mapped[str | None] = mapped_column(ForeignKey("narrative_structure_revisions.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    structure_status: Mapped[ChapterStructureStatus] = mapped_column(Enum(ChapterStructureStatus, native_enum=False, length=20), default=ChapterStructureStatus.LEGACY, nullable=False)
    start_sequence: Mapped[int | None] = mapped_column(Integer)
    end_sequence: Mapped[int | None] = mapped_column(Integer)
    structure_fingerprint: Mapped[str | None] = mapped_column(String(120))
    boundary_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    supersedes_chapter_id: Mapped[str | None] = mapped_column(ForeignKey("chapters.id"))
    current_writer_draft_id: Mapped[str | None] = mapped_column(ForeignKey("chapter_writer_drafts.id"))
    writer_content_fingerprint: Mapped[str | None] = mapped_column(String(120))
    writer_context_fingerprint: Mapped[str | None] = mapped_column(String(120))
    written_at: Mapped[datetime | None] = mapped_column(DateTime)
    current_quality_assessment_id: Mapped[str | None] = mapped_column(ForeignKey("chapter_quality_assessments.id"))
    quality_status: Mapped[str | None] = mapped_column(String(30))
    quality_content_fingerprint: Mapped[str | None] = mapped_column(String(120))
    quality_approved_at: Mapped[datetime | None] = mapped_column(DateTime)

class ChapterWriterDraft(Base):
    __tablename__ = "chapter_writer_drafts"
    __table_args__ = (
        UniqueConstraint("chapter_id", "version", name="uq_chapter_writer_draft_version"),
        UniqueConstraint("chapter_id", "client_request_id", name="uq_chapter_writer_draft_request"),
        Index("ix_chapter_writer_drafts_chapter_status", "chapter_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[WriterDraftStatus] = mapped_column(Enum(WriterDraftStatus, native_enum=False, length=20), default=WriterDraftStatus.GENERATING, nullable=False, index=True)
    client_request_id: Mapped[str | None] = mapped_column(String(200))
    request_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    chapter_structure_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    chapter_source_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    writer_context_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    source_structure_status: Mapped[str] = mapped_column(String(30), nullable=False)
    source_scene_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    source_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    writing_bible_id: Mapped[str | None] = mapped_column(ForeignKey("writing_bibles.id"))
    writing_bible_version: Mapped[int | None] = mapped_column(Integer)
    writing_bible_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    pov_mode: Mapped[WriterPOVMode] = mapped_column(Enum(WriterPOVMode, native_enum=False, length=30), nullable=False)
    pov_character_id: Mapped[str | None] = mapped_column(ForeignKey("characters.id"))
    provider: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(200))
    model_request_id: Mapped[str | None] = mapped_column(String(200))
    prompt_fingerprint: Mapped[str | None] = mapped_column(String(120))
    title_candidate: Mapped[str | None] = mapped_column(String(300))
    content: Mapped[str | None] = mapped_column(Text)
    content_fingerprint: Mapped[str | None] = mapped_column(String(120), index=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scene_coverage: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    parent_draft_id: Mapped[str | None] = mapped_column(ForeignKey("chapter_writer_drafts.id"))
    supersedes_draft_id: Mapped[str | None] = mapped_column(ForeignKey("chapter_writer_drafts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime)
    stale_at: Mapped[datetime | None] = mapped_column(DateTime)
    origin: Mapped[WriterDraftOrigin] = mapped_column(Enum(WriterDraftOrigin, native_enum=False, length=30), default=WriterDraftOrigin.WRITER, nullable=False)
    source_quality_assessment_id: Mapped[str | None] = mapped_column(ForeignKey("chapter_quality_assessments.id"))

class ChapterQualityAssessment(Base):
    __tablename__ = "chapter_quality_assessments"
    __table_args__ = (
        UniqueConstraint("chapter_id", "version", name="uq_chapter_quality_assessment_version"),
        UniqueConstraint("chapter_id", "client_request_id", name="uq_chapter_quality_assessment_request"),
        Index("uq_chapter_quality_assessment_active", "chapter_id", unique=True, postgresql_where=text("active = true"), sqlite_where=text("active = 1")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), nullable=False, index=True)
    writer_draft_id: Mapped[str] = mapped_column(ForeignKey("chapter_writer_drafts.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[QualityAssessmentStatus] = mapped_column(Enum(QualityAssessmentStatus, native_enum=False, length=30), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    client_request_id: Mapped[str | None] = mapped_column(String(200))
    request_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    content_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    writer_context_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    chapter_source_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    anti_ai_bible_id: Mapped[str | None] = mapped_column(ForeignKey("anti_ai_bibles.id"))
    anti_ai_bible_version: Mapped[int | None] = mapped_column(Integer)
    anti_ai_bible_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    writing_bible_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    quality_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    quality_config_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    quality_context_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    deterministic_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    critic_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    overall_score: Mapped[float | None] = mapped_column(Float)
    decision_reason_codes: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    critic_provider: Mapped[str | None] = mapped_column(String(100))
    critic_model: Mapped[str | None] = mapped_column(String(200))
    critic_request_id: Mapped[str | None] = mapped_column(String(200))
    critic_prompt_fingerprint: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    stale_at: Mapped[datetime | None] = mapped_column(DateTime)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)

class ChapterQualityFinding(Base):
    __tablename__ = "chapter_quality_findings"
    __table_args__ = (UniqueConstraint("assessment_id", "ordinal", name="uq_chapter_quality_finding_ordinal"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    assessment_id: Mapped[str] = mapped_column(ForeignKey("chapter_quality_assessments.id"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[QualityFindingSource] = mapped_column(Enum(QualityFindingSource, native_enum=False, length=20), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[QualityFindingSeverity] = mapped_column(Enum(QualityFindingSeverity, native_enum=False, length=20), nullable=False)
    rule_code: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    excerpt: Mapped[str | None] = mapped_column(Text)
    source_refs: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    finding_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    finding_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False, index=True)

class NarrativeStructureRevision(Base):
    __tablename__ = "narrative_structure_revisions"
    __table_args__ = (Index("uq_narrative_structure_revision_project_active", "project_id", unique=True, postgresql_where=text("active = true"), sqlite_where=text("active = 1")),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source_history_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    source_max_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    rebuild_from_sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    structure_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

class ChapterSceneBinding(Base):
    __tablename__ = "chapter_scene_bindings"
    __table_args__ = (UniqueConstraint("chapter_id", "ordinal", name="uq_chapter_scene_binding_ordinal"), UniqueConstraint("chapter_id", "scene_id", name="uq_chapter_scene_binding_scene"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), nullable=False, index=True)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scenes.id"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    scene_sequence: Mapped[int] = mapped_column(Integer, nullable=False)

class NarrativeArc(Base):
    __tablename__ = "narrative_arcs"
    __table_args__ = (Index("uq_narrative_arc_project_active_number", "project_id", "number", unique=True, postgresql_where=text("active = true"), sqlite_where=text("active = 1")),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    structure_revision_id: Mapped[str] = mapped_column(ForeignKey("narrative_structure_revisions.id"), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[NarrativeArcStatus] = mapped_column(Enum(NarrativeArcStatus, native_enum=False, length=20), default=NarrativeArcStatus.OPEN, nullable=False)
    start_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    end_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    dominant_thread_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    supporting_thread_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    structure_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    structure_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    supersedes_arc_id: Mapped[str | None] = mapped_column(ForeignKey("narrative_arcs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class NarrativeArcChapterBinding(Base):
    __tablename__ = "narrative_arc_chapter_bindings"
    __table_args__ = (UniqueConstraint("narrative_arc_id", "ordinal", name="uq_narrative_arc_chapter_ordinal"), UniqueConstraint("narrative_arc_id", "chapter_id", name="uq_narrative_arc_chapter_chapter"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    narrative_arc_id: Mapped[str] = mapped_column(ForeignKey("narrative_arcs.id"), nullable=False, index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

class NarrativeVolume(Base):
    __tablename__ = "narrative_volumes"
    __table_args__ = (Index("uq_narrative_volume_project_active_number", "project_id", "number", unique=True, postgresql_where=text("active = true"), sqlite_where=text("active = 1")),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    structure_revision_id: Mapped[str] = mapped_column(ForeignKey("narrative_structure_revisions.id"), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[NarrativeVolumeStatus] = mapped_column(Enum(NarrativeVolumeStatus, native_enum=False, length=20), default=NarrativeVolumeStatus.OPEN, nullable=False)
    start_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    end_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    dominant_thread_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    structure_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    structure_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    supersedes_volume_id: Mapped[str | None] = mapped_column(ForeignKey("narrative_volumes.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class NarrativeVolumeArcBinding(Base):
    __tablename__ = "narrative_volume_arc_bindings"
    __table_args__ = (UniqueConstraint("volume_id", "ordinal", name="uq_narrative_volume_arc_ordinal"), UniqueConstraint("volume_id", "narrative_arc_id", name="uq_narrative_volume_arc_arc"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    volume_id: Mapped[str] = mapped_column(ForeignKey("narrative_volumes.id"), nullable=False, index=True)
    narrative_arc_id: Mapped[str] = mapped_column(ForeignKey("narrative_arcs.id"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

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
    replay_session_id: Mapped[str | None] = mapped_column(String(36))
    replay_of_id: Mapped[str | None] = mapped_column(String(36))

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
    replay_session_id: Mapped[str | None] = mapped_column(String(36))
    replay_of_id: Mapped[str | None] = mapped_column(String(36))

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
    state_effects: Mapped[list[Any]] = mapped_column(JSON, default=list, server_default=text("'[]'"), nullable=False)
    actor_observation: Mapped[str | None] = mapped_column(Text)
    public_observation: Mapped[str | None] = mapped_column(Text)
    recipient_character_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    canon_fact_ids_used: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    world_entity_ids_used: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    resolution_basis_summary: Mapped[str | None] = mapped_column(Text)
    missing_information: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    replay_session_id: Mapped[str | None] = mapped_column(String(36))
    replay_of_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class StateDeltaBatch(Base):
    __tablename__ = "state_delta_batches"
    __table_args__ = (UniqueConstraint("project_id", "input_fingerprint", name="uq_state_delta_batch_project_input"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_scene_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("scene_proposals.id"))
    source_performance_id: Mapped[str | None] = mapped_column(ForeignKey("scene_performances.id"), index=True)
    source_turn_id: Mapped[str | None] = mapped_column(ForeignKey("scene_performance_turns.id"), index=True)
    source_resolution_id: Mapped[str | None] = mapped_column(ForeignKey("world_resolutions.id"), index=True)
    base_world_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    status: Mapped[StateDeltaBatchStatus] = mapped_column(Enum(StateDeltaBatchStatus), default=StateDeltaBatchStatus.CANDIDATE, nullable=False, index=True)
    derivation_version: Mapped[str] = mapped_column(String(40), nullable=False)
    derivation_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    validation_version: Mapped[str | None] = mapped_column(String(40))
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default=text("'{}'"), nullable=False)
    validation_fingerprint: Mapped[str | None] = mapped_column(String(120))
    validated_world_fingerprint: Mapped[str | None] = mapped_column(String(120))
    validation_completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    applied_scene_id: Mapped[str | None] = mapped_column(ForeignKey("scenes.id"), index=True)
    applied_commit_id: Mapped[str | None] = mapped_column(ForeignKey("scene_commits.id"), index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class StateDeltaItem(Base):
    __tablename__ = "state_delta_items"
    __table_args__ = (UniqueConstraint("batch_id", "ordinal", name="uq_state_delta_item_batch_ordinal"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("state_delta_batches.id"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    target_type: Mapped[StateDeltaTargetType] = mapped_column(Enum(StateDeltaTargetType), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[StateDeltaDomain] = mapped_column(Enum(StateDeltaDomain), nullable=False)
    operation: Mapped[StateDeltaOperation] = mapped_column(Enum(StateDeltaOperation), nullable=False)
    path: Mapped[str] = mapped_column(String(300), nullable=False)
    before_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    after_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    causal_reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_turn_id: Mapped[str | None] = mapped_column(ForeignKey("scene_performance_turns.id"))
    source_resolution_id: Mapped[str | None] = mapped_column(ForeignKey("world_resolutions.id"))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    semantic_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class SceneCommit(Base):
    __tablename__ = "scene_commits"
    __table_args__ = (
        UniqueConstraint("project_id", "performance_id", name="uq_scene_commit_project_performance"),
        UniqueConstraint("scene_id", name="uq_scene_commit_scene"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("scene_proposals.id"), nullable=False)
    performance_id: Mapped[str] = mapped_column(ForeignKey("scene_performances.id"), nullable=False)
    scene_id: Mapped[str | None] = mapped_column(ForeignKey("scenes.id"))
    status: Mapped[SceneCommitStatus] = mapped_column(Enum(SceneCommitStatus, native_enum=False, length=20), default=SceneCommitStatus.PENDING, server_default=text("'PENDING'"), nullable=False, index=True)
    delta_batch_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, server_default=text("'[]'"), nullable=False)
    pre_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("world_snapshots.id"))
    post_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("world_snapshots.id"))
    checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey("scene_state_checkpoints.id"))
    pre_world_fingerprint: Mapped[str | None] = mapped_column(String(120))
    post_world_fingerprint: Mapped[str | None] = mapped_column(String(120))
    source_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    commit_fingerprint: Mapped[str | None] = mapped_column(String(120))
    applied_delta_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    created_knowledge_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    created_memory_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

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
    resolution_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class RetconReplaySession(TimestampMixin, Base):
    __tablename__ = "retcon_replay_sessions"
    __table_args__ = (UniqueConstraint("retcon_application_id", name="uq_replay_session_application"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    retcon_application_id: Mapped[str] = mapped_column(ForeignKey("retcon_applications.id"), nullable=False)
    status: Mapped[ReplaySessionStatus] = mapped_column(String(30), default=ReplaySessionStatus.READY, nullable=False)
    current_sequence: Mapped[int | None] = mapped_column(Integer)
    baseline_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("world_snapshots.id"))
    baseline_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    current_fingerprint: Mapped[str | None] = mapped_column(String(120))
    queue: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    staged_world_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    pre_commit_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("world_snapshots.id"))
    post_commit_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("world_snapshots.id"))

class SceneStateCheckpoint(Base):
    __tablename__ = "scene_state_checkpoints"
    __table_args__ = (
        UniqueConstraint("project_id", "scene_id", "version", name="uq_scene_state_checkpoint_version"),
        Index("uq_scene_state_checkpoint_active", "project_id", "scene_id", unique=True,
              postgresql_where=text("active = true"), sqlite_where=text("active = 1")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scenes.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    pre_snapshot_id: Mapped[str] = mapped_column(ForeignKey("world_snapshots.id"), nullable=False)
    post_snapshot_id: Mapped[str] = mapped_column(ForeignKey("world_snapshots.id"), nullable=False)
    current_scene_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    capture_protocol_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    origin: Mapped[SceneCheckpointOrigin] = mapped_column(String(30), default=SceneCheckpointOrigin.LEGACY, nullable=False)
    source_scene_commit_id: Mapped[str | None] = mapped_column(ForeignKey("scene_commits.id"))
    source_replay_session_id: Mapped[str | None] = mapped_column(ForeignKey("retcon_replay_sessions.id"))
    supersedes_checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey("scene_state_checkpoints.id"))
    pre_state_fingerprint: Mapped[str | None] = mapped_column(String(120))
    post_state_fingerprint: Mapped[str | None] = mapped_column(String(120))
    checkpoint_fingerprint: Mapped[str | None] = mapped_column(String(120), index=True)

class TimelineEvent(Base):
    __tablename__ = "timeline_events"
    __table_args__ = (
        UniqueConstraint("project_id", "source_key", name="uq_timeline_event_project_source_key"),
        Index("ix_timeline_event_project_sequence_active", "project_id", "sequence", "active"),
        Index("ix_timeline_event_project_type_active", "project_id", "event_type", "active"),
        Index("ix_timeline_event_target_path", "target_type", "target_id", "path"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    event_type: Mapped[TimelineEventType] = mapped_column(Enum(TimelineEventType, native_enum=False, length=30), nullable=False)
    source_type: Mapped[str] = mapped_column(String(60), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_key: Mapped[str] = mapped_column(String(500), nullable=False)
    scene_id: Mapped[str | None] = mapped_column(ForeignKey("scenes.id"))
    sequence: Mapped[int | None] = mapped_column(Integer)
    ordinal: Mapped[int | None] = mapped_column(Integer)
    world_time: Mapped[datetime | None] = mapped_column(DateTime)
    origin: Mapped[TimelineOrigin] = mapped_column(Enum(TimelineOrigin, native_enum=False, length=30), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supersedes_event_id: Mapped[str | None] = mapped_column(ForeignKey("timeline_events.id"))
    checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey("scene_state_checkpoints.id"))
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(36))
    path: Mapped[str | None] = mapped_column(String(500))
    before_value: Mapped[Any] = mapped_column(JSON)
    after_value: Mapped[Any] = mapped_column(JSON)
    structured_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    event_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class CausalLink(Base):
    __tablename__ = "causal_links"
    __table_args__ = (UniqueConstraint("project_id", "source_key", name="uq_causal_link_project_source_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    cause_type: Mapped[CausalResourceType] = mapped_column(Enum(CausalResourceType, native_enum=False, length=40), nullable=False)
    cause_id: Mapped[str] = mapped_column(String(36), nullable=False)
    effect_type: Mapped[CausalResourceType] = mapped_column(Enum(CausalResourceType, native_enum=False, length=40), nullable=False)
    effect_id: Mapped[str] = mapped_column(String(36), nullable=False)
    edge_kind: Mapped[CausalEdgeKind] = mapped_column(Enum(CausalEdgeKind, native_enum=False, length=30), nullable=False)
    relation_type: Mapped[CausalRelationType] = mapped_column(Enum(CausalRelationType, native_enum=False, length=60), nullable=False)
    scene_id: Mapped[str | None] = mapped_column(ForeignKey("scenes.id"))
    sequence: Mapped[int | None] = mapped_column(Integer)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_key: Mapped[str] = mapped_column(String(700), nullable=False)
    link_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    replay_session_id: Mapped[str | None] = mapped_column(ForeignKey("retcon_replay_sessions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class SceneExecutionBinding(Base):
    __tablename__ = "scene_execution_bindings"
    __table_args__ = (Index("uq_scene_active_execution_binding", "scene_id", unique=True, postgresql_where=text("active = true"), sqlite_where=text("active = 1")),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scenes.id"), nullable=False)
    performance_id: Mapped[str] = mapped_column(ForeignKey("scene_performances.id"), nullable=False)
    replay_session_id: Mapped[str | None] = mapped_column(String(36))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class ReplaySceneRun(Base):
    __tablename__ = "replay_scene_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    replay_session_id: Mapped[str] = mapped_column(ForeignKey("retcon_replay_sessions.id"), nullable=False, index=True)
    original_scene_id: Mapped[str] = mapped_column(ForeignKey("scenes.id"), nullable=False)
    original_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[ReplaySceneRunStatus] = mapped_column(String(30), default=ReplaySceneRunStatus.PENDING, nullable=False)
    input_fingerprint: Mapped[str | None] = mapped_column(String(120))
    new_decision_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    new_turn_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    new_resolution_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    new_knowledge_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    new_memory_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    replacement_scene_id: Mapped[str | None] = mapped_column(ForeignKey("scenes.id"))
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

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

class AutonomousWorldRun(TimestampMixin, Base):
    __tablename__ = "autonomous_world_runs"
    __table_args__ = (Index("uq_autonomous_run_project_active", "project_id", unique=True, postgresql_where=text("active = true"), sqlite_where=text("active = 1")),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    status: Mapped[AutonomousRunStatus] = mapped_column(Enum(AutonomousRunStatus, native_enum=False, length=20), default=AutonomousRunStatus.CREATED, nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    scene_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    committed_scene_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_turns_per_scene: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    performance_mode: Mapped[PerformanceMode] = mapped_column(Enum(PerformanceMode, native_enum=False, length=20), default=PerformanceMode.HEURISTIC, nullable=False)
    resolver_mode: Mapped[ResolverMode] = mapped_column(Enum(ResolverMode, native_enum=False, length=20), default=ResolverMode.HEURISTIC, nullable=False)
    start_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_committed_sequence: Mapped[int | None] = mapped_column(Integer)
    start_world_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    current_world_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    # Structured current-history authority.  This deliberately excludes writer prose.
    start_history_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    current_history_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    autonomous_run_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(120))
    last_error_code: Mapped[str | None] = mapped_column(String(120))
    last_error_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    client_request_id: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    lock_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

class AutonomousWorldStep(Base):
    __tablename__ = "autonomous_world_steps"
    __table_args__ = (UniqueConstraint("run_id", "ordinal", name="uq_autonomous_step_run_ordinal"), UniqueConstraint("run_id", "request_key", "request_offset", name="uq_autonomous_step_request"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("autonomous_world_runs.id"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AutonomousStepStatus] = mapped_column(Enum(AutonomousStepStatus, native_enum=False, length=20), default=AutonomousStepStatus.PENDING, nullable=False, index=True)
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_offset: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stage: Mapped[str] = mapped_column(String(80), nullable=False)
    scene_sequence_before: Mapped[int] = mapped_column(Integer, nullable=False)
    scene_sequence_after: Mapped[int | None] = mapped_column(Integer)
    world_fingerprint_before: Mapped[str] = mapped_column(String(120), nullable=False)
    world_fingerprint_after: Mapped[str | None] = mapped_column(String(120))
    step_input_fingerprint: Mapped[str | None] = mapped_column(String(120))
    step_output_fingerprint: Mapped[str | None] = mapped_column(String(120))
    director_context_fingerprint: Mapped[str | None] = mapped_column(String(120))
    gravity_fingerprint: Mapped[str | None] = mapped_column(String(120))
    candidate_key: Mapped[str | None] = mapped_column(String(500))
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey("scene_proposals.id"))
    performance_id: Mapped[str | None] = mapped_column(ForeignKey("scene_performances.id"))
    scene_commit_id: Mapped[str | None] = mapped_column(ForeignKey("scene_commits.id"))
    scene_id: Mapped[str | None] = mapped_column(ForeignKey("scenes.id"))
    checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey("scene_state_checkpoints.id"))
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resolution_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delta_batch_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    recovery_candidate_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(120))
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
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


class ResearchSourceTier(str, enum.Enum):
    PROJECT_RESEARCH = "PROJECT_RESEARCH"
    PUBLIC_KB = "PUBLIC_KB"
    WEB = "WEB"


class ResearchSourceKind(str, enum.Enum):
    MANUAL_TEXT = "MANUAL_TEXT"
    USER_DOCUMENT = "USER_DOCUMENT"
    PUBLIC_KB_IMPORT = "PUBLIC_KB_IMPORT"
    WEB_SNAPSHOT = "WEB_SNAPSHOT"


class ResearchDocument(TimestampMixin, Base):
    __tablename__ = "research_documents"
    __table_args__ = (
        UniqueConstraint("project_id", "client_request_id", name="uq_research_document_request"),
        Index("ix_research_documents_project_active", "project_id", "active"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_tier: Mapped[ResearchSourceTier] = mapped_column(Enum(ResearchSourceTier, native_enum=False, length=30), nullable=False, index=True)
    source_kind: Mapped[ResearchSourceKind] = mapped_column(Enum(ResearchSourceKind, native_enum=False, length=30), nullable=False, index=True)
    source_uri: Mapped[str | None] = mapped_column(String(2048))
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    client_request_id: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)


class ResearchDocumentRevision(Base):
    __tablename__ = "research_document_revisions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_research_revision_version"),
        Index("uq_research_revision_active", "document_id", unique=True, postgresql_where=text("active = true"), sqlite_where=text("active = 1")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("research_documents.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    normalized_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    ingestion_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    ingestion_config_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    supersedes_revision_id: Mapped[str | None] = mapped_column(ForeignKey("research_document_revisions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class ResearchChunk(Base):
    __tablename__ = "research_chunks"
    __table_args__ = (
        UniqueConstraint("revision_id", "ordinal", name="uq_research_chunk_revision_ordinal"),
        Index("ix_research_chunks_project_active", "project_id", "active"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("research_documents.id"), nullable=False, index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("research_document_revisions.id"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
