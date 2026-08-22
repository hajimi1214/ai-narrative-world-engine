"""Shared HTTP-layer helpers; domain rules remain in application services."""
from datetime import datetime
from enum import Enum
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..model_router import ProviderCredentialResolver
from ..models import Project, ProjectModelConfig


class Payload(BaseModel):
    """Legacy-compatible flexible request envelope for existing endpoints."""

    model_config = ConfigDict(extra="allow")


def get_db():
    # Retain the established API seam used by integration tests and local
    # deployments that replace the session factory before serving requests.
    from .. import api

    db = api.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    return value


def record_dict(record: Any) -> dict[str, Any]:
    return {column.name: serialize(getattr(record, column.name)) for column in record.__table__.columns}


def require_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def routed_provider(settings, route, db: Session | None = None, project_id: str | None = None):
    from .. import api

    key = ProviderCredentialResolver().generation_key(db, project_id, settings) if db is not None and project_id else None
    config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == project_id)) if db is not None and project_id else None
    timeout = config.request_timeout_seconds if config else None
    retries = config.max_retries if config else 0
    rate_limit = config.rate_limit_per_minute if config else 0
    try:
        return api.get_model_provider(settings, route.provider, route.base_url, key, timeout_seconds=timeout, max_retries=retries, rate_limit_per_minute=rate_limit)
    except TypeError as exc:
        # Existing fake factories from frozen phases accept three arguments.
        if "positional" not in str(exc) and "argument" not in str(exc):
            raise
        return api.get_model_provider(settings, route.provider, route.base_url)
