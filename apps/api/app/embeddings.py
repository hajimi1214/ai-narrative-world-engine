"""Derived character-memory embedding infrastructure.

No function here creates or mutates formal memory. Index generation is explicit
and query failures deliberately degrade to deterministic Phase9 recall.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Protocol

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Float, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .ai.errors import MODEL_AUTH_FAILED, MODEL_RATE_LIMITED, MODEL_TIMEOUT, MODEL_UPSTREAM_ERROR, ModelProviderError
from .models import Character, CharacterMemory, CharacterMemoryEmbedding, EmbeddingStatus, MemoryRetrievalMode, ProjectModelConfig, ProjectProviderCredential, ProviderCredentialPurpose, ResearchChunk, ResearchChunkEmbedding, ResearchDocument, ResearchDocumentRevision


def _fingerprint(value: object, protocol: str) -> str:
    return f"{protocol}:" + hashlib.sha256(repr(value).encode()).hexdigest()


def memory_content_fingerprint(content: str) -> str:
    return _fingerprint(content, "character-memory-content-v1")


def credential_fingerprint(secret: str) -> str:
    return _fingerprint(secret, "provider-credential-v1")


_EMBEDDING_ERROR_CODES = {
    MODEL_TIMEOUT, MODEL_AUTH_FAILED, MODEL_RATE_LIMITED, MODEL_UPSTREAM_ERROR,
    "EMBEDDING_OUTPUT_INVALID", "EMBEDDING_DIMENSION_MISMATCH",
    "EMBEDDING_CONFIG_INCOMPLETE", "MODEL_CREDENTIAL_VAULT_NOT_CONFIGURED",
    "MODEL_CREDENTIAL_INVALID",
}


def embedding_error_code(exc: Exception, default: str = "EMBEDDING_INDEX_FAILED") -> str:
    """Extract only a known embedding failure code; never expose provider text."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _EMBEDDING_ERROR_CODES:
        return code
    detail = str(exc)
    for candidate in _EMBEDDING_ERROR_CODES:
        if candidate in detail:
            return candidate
    return default


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    provider: str
    model: str
    dimension: int
    latency_ms: int
    request_id: str | None = None


class EmbeddingProvider(Protocol):
    def embed(self, inputs: list[str], model: str) -> EmbeddingResult: ...


class OpenAICompatibleEmbeddingProvider:
    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 120.0, client: httpx.Client | None = None):
        self.base_url, self.api_key, self.timeout_seconds, self.client = base_url.rstrip("/"), api_key, timeout_seconds, client

    def embed(self, inputs: list[str], model: str) -> EmbeddingResult:
        started = time.perf_counter()
        try:
            if self.client:
                response = self.client.post(f"{self.base_url}/embeddings", headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, json={"model": model, "input": inputs}, timeout=self.timeout_seconds)
            else:
                response = httpx.post(f"{self.base_url}/embeddings", headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, json={"model": model, "input": inputs}, timeout=self.timeout_seconds)
        except httpx.TimeoutException as exc:
            raise ModelProviderError(MODEL_TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise ModelProviderError(MODEL_UPSTREAM_ERROR) from exc
        if response.status_code in (401, 403): raise ModelProviderError(MODEL_AUTH_FAILED, upstream_status=response.status_code)
        if response.status_code == 429: raise ModelProviderError(MODEL_RATE_LIMITED, upstream_status=response.status_code)
        if response.status_code >= 500: raise ModelProviderError(MODEL_UPSTREAM_ERROR, upstream_status=response.status_code)
        if response.status_code >= 400: raise ModelProviderError(MODEL_UPSTREAM_ERROR, upstream_status=response.status_code)
        try:
            rows = response.json()["data"]
            if len(rows) != len(inputs): raise ValueError("wrong vector count")
            ordered = [None] * len(inputs)
            for row in rows:
                index, vector = row["index"], row["embedding"]
                if not isinstance(index, int) or not 0 <= index < len(inputs) or ordered[index] is not None: raise ValueError("invalid response index")
                if not isinstance(vector, list) or not vector or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector): raise ValueError("invalid vector")
                ordered[index] = [float(value) for value in vector]
            if any(value is None for value in ordered) or len({len(value) for value in ordered}) != 1: raise ValueError("inconsistent vectors")
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise ModelProviderError("EMBEDDING_OUTPUT_INVALID") from exc
        return EmbeddingResult(ordered, self.name, model, len(ordered[0]), int((time.perf_counter() - started) * 1000), response.headers.get("x-request-id"))


