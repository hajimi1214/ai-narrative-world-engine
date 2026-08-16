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
