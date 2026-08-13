"""Adaptadores para OpenAI Responses y endpoints OpenAI-compatible.

OpenAI usa su API Responses nativa. Ollama y vLLM usan Chat Completions, una
superficie ampliamente implementada por ambos servidores. Todos aceptan el
mismo contrato local, conservan uso exacto reportado por el backend y emiten
callbacks LangChain para la observabilidad de CENtinela.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from .base import (
    Clock,
    JsonValue,
    ProviderConfigurationError,
    ProviderHealth,
    ProviderOutputError,
    ProviderResult,
    ProviderUsage,
    accounting_model,
    callback_metadata,
    configured_callbacks,
    elapsed,
    nonnegative_int,
    notify_end,
    notify_error,
    notify_start,
    object_value,
    optional_nonempty,
    positive_timeout,
    safe_provider_error,
    validate_endpoint,
    validate_schema,
)


def _sdk_client(
    *,
    api_key: str | None,
    base_url: str | None,
    timeout_seconds: float,
    max_retries: int,
) -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - depende de instalacion
        raise ProviderConfigurationError(
            "Falta el paquete openai; instala las dependencias del proyecto"
        ) from exc

    kwargs: dict[str, Any] = {
        "timeout": timeout_seconds,
        "max_retries": max_retries,
    }
    if api_key is not None:
        kwargs["api_key"] = api_key
    if base_url is not None:
        kwargs["base_url"] = base_url
    try:
        return OpenAI(**kwargs)
    except Exception as exc:
        raise ProviderConfigurationError(
            f"No se pudo inicializar el cliente OpenAI: {type(exc).__name__}"
        ) from None


def _usage_from_response(response: Any, *, responses_api: bool) -> ProviderUsage | None:
    usage = object_value(response, "usage")
    if usage is None:
        return None
    if responses_api:
        prompt = object_value(usage, "input_tokens")
        completion = object_value(usage, "output_tokens")
        prompt_details = object_value(usage, "input_tokens_details")
        completion_details = object_value(usage, "output_tokens_details")
    else:
        prompt = object_value(usage, "prompt_tokens")
        completion = object_value(usage, "completion_tokens")
        prompt_details = object_value(usage, "prompt_tokens_details")
        completion_details = object_value(usage, "completion_tokens_details")
    if prompt is None and completion is None:
        return None
    return ProviderUsage(
        prompt_tokens=nonnegative_int(prompt),
        completion_tokens=nonnegative_int(completion),
        cached_prompt_tokens=nonnegative_int(object_value(prompt_details, "cached_tokens")),
        reasoning_tokens=nonnegative_int(
            object_value(completion_details, "reasoning_tokens")
        ),
    )


def _responses_text(response: Any) -> str:
    direct = object_value(response, "output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    fragments: list[str] = []
    output = object_value(response, "output", ())
    if isinstance(output, Sequence) and not isinstance(output, (str, bytes)):
        for item in output:
            content = object_value(item, "content", ())
            if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
                continue
            for block in content:
                text = object_value(block, "text")
                if isinstance(text, str) and text.strip():
                    fragments.append(text.strip())
    if fragments:
        return "\n".join(fragments)
    raise ProviderOutputError("OpenAI termino sin producir texto de salida")


def _chat_text(response: Any) -> str:
    choices = object_value(response, "choices", ())
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise ProviderOutputError("El endpoint no devolvio ninguna eleccion")
    message = object_value(choices[0], "message")
    content = object_value(message, "content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        fragments = [
            str(text).strip()
            for block in content
            if (text := object_value(block, "text")) is not None and str(text).strip()
        ]
        if fragments:
            return "\n".join(fragments)
    raise ProviderOutputError("El endpoint termino sin producir texto de salida")


def _structured_data(text: str, schema: Mapping[str, Any] | None) -> JsonValue | None:
    if schema is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ProviderOutputError(
            "El proveedor produjo una respuesta que no es JSON valido"
        ) from None


def _listed_models(response: Any) -> tuple[str, ...]:
    data = object_value(response, "data", response)
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        return ()
    identifiers = {
        str(identifier)
        for item in data
        if (identifier := object_value(item, "id")) is not None
    }
    return tuple(sorted(identifiers))


class _OpenAIClientBase:
    def __init__(
        self,
        *,
        provider: str,
        model: str | None,
        reasoning_effort: str | None,
        timeout_seconds: float,
        base_url: str | None,
        api_key: str | None,
        billing_mode: Literal["api", "self_hosted"],
        client: Any | None,
        max_retries: int,
        clock: Clock,
    ) -> None:
        self.provider = optional_nonempty(provider, name="provider") or "provider"
        self.model = optional_nonempty(model, name="model")
        self.reasoning_effort = optional_nonempty(
            reasoning_effort,
            name="reasoning_effort",
        )
        self.timeout_seconds = positive_timeout(timeout_seconds)
        self.base_url = validate_endpoint(base_url)
        self.billing_mode = billing_mode
        self._api_key = api_key
        self._client = client
        if isinstance(max_retries, bool) or not 0 <= int(max_retries) <= 10:
            raise ValueError("max_retries debe estar entre 0 y 10")
        self.max_retries = int(max_retries)
        self._clock = clock

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = _sdk_client(
                api_key=self._api_key,
                base_url=self.base_url,
                timeout_seconds=self.timeout_seconds,
                max_retries=self.max_retries,
            )
        return self._client

    def _effective_values(
        self,
        *,
        model: str | None,
        reasoning_effort: str | None,
        timeout_seconds: float | None,
    ) -> tuple[str, str | None, float]:
        effective_model = self.model if model is None else optional_nonempty(model, name="model")
        if effective_model is None:
            raise ProviderConfigurationError("Debe configurarse un modelo de inferencia")
        effective_reasoning = (
            self.reasoning_effort
            if reasoning_effort is None
            else optional_nonempty(reasoning_effort, name="reasoning_effort")
        )
        effective_timeout = (
            self.timeout_seconds
            if timeout_seconds is None
            else positive_timeout(timeout_seconds)
        )
        return effective_model, effective_reasoning, effective_timeout

    def health(self, *, timeout_seconds: float = 5.0) -> ProviderHealth:
        timeout = positive_timeout(timeout_seconds)
        started = self._clock()
        try:
            models_response = self._get_client().models.list(timeout=timeout)
            models = _listed_models(models_response)
            model_available = None if self.model is None else self.model in models
            available = model_available is not False
            detail = (
                "Endpoint operativo"
                if available
                else f"Endpoint operativo, pero el modelo {self.model!r} no esta cargado"
            )
            return ProviderHealth(
                provider=self.provider,
                available=available,
                reachable=True,
                authenticated=True if self.billing_mode == "api" else None,
                model=self.model,
                model_available=model_available,
                endpoint=self.base_url,
                latency_seconds=elapsed(self._clock, started),
                detail=detail,
                models=models,
            )
        except Exception as exc:
            return ProviderHealth(
                provider=self.provider,
                available=False,
                reachable=False,
                authenticated=False if self.billing_mode == "api" else None,
                model=self.model,
                model_available=None,
                endpoint=self.base_url,
                latency_seconds=elapsed(self._clock, started),
                detail=f"Endpoint no disponible: {type(exc).__name__}",
            )


class OpenAIResponsesClient(_OpenAIClientBase):
    """Cliente de OpenAI basado en la API Responses."""

    def __init__(
        self,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: float = 180.0,
        api_key: str | None = None,
        base_url: str | None = None,
        client: Any | None = None,
        max_retries: int = 2,
        clock: Clock = time.perf_counter,
    ) -> None:
        super().__init__(
            provider="openai_api",
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            base_url=base_url,
            api_key=api_key,
            billing_mode="api",
            client=client,
            max_retries=max_retries,
            clock=clock,
        )

    def invoke(
        self,
        prompt: str,
        config: Mapping[str, Any] | None = None,
        *,
        output_schema: Mapping[str, Any] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: float | None = None,
    ) -> ProviderResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt debe ser un texto no vacio")
        schema = validate_schema(output_schema)
        effective_model, effective_reasoning, effective_timeout = self._effective_values(
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
        )
        metadata = callback_metadata(
            config,
            provider=self.provider,
            requested_model=effective_model,
            api_surface="responses",
            billing_mode=self.billing_mode,
            endpoint=self.base_url,
        )
        callback_model = accounting_model(
            self.provider,
            effective_model,
            self.billing_mode,
        )
        callbacks = configured_callbacks(config)
        run_id = uuid.uuid4()
        notify_start(
            callbacks,
            prompt=prompt,
            run_id=run_id,
            model=callback_model,
            metadata=metadata,
            provider="OpenAIResponses",
        )

        request: dict[str, Any] = {
            "model": effective_model,
            "input": prompt,
            "store": False,
            "timeout": effective_timeout,
        }
        if effective_reasoning is not None:
            request["reasoning"] = {"effort": effective_reasoning}
        if schema is not None:
            request["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "centinela_output",
                    "strict": True,
                    "schema": schema,
                }
            }

        started = self._clock()
        try:
            response = self._get_client().responses.create(**request)
            latency = elapsed(self._clock, started)
            text = _responses_text(response)
            data = _structured_data(text, schema)
            usage = _usage_from_response(response, responses_api=True)
        except ProviderOutputError as error:
            notify_error(callbacks, run_id=run_id, error=error)
            raise
        except Exception as exc:
            error = safe_provider_error(
                self.provider,
                exc,
                sensitive_values=(self._api_key or "", prompt),
            )
            notify_error(callbacks, run_id=run_id, error=error)
            raise error from None

        response_model = object_value(response, "model") or effective_model
        notify_end(
            callbacks,
            run_id=run_id,
            model=callback_model,
            usage=usage,
        )
        return ProviderResult(
            text=text,
            data=data,
            usage=usage,
            model=str(response_model),
            response_id=(
                str(response_id)
                if (response_id := object_value(response, "id")) is not None
                else None
            ),
            latency_seconds=latency,
            metadata=metadata,
        )

    def invoke_text(self, prompt: str, **kwargs: Any) -> ProviderResult:
        if "output_schema" in kwargs:
            raise TypeError("invoke_text no acepta output_schema")
        return self.invoke(prompt, **kwargs)

    def invoke_json(
        self,
        prompt: str,
        output_schema: Mapping[str, Any],
        **kwargs: Any,
    ) -> ProviderResult:
        return self.invoke(prompt, output_schema=output_schema, **kwargs)


class OpenAICompatibleChatClient(_OpenAIClientBase):
    """Chat Completions para Ollama, vLLM u otro servidor compatible."""

    def __init__(
        self,
        *,
        provider: Literal["ollama", "vllm", "openai_compatible"] = "ollama",
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: float = 180.0,
        base_url: str = "http://localhost:11434/v1",
        api_key: str | None = None,
        client: Any | None = None,
        max_retries: int = 2,
        clock: Clock = time.perf_counter,
    ) -> None:
        effective_key = api_key
        if client is None and effective_key is None:
            # El SDK exige un valor; Ollama/vLLM pueden ignorarlo en redes privadas.
            effective_key = "local-endpoint"
        super().__init__(
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            base_url=base_url,
            api_key=effective_key,
            billing_mode="self_hosted",
            client=client,
            max_retries=max_retries,
            clock=clock,
        )

    def invoke(
        self,
        prompt: str,
        config: Mapping[str, Any] | None = None,
        *,
        output_schema: Mapping[str, Any] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: float | None = None,
    ) -> ProviderResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt debe ser un texto no vacio")
        schema = validate_schema(output_schema)
        effective_model, effective_reasoning, effective_timeout = self._effective_values(
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
        )
        metadata = callback_metadata(
            config,
            provider=self.provider,
            requested_model=effective_model,
            api_surface="chat_completions",
            billing_mode=self.billing_mode,
            endpoint=self.base_url,
        )
        callback_model = accounting_model(
            self.provider,
            effective_model,
            self.billing_mode,
        )
        callbacks = configured_callbacks(config)
        run_id = uuid.uuid4()
        notify_start(
            callbacks,
            prompt=prompt,
            run_id=run_id,
            model=callback_model,
            metadata=metadata,
            provider=f"{self.provider}ChatCompletions",
        )

        request: dict[str, Any] = {
            "model": effective_model,
            "messages": [{"role": "user", "content": prompt}],
            "timeout": effective_timeout,
        }
        if effective_reasoning is not None:
            request["reasoning_effort"] = effective_reasoning
        if schema is not None:
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "centinela_output",
                    "strict": True,
                    "schema": schema,
                },
            }

        started = self._clock()
        try:
            response = self._get_client().chat.completions.create(**request)
            latency = elapsed(self._clock, started)
            text = _chat_text(response)
            data = _structured_data(text, schema)
            usage = _usage_from_response(response, responses_api=False)
        except ProviderOutputError as error:
            notify_error(callbacks, run_id=run_id, error=error)
            raise
        except Exception as exc:
            error = safe_provider_error(
                self.provider,
                exc,
                sensitive_values=(self._api_key or "", prompt),
            )
            notify_error(callbacks, run_id=run_id, error=error)
            raise error from None

        response_model = object_value(response, "model") or effective_model
        notify_end(
            callbacks,
            run_id=run_id,
            model=callback_model,
            usage=usage,
        )
        return ProviderResult(
            text=text,
            data=data,
            usage=usage,
            model=str(response_model),
            response_id=(
                str(response_id)
                if (response_id := object_value(response, "id")) is not None
                else None
            ),
            latency_seconds=latency,
            metadata=metadata,
        )

    def invoke_text(self, prompt: str, **kwargs: Any) -> ProviderResult:
        if "output_schema" in kwargs:
            raise TypeError("invoke_text no acepta output_schema")
        return self.invoke(prompt, **kwargs)

    def invoke_json(
        self,
        prompt: str,
        output_schema: Mapping[str, Any],
        **kwargs: Any,
    ) -> ProviderResult:
        return self.invoke(prompt, output_schema=output_schema, **kwargs)


class OpenAICompatibleEmbeddings:
    """Embeddings por ``/v1/embeddings`` para OpenAI, Ollama o vLLM."""

    def __init__(
        self,
        *,
        model: str,
        provider: str = "ollama",
        base_url: str | None = "http://localhost:11434/v1",
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        batch_size: int = 64,
        dimensions: int | None = None,
        client: Any | None = None,
        callback: Any | None = None,
        max_retries: int = 2,
    ) -> None:
        self.model_name = optional_nonempty(model, name="model") or ""
        self.model = self.model_name
        self.provider = optional_nonempty(provider, name="provider") or "provider"
        self.embedding_identity = f"{self.provider}/{self.model_name}"
        self.base_url = validate_endpoint(base_url)
        self.timeout_seconds = positive_timeout(timeout_seconds)
        if isinstance(batch_size, bool) or not 1 <= int(batch_size) <= 2048:
            raise ValueError("batch_size debe estar entre 1 y 2048")
        self.batch_size = int(batch_size)
        if dimensions is not None and (
            isinstance(dimensions, bool) or not 1 <= int(dimensions) <= 65_536
        ):
            raise ValueError("dimensions debe ser un entero positivo")
        self.dimensions = int(dimensions) if dimensions is not None else None
        self._api_key = api_key
        if client is None and self._api_key is None and self.provider != "openai_api":
            self._api_key = "local-endpoint"
        self._client = client
        self.callback = callback
        if isinstance(max_retries, bool) or not 0 <= int(max_retries) <= 10:
            raise ValueError("max_retries debe estar entre 0 y 10")
        self.max_retries = int(max_retries)

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = _sdk_client(
                api_key=self._api_key,
                base_url=self.base_url,
                timeout_seconds=self.timeout_seconds,
                max_retries=self.max_retries,
            )
        return self._client

    @staticmethod
    def _validate_texts(texts: Sequence[str]) -> list[str]:
        validated: list[str] = []
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Cada texto para embeddings debe ser no vacio")
            validated.append(text)
        return validated

    @staticmethod
    def _vectors(response: Any, *, expected: int) -> list[list[float]]:
        data = object_value(response, "data")
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            raise ProviderOutputError("El endpoint no devolvio embeddings")
        indexed: list[tuple[int, list[float]]] = []
        for position, item in enumerate(data):
            raw_vector = object_value(item, "embedding")
            if not isinstance(raw_vector, Sequence) or isinstance(raw_vector, (str, bytes)):
                raise ProviderOutputError("El endpoint devolvio un embedding invalido")
            vector = [float(value) for value in raw_vector]
            if not vector or any(not math.isfinite(value) for value in vector):
                raise ProviderOutputError("El endpoint devolvio un embedding no finito")
            index = nonnegative_int(object_value(item, "index", position))
            indexed.append((index, vector))
        indexed.sort(key=lambda pair: pair[0])
        vectors = [vector for _, vector in indexed]
        if len(vectors) != expected:
            raise ProviderOutputError(
                "El proveedor devolvio un numero inesperado de embeddings"
            )
        if len({len(vector) for vector in vectors}) > 1:
            raise ProviderOutputError("Los embeddings no tienen dimension uniforme")
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        validated = self._validate_texts(texts)
        if not validated:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(validated), self.batch_size):
            batch = validated[start : start + self.batch_size]
            request: dict[str, Any] = {
                "model": self.model_name,
                "input": batch,
                "timeout": self.timeout_seconds,
            }
            if self.dimensions is not None:
                request["dimensions"] = self.dimensions
            billing_mode: Literal["api", "self_hosted"] = (
                "api" if self.provider == "openai_api" else "self_hosted"
            )
            callbacks = [self.callback] if self.callback is not None else []
            run_id = uuid.uuid4()
            callback_model = accounting_model(
                self.provider,
                self.model_name,
                billing_mode,
            )
            metadata = callback_metadata(
                None,
                provider=self.provider,
                requested_model=self.model_name,
                api_surface="embeddings",
                billing_mode=billing_mode,
                endpoint=self.base_url,
            )
            notify_start(
                callbacks,
                prompt=f"[embedding batch: {len(batch)} items]",
                run_id=run_id,
                model=callback_model,
                metadata=metadata,
                provider=f"{self.provider}Embeddings",
            )
            try:
                response = self._get_client().embeddings.create(**request)
                vectors.extend(self._vectors(response, expected=len(batch)))
                usage = _usage_from_response(response, responses_api=False)
            except ProviderOutputError as error:
                notify_error(callbacks, run_id=run_id, error=error)
                raise
            except Exception as exc:
                error = safe_provider_error(
                    self.provider,
                    exc,
                    sensitive_values=(self._api_key or "", *batch),
                )
                notify_error(callbacks, run_id=run_id, error=error)
                raise error from None
            notify_end(
                callbacks,
                run_id=run_id,
                model=callback_model,
                usage=usage,
            )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def health(self, *, timeout_seconds: float = 5.0) -> ProviderHealth:
        timeout = positive_timeout(timeout_seconds)
        started = time.perf_counter()
        try:
            response = self._get_client().models.list(timeout=timeout)
            models = _listed_models(response)
            model_available = self.model_name in models
            available = model_available is not False
            return ProviderHealth(
                provider=self.provider,
                available=available,
                reachable=True,
                authenticated=None if self.provider != "openai_api" else True,
                model=self.model_name,
                model_available=model_available,
                endpoint=self.base_url,
                latency_seconds=max(time.perf_counter() - started, 0.0),
                detail=(
                    "Endpoint de embeddings operativo"
                    if available
                    else f"El modelo {self.model_name!r} no esta cargado"
                ),
                models=models,
            )
        except Exception as exc:
            return ProviderHealth(
                provider=self.provider,
                available=False,
                reachable=False,
                authenticated=False if self.provider == "openai_api" else None,
                model=self.model_name,
                model_available=None,
                endpoint=self.base_url,
                latency_seconds=max(time.perf_counter() - started, 0.0),
                detail=f"Endpoint no disponible: {type(exc).__name__}",
            )


__all__ = [
    "OpenAICompatibleChatClient",
    "OpenAICompatibleEmbeddings",
    "OpenAIResponsesClient",
]