class FakeEmbeddingProvider:
    name = "fake_embedding"
    def __init__(self, vectors: dict[str, list[float]] | None = None, error: ModelProviderError | None = None): self.vectors, self.error, self.calls = vectors or {}, error, 0
    def embed(self, inputs: list[str], model: str) -> EmbeddingResult:
        self.calls += 1
        if self.error: raise self.error
        values = [list(self.vectors.get(item, [float((sum(map(ord, item)) % 17) + 1), 1.0])) for item in inputs]
        if len({len(value) for value in values}) != 1: raise ModelProviderError("EMBEDDING_OUTPUT_INVALID")
        return EmbeddingResult(values, self.name, model, len(values[0]), 0, "fake-embedding")


class CredentialVault:
    def __init__(self, master_key: str | None):
        if not master_key: raise ValueError("MODEL_CREDENTIAL_VAULT_NOT_CONFIGURED")
        try: self.fernet = Fernet(master_key.encode())
        except Exception as exc: raise ValueError("MODEL_CREDENTIAL_VAULT_NOT_CONFIGURED") from exc
    def encrypt(self, secret: str) -> str: return self.fernet.encrypt(secret.encode()).decode()
    def decrypt(self, ciphertext: str) -> str:
        try: return self.fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc: raise ValueError("MODEL_CREDENTIAL_INVALID") from exc
    @staticmethod
    def hint(secret: str) -> str: return "...." + secret[-4:] if len(secret) >= 4 else "...."


@dataclass(frozen=True)
class EmbeddingRoute:
    enabled: bool; provider: str; base_url: str; model: str; dimension: int; api_key: str | None; credential_source: str; embedding_config_fingerprint: str


class EmbeddingRouter:
    def _resolve(self, db: Session, project_id: str, settings, *, require_memory_hybrid: bool) -> EmbeddingRoute:
        config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == project_id))
        if not config or not config.embedding_enabled or (require_memory_hybrid and config.memory_retrieval_mode != MemoryRetrievalMode.HYBRID_RRF):
            return EmbeddingRoute(False, "disabled", "", "", 0, None, "NONE", "")
        if config.embedding_use_main_connection:
            provider, base_url = config.provider or settings.ai_provider, config.base_url or settings.ai_base_url
            purpose, fallback = ProviderCredentialPurpose.GENERATION, settings.ai_api_key.get_secret_value() if settings.ai_api_key else None
        else:
            provider, base_url = config.embedding_provider or settings.ai_embedding_provider, config.embedding_base_url or settings.ai_embedding_base_url
            purpose, fallback = ProviderCredentialPurpose.EMBEDDING, settings.ai_embedding_api_key.get_secret_value() if settings.ai_embedding_api_key else None
        if not config.embedding_model or not config.embedding_dimension or config.embedding_dimension <= 0 or not base_url or provider != "openai_compatible":
            raise ValueError("EMBEDDING_CONFIG_INCOMPLETE")
        credential = db.scalar(select(ProjectProviderCredential).where(ProjectProviderCredential.project_id == project_id, ProjectProviderCredential.purpose == purpose))
        key, source = fallback, "ENV" if fallback else "NONE"
        if credential:
            import os
            key = CredentialVault(os.getenv("AI_CREDENTIAL_MASTER_KEY")).decrypt(credential.secret_ciphertext); source = "PROJECT"
        if not key:
            raise ValueError("EMBEDDING_CONFIG_INCOMPLETE")
        identity = (provider, (base_url or "").rstrip("/").casefold(), config.embedding_model, config.embedding_dimension, "character-memory-embedding-v1")
        return EmbeddingRoute(True, provider, base_url, config.embedding_model, config.embedding_dimension, key, source, _fingerprint(identity, "character-memory-embedding-config-v1"))

    def resolve(self, db: Session, project_id: str, settings) -> EmbeddingRoute:
        return self._resolve(db, project_id, settings, require_memory_hybrid=True)

    def resolve_research(self, db: Session, project_id: str, settings) -> EmbeddingRoute:
        """Resolve the independent vector connection for research chunks."""
        route = self._resolve(db, project_id, settings, require_memory_hybrid=False)
        if not route.enabled:
            return route
        identity = (route.provider, route.base_url.rstrip("/").casefold(), route.model, route.dimension, "research-chunk-embedding-v1")
        return EmbeddingRoute(route.enabled, route.provider, route.base_url, route.model, route.dimension, route.api_key, route.credential_source, _fingerprint(identity, "research-chunk-embedding-config-v1"))


