from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AutoDirectorRunCreatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    inspiration: str = Field(default="", max_length=12000)
    name: str | None = Field(default=None, max_length=200)
    genre: str = ""
    audience: str = ""
    target_chapters: int = Field(default=10, ge=1, le=500)
    target_words_per_chapter: int = Field(default=3000, ge=500, le=20000)
    pov: str = "THIRD_PERSON_LIMITED"
    tone: str = ""
    max_chapters: int | None = Field(default=None, ge=1, le=500)
    max_repairs: int = Field(default=2, ge=0, le=10)
    max_tokens: int = Field(default=100000, ge=1, le=10000000)
    max_retries: int = Field(default=2, ge=0, le=10)
    idempotency_key: str | None = Field(default=None, max_length=200)


class DirectionSelectionPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    direction_index: int | None = Field(default=None, ge=0, le=2)
    direction: dict[str, Any] | None = None


class AuthorGuidedRunCreatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: str | None = Field(default=None, max_length=300)
    theme: str | None = Field(default=None, max_length=4000)
    premise: str | None = Field(default=None, max_length=12000)
    ending_direction: str | None = Field(default=None, max_length=12000)
    protagonist_contract: dict[str, Any] = Field(default_factory=dict)
    global_plot_direction: str | None = Field(default=None, max_length=12000)
    global_required_events: list[Any] = Field(default_factory=list)
    global_forbidden_events: list[Any] = Field(default_factory=list)
    style_contract: dict[str, Any] = Field(default_factory=dict)
    author_locked_constraints: list[Any] = Field(default_factory=list)
    length_policy: dict[str, Any] = Field(default_factory=dict)
    estimated_chapters: int | None = Field(default=None, ge=1)
    estimated_volumes: int | None = Field(default=None, ge=1)
    operational_run_chapter_budget: int | None = Field(default=10, ge=1, le=100)
    operational_token_budget: int | None = Field(default=None, ge=1)
    volume: dict[str, Any] = Field(default_factory=dict)
    window_size: int = Field(default=5, ge=1, le=10)
    author_note: str | None = Field(default=None, max_length=4000)
    idempotency_key: str | None = Field(default=None, max_length=200)


class AuthorGuidancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    author_note: str = Field(min_length=1, max_length=4000)
    author_locked_constraints: list[Any] = Field(default_factory=list)
    author_override_reason: str | None = Field(default=None, max_length=4000)
    affected_scope: str = Field(default="WINDOW", max_length=40)
    requires_replan: bool = False


class VolumeActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=4000)
    author_confirmed: bool = False
