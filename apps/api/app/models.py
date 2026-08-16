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