class MemoryEmbeddingIndexService:
    def __init__(self, provider_factory=None): self.provider_factory = provider_factory
    def _provider(self, route: EmbeddingRoute):
        if not route.api_key: raise ValueError("EMBEDDING_CONFIG_INCOMPLETE")
        return self.provider_factory(route) if self.provider_factory else OpenAICompatibleEmbeddingProvider(route.base_url, route.api_key)
    def index_memories(self, db: Session, project_id: str, memory_ids: list[str] | None = None, *, rebuild: bool = False, settings=None) -> dict:
        from .settings import get_settings
        route = EmbeddingRouter().resolve(db, project_id, settings or get_settings())
        if not route.enabled: raise ValueError("EMBEDDING_CONFIG_INCOMPLETE")
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            db.execute(select(func.pg_advisory_xact_lock(func.hashtext(project_id))))
        from .character_mind import ActiveCharacterCognitionReader
        eligible = {memory.id: memory for character in db.scalars(select(Character).where(Character.project_id == project_id).order_by(Character.id)).all() for memory in ActiveCharacterCognitionReader().memories(db, project_id, character.id)}
        query = select(CharacterMemory).where(CharacterMemory.id.in_(sorted(eligible)))
        if memory_ids: query = query.where(CharacterMemory.id.in_(memory_ids))
        memories = db.scalars(query.order_by(CharacterMemory.id).with_for_update()).all()
        candidates = []
        for memory in memories:
            fp = memory_content_fingerprint(memory.content)
            existing = db.scalar(select(CharacterMemoryEmbedding).where(CharacterMemoryEmbedding.memory_id == memory.id, CharacterMemoryEmbedding.embedding_config_fingerprint == route.embedding_config_fingerprint))
            if existing and not rebuild and existing.status == EmbeddingStatus.READY and existing.content_fingerprint == fp: continue
            candidates.append((memory, existing, fp))
        if not candidates: return {"indexed": 0, "skipped": len(memories), "config_fingerprint": route.embedding_config_fingerprint}
        rows = []
        for memory, existing, fp in candidates:
            row = existing or CharacterMemoryEmbedding(project_id=project_id, character_id=memory.character_id, memory_id=memory.id, embedding_config_fingerprint=route.embedding_config_fingerprint, provider=route.provider, model=route.model, dimension=route.dimension, content_fingerprint=fp)
            row.status = EmbeddingStatus.PENDING; row.attempt_count = (row.attempt_count or 0) + 1; row.last_error_code = None; row.embedding = None
            if not existing: db.add(row)
            rows.append(row)
        db.flush()
        try:
            result = self._provider(route).embed([memory.content for memory, _, _ in candidates], route.model)
            if result.dimension != route.dimension: raise ValueError("EMBEDDING_DIMENSION_MISMATCH")
        except Exception as exc:
            safe_code = embedding_error_code(exc, "EMBEDDING_OUTPUT_INVALID")
            for row in rows: row.status = EmbeddingStatus.FAILED; row.last_error_code = safe_code
            db.flush()
            return {"indexed": 0, "failed": len(rows), "skipped": len(memories) - len(candidates), "config_fingerprint": route.embedding_config_fingerprint, "error_code": safe_code}
        for (memory, existing, fp), vector in zip(candidates, result.vectors):
            row = next(item for item in rows if item.memory_id == memory.id)
            row.provider, row.model, row.dimension, row.content_fingerprint = route.provider, route.model, route.dimension, fp
            # ``attempt_count`` was incremented when the row entered PENDING;
            # a successful provider response completes that same attempt.
            row.embedding, row.status, row.last_error_code, row.request_id, row.latency_ms, row.indexed_at = vector, EmbeddingStatus.READY, None, result.request_id, result.latency_ms, datetime.now(UTC)
            if not existing: db.add(row)
        db.flush()
        return {"indexed": len(candidates), "skipped": len(memories) - len(candidates), "config_fingerprint": route.embedding_config_fingerprint}

    def status(self, db: Session, project_id: str, settings=None) -> dict:
        from .settings import get_settings
        from .character_mind import ActiveCharacterCognitionReader
        route = EmbeddingRouter().resolve(db, project_id, settings or get_settings())
        current = [memory for character in db.scalars(select(Character).where(Character.project_id == project_id)).all() for memory in ActiveCharacterCognitionReader().memories(db, project_id, character.id)]
        ids = {memory.id for memory in current}; rows = db.scalars(select(CharacterMemoryEmbedding).where(CharacterMemoryEmbedding.project_id == project_id)).all()
        current_rows = [row for row in rows if row.memory_id in ids and row.embedding_config_fingerprint == route.embedding_config_fingerprint]
        ready = sum(row.status == EmbeddingStatus.READY and row.content_fingerprint == memory_content_fingerprint(next(memory.content for memory in current if memory.id == row.memory_id)) for row in current_rows)
        failed = sum(row.status == EmbeddingStatus.FAILED for row in current_rows)
        return {"embedding_enabled": route.enabled, "memory_retrieval_mode": getattr(route, "mode", None), "provider": route.provider, "model": route.model, "dimension": route.dimension, "embedding_config_fingerprint": route.embedding_config_fingerprint, "current_valid_memory_count": len(current), "ready_count": ready, "missing_count": max(0, len(current) - len(current_rows)), "failed_count": failed, "stale_count": sum(row.status == EmbeddingStatus.READY and row.content_fingerprint != memory_content_fingerprint(next(memory.content for memory in current if memory.id == row.memory_id)) for row in current_rows), "coverage_ratio": ready / len(current) if current else 1.0}


