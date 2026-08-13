"""Superficie publica de proveedores multiproveedor de CENtinela."""

from .base import (
    EmbeddingsClient,
    GenerationClient,
    ProviderConfigurationError,
    ProviderError,
    ProviderExecutionError,
    ProviderHealth,
    ProviderOutputError,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUsage,
)
from .factory import (
    EmbeddingProviderName,
    ProviderName,
    create_embeddings_client,
    create_generation_client,
)
from .openai_compatible import (
    OpenAICompatibleChatClient,
    OpenAICompatibleEmbeddings,
    OpenAIResponsesClient,
)

__all__ = [
    "EmbeddingProviderName",
    "EmbeddingsClient",
    "GenerationClient",
    "OpenAICompatibleChatClient",
    "OpenAICompatibleEmbeddings",
    "OpenAIResponsesClient",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderExecutionError",
    "ProviderHealth",
    "ProviderName",
    "ProviderOutputError",
    "ProviderResult",
    "ProviderTimeoutError",
    "ProviderUsage",
    "create_embeddings_client",
    "create_generation_client",
]
