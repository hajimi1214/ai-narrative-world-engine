"""Live, redacted connectivity check for the configured generation/embedding routes."""
from __future__ import annotations

from app.ai.factory import get_model_provider
from app.embeddings import OpenAICompatibleEmbeddingProvider
from app.settings import Settings


def main() -> None:
    settings = Settings()
    generation = get_model_provider(settings)
    result = generation.generate(
        [
            {"role": "system", "content": "Return only JSON."},
            {"role": "user", "content": '{"phase":"phase1","ok":true}'},
        ],
        settings.ai_writer_model,
    )
    print(f"generation: provider={result.provider} model={result.model} chars={len(result.content)} latency_ms={result.latency_ms}")

    if not settings.ai_embedding_api_key or not settings.ai_embedding_base_url or not settings.ai_embedding_model:
        raise RuntimeError("embedding route is not configured")
    embedding = OpenAICompatibleEmbeddingProvider(settings.ai_embedding_base_url, settings.ai_embedding_api_key.get_secret_value())
    vector = embedding.embed(["phase1 embedding connectivity test"], settings.ai_embedding_model)
    print(f"embedding: provider={vector.provider} model={vector.model} dimension={vector.dimension} latency_ms={vector.latency_ms}")


if __name__ == "__main__":
    main()