class ResearchChunkEmbeddingIndexService:
    """Persist vectors for the active revision of each research document."""

    def __init__(self, provider_factory=None):
        self.provider_factory = provider_factory

    def _provider(self, route: EmbeddingRoute):
        if not route.api_key:
            raise ValueError("EMBEDDING_CONFIG_INCOMPLETE")
        return self.provider_factory(route) if self.provider_factory else OpenAICompatibleEmbeddingProvider(route.base_url, route.api_key)

    @staticmethod
    def _content_fingerprint(content: str) -> str:
        return _fingerprint(content, "research-chunk-content-v1")

    def index_chunks(self, db: Session, project_id: str, chunk_ids: list[str] | None = None, *, rebuild: bool = False, settings=None) -> dict[str, Any]:
        from .settings import get_settings
        route = EmbeddingRouter().resolve_research(db, project_id, settings or get_settings())
        if not route.enabled:
            raise ValueError("EMBEDDING_CONFIG_INCOMPLETE")
        query = select(ResearchChunk).join(ResearchDocument, ResearchDocument.id == ResearchChunk.document_id).join(ResearchDocumentRevision, ResearchDocumentRevision.id == ResearchChunk.revision_id).where(
            ResearchChunk.project_id == project_id, ResearchChunk.active.is_(True), ResearchDocument.active.is_(True), ResearchDocumentRevision.active.is_(True),
        ).order_by(ResearchChunk.id)
        if chunk_ids:
            query = query.where(ResearchChunk.id.in_(chunk_ids))
        chunks = db.scalars(query.with_for_update()).all()
        candidates = []
        for chunk in chunks:
            fp = self._content_fingerprint(chunk.content)
            existing = db.scalar(select(ResearchChunkEmbedding).where(ResearchChunkEmbedding.chunk_id == chunk.id, ResearchChunkEmbedding.embedding_config_fingerprint == route.embedding_config_fingerprint))
            if existing and not rebuild and existing.status == EmbeddingStatus.READY and existing.content_fingerprint == fp:
                continue
            candidates.append((chunk, existing, fp))
        if not candidates:
            return {"indexed": 0, "failed": 0, "skipped": len(chunks), "config_fingerprint": route.embedding_config_fingerprint}
        rows = []
        for chunk, existing, fp in candidates:
            row = existing or ResearchChunkEmbedding(project_id=project_id, document_id=chunk.document_id, revision_id=chunk.revision_id, chunk_id=chunk.id, embedding_config_fingerprint=route.embedding_config_fingerprint, provider=route.provider, model=route.model, dimension=route.dimension, content_fingerprint=fp)
            row.status = EmbeddingStatus.PENDING; row.attempt_count = (row.attempt_count or 0) + 1; row.last_error_code = None; row.embedding = None
            if not existing:
                db.add(row)
            rows.append(row)
        db.flush()
        try:
            result = self._provider(route).embed([chunk.content for chunk, _, _ in candidates], route.model)
            if result.dimension != route.dimension or any(len(vector) != route.dimension for vector in result.vectors):
                raise ValueError("EMBEDDING_DIMENSION_MISMATCH")
            if len(result.vectors) != len(candidates) or any(
                any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector)
                for vector in result.vectors
            ):
                raise ValueError("EMBEDDING_OUTPUT_INVALID")
        except Exception as exc:
            safe_code = embedding_error_code(exc, "EMBEDDING_OUTPUT_INVALID")
            for row in rows:
                row.status = EmbeddingStatus.FAILED; row.last_error_code = safe_code
            db.flush()
            return {"indexed": 0, "failed": len(rows), "skipped": len(chunks) - len(candidates), "config_fingerprint": route.embedding_config_fingerprint, "error_code": safe_code}
        for (chunk, _, fp), vector in zip(candidates, result.vectors):
            row = next(item for item in rows if item.chunk_id == chunk.id)
            row.provider, row.model, row.dimension, row.content_fingerprint = route.provider, route.model, route.dimension, fp
            row.embedding, row.status, row.last_error_code, row.request_id, row.latency_ms, row.indexed_at = vector, EmbeddingStatus.READY, None, result.request_id, result.latency_ms, datetime.now(UTC)
        db.flush()
        return {"indexed": len(candidates), "failed": 0, "skipped": len(chunks) - len(candidates), "config_fingerprint": route.embedding_config_fingerprint}

    def status(self, db: Session, project_id: str, settings=None) -> dict[str, Any]:
        from .settings import get_settings
        route = EmbeddingRouter().resolve_research(db, project_id, settings or get_settings())
        chunks = db.scalars(select(ResearchChunk).join(ResearchDocument, ResearchDocument.id == ResearchChunk.document_id).join(ResearchDocumentRevision, ResearchDocumentRevision.id == ResearchChunk.revision_id).where(ResearchChunk.project_id == project_id, ResearchChunk.active.is_(True), ResearchDocument.active.is_(True), ResearchDocumentRevision.active.is_(True))).all()
        rows = db.scalars(select(ResearchChunkEmbedding).where(ResearchChunkEmbedding.project_id == project_id, ResearchChunkEmbedding.embedding_config_fingerprint == route.embedding_config_fingerprint)).all()
        by_chunk = {row.chunk_id: row for row in rows}
        ready = sum(1 for chunk in chunks if (row := by_chunk.get(chunk.id)) and row.status == EmbeddingStatus.READY and row.content_fingerprint == self._content_fingerprint(chunk.content))
        return {"embedding_enabled": route.enabled, "provider": route.provider, "model": route.model, "dimension": route.dimension, "embedding_config_fingerprint": route.embedding_config_fingerprint, "active_chunk_count": len(chunks), "ready_count": ready, "missing_count": max(0, len(chunks) - len(by_chunk)), "failed_count": sum(row.status == EmbeddingStatus.FAILED for row in rows), "coverage_ratio": ready / len(chunks) if chunks else 1.0}


