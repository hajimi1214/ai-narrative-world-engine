"""Derived character-memory embedding infrastructure.

No function here creates or mutates formal memory. Index generation is explicit
and query failures deliberately degrade to deterministic Phase9 recall.
"""
from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Float, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .ai.errors import MODEL_AUTH_FAILED, MODEL_RATE_LIMITED, MODEL_TIMEOUT, MODEL_UPSTREAM_ERROR, ModelProviderError
from .models import Character, CharacterMemory, CharacterMemoryEmbedding, EmbeddingStatus, MemoryRetrievalMode, ProjectModelConfig, ProjectProviderCredential, ProviderCredentialPurpose


def _fingerprint(value: object, protocol: str) -> str:
    return f"{protocol}:" + hashlib.sha256(repr(value).encode()).hexdigest()


def memory_content_fingerprint(content: str) -> str:
    return _fingerprint(content, "character-memory-content-v1")


def credential_fingerprint(secret: str) -> str:
    return _fingerprint(secret, "provider-credential-v1")


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
    def resolve(self, db: Session, project_id: str, settings) -> EmbeddingRoute:
        config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == project_id))
        if not config or not config.embedding_enabled or config.memory_retrieval_mode != MemoryRetrievalMode.HYBRID_RRF:
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


class MemoryEmbeddingIndexService:
    def __init__(self, provider_factory=None): self.provider_factory = provider_factory
    def _provider(self, route: EmbeddingRoute):
        if not route.api_key: raise ValueError("EMBEDDING_CONFIG_INCOMPLETE")
        return self.provider_factory(route) if self.provider_factory else OpenAICompatibleEmbeddingProvider(route.base_url, route.api_key)
    def index_memories(self, db: Session, project_id: str, memory_ids: list[str] | None = None, *, rebuild: bool = False, settings=None) -> dict:
        from .settings import get_settings
        route = EmbeddingRouter().resolve(db, project_id, settings or get_settings())
        if not route.enabled: raise ValueError("EMBEDDING_CONFIG_INCOMPLETE")
        query = select(CharacterMemory).join(Character, Character.id == CharacterMemory.character_id).where(Character.project_id == project_id)
        if memory_ids: query = query.where(CharacterMemory.id.in_(memory_ids))
        memories = db.scalars(query.order_by(CharacterMemory.id).with_for_update()).all()
        candidates = []
        for memory in memories:
            fp = memory_content_fingerprint(memory.content)
            existing = db.scalar(select(CharacterMemoryEmbedding).where(CharacterMemoryEmbedding.memory_id == memory.id, CharacterMemoryEmbedding.embedding_config_fingerprint == route.embedding_config_fingerprint))
            if existing and not rebuild and existing.status == EmbeddingStatus.READY and existing.content_fingerprint == fp: continue
            candidates.append((memory, existing, fp))
        if not candidates: return {"indexed": 0, "skipped": len(memories), "config_fingerprint": route.embedding_config_fingerprint}
        try: result = self._provider(route).embed([memory.content for memory, _, _ in candidates], route.model)
        except ModelProviderError: raise
        if result.dimension != route.dimension: raise ValueError("EMBEDDING_DIMENSION_MISMATCH")
        for (memory, existing, fp), vector in zip(candidates, result.vectors):
            row = existing or CharacterMemoryEmbedding(project_id=project_id, character_id=memory.character_id, memory_id=memory.id, embedding_config_fingerprint=route.embedding_config_fingerprint, provider=route.provider, model=route.model, dimension=route.dimension, content_fingerprint=fp)
            row.provider, row.model, row.dimension, row.content_fingerprint = route.provider, route.model, route.dimension, fp
            row.embedding, row.status, row.attempt_count, row.last_error_code, row.request_id, row.latency_ms, row.indexed_at = vector, EmbeddingStatus.READY, (row.attempt_count or 0) + 1, None, result.request_id, result.latency_ms, datetime.now(UTC)
            if not existing: db.add(row)
        db.flush()
        return {"indexed": len(candidates), "skipped": len(memories) - len(candidates), "config_fingerprint": route.embedding_config_fingerprint}


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
    def retrieve(self, db: Session, project_id: str, character_id: str, query_vector: list[float], config_fingerprint: str, top_k: int = 12, min_similarity: float | None = None) -> list[tuple[str, float]]:
        base = select(CharacterMemoryEmbedding).where(CharacterMemoryEmbedding.project_id == project_id, CharacterMemoryEmbedding.character_id == character_id, CharacterMemoryEmbedding.embedding_config_fingerprint == config_fingerprint, CharacterMemoryEmbedding.status == EmbeddingStatus.READY, CharacterMemoryEmbedding.dimension == len(query_vector))
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
    """Build a provider query from structured cues only; prose is excluded."""

    def build(self, cues: dict[str, tuple[str, ...]] | None) -> str:
        cues = cues or {}
        parts: list[str] = []
        for key in ("entity_ids", "participant_ids", "thread_ids", "location_ids", "item_ids"):
            values = sorted({str(value) for value in cues.get(key, ()) if value is not None})
            if values:
                parts.append(f"{key}=" + ",".join(values))
        return "|".join(parts)


class CharacterMemoryHybridRetriever:
    """Fuse deterministic Phase9 recall with optional vector candidates."""

    def __init__(self, semantic: CharacterMemorySemanticRetriever | None = None, merger: CharacterMemoryRRFMerger | None = None):
        self.semantic = semantic or CharacterMemorySemanticRetriever()
        self.merger = merger or CharacterMemoryRRFMerger()

    def merge(self, deterministic_items: list[dict], semantic_ids: list[tuple[str, float]], top_k: int = 12, rrf_k: int | None = None) -> list[dict]:
        by_id = {item.get("memory_id"): item for item in deterministic_items if item.get("memory_id")}
        deterministic_ids = list(by_id)
        merger = self.merger if rrf_k is None or rrf_k == self.merger.rrf_k else CharacterMemoryRRFMerger(rrf_k)
        fused = merger.merge(deterministic_ids, [memory_id for memory_id, _ in semantic_ids])
        for memory_id, _score in fused:
            if memory_id not in by_id:
                # A semantic candidate is only eligible when its deterministic
                # authority has supplied the corresponding memory record.
                continue
            by_id[memory_id]["retrieval_mode"] = "HYBRID_RRF"
        return [by_id[memory_id] for memory_id, _ in fused if memory_id in by_id][:top_k]

    def retrieve(self, deterministic_items: list[dict], *, query_vector: list[float] | None = None, semantic_ids: list[tuple[str, float]] | None = None, top_k: int = 12) -> list[dict]:
        return self.merge(deterministic_items, semantic_ids or [], top_k) if query_vector is not None or semantic_ids is not None else deterministic_items[:top_k]
