"""Deterministic project research library.

Research is advisory reference data.  This module deliberately has no write
path into canon, cognition, world state, timeline, ledger, writer, or quality.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    CanonFact,
    CanonType,
    Project,
    ResearchChunk,
    ResearchDocument,
    ResearchDocumentRevision,
    ResearchSourceKind,
    ResearchSourceTier,
    WorldEntity,
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def research_fingerprint(value: Any, protocol: str) -> str:
    return f"{protocol}:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class ResearchDomainError(ValueError):
    def __init__(self, code: str, detail: Any | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail if detail is not None else {}


class ResearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_size_chars: int = Field(default=1200, gt=0, le=100000)
    chunk_overlap_chars: int = Field(default=120, ge=0)
    top_k: int = Field(default=8, gt=0, le=100)
    max_context_chars: int = Field(default=8000, gt=0, le=1000000)
    per_document_limit: int = Field(default=3, gt=0, le=100)
    bm25_k1: float = Field(default=1.2, gt=0, le=3)
    bm25_b: float = Field(default=0.75, ge=0, le=1)
    diversity_lambda: float = Field(default=0.75, ge=0, le=1)
    deduplicate_exact: bool = True

    def model_post_init(self, __context: Any) -> None:
        if self.chunk_overlap_chars >= self.chunk_size_chars:
            raise ValueError("chunk_overlap_chars must be smaller than chunk_size_chars")


RETRIEVAL_CONFIG_FIELDS = (
    "top_k", "max_context_chars", "per_document_limit", "bm25_k1", "bm25_b",
    "diversity_lambda", "deduplicate_exact",
)


class ResearchIngestionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_size_chars: int = Field(default=1200, gt=0, le=100000)
    chunk_overlap_chars: int = Field(default=120, ge=0)
    tokenizer_protocol: str = "knowledge-tokenizer-v1"
    chunker_protocol: str = "research-chunker-v1"

    def model_post_init(self, __context: Any) -> None:
        if self.chunk_overlap_chars >= self.chunk_size_chars:
            raise ValueError("chunk_overlap_chars must be smaller than chunk_size_chars")


class ResearchRetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    top_k: int = Field(default=8, gt=0, le=100)
    max_context_chars: int = Field(default=8000, gt=0, le=1000000)
    per_document_limit: int = Field(default=3, gt=0, le=100)
    bm25_k1: float = Field(default=1.2, gt=0, le=3)
    bm25_b: float = Field(default=0.75, ge=0, le=1)
    diversity_lambda: float = Field(default=0.75, ge=0, le=1)
    deduplicate_exact: bool = True


class ResearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_ids: list[str] | None = None
    source_tiers: list[ResearchSourceTier] | None = None
    source_kinds: list[ResearchSourceKind] | None = None
    tags: list[str] | None = None

    def model_post_init(self, __context: Any) -> None:
        for values in (self.document_ids, self.tags):
            if values is not None and any(not value.strip() for value in values):
                raise ValueError("filter value is empty")


class KnowledgeConsumerMode(str, Enum):
    AUTHOR = "AUTHOR"
    CHARACTER = "CHARACTER"
    DIRECTOR = "DIRECTOR"
    WORLD = "WORLD"


class ResearchSourcePolicy:
    pairs = {
        ResearchSourceKind.MANUAL_TEXT.value: ResearchSourceTier.PROJECT_RESEARCH.value,
        ResearchSourceKind.USER_DOCUMENT.value: ResearchSourceTier.PROJECT_RESEARCH.value,
        ResearchSourceKind.PUBLIC_KB_IMPORT.value: ResearchSourceTier.PUBLIC_KB.value,
        ResearchSourceKind.WEB_SNAPSHOT.value: ResearchSourceTier.WEB.value,
    }

    def validate(self, source_tier: str | ResearchSourceTier, source_kind: str | ResearchSourceKind) -> tuple[str, str]:
        try:
            tier, kind = ResearchSourceTier(source_tier).value, ResearchSourceKind(source_kind).value
        except (TypeError, ValueError) as exc:
            raise ResearchDomainError("RESEARCH_SOURCE_CLASSIFICATION_INVALID") from exc
        if self.pairs.get(kind) != tier:
            raise ResearchDomainError("RESEARCH_SOURCE_CLASSIFICATION_INVALID")
        return tier, kind


class ResearchConfigResolver:
    defaults = ResearchConfig().model_dump(mode="json")

    @staticmethod
    def _stored(project: Project) -> dict[str, Any]:
        value = project.research_settings or {}
        if not isinstance(value, dict):
            raise ResearchDomainError("RESEARCH_CONFIG_INVALID")
        if isinstance(value.get("research"), dict):
            value = value["research"]
        return value

    @staticmethod
    def _overrides(request: dict[str, Any] | None) -> dict[str, Any]:
        value = (request or {}).get("config", {})
        if not isinstance(value, dict):
            raise ResearchDomainError("RESEARCH_CONFIG_INVALID")
        try:
            normalized = ResearchConfig.model_validate({**ResearchConfigResolver.defaults, **value}).model_dump(mode="json")
        except ValidationError as exc:
            raise ResearchDomainError("RESEARCH_CONFIG_INVALID", {"errors": exc.errors(include_url=False)}) from exc
        return {key: normalized[key] for key in value}

    def resolve(self, project: Project, request: dict[str, Any] | None = None) -> ResearchConfig:
        try:
            return ResearchConfig.model_validate({**self._stored(project), **self._overrides(request)})
        except ValidationError as exc:
            raise ResearchDomainError("RESEARCH_CONFIG_INVALID", {"errors": exc.errors(include_url=False)}) from exc

    def envelope(self, project: Project, request: dict[str, Any] | None = None) -> dict[str, Any]:
        resolved = self.resolve(project, request).model_dump(mode="json")
        explicit = self._overrides(request)
        stored = self._stored(project)
        return {
            "resolved": resolved,
            "explicit_overrides": explicit,
            "source": {
                "project_research_settings_fingerprint": research_fingerprint(stored, "research-project-config-v1"),
                "explicit_overrides_fingerprint": research_fingerprint(explicit, "research-explicit-config-v1"),
            },
        }

    def ingestion_envelope(self, project: Project, request: dict[str, Any] | None = None) -> dict[str, Any]:
        full = self.envelope(project, request)
        semantics = ResearchIngestionConfig.model_validate({key: full["resolved"][key] for key in ("chunk_size_chars", "chunk_overlap_chars")}).model_dump(mode="json")
        return {"resolved": semantics, "explicit_overrides": {key: value for key, value in full["explicit_overrides"].items() if key in {"chunk_size_chars", "chunk_overlap_chars"}}, "source": full["source"]}

    def retrieval_config(self, project: Project, request: dict[str, Any] | None = None) -> ResearchRetrievalConfig:
        resolved = self.resolve(project, request).model_dump(mode="json")
        return ResearchRetrievalConfig.model_validate({key: resolved[key] for key in RETRIEVAL_CONFIG_FIELDS})


def resolved_research_config(value: dict[str, Any] | ResearchConfig) -> dict[str, Any]:
    if isinstance(value, ResearchConfig):
        return value.model_dump(mode="json")
    try:
        raw = value.get("resolved") if isinstance(value, dict) and "resolved" in value else value
        return ResearchConfig.model_validate(raw).model_dump(mode="json")
    except (AttributeError, ValidationError) as exc:
        raise ResearchDomainError("RESEARCH_CONFIG_INVALID") from exc


def normalize_retrieval_config(value: ResearchRetrievalConfig | ResearchConfig | dict[str, Any] | None = None) -> ResearchRetrievalConfig:
    if isinstance(value, ResearchRetrievalConfig):
        return value
    if isinstance(value, ResearchConfig):
        value = value.model_dump(mode="json")
    try:
        raw = value.get("resolved") if isinstance(value, dict) and "resolved" in value else (value or {})
        return ResearchRetrievalConfig.model_validate({key: raw[key] for key in RETRIEVAL_CONFIG_FIELDS if key in raw})
    except (AttributeError, ValidationError, ValueError, TypeError) as exc:
        raise ResearchDomainError("RESEARCH_CONFIG_INVALID") from exc


class KnowledgeTokenizer:
    protocol = "knowledge-tokenizer-v1"
    _latin = re.compile(r"[A-Za-z0-9]+")
    _cjk = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]")

    def tokenize(self, text: str) -> list[str]:
        if not isinstance(text, str):
            raise ResearchDomainError("RESEARCH_QUERY_INVALID")
        tokens: list[str] = []
        for match in self._latin.finditer(text):
            tokens.append(match.group(0).casefold())
        chars = [match.group(0) for match in self._cjk.finditer(text)]
        tokens.extend(chars)
        tokens.extend(chars[index] + chars[index + 1] for index in range(len(chars) - 1))
        return tokens

    def fingerprint(self, text: str) -> str:
        return research_fingerprint(self.tokenize(text), self.protocol)


@dataclass(frozen=True)
class ResearchChunkCandidate:
    ordinal: int
    start_offset: int
    end_offset: int
    content: str
    token_count: int
    char_count: int


class ResearchChunker:
    protocol = "research-chunker-v1"

    def __init__(self, tokenizer: KnowledgeTokenizer | None = None):
        self.tokenizer = tokenizer or KnowledgeTokenizer()

    def chunk(self, content: str, config: ResearchConfig | dict[str, Any] | None = None) -> list[ResearchChunkCandidate]:
        if not isinstance(content, str) or not content:
            raise ResearchDomainError("RESEARCH_CONTENT_EMPTY")
        cfg = ResearchConfig.model_validate(config or {}) if not isinstance(config, ResearchConfig) else config
        size, overlap = cfg.chunk_size_chars, cfg.chunk_overlap_chars
        result: list[ResearchChunkCandidate] = []
        start = 0
        while start < len(content):
            limit = min(len(content), start + size)
            end = limit
            if limit < len(content):
                boundary = max((item.end() for item in re.finditer(r"(?:\n\s*\n|[。！？.!?]\s+|[。！？.!?])", content[start:limit])), default=0)
                if boundary > 0:
                    end = start + boundary
            if end <= start:
                end = limit
            text = content[start:end]
            result.append(ResearchChunkCandidate(len(result) + 1, start, end, text, len(self.tokenizer.tokenize(text)), len(text)))
            if end >= len(content):
                break
            next_start = max(start + 1, end - overlap)
            if next_start >= end:
                next_start = end
            start = next_start
        return result


def _safe_source_uri(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 2048 or any(char.isspace() for char in value):
        raise ResearchDomainError("RESEARCH_SOURCE_URI_INVALID")
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        if not parsed.hostname or parsed.username or parsed.password:
            raise ResearchDomainError("RESEARCH_SOURCE_URI_INVALID")
        if any(normalize_secret_key(key) in _SECRET_KEYS for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ResearchDomainError("RESEARCH_SOURCE_URI_SECRET")
        return value
    if "://" in value or not re.fullmatch(r"[A-Za-z0-9._:/#?=&%+\-]+", value) or any(normalize_secret_key(key) in _SECRET_KEYS for key, _ in parse_qsl(urlparse(value).query, keep_blank_values=True)):
        raise ResearchDomainError("RESEARCH_SOURCE_URI_INVALID")
    return value


_SECRET_KEYS = {
    "password", "passwd", "token", "apikey", "accesstoken", "authorization",
    "authtoken", "secret", "credential", "credentials", "signature", "sig",
    "xamzsignature", "xgoogsignature",
}


def normalize_secret_key(value: Any) -> str:
    return re.sub(r"[-_.\s]", "", str(value).strip().casefold())


def _safe_metadata(value: dict[str, Any] | None, *, reject: bool = True) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ResearchDomainError("RESEARCH_METADATA_INVALID")
    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            output: dict[str, Any] = {}
            for key, child in item.items():
                if normalize_secret_key(key) in _SECRET_KEYS:
                    if reject:
                        raise ResearchDomainError("RESEARCH_METADATA_SECRET")
                    continue
                if str(key) == "tags":
                    if not isinstance(child, list) or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 120 for tag in child):
                        raise ResearchDomainError("RESEARCH_METADATA_INVALID")
                    output["tags"] = sorted(set(tag.strip() for tag in child))
                    continue
                output[str(key)] = visit(child)
            return output
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        raise ResearchDomainError("RESEARCH_METADATA_INVALID")
    return visit(value)


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


@dataclass(frozen=True)
class ResearchIngestionResult:
    document: ResearchDocument
    revision: ResearchDocumentRevision
    chunks: tuple[ResearchChunk, ...]
    idempotent: bool = False

    def __iter__(self):
        yield self.document
        yield self.revision
        yield list(self.chunks)


class ResearchIngestionService:
    def __init__(self, failure_injector=None, chunker: ResearchChunker | None = None):
        self.failure_injector = failure_injector
        self.chunker = chunker or ResearchChunker()

    def _inject(self, stage: str) -> None:
        if self.failure_injector:
            self.failure_injector(stage)

    @staticmethod
    def _enum(value: Any, cls: type[Enum], code: str) -> str:
        try:
            return cls(value).value if not isinstance(value, cls) else value.value
        except (TypeError, ValueError) as exc:
            raise ResearchDomainError(code) from exc

    def _chunks(self, db: Session, project_id: str, document_id: str, revision: ResearchDocumentRevision, config: dict[str, Any], metadata: dict[str, Any]) -> list[ResearchChunk]:
        rows: list[ResearchChunk] = []
        for candidate in self.chunker.chunk(revision.content, config):
            rows.append(ResearchChunk(project_id=project_id, document_id=document_id, revision_id=revision.id, ordinal=candidate.ordinal, start_offset=candidate.start_offset, end_offset=candidate.end_offset, content=candidate.content, content_fingerprint=research_fingerprint(candidate.content, "research-chunk-content-v1"), token_count=candidate.token_count, char_count=candidate.char_count, chunk_metadata={"tags": list(metadata.get("tags", [])) if isinstance(metadata.get("tags", []), list) else []}, active=True))
        db.add_all(rows)
        db.flush()
        return rows

    def ingest(self, db: Session, project_id: str, *, title: str, content: str, source_tier: ResearchSourceTier | str = ResearchSourceTier.PROJECT_RESEARCH, source_kind: ResearchSourceKind | str = ResearchSourceKind.MANUAL_TEXT, source_uri: str | None = None, source_metadata: dict[str, Any] | None = None, client_request_id: str | None = None, request: dict[str, Any] | None = None) -> ResearchIngestionResult:
        project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
        if not project:
            raise ResearchDomainError("PROJECT_NOT_FOUND")
        if not isinstance(title, str) or not title.strip():
            raise ResearchDomainError("RESEARCH_TITLE_REQUIRED")
        if not isinstance(content, str) or not content:
            raise ResearchDomainError("RESEARCH_CONTENT_EMPTY")
        tier, kind = ResearchSourcePolicy().validate(source_tier, source_kind)
        uri = _safe_source_uri(source_uri)
        metadata = _safe_metadata(source_metadata)
        full_config = ResearchConfigResolver().resolve(project, request).model_dump(mode="json")
        config = ResearchConfigResolver().ingestion_envelope(project, request)
        config_fp = research_fingerprint(config["resolved"], "research-ingestion-config-v1")
        content_fp = research_fingerprint(content, "research-document-content-v1")
        request_semantics = {"title": title, "content_fingerprint": content_fp, "source_tier": tier, "source_kind": kind, "source_uri": uri, "source_metadata": metadata, "config_fingerprint": config_fp}
        existing = db.scalar(select(ResearchDocument).where(ResearchDocument.project_id == project_id, ResearchDocument.client_request_id == client_request_id)) if client_request_id else None
        if existing:
            active = db.scalar(select(ResearchDocumentRevision).where(ResearchDocumentRevision.document_id == existing.id, ResearchDocumentRevision.active.is_(True)))
            if not active or existing.title != title or _value(existing.source_tier) != tier or _value(existing.source_kind) != kind or existing.source_uri != uri or existing.source_metadata != metadata or active.content_fingerprint != content_fp or active.ingestion_config_fingerprint != config_fp:
                raise ResearchDomainError("RESEARCH_REQUEST_MISMATCH")
            chunks = tuple(db.scalars(select(ResearchChunk).where(ResearchChunk.revision_id == active.id).order_by(ResearchChunk.ordinal)).all())
            return ResearchIngestionResult(existing, active, chunks, True)
        document = ResearchDocument(project_id=project_id, title=title, source_tier=tier, source_kind=kind, source_uri=uri, source_metadata=metadata, client_request_id=client_request_id, active=True)
        db.add(document)
        db.flush()
        revision = ResearchDocumentRevision(project_id=project_id, document_id=document.id, version=1, active=False, content=content, content_fingerprint=content_fp, normalized_fingerprint=research_fingerprint(unicodedata.normalize("NFKC", content).strip(), "research-normalized-content-v1"), ingestion_config=config, ingestion_config_fingerprint=config_fp, supersedes_revision_id=None)
        db.add(revision)
        db.flush()
        chunks = self._chunks(db, project_id, document.id, revision, full_config, metadata)
        self._inject("AFTER_REVISION_BEFORE_ACTIVE_SWITCH")
        revision.active = True
        db.flush()
        try:
            from .retrieval_index import ResearchLexicalIndexService
            with db.begin_nested():
                ResearchLexicalIndexService().sync_after_ingestion(db, project_id, document.id)
        except Exception:
            from .retrieval_index import ResearchLexicalIndexService
            with db.begin_nested():
                ResearchLexicalIndexService().mark_dirty(db, project_id)
        return ResearchIngestionResult(document, revision, tuple(chunks), False)

    create_document = ingest

    def add_revision(self, db: Session, document_id: str, *, content: str, request: dict[str, Any] | None = None, source_metadata: dict[str, Any] | None = None) -> ResearchIngestionResult:
        document = db.scalar(select(ResearchDocument).where(ResearchDocument.id == document_id).with_for_update())
        if not document or not document.active:
            raise ResearchDomainError("RESEARCH_DOCUMENT_NOT_FOUND")
        if not isinstance(content, str) or not content:
            raise ResearchDomainError("RESEARCH_CONTENT_EMPTY")
        project = db.get(Project, document.project_id)
        full_config = ResearchConfigResolver().resolve(project, request).model_dump(mode="json")
        config = ResearchConfigResolver().ingestion_envelope(project, request)
        config_fp = research_fingerprint(config["resolved"], "research-ingestion-config-v1")
        content_fp = research_fingerprint(content, "research-document-content-v1")
        old = db.scalar(select(ResearchDocumentRevision).where(ResearchDocumentRevision.document_id == document.id, ResearchDocumentRevision.active.is_(True)).with_for_update())
        if old and old.content_fingerprint == content_fp and old.ingestion_config_fingerprint == config_fp:
            chunks = tuple(db.scalars(select(ResearchChunk).where(ResearchChunk.revision_id == old.id).order_by(ResearchChunk.ordinal)).all())
            return ResearchIngestionResult(document, old, chunks, True)
        version = (db.scalar(select(func.max(ResearchDocumentRevision.version)).where(ResearchDocumentRevision.document_id == document.id)) or 0) + 1
        metadata = _safe_metadata(source_metadata if source_metadata is not None else document.source_metadata)
        revision = ResearchDocumentRevision(project_id=document.project_id, document_id=document.id, version=version, active=False, content=content, content_fingerprint=content_fp, normalized_fingerprint=research_fingerprint(unicodedata.normalize("NFKC", content).strip(), "research-normalized-content-v1"), ingestion_config=config, ingestion_config_fingerprint=config_fp, supersedes_revision_id=old.id if old else None)
        db.add(revision)
        db.flush()
        chunks = self._chunks(db, document.project_id, document.id, revision, full_config, metadata)
        self._inject("AFTER_REVISION_BEFORE_ACTIVE_SWITCH")
        if old:
            old.active = False
            for chunk in db.scalars(
                select(ResearchChunk)
                .where(ResearchChunk.revision_id == old.id, ResearchChunk.active.is_(True))
                .with_for_update()
            ).all():
                chunk.active = False
            db.flush()
        revision.active = True
        db.flush()
        try:
            from .retrieval_index import ResearchLexicalIndexService
            with db.begin_nested():
                ResearchLexicalIndexService().sync_after_ingestion(db, document.project_id, document.id)
        except Exception:
            from .retrieval_index import ResearchLexicalIndexService
            with db.begin_nested():
                ResearchLexicalIndexService().mark_dirty(db, document.project_id)
        return ResearchIngestionResult(document, revision, tuple(chunks), False)

    update_revision = add_revision

    def archive(self, db: Session, document_id: str) -> ResearchDocument:
        document = db.scalar(select(ResearchDocument).where(ResearchDocument.id == document_id).with_for_update())
        if not document:
            raise ResearchDomainError("RESEARCH_DOCUMENT_NOT_FOUND")
        document.active = False
        document.archived_at = datetime.now(UTC)
        for revision in db.scalars(select(ResearchDocumentRevision).where(ResearchDocumentRevision.document_id == document.id, ResearchDocumentRevision.active.is_(True)).with_for_update()).all():
            revision.active = False
            for chunk in db.scalars(
                select(ResearchChunk)
                .where(ResearchChunk.revision_id == revision.id, ResearchChunk.active.is_(True))
                .with_for_update()
            ).all():
                chunk.active = False
        db.flush()
        try:
            from .retrieval_index import ResearchLexicalIndexService
            with db.begin_nested():
                ResearchLexicalIndexService().sync_after_ingestion(db, document.project_id, document.id)
        except Exception:
            from .retrieval_index import ResearchLexicalIndexService
            with db.begin_nested():
                ResearchLexicalIndexService().mark_dirty(db, document.project_id)
        return document


class ResearchRevisionAudit:
    def audit(self, db: Session, revision_id: str) -> dict[str, Any]:
        revision = db.get(ResearchDocumentRevision, revision_id)
        document = db.get(ResearchDocument, revision.document_id) if revision else None
        if not revision or not document or revision.project_id != document.project_id:
            raise ResearchDomainError("RESEARCH_REVISION_INTEGRITY_INVALID")
        active_count = db.scalar(select(func.count(ResearchDocumentRevision.id)).where(ResearchDocumentRevision.document_id == document.id, ResearchDocumentRevision.active.is_(True))) or 0
        if active_count > 1:
            raise ResearchDomainError("RESEARCH_ACTIVE_REVISION_INVALID")
        legacy = not isinstance(revision.ingestion_config, dict) or "resolved" not in revision.ingestion_config
        config_value = resolved_research_config(revision.ingestion_config) if legacy else ResearchIngestionConfig.model_validate(revision.ingestion_config["resolved"]).model_dump(mode="json")
        config_protocol = "research-config-v1" if legacy else "research-ingestion-config-v1"
        if revision.version < 1 or research_fingerprint(revision.content, "research-document-content-v1") != revision.content_fingerprint or research_fingerprint(unicodedata.normalize("NFKC", revision.content).strip(), "research-normalized-content-v1") != revision.normalized_fingerprint or research_fingerprint(config_value, config_protocol) != revision.ingestion_config_fingerprint:
            raise ResearchDomainError("RESEARCH_REVISION_INTEGRITY_INVALID")
        chunks = db.scalars(select(ResearchChunk).where(ResearchChunk.revision_id == revision.id).order_by(ResearchChunk.ordinal)).all()
        if not chunks:
            raise ResearchDomainError("RESEARCH_CHUNK_ORDER_INVALID")
        if revision.active and any(not chunk.active for chunk in chunks):
            raise ResearchDomainError("RESEARCH_REVISION_INTEGRITY_INVALID")
        if not revision.active and any(chunk.active for chunk in chunks):
            raise ResearchDomainError("RESEARCH_REVISION_INTEGRITY_INVALID")
        if [item.ordinal for item in chunks] != list(range(1, len(chunks) + 1)):
            raise ResearchDomainError("RESEARCH_CHUNK_ORDER_INVALID")
        for chunk in chunks:
            if chunk.project_id != revision.project_id or chunk.document_id != document.id or chunk.start_offset < 0 or chunk.end_offset <= chunk.start_offset or revision.content[chunk.start_offset:chunk.end_offset] != chunk.content or research_fingerprint(chunk.content, "research-chunk-content-v1") != chunk.content_fingerprint or chunk.char_count != len(chunk.content) or chunk.token_count != len(KnowledgeTokenizer().tokenize(chunk.content)):
                raise ResearchDomainError("RESEARCH_CHUNK_INTEGRITY_INVALID")
        return {"valid": True, "revision_id": revision.id, "chunk_count": len(chunks)}


class ResearchCorpusAudit:
    def audit(self, db: Session, project_id: str) -> dict[str, Any]:
        documents = db.scalars(select(ResearchDocument).where(ResearchDocument.project_id == project_id, ResearchDocument.active.is_(True)).order_by(ResearchDocument.id)).all()
        for document in documents:
            ResearchSourcePolicy().validate(document.source_tier, document.source_kind)
            _safe_source_uri(document.source_uri)
            _safe_metadata(document.source_metadata)
            active = db.scalar(select(ResearchDocumentRevision).where(ResearchDocumentRevision.document_id == document.id, ResearchDocumentRevision.active.is_(True)))
            if not active:
                raise ResearchDomainError("RESEARCH_ACTIVE_REVISION_MISSING")
            ResearchRevisionAudit().audit(db, active.id)
        return {"valid": True, "document_count": len(documents)}


@dataclass(frozen=True)
class ResearchHit:
    chunk_id: str
    document_id: str
    revision_id: str
    title: str
    source_tier: str
    source_kind: str
    score: float
    rank: int
    content: str
    content_fingerprint: str
    source_uri: str | None
    source_metadata: dict[str, Any] = field(default_factory=dict)
    authority_rank: int = 0
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"chunk_id": self.chunk_id, "document_id": self.document_id, "revision_id": self.revision_id, "title": self.title, "source_tier": self.source_tier, "source_kind": self.source_kind, "score": self.score, "rank": self.rank, "content": self.content, "content_fingerprint": self.content_fingerprint, "source_uri": self.source_uri, "source_metadata": _safe_metadata(self.source_metadata, reject=False), "authority_rank": self.authority_rank, "untrusted_external": self.source_kind in {ResearchSourceKind.PUBLIC_KB_IMPORT.value, ResearchSourceKind.WEB_SNAPSHOT.value}, "truncated": self.truncated}


class ResearchDiversifier:
    def __init__(self, tokenizer: KnowledgeTokenizer | None = None):
        self.tokenizer = tokenizer or KnowledgeTokenizer()

    def _jaccard(self, left: str, right: str) -> float:
        a, b = set(self.tokenizer.tokenize(left)), set(self.tokenizer.tokenize(right))
        return len(a & b) / len(a | b) if a | b else 0.0

    def select(self, candidates: list[dict[str, Any]], config: ResearchConfig | dict[str, Any]) -> list[dict[str, Any]]:
        cfg = normalize_retrieval_config(config)
        remaining = list(candidates)
        selected: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        while remaining and len(selected) < cfg.top_k:
            allowed = [item for item in remaining if counts.get(item["document_id"], 0) < cfg.per_document_limit]
            if not allowed:
                break
            if not selected:
                best = allowed[0]
            else:
                scored = []
                for item in allowed:
                    similarity = max((self._jaccard(item["content"], other["content"]) for other in selected), default=0.0)
                    mmr = cfg.diversity_lambda * item["score"] - (1 - cfg.diversity_lambda) * similarity
                    scored.append((mmr, item["score"], item["authority_rank"], item["content_fingerprint"], item["document_id"], item["ordinal"], item))
                best = min(
                    scored,
                    key=lambda row: (-row[0], -row[1], row[2], "" if row[3] is None else row[3], row[4], row[5]),
                )[-1]
            selected.append(best)
            remaining.remove(best)
            counts[best["document_id"]] = counts.get(best["document_id"], 0) + 1
        return selected


class ResearchBM25Retriever:

    def __init__(self, tokenizer: KnowledgeTokenizer | None = None, diversifier: ResearchDiversifier | None = None):
        self.tokenizer = tokenizer or KnowledgeTokenizer()
        self.diversifier = diversifier or ResearchDiversifier(self.tokenizer)
        # Ephemeral route evidence; never included in KnowledgePacket or hit
        # payloads. This makes indexed success distinct from legacy fallback.
        self.last_route: str = "UNSET"
        self.last_fallback_reason: str | None = None

    def search(self, db: Session, project_id: str, query_text: str, *, filters: dict[str, Any] | None = None, config: ResearchConfig | dict[str, Any] | None = None, browse_mode: bool = False) -> list[ResearchHit]:
        self.last_route = "UNSET"
        self.last_fallback_reason = None
        try:
            cfg = normalize_retrieval_config(config)
        except ResearchDomainError:
            raise
        query_tokens = self.tokenizer.tokenize(query_text or "")
        if not query_tokens and not browse_mode:
            raise ResearchDomainError("RESEARCH_QUERY_EMPTY")
        try:
            filters = ResearchFilters.model_validate(filters or {}).model_dump(mode="json", exclude_none=True)
        except (ValidationError, ValueError, TypeError) as exc:
            raise ResearchDomainError("RESEARCH_FILTER_INVALID") from exc
        # READY postings are a derived acceleration only. Any indexed-path
        # error falls through to this frozen Python reference implementation.
        # Browse remains a legacy-only mode; normal tag filters have an exact
        # PostgreSQL JSONB predicate in the indexed path.
        if not browse_mode:
            try:
                from .retrieval_index import ResearchIndexedBM25Retriever, ResearchLexicalIndexService
                if ResearchLexicalIndexService().fast_path_available(db, project_id):
                    result = ResearchIndexedBM25Retriever().search(db, project_id, query_text, filters=filters, config=cfg)
                    self.last_route = "INDEXED_FAST"
                    return result
                self.last_route = "LEGACY_INDEX_UNAVAILABLE"
                self.last_fallback_reason = "RESEARCH_LEXICAL_INDEX_NOT_READY"
            except Exception as exc:
                # The reference implementation remains authoritative, but
                # the route is explicit and only a safe exception code/type is
                # exposed to diagnostics.
                self.last_route = "LEGACY_FALLBACK"
                self.last_fallback_reason = self._safe_route_reason(exc)
        elif browse_mode:
            self.last_route = "LEGACY_BROWSE"
        docs = db.scalars(select(ResearchDocument).where(ResearchDocument.project_id == project_id, ResearchDocument.active.is_(True)).order_by(ResearchDocument.id)).all()
        rows: list[tuple[ResearchDocument, ResearchDocumentRevision, ResearchChunk]] = []
        for document in docs:
            ResearchSourcePolicy().validate(document.source_tier, document.source_kind)
            _safe_source_uri(document.source_uri)
            _safe_metadata(document.source_metadata)
            if filters.get("document_ids") and document.id not in set(filters["document_ids"]):
                continue
            if filters.get("source_tiers") and _value(document.source_tier) not in set(filters["source_tiers"]):
                continue
            if filters.get("source_kinds") and _value(document.source_kind) not in set(filters["source_kinds"]):
                continue
            revision = db.scalar(select(ResearchDocumentRevision).where(ResearchDocumentRevision.document_id == document.id, ResearchDocumentRevision.active.is_(True)))
            if not revision:
                continue
            chunks = db.scalars(select(ResearchChunk).where(ResearchChunk.revision_id == revision.id, ResearchChunk.active.is_(True)).order_by(ResearchChunk.ordinal)).all()
            for chunk in chunks:
                tags = set(document.source_metadata.get("tags", []) if isinstance(document.source_metadata, dict) else []) | set(chunk.chunk_metadata.get("tags", []) if isinstance(chunk.chunk_metadata, dict) else [])
                if filters.get("tags") and not tags.intersection(set(filters["tags"])):
                    continue
                rows.append((document, revision, chunk))
        token_rows = [(row, self.tokenizer.tokenize(row[2].content)) for row in rows]
        n = len(token_rows)
        df: dict[str, int] = {}
        for _, tokens in token_rows:
            for token in set(tokens):
                df[token] = df.get(token, 0) + 1
        avgdl = sum(len(tokens) for _, tokens in token_rows) / n if n else 1.0
        candidates: list[dict[str, Any]] = []
        for document, revision, chunk in rows:
            tokens = self.tokenizer.tokenize(chunk.content)
            counts = {token: tokens.count(token) for token in set(tokens)}
            score = 0.0
            for token in set(query_tokens):
                if token not in counts:
                    continue
                idf = math.log(1 + (n - df[token] + 0.5) / (df[token] + 0.5))
                tf = counts[token]
                score += idf * (tf * (cfg.bm25_k1 + 1)) / (tf + cfg.bm25_k1 * (1 - cfg.bm25_b + cfg.bm25_b * len(tokens) / avgdl))
            if not query_tokens:
                score = 0.0
            if score > 0 or browse_mode:
                candidates.append({"document": document, "revision": revision, "chunk": chunk, "document_id": document.id, "ordinal": chunk.ordinal, "content": chunk.content, "content_fingerprint": chunk.content_fingerprint, "score": score, "authority_rank": KnowledgeAuthorityResolver().rank(KnowledgeAuthorityResolver().for_research(document.source_tier))})
        candidates.sort(key=lambda item: (-item["score"], item["authority_rank"], item["content_fingerprint"], item["document_id"], item["ordinal"]))
        if cfg.deduplicate_exact:
            seen: set[str] = set()
            candidates = [item for item in candidates if not (item["content_fingerprint"] in seen or seen.add(item["content_fingerprint"]))]
        selected = self.diversifier.select(candidates, cfg)
        hits: list[ResearchHit] = []
        total = 0
        for index, item in enumerate(selected, 1):
            content = item["content"]
            truncated = False
            remaining = cfg.max_context_chars - total
            if remaining <= 0:
                break
            if len(content) > remaining:
                content, truncated = content[:remaining], True
            total += len(content)
            document = item["document"]
            hits.append(ResearchHit(item["chunk"].id, document.id, item["revision"].id, document.title, _value(document.source_tier), _value(document.source_kind), float(item["score"]), index, content, item["content_fingerprint"], document.source_uri, _safe_metadata(document.source_metadata, reject=False), item["authority_rank"], truncated))
        return hits

    @staticmethod
    def _safe_route_reason(exc: BaseException) -> str:
        code = getattr(exc, "code", None)
        return str(code) if isinstance(code, str) and code else type(exc).__name__

    retrieve = search


class ResearchCorpusFingerprintBuilder:
    protocol = "research-corpus-v1"

    def build(self, db: Session, project_id: str) -> str:
        rows = db.execute(select(ResearchDocument.id, ResearchDocument.source_tier, ResearchDocument.source_kind, ResearchDocumentRevision.id, ResearchDocumentRevision.version, ResearchDocumentRevision.content_fingerprint).join(ResearchDocumentRevision, ResearchDocumentRevision.document_id == ResearchDocument.id).where(ResearchDocument.project_id == project_id, ResearchDocument.active.is_(True), ResearchDocumentRevision.active.is_(True))).all()
        values = [{"document_id": row[0], "source_tier": _value(row[1]), "source_kind": _value(row[2]), "revision_id": row[3], "version": row[4], "content_fingerprint": row[5]} for row in sorted(rows, key=lambda item: (item[0], item[3]))]
        return research_fingerprint(values, self.protocol)


class KnowledgeAuthorityTier(str, Enum):
    USER_EXPLICIT = "USER_EXPLICIT"
    CORE_CANON = "CORE_CANON"
    WORLD_BIBLE = "WORLD_BIBLE"
    CURRENT_WORLD = "CURRENT_WORLD"
    PROJECT_RESEARCH = "PROJECT_RESEARCH"
    PUBLIC_KB = "PUBLIC_KB"
    WEB = "WEB"
    MODEL_PRIOR = "MODEL_PRIOR"


class KnowledgeAuthorityResolver:
    research_order = {ResearchSourceTier.PROJECT_RESEARCH.value: 4, ResearchSourceTier.PUBLIC_KB.value: 5, ResearchSourceTier.WEB.value: 6}
    ordered_tiers = (
        KnowledgeAuthorityTier.USER_EXPLICIT,
        KnowledgeAuthorityTier.CORE_CANON,
        KnowledgeAuthorityTier.WORLD_BIBLE,
        KnowledgeAuthorityTier.CURRENT_WORLD,
        KnowledgeAuthorityTier.PROJECT_RESEARCH,
        KnowledgeAuthorityTier.PUBLIC_KB,
        KnowledgeAuthorityTier.WEB,
        KnowledgeAuthorityTier.MODEL_PRIOR,
    )

    def for_research(self, source_tier: str | ResearchSourceTier) -> KnowledgeAuthorityTier:
        value = _value(source_tier)
        try:
            return KnowledgeAuthorityTier(value)
        except ValueError as exc:
            raise ResearchDomainError("RESEARCH_AUTHORITY_INVALID") from exc

    def for_canon(self, fact_type: str | CanonType) -> KnowledgeAuthorityTier:
        value = _value(fact_type)
        if value == CanonType.CORE_CANON.value:
            return KnowledgeAuthorityTier.CORE_CANON
        if value == CanonType.WORLD_FACT.value:
            return KnowledgeAuthorityTier.WORLD_BIBLE
        raise ResearchDomainError("SECRET_CANON_NOT_PUBLIC")

    def manifest(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {"authority": tier.value, "rank": index}
            for index, tier in enumerate(self.ordered_tiers)
        )

    def rank(self, tier: KnowledgeAuthorityTier) -> int:
        return self.ordered_tiers.index(tier)


@dataclass(frozen=True)
class KnowledgePacket:
    query: str
    query_fingerprint: str
    corpus_fingerprint: str
    config_fingerprint: str
    packet_fingerprint: str
    canon_refs: tuple[dict[str, Any], ...]
    world_refs: tuple[dict[str, Any], ...]
    hits: tuple[dict[str, Any], ...]
    authority_manifest: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    mode: str = "AUTHOR"

    def as_dict(self) -> dict[str, Any]:
        return {"query": self.query, "query_fingerprint": self.query_fingerprint, "corpus_fingerprint": self.corpus_fingerprint, "config_fingerprint": self.config_fingerprint, "packet_fingerprint": self.packet_fingerprint, "canon_refs": list(self.canon_refs), "world_refs": list(self.world_refs), "hits": list(self.hits), "authority_manifest": list(self.authority_manifest), "warnings": list(self.warnings), "mode": self.mode}


class KnowledgePacketBuilder:
    def build(self, db: Session, project_id: str, query_text: str, *, mode: str = "AUTHOR", filters: dict[str, Any] | None = None, request: dict[str, Any] | None = None) -> KnowledgePacket:
        project = db.get(Project, project_id)
        if not project:
            raise ResearchDomainError("PROJECT_NOT_FOUND")
        resolver_config = ResearchConfigResolver()
        envelope = resolver_config.envelope(project, request)
        config = resolver_config.retrieval_config(project, request)
        try:
            normalized_mode = KnowledgeConsumerMode(mode.upper()).value
        except (AttributeError, ValueError) as exc:
            raise ResearchDomainError("RESEARCH_MODE_INVALID") from exc
        if normalized_mode == "CHARACTER":
            hits: list[ResearchHit] = []
            canon_refs: tuple[dict[str, Any], ...] = ()
            world_refs: tuple[dict[str, Any], ...] = ()
            warnings = ("CHARACTER_RESEARCH_DISABLED",)
        else:
            hits = ResearchBM25Retriever().search(db, project_id, query_text, filters=filters, config=config)
            canon_refs = tuple(
                {
                    "source_type": "CANON_FACT",
                    "source_id": fact.id,
                    "fact_type": _value(fact.fact_type),
                    "proposition": fact.proposition,
                    "authority": KnowledgeAuthorityResolver().for_canon(fact.fact_type).value,
                }
                for fact in db.scalars(
                    select(CanonFact)
                    .where(
                        CanonFact.project_id == project_id,
                        CanonFact.fact_type.in_([CanonType.CORE_CANON, CanonType.WORLD_FACT]),
                    )
                    .order_by(CanonFact.id)
                ).all()
            )
            world_refs = tuple(
                {
                    "source_type": "WORLD_ENTITY",
                    "source_id": entity.id,
                    "entity_type": _value(entity.entity_type),
                    "name": entity.name,
                    "authority": KnowledgeAuthorityTier.CURRENT_WORLD.value,
                }
                for entity in db.scalars(
                    select(WorldEntity)
                    .where(WorldEntity.project_id == project_id, WorldEntity.active.is_(True))
                    .order_by(WorldEntity.id)
                ).all()
            )
            warnings = tuple(
                sorted({"UNTRUSTED_EXTERNAL_SOURCE" for hit in hits if hit.source_tier in {ResearchSourceTier.PUBLIC_KB.value, ResearchSourceTier.WEB.value}})
            )
        resolver = KnowledgeAuthorityResolver()
        manifest = resolver.manifest() + tuple(
            {"source_type": "RESEARCH_CHUNK", "source_id": hit.chunk_id, "authority": resolver.for_research(hit.source_tier).value, "authority_rank": hit.authority_rank}
            for hit in hits
        )
        try:
            from .retrieval_index import ResearchLexicalIndexService
            state = ResearchLexicalIndexService()._state(db, project_id)
            corpus_fingerprint = state.corpus_fingerprint if state and state.status.value == "READY" and state.corpus_fingerprint else ResearchCorpusFingerprintBuilder().build(db, project_id)
        except Exception:
            corpus_fingerprint = ResearchCorpusFingerprintBuilder().build(db, project_id)
        config_fingerprint = research_fingerprint(config.model_dump(mode="json"), "research-retrieval-config-v1")
        query_fingerprint = research_fingerprint(query_text, "knowledge-query-v1")
        hit_values = tuple(hit.as_dict() for hit in hits)
        packet_fingerprint = research_fingerprint(
            {
                "query_fingerprint": query_fingerprint,
                "corpus_fingerprint": corpus_fingerprint,
                "config_fingerprint": config_fingerprint,
                "canon_refs": canon_refs,
                "world_refs": world_refs,
                "hits": hit_values,
                "authority_manifest": manifest,
                "warnings": warnings,
                "mode": normalized_mode,
            },
            "knowledge-packet-v1",
        )
        return KnowledgePacket(query=query_text, query_fingerprint=query_fingerprint, corpus_fingerprint=corpus_fingerprint, config_fingerprint=config_fingerprint, packet_fingerprint=packet_fingerprint, canon_refs=canon_refs, world_refs=world_refs, hits=hit_values, authority_manifest=manifest, warnings=warnings, mode=normalized_mode)

    preview = build