class ResearchChunkEmbeddingAudit:
    def audit(self, db: Session, embedding_id: str) -> dict[str, Any]:
        row = db.get(ResearchChunkEmbedding, embedding_id); chunk = db.get(ResearchChunk, row.chunk_id) if row else None
        if not row or not chunk or row.project_id != chunk.project_id or row.document_id != chunk.document_id or row.revision_id != chunk.revision_id or row.content_fingerprint != ResearchChunkEmbeddingIndexService._content_fingerprint(chunk.content):
            raise ValueError("RESEARCH_CHUNK_EMBEDDING_INTEGRITY_INVALID")
        vector = row.embedding or []
        if row.status == EmbeddingStatus.READY and (not vector or len(vector) != row.dimension or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector)):
            raise ValueError("RESEARCH_CHUNK_EMBEDDING_INTEGRITY_INVALID")
        return {"valid": True, "embedding_id": row.id, "chunk_id": chunk.id}


class ResearchChunkSemanticRetriever:
    def retrieve(self, db: Session, project_id: str, query_vector: list[float], config_fingerprint: str, top_k: int = 12, filters: dict[str, Any] | None = None) -> list[tuple[str, float]]:
        filters = filters or {}
        clauses = [ResearchChunkEmbedding.project_id == project_id, ResearchChunkEmbedding.embedding_config_fingerprint == config_fingerprint, ResearchChunkEmbedding.status == EmbeddingStatus.READY, ResearchChunkEmbedding.dimension == len(query_vector), ResearchChunk.active.is_(True), ResearchDocument.active.is_(True), ResearchDocumentRevision.active.is_(True)]
        if filters.get("document_ids"):
            clauses.append(ResearchChunk.document_id.in_(filters["document_ids"]))
        if filters.get("source_tiers"):
            clauses.append(ResearchDocument.source_tier.in_(filters["source_tiers"]))
        if filters.get("source_kinds"):
            clauses.append(ResearchDocument.source_kind.in_(filters["source_kinds"]))
        base = select(ResearchChunkEmbedding).join(ResearchChunk, ResearchChunk.id == ResearchChunkEmbedding.chunk_id).join(ResearchDocument, ResearchDocument.id == ResearchChunk.document_id).join(ResearchDocumentRevision, ResearchDocumentRevision.id == ResearchChunk.revision_id).where(*clauses)
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            distance = cast(ResearchChunkEmbedding.embedding.op("<=>")(query_vector), Float).label("distance")
            rows = db.execute(select(ResearchChunkEmbedding, distance).select_from(ResearchChunkEmbedding).join(ResearchChunk, ResearchChunk.id == ResearchChunkEmbedding.chunk_id).join(ResearchDocument, ResearchDocument.id == ResearchChunk.document_id).join(ResearchDocumentRevision, ResearchDocumentRevision.id == ResearchChunk.revision_id).where(*clauses).order_by(distance, ResearchChunkEmbedding.chunk_id).limit(top_k)).all()
            values = []
            for row, distance_value in rows:
                chunk = db.get(ResearchChunk, row.chunk_id)
                if not chunk or row.content_fingerprint != ResearchChunkEmbeddingIndexService._content_fingerprint(chunk.content):
                    continue
                values.append((row.chunk_id, 1.0 - float(distance_value)))
            return values
        rows = db.scalars(base).all()
        values = []
        for row in rows:
            chunk = db.get(ResearchChunk, row.chunk_id)
            if not chunk or row.content_fingerprint != ResearchChunkEmbeddingIndexService._content_fingerprint(chunk.content):
                continue
            vector = row.embedding or []
            denom = math.sqrt(sum(value * value for value in query_vector)) * math.sqrt(sum(value * value for value in vector))
            similarity = sum(left * right for left, right in zip(query_vector, vector)) / denom if denom else 0.0
            values.append((row.chunk_id, similarity))
        return sorted(values, key=lambda item: (-item[1], item[0]))[:top_k]


