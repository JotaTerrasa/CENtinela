"""Contratos y utilidades compartidas por los proveedores de IA.

Los consumidores de CENtinela solo necesitan ``invoke``/``invoke_json`` y no
dependen del transporte concreto.  Las estructuras de este modulo mantienen
la misma superficie publica que :class:`core.codex_client.CodexResult`, lo que
permite alternar Codex, OpenAI y servidores OpenAI-compatible sin ramificar el
grafo ni el motor RAG.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypeAlias, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from langchain_core.outputs import LLMResult

JsonValue: TypeAlias = dict[str, Any] | list[Any] | str | int | float | bool | None
Clock: TypeAlias = Callable[[], float]


class ProviderError(RuntimeError):
    """Error base seguro para los adaptadores de inferencia."""


class ProviderConfigurationError(ProviderError):
    """La configuracion no permite crear o contactar el proveedor."""


class ProviderExecutionError(ProviderError):
    """El proveedor rechazo o no pudo completar una solicitud."""


class ProviderTimeoutError(ProviderExecutionError):
    """La inferencia supero el timeout configurado."""


class ProviderOutputError(ProviderError):
    """La respuesta del proveedor no cumple el contrato solicitado."""


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Contadores normalizados entre Responses y Chat Completions."""

    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def input_tokens(self) -> int:
        return self.prompt_tokens

    @property
    def output_tokens(self) -> int:
        return self.completion_tokens

    @property
    def cached_input_tokens(self) -> int:
        return self.cached_prompt_tokens

    @property
    def reasoning_output_tokens(self) -> int:
        return self.reasoning_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, int]:
        values = asdict(self)
        values["total_tokens"] = self.total_tokens
        return values


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Respuesta comun para cualquier backend de generacion."""

    text: str
    data: JsonValue | None
    usage: ProviderUsage | None
    model: str | None
    response_id: str | None
    latency_seconds: float
    metadata: Mapping[str, Any]

    @property
    def content(self) -> str:
        return self.text

    @property
    def structured_output(self) -> JsonValue | None:
        return self.data

    @property
    def thread_id(self) -> str | None:
        """Alias de compatibilidad; las APIs HTTP no mantienen hilo local."""

        return self.response_id

    @property
    def cost_usd(self) -> None:
        """El coste se calcula en ``CostTrackingCallback``, nunca se inventa aqui."""

        return None

    @property
    def cost_clp(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Estado no sensible de un endpoint y del modelo solicitado."""

    provider: str
    available: bool
    reachable: bool
    authenticated: bool | None
    model: str | None
    model_available: bool | None
    endpoint: str | None
    latency_seconds: float
    detail: str
    models: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["models"] = list(self.models)
        return values


@runtime_checkable
class GenerationClient(Protocol):
    """Interfaz estructural que tambien satisface ``CodexClient``."""

    model: str | None
    reasoning_effort: str | None
    timeout_seconds: float

    def invoke(
        self,
        prompt: str,
        config: Mapping[str, Any] | None = None,
        *,
        output_schema: Mapping[str, Any] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Any: ...

    def invoke_json(
        self,
        prompt: str,
        output_schema: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any: ...


@runtime_checkable
class EmbeddingsClient(Protocol):
    """Contrato minimo consumido por ``rag.vector_engine.VectorEngine``."""

    model_name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def positive_timeout(value: float) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds debe ser positivo y finito")
    return timeout


def optional_nonempty(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} no puede estar vacio")
    if any(character in normalized for character in ("\x00", "\n", "\r")):
        raise ValueError(f"{name} contiene caracteres no permitidos")
    return normalized


def validate_endpoint(value: str | None) -> str | None:
    """Normaliza una URL base sin permitir credenciales ni componentes opacos."""

    if value is None:
        return None
    normalized = optional_nonempty(value, name="base_url")
    assert normalized is not None
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url debe ser una URL HTTP(S) absoluta")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url no puede contener credenciales")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url no puede contener query ni fragment")
    clean_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, clean_path, "", ""))


