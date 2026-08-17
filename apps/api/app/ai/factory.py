from .errors import MODEL_PROVIDER_NOT_CONFIGURED, MODEL_PROVIDER_UNSUPPORTED, ModelProviderError
from .openai_compatible import OpenAICompatibleProvider
from ..settings import Settings


def get_model_provider(settings: Settings, provider: str | None = None, base_url: str | None = None):
    selected_provider = provider or settings.ai_provider
    selected_base_url = base_url or settings.ai_base_url
    if selected_provider == "openai_compatible":
        if not settings.ai_api_key:
            raise ModelProviderError(MODEL_PROVIDER_NOT_CONFIGURED)
        return OpenAICompatibleProvider(selected_base_url, settings.ai_api_key.get_secret_value(), settings.ai_timeout_seconds)
    if selected_provider == "disabled":
        raise ModelProviderError(MODEL_PROVIDER_NOT_CONFIGURED)
    raise ModelProviderError(MODEL_PROVIDER_UNSUPPORTED)