class CharacterMemoryEmbeddingAudit:
    def audit(self, db: Session, embedding_id: str) -> dict:
        row = db.get(CharacterMemoryEmbedding, embedding_id); memory = db.get(CharacterMemory, row.memory_id) if row else None; character = db.get(Character, row.character_id) if row else None
        if not row or not memory or not character or row.character_id != memory.character_id or row.project_id != character.project_id or row.content_fingerprint != memory_content_fingerprint(memory.content): raise ValueError("MEMORY_EMBEDDING_INTEGRITY_INVALID")
        vector = row.embedding or []
        if row.status == EmbeddingStatus.READY and (not vector or len(vector) != row.dimension or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector)): raise ValueError("MEMORY_EMBEDDING_INTEGRITY_INVALID")
        return {"valid": True, "embedding_id": row.id}


class CharacterMemoryRRFMerger:
    """Rank fusion by ordinal only; raw scores never share a scale."""
    def __init__(self, rrf_k: int = 60): self.rrf_k = rrf_k
    def merge(self, deterministic_ids: list[str], semantic_ids: list[str]) -> list[tuple[str, float]]:
        scores: dict[str, float] = {}
        for rank, memory_id in enumerate(deterministic_ids, 1): scores[memory_id] = scores.get(memory_id, 0.0) + 1 / (self.rrf_k + rank)
        for rank, memory_id in enumerate(semantic_ids, 1): scores[memory_id] = scores.get(memory_id, 0.0) + 1 / (self.rrf_k + rank)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class CharacterMemorySemanticRetriever:
    def retrieve(self, db: Session, project_id: str, character_id: str, query_vector: list[float], config_fingerprint: str, eligible_memory_ids: Iterable[str], top_k: int = 12, min_similarity: float | None = None) -> list[tuple[str, float]]:
        eligible_memory_ids = tuple(sorted(set(eligible_memory_ids)))
        if not eligible_memory_ids:
            return []
        base = select(CharacterMemoryEmbedding).where(CharacterMemoryEmbedding.project_id == project_id, CharacterMemoryEmbedding.character_id == character_id, CharacterMemoryEmbedding.embedding_config_fingerprint == config_fingerprint, CharacterMemoryEmbedding.status == EmbeddingStatus.READY, CharacterMemoryEmbedding.dimension == len(query_vector))
        base = base.where(CharacterMemoryEmbedding.memory_id.in_(eligible_memory_ids))
        # PostgreSQL does the ranked vector work in the database.  SQLite keeps
        # a deterministic cosine fallback for unit tests and local development.
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            # The mapped column is a portable TypeDecorator, so invoke the
            # pgvector cosine operator directly instead of depending on the
            # Vector comparator that is intentionally absent on SQLite.
            distance = cast(CharacterMemoryEmbedding.embedding.op("<=>")(query_vector), Float).label("distance")
            rows = [(row, float(distance_value)) for row, distance_value in db.execute(select(CharacterMemoryEmbedding, distance).where(*base.whereclause.get_children()).order_by(distance).limit(top_k)).all()]
            values = []
            for row, distance_value in rows:
                memory = db.get(CharacterMemory, row.memory_id)
                if not memory or row.content_fingerprint != memory_content_fingerprint(memory.content): continue
                similarity = 1.0 - distance_value
                if min_similarity is None or similarity >= min_similarity: values.append((row.memory_id, similarity))
            return values
        rows = db.scalars(base).all()
        values = []
        for row in rows:
            memory = db.get(CharacterMemory, row.memory_id)
            if not memory or row.content_fingerprint != memory_content_fingerprint(memory.content): continue
            vector = row.embedding or []
            denom = math.sqrt(sum(value * value for value in query_vector)) * math.sqrt(sum(value * value for value in vector))
            similarity = sum(left * right for left, right in zip(query_vector, vector)) / denom if denom else 0.0
            if min_similarity is None or similarity >= min_similarity: values.append((row.memory_id, similarity))
        return sorted(values, key=lambda item: (-item[1], item[0]))[:top_k]


