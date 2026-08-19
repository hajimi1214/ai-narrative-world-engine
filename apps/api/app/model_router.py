from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from .models import ProjectModelConfig, ProjectProviderCredential, ProviderCredentialPurpose


class ProjectModelConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str | None = None
    base_url: str | None = None
    character_model: str | None = None
    world_model: str | None = None
    director_model: str | None = None
    repair_model: str | None = None
    writer_model: str | None = None
    critic_model: str | None = None
    fallback_model: str | None = None
    auto_failover: bool = False
    max_repair_attempts: int = Field(default=1, ge=0, le=3)
    embedding_enabled: bool = False
    embedding_use_main_connection: bool = True
    embedding_provider: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_dimension: int | None = Field(default=None, gt=0)
    memory_retrieval_mode: str = "DETERMINISTIC"
    memory_vector_top_k: int = Field(default=12, gt=0, le=100)
    memory_rrf_k: int = Field(default=60, gt=0, le=1000)
    memory_semantic_min_similarity: float | None = Field(default=None, ge=-1, le=1)
    api_key: str | None = None
    embedding_api_key: str | None = None
    clear_api_key: bool = False
    clear_embedding_api_key: bool = False

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value):
        if value is not None and value not in {"disabled", "openai_compatible"}:
            raise ValueError("unsupported provider")
        return value

    @field_validator("embedding_provider")
    @classmethod
    def validate_embedding_provider(cls, value):
        if value is not None and value not in {"disabled", "openai_compatible"}:
            raise ValueError("unsupported embedding provider")
        return value

    @field_validator("memory_retrieval_mode")
    @classmethod
    def validate_memory_retrieval_mode(cls, value):
        if value not in {"DETERMINISTIC", "HYBRID_RRF"}:
            raise ValueError("unsupported memory retrieval mode")
        return value

    @field_validator("base_url", "embedding_base_url")
    @classmethod
    def validate_url(cls, value):
        if value is None:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("base_url must be an http(s) URL without credentials")
        return value


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    base_url: str
    model: str


class ModelRouter:
    FIELDS = {"CHARACTER": "character_model", "WORLD": "world_model", "DIRECTOR": "director_model", "REPAIR": "repair_model", "WRITER": "writer_model", "CRITIC": "critic_model"}
    DEFAULTS = {"CHARACTER": "ai_character_model", "WORLD": "ai_world_model", "DIRECTOR": "ai_director_model", "REPAIR": "ai_repair_model", "WRITER": "ai_writer_model", "CRITIC": "ai_critic_model"}

    def resolve(self, db, project_id: str, settings, role: str) -> ModelRoute:
        if role not in self.FIELDS:
            raise ValueError("UNKNOWN_MODEL_ROLE")
        config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == project_id))
        field = self.FIELDS[role]
        model = getattr(config, field) if config and getattr(config, field) else getattr(settings, self.DEFAULTS[role])
        return ModelRoute(config.provider if config and config.provider else settings.ai_provider, config.base_url if config and config.base_url else settings.ai_base_url, model)


class ProviderCredentialResolver:
    """Resolve a project credential without putting it into route metadata."""

    def generation_key(self, db, project_id: str, settings) -> str | None:
        import os
        credential = db.scalar(select(ProjectProviderCredential).where(ProjectProviderCredential.project_id == project_id, ProjectProviderCredential.purpose == ProviderCredentialPurpose.GENERATION))
        if credential:
            from .embeddings import CredentialVault
            return CredentialVault(os.getenv("AI_CREDENTIAL_MASTER_KEY")).decrypt(credential.secret_ciphertext)
        return settings.ai_api_key.get_secret_value() if settings.ai_api_key else None
