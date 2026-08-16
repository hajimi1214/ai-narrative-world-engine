from .fake import FakeModelProvider
from .openai_compatible import OpenAICompatibleProvider
from ..settings import Settings


def get_model_provider(settings: Settings):
    if settings.ai_provider == "openai_compatible":
        if not settings.ai_api_key:
            raise ValueError("AI provider credentials are not configured")
        return OpenAICompatibleProvider(settings.ai_base_url, settings.ai_api_key.get_secret_value(), settings.ai_timeout_seconds)
    return FakeModelProvider()