class CharacterSemanticCueBuilder:
    """Build a bounded, canonical semantic cue from visible structured data."""

    PROTOCOL = "character-memory-semantic-cue-v1"
    MAX_CHARS = 1800
    _PRIVATE_KEYS = {
        "director_only", "director_reasoning", "secret", "hidden", "private",
        "canon", "prose", "summary", "rationale", "reasoning", "gravity",
        "story_gravity", "required_canon", "forbidden_reveals", "allowed_reveals",
        "expected_progress",
    }

    def _safe(self, value: Any, depth: int = 0) -> Any:
        if depth > 3:
            return None
        if isinstance(value, str):
            return unicodedata.normalize("NFKC", value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, list):
            return [item for item in (self._safe(item, depth + 1) for item in value[:32]) if item is not None]
        if isinstance(value, dict):
            return {str(key): safe for key, item in sorted(value.items(), key=lambda pair: str(pair[0])) if str(key).casefold() not in self._PRIVATE_KEYS and (safe := self._safe(item, depth + 1)) is not None}
        return str(value)

    def build(self, cues: dict[str, tuple[str, ...]] | None = None, *, character: Any = None, scene: Any = None, location: Any = None, participants: Iterable[Any] | None = None) -> str:
        cues = cues or {}
        payload: dict[str, Any] = {}
        for key in ("entity_ids", "participant_ids", "thread_ids", "location_ids", "item_ids"):
            values = sorted({str(value) for value in cues.get(key, ()) if value is not None})
            if values:
                payload[key] = values

        def attr(source: Any, key: str, default=None):
            return source.get(key, default) if isinstance(source, dict) else getattr(source, key, default)

        if character is not None:
            payload["character"] = {key: self._safe(attr(character, key, {})) for key in ("goals", "current_state", "emotional_state")}
        if scene is not None:
            entry = attr(scene, "entry_state", {}) or {}
            visible = entry.get("visible_context", {}) if isinstance(entry, dict) else {}
            actor_id = attr(character, "id") if character is not None else None
            actor_visible = (entry.get("actor_visible_context", {}) or {}).get(actor_id, {}) if isinstance(entry, dict) and actor_id else {}
            payload["visible_context"] = self._safe(visible)
            payload["actor_visible_context"] = self._safe(actor_visible)
            payload["scene_location"] = self._safe(attr(scene, "location_id"))
            payload["scene_participants"] = sorted(str(value) for value in (attr(scene, "participants", []) or []))
        if location is not None:
            payload["location"] = self._safe({"id": attr(location, "id"), "name": attr(location, "name")})
        if participants is not None:
            participant_values = [self._safe({"id": attr(item, "id"), "name": attr(item, "name")}) for item in participants]
            payload["participants"] = sorted(participant_values, key=lambda item: (str(item.get("id", "")), str(item.get("name", ""))) if isinstance(item, dict) else str(item))
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return rendered[: self.MAX_CHARS]


