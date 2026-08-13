"""Factorias pequenas para seleccionar proveedor sin acoplar consumidores."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from .base import EmbeddingsClient, GenerationClient
from .openai_compatible import (
    OpenAICompatibleChatClient,
    OpenAICompatibleEmbeddings,
    OpenAIResponsesClient,
)


ProviderName = Literal["codex", "openai", "ollama", "vllm"]
EmbeddingProviderName = Literal["openai", "ollama", "vllm"]


def create_generation_client(
    provider: ProviderName,
    *,
    model: str,
    reasoning_effort: str | None = None,
    timeout_seconds: float = 180.0,
    api_key: str | None = None,
    base_url: str | None = None,
    codex_executable: str = "codex",
    codex_workdir: str | Path | None = None,
    client: Any | None = None,
) -> GenerationClient:
    """Construye un cliente con la misma interfaz para los cuatro backends."""

    normalized = str(provider).strip().lower()
    if normalized == "codex":
        if client is not None:
            raise ValueError("client solo se admite para proveedores HTTP")
        from core.codex_client import CodexClient

        return CodexClient(
            executable=codex_executable,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            workdir=codex_workdir,
        )
    if normalized == "openai":
        return OpenAIResponsesClient(
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            api_key=api_key,
            base_url=base_url,
            client=client,
        )
    if normalized in {"ollama", "vllm"}:
        default_url = (
            "http://localhost:11434/v1"
            if normalized == "ollama"
            else "http://localhost:8000/v1"
        )
        return OpenAICompatibleChatClient(
            provider=normalized,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            api_key=api_key,
            base_url=base_url or default_url,
            client=client,
        )
    raise ValueError(f"Proveedor de generacion desconocido: {provider!r}")


def create_embeddings_client(
    provider: EmbeddingProviderName,
    *,
    model: str,
    timeout_seconds: float = 60.0,
    api_key: str | None = None,
    base_url: str | None = None,
    batch_size: int = 64,
    dimensions: int | None = None,
    client: Any | None = None,
    callback: Any | None = None,
) -> EmbeddingsClient:
    """Crea embeddings sobre la superficie comun ``/v1/embeddings``."""

    normalized = str(provider).strip().lower()
    defaults = {
        "openai": None,
        "ollama": "http://localhost:11434/v1",
        "vllm": "http://localhost:8000/v1",
    }
    if normalized not in defaults:
        raise ValueError(f"Proveedor de embeddings desconocido: {provider!r}")
    return OpenAICompatibleEmbeddings(
        model=model,
        provider="openai_api" if normalized == "openai" else normalized,
        base_url=base_url if base_url is not None else defaults[normalized],
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        batch_size=batch_size,
        dimensions=dimensions,
        client=client,
        callback=callback,
    )


__all__ = [
    "EmbeddingProviderName",
    "ProviderName",
    "create_embeddings_client",
    "create_generation_client",
]
