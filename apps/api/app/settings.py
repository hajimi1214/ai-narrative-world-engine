from functools import lru_cache
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", repr=False)

    ai_provider: str = "fake"
    ai_base_url: str = "https://tokenrhythm.studio/v1"
    ai_api_key: SecretStr | None = None
    ai_character_model: str = "deepseek-v4-pro"
    ai_timeout_seconds: float = 120.0
    character_actor_mode: str = "heuristic"


@lru_cache
def get_settings() -> Settings:
    return Settings()