class CharacterMemoryHybridRetriever:
    """Fuse deterministic Phase9 recall with optional vector candidates."""

    def __init__(self, semantic: CharacterMemorySemanticRetriever | None = None, merger: CharacterMemoryRRFMerger | None = None):
        self.semantic = semantic or CharacterMemorySemanticRetriever()
        self.merger = merger or CharacterMemoryRRFMerger()

    def merge(self, eligible_items: dict[str, dict] | list[dict], deterministic_ids: list[str] | list[tuple[str, Any]], semantic_ids: list[tuple[str, float]] | None = None, top_k: int = 12, rrf_k: int | None = None, strong_memory_ids: set[str] | None = None, max_per_source_scene: int = 3) -> list[dict]:
        # The list form remains a small compatibility adapter for callers from
        # the previous phase; production callers pass the full eligible map.
        if semantic_ids is None:
            semantic_ids = deterministic_ids  # type: ignore[assignment]
            deterministic_ids = list(eligible_items) if isinstance(eligible_items, dict) else [item.get("memory_id") for item in eligible_items]
        by_id = eligible_items if isinstance(eligible_items, dict) else {item.get("memory_id"): item for item in eligible_items if item.get("memory_id")}
        deterministic_ids = [item[0] if isinstance(item, tuple) else item for item in deterministic_ids]
        merger = self.merger if rrf_k is None or rrf_k == self.merger.rrf_k else CharacterMemoryRRFMerger(rrf_k)
        fused = merger.merge(deterministic_ids, [memory_id for memory_id, _ in semantic_ids])
        strong_memory_ids = strong_memory_ids or set()
        selected: list[dict] = []; source_counts: dict[str, int] = {}
        from .character_mind import memory_source_bucket
        for memory_id, _score in fused:
            item = by_id.get(memory_id)
            if item is None:
                continue
            bucket = memory_source_bucket(item)
            if source_counts.get(bucket, 0) >= max_per_source_scene and memory_id not in strong_memory_ids:
                continue
            selected.append(item); source_counts[bucket] = source_counts.get(bucket, 0) + 1
            if len(selected) >= top_k:
                break
        return selected

    def retrieve(self, deterministic_items: list[dict], *, query_vector: list[float] | None = None, semantic_ids: list[tuple[str, float]] | None = None, top_k: int = 12) -> list[dict]:
        return self.merge({item.get("memory_id"): item for item in deterministic_items}, [item.get("memory_id") for item in deterministic_items], semantic_ids or [], top_k) if query_vector is not None or semantic_ids is not None else deterministic_items[:top_k]
