from .errors import MODEL_PROVIDER_NOT_CONFIGURED, MODEL_PROVIDER_UNSUPPORTED, ModelProviderError
from .openai_compatible import OpenAICompatibleProvider
from ..settings import Settings


def get_model_provider(settings: Settings):
    if settings.ai_provider == "openai_compatible":
        if not settings.ai_api_key:
            raise ModelProviderError(MODEL_PROVIDER_NOT_CONFIGURED)
        return OpenAICompatibleProvider(settings.ai_base_url, settings.ai_api_key.get_secret_value(), settings.ai_timeout_seconds)
    if settings.ai_provider == "disabled":
        raise ModelProviderError(MODEL_PROVIDER_NOT_CONFIGURED)
    raise ModelProviderError(MODEL_PROVIDER_UNSUPPORTED)