def validate_schema(schema: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if schema is None:
        return None
    if not isinstance(schema, Mapping):
        raise ValueError("output_schema debe ser un objeto JSON Schema")
    try:
        serialized = json.dumps(dict(schema), ensure_ascii=False, allow_nan=False)
        parsed = json.loads(serialized)
    except (TypeError, ValueError) as exc:
        raise ValueError("output_schema debe ser serializable como JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("output_schema debe ser un objeto JSON Schema")
    return parsed


def configured_callbacks(config: Mapping[str, Any] | None) -> list[Any]:
    if not config:
        return []
    configured = config.get("callbacks")
    if configured is None:
        return []
    if isinstance(configured, Sequence) and not isinstance(configured, (str, bytes)):
        return list(configured)
    return [configured]


def callback_metadata(
    config: Mapping[str, Any] | None,
    *,
    provider: str,
    requested_model: str | None,
    api_surface: str,
    billing_mode: str,
    endpoint: str | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if config and isinstance(config.get("metadata"), Mapping):
        metadata.update(dict(config["metadata"]))
    metadata.update(
        {
            "provider": provider,
            "billing_mode": billing_mode,
            "api_surface": api_surface,
            "auth_method": "api_key" if billing_mode == "api" else "endpoint_policy",
        }
    )
    if billing_mode == "api":
        metadata.update(
            {
                "cost_attribution": "token_pricing",
                "cost_status": "calculated_from_usage",
            }
        )
    else:
        metadata.update(
            {
                "cost_attribution": "external_compute",
                "cost_status": "compute_cost_not_configured",
                "attributable_cost_usd": 0.0,
            }
        )
    if requested_model:
        metadata["requested_model"] = requested_model
    if endpoint:
        metadata["endpoint"] = endpoint
    return metadata


def accounting_model(provider: str, requested_model: str | None, billing_mode: str) -> str:
    if billing_mode == "api":
        return requested_model or "unknown"
    return f"self-hosted/{provider}/{requested_model or 'default'}"


def notify_start(
    callbacks: Sequence[Any],
    *,
    prompt: str,
    run_id: uuid.UUID,
    model: str,
    metadata: Mapping[str, Any],
    provider: str,
) -> None:
    for callback in callbacks:
        handler = getattr(callback, "on_llm_start", None)
        if not callable(handler):
            continue
        try:
            handler(
                {"name": provider},
                [prompt],
                run_id=run_id,
                invocation_params={"model": model},
                metadata=dict(metadata),
            )
        except Exception:
            continue


def notify_end(
    callbacks: Sequence[Any],
    *,
    run_id: uuid.UUID,
    model: str,
    usage: ProviderUsage | None,
) -> None:
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0
    llm_output: dict[str, Any] = {
        "model_name": model,
        "usage_reported": usage is not None,
    }
    if usage is not None:
        llm_output["token_usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    response = LLMResult(
        generations=[],
        llm_output=llm_output,
    )
    for callback in callbacks:
        handler = getattr(callback, "on_llm_end", None)
        if not callable(handler):
            continue
        try:
            handler(response, run_id=run_id)
        except Exception:
            continue


def notify_error(
    callbacks: Sequence[Any],
    *,
    run_id: uuid.UUID,
    error: BaseException,
) -> None:
    for callback in callbacks:
        handler = getattr(callback, "on_llm_error", None)
        if not callable(handler):
            continue
        try:
            handler(error, run_id=run_id)
        except Exception:
            continue


def safe_provider_error(
    provider: str,
    error: BaseException,
    *,
    sensitive_values: Sequence[str] = (),
) -> ProviderExecutionError:
    """Convierte errores SDK sin copiar ningún detalle textual del remoto.

    Un gateway puede devolver claves o fragmentos del prompt sin etiquetarlos.
    Redactar patrones o valores completos no cubre esos ecos parciales; por eso
    solo se conserva la clase local del error. ``sensitive_values`` permanece en
    el contrato para que los llamadores declaren explícitamente los datos que
    nunca deben cruzar esta frontera.
    """

    del sensitive_values
    detail = f"{type(error).__name__}; detalle remoto omitido [REDACTED]"
    error_type = type(error).__name__.lower()
    if "timeout" in error_type:
        return ProviderTimeoutError(f"{provider} excedio el timeout configurado: {detail}")
    return ProviderExecutionError(f"{provider} no pudo completar la solicitud: {detail}")


def elapsed(clock: Clock, started: float) -> float:
    return max(float(clock()) - started, 0.0)


def nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def object_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


__all__ = [
    "Clock",
    "EmbeddingsClient",
    "GenerationClient",
    "JsonValue",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderExecutionError",
    "ProviderHealth",
    "ProviderOutputError",
    "ProviderResult",
    "ProviderTimeoutError",
    "ProviderUsage",
]
