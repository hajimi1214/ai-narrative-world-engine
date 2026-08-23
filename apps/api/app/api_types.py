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
