"""Callbacks de tokenomics, latencia y persistencia para LangChain."""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from .config import ModelPricing, Settings, get_settings


_ERROR_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:sk|sess|proj)-[A-Za-z0-9_-]{6,}\b"), "[REDACTED_SECRET]"),
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r'(?is)(["\']?(?:prompt|messages|body|payload|input)["\']?\s*[:=]\s*)'
            r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')'
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)([?&](?:api[_-]?key|token|secret)=)[^&#\s]+"),
        r"\1[REDACTED]",
    ),
)


def sanitize_error(
    error: BaseException | str,
    *,
    max_length: int = 500,
    sensitive_values: Sequence[str] = (),
) -> str:
    """Reduce un error a texto trazable sin secretos, prompts ni cuerpos de peticion."""

    if max_length < 80:
        raise ValueError("max_length debe ser al menos 80")
    prefix = f"{type(error).__name__}: " if isinstance(error, BaseException) else ""
    message = str(error)
    # Los proxies no siempre etiquetan una credencial como header o ``api_key``.
    # Sustituir también los valores configurados evita persistir una clave desnuda.
    secrets = sorted(
        {str(value) for value in sensitive_values if value is not None and str(value)},
        key=len,
        reverse=True,
    )
    for secret in secrets:
        message = message.replace(secret, "[REDACTED_SECRET]")
    for pattern, replacement in _ERROR_REDACTIONS:
        message = pattern.sub(replacement, message)
    message = re.sub(r"\s+", " ", message).strip()
    safe = f"{prefix}{message}" if message else f"{prefix}error sin detalle"
    return safe if len(safe) <= max_length else f"{safe[: max_length - 1]}…"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_price(value: ModelPricing | Mapping[str, float]) -> tuple[float, float]:
    if isinstance(value, ModelPricing):
        return value.input_per_million, value.output_per_million
    input_price = value.get("input_per_million", value.get("input", 0.0))
    output_price = value.get("output_per_million", value.get("output", 0.0))
    return float(input_price), float(output_price)


def _price_for_model(
    model: str,
    pricing: Mapping[str, ModelPricing | Mapping[str, float]],
) -> tuple[float, float]:
    if model in pricing:
        return _coerce_price(pricing[model])
    matches = [name for name in pricing if model.startswith(name)]
    if matches:
        return _coerce_price(pricing[max(matches, key=len)])
    raise KeyError(f"No existe pricing configurado para el modelo {model!r}")


def calculate_cost(
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
    *,
    pricing: Mapping[str, ModelPricing | Mapping[str, float]] | None = None,
    usd_to_clp: float = 940.0,
) -> tuple[float, float]:
    """Calcula coste exacto en USD y CLP a partir de tokens facturados.

    Los precios son USD por un millon de tokens. No se redondea el resultado,
    para evitar perder precision al sumar muchas llamadas pequenas.
    """

    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("Los contadores de tokens no pueden ser negativos")
    if usd_to_clp <= 0:
        raise ValueError("USD_TO_CLP debe ser positivo")
    effective_pricing = pricing or get_settings().model_pricing
    input_price, output_price = _price_for_model(model, effective_pricing)
    input_subtotal = Decimal(prompt_tokens) * Decimal(str(input_price))
    output_subtotal = Decimal(completion_tokens) * Decimal(str(output_price))
    cost_usd_decimal = (input_subtotal + output_subtotal) / Decimal(1_000_000)
    cost_clp_decimal = cost_usd_decimal * Decimal(str(usd_to_clp))
    return float(cost_usd_decimal), float(cost_clp_decimal)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Uso y coste de una llamada LLM."""

    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    cost_clp: float

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, int | float]:
        values: dict[str, int | float] = asdict(self)
        values["total_tokens"] = self.total_tokens
        return values


@dataclass(frozen=True, slots=True)
class ExecutionMetrics:
    """Agregado inmutable de una instancia del callback."""

    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    cost_clp: float
    latency_seconds: float
    call_count: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, int | float]:
        values: dict[str, int | float] = asdict(self)
        values["total_tokens"] = self.total_tokens
        return values


@dataclass(slots=True)
class _RunContext:
    call_id: str
    run_id: str
    parent_run_id: str | None
    model: str
    started_at: datetime
    started_perf: float
    metadata: dict[str, Any]


class CostTrackingCallback(BaseCallbackHandler):
    """Callback LangChain que registra tokens, coste, latencia y modelo.

    Es seguro para callbacks concurrentes: cada ``run_id`` tiene su propio
    contexto y los acumuladores se actualizan bajo un ``RLock``. Si se inyecta
    una instancia de :class:`core.database.Database`, cada llamada se persiste
    inmediatamente y se agrega de forma idempotente a su ejecucion/paso.
    """

    raise_error = False
    run_inline = True

    def __init__(
        self,
        *,
        database: Any | None = None,
        execution_id: str | None = None,
        step_id: str | None = None,
        model: str | None = None,
        workflow: str = "llm",
        settings: Settings | None = None,
        pricing: Mapping[str, ModelPricing | Mapping[str, float]] | None = None,
        usd_to_clp: float | None = None,
        metadata: Mapping[str, Any] | None = None,
        auto_start_execution: bool = True,
        raise_on_persistence_error: bool = False,
    ) -> None:
        super().__init__()
        self.settings = settings or get_settings()
        self.database = database
        self.step_id = step_id
        self.default_model = model or self.settings.planner_model
        self.pricing = dict(pricing or self.settings.model_pricing)
        self.usd_to_clp = float(
            self.settings.usd_to_clp if usd_to_clp is None else usd_to_clp
        )
        if self.usd_to_clp <= 0:
            raise ValueError("USD_TO_CLP debe ser positivo")
        self.metadata = dict(metadata or {})
        self.raise_on_persistence_error = raise_on_persistence_error
        self.execution_id = execution_id
        if self.database is not None and auto_start_execution:
            self.execution_id = self.database.start_execution(
                workflow,
                metadata=self.metadata,
                execution_id=execution_id,
            )

        self._lock = threading.RLock()
        self._runs: dict[str, _RunContext] = {}
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cost_usd = 0.0
        self._cost_clp = 0.0
        self._latency_seconds = 0.0
        self._call_count = 0
        self._calls: list[dict[str, Any]] = []
        self.persistence_errors: list[str] = []

    @staticmethod
    def _key(run_id: Any | None) -> str:
        return str(run_id) if run_id is not None else str(uuid.uuid4())

    def _extract_model(
        self,
        serialized: Mapping[str, Any] | None,
        kwargs: Mapping[str, Any],
    ) -> str:
        metadata = kwargs.get("metadata") or {}
        invocation = kwargs.get("invocation_params") or {}
        candidates: Sequence[Any] = (
            invocation.get("model"),
            invocation.get("model_name"),
            metadata.get("ls_model_name"),
            metadata.get("model"),
            kwargs.get("model"),
            kwargs.get("model_name"),
        )
        for candidate in candidates:
            if candidate:
                return str(candidate)
        if serialized:
            serialized_kwargs = serialized.get("kwargs") or {}
            for key in ("model", "model_name"):
                if serialized_kwargs.get(key):
                    return str(serialized_kwargs[key])
            name = serialized.get("name")
            if isinstance(name, str) and name.startswith("gpt-"):
                return name
        return self.default_model

    def _start_run(
        self,
        serialized: Mapping[str, Any] | None,
        *,
        run_id: Any | None,
        parent_run_id: Any | None,
        kwargs: Mapping[str, Any],
    ) -> None:
        key = self._key(run_id)
        run_metadata = dict(self.metadata)
        run_metadata.update(dict(kwargs.get("metadata") or {}))
        context = _RunContext(
            call_id=str(uuid.uuid4()),
            run_id=key,
            parent_run_id=str(parent_run_id) if parent_run_id is not None else None,
            model=self._extract_model(serialized, kwargs),
            started_at=_utc_now(),
            started_perf=time.perf_counter(),
            metadata=run_metadata,
        )
        with self._lock:
            self._runs[key] = context

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **kwargs: Any,
    ) -> None:
        self._start_run(
            serialized,
            run_id=run_id,
            parent_run_id=parent_run_id,
            kwargs=kwargs,
        )

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **kwargs: Any,
    ) -> None:
        self._start_run(
            serialized,
            run_id=run_id,
            parent_run_id=parent_run_id,
            kwargs=kwargs,
        )

    @staticmethod
    def _usage_from_mapping(mapping: Mapping[str, Any] | None) -> tuple[int, int] | None:
        if not mapping:
            return None
        nested = mapping.get("token_usage") or mapping.get("usage")
        if isinstance(nested, Mapping):
            mapping = nested
        prompt = mapping.get("prompt_tokens", mapping.get("input_tokens"))
        completion = mapping.get("completion_tokens", mapping.get("output_tokens"))
        if prompt is None and completion is None:
            return None
        return int(prompt or 0), int(completion or 0)

    @classmethod
    def _extract_usage(cls, response: LLMResult) -> tuple[int, int] | None:
        llm_output = response.llm_output or {}
        direct = cls._usage_from_mapping(llm_output)
        if direct is not None:
            return direct

        prompt_total = 0
        completion_total = 0
        found = False
        for generation_group in response.generations:
            for generation in generation_group:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                parsed = cls._usage_from_mapping(usage)
                if parsed is None and message is not None:
                    parsed = cls._usage_from_mapping(
                        getattr(message, "response_metadata", None)
                    )
                if parsed is None:
                    parsed = cls._usage_from_mapping(
                        getattr(generation, "generation_info", None)
                    )
                if parsed is not None:
                    prompt_total += parsed[0]
                    completion_total += parsed[1]
                    found = True
        return (prompt_total, completion_total) if found else None

    @staticmethod
    def _response_model(response: LLMResult) -> str | None:
        output = response.llm_output or {}
        for key in ("model_name", "model"):
            if output.get(key):
                return str(output[key])
        return None

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **kwargs: Any,
    ) -> None:
        usage = self._extract_usage(response)
        prompt_tokens, completion_tokens = usage or (0, 0)
        key = str(run_id)
        with self._lock:
            context = self._runs.pop(key, None)
        if context is None:
            context = _RunContext(
                call_id=str(uuid.uuid4()),
                run_id=key,
                parent_run_id=str(parent_run_id) if parent_run_id is not None else None,
                model=self.default_model,
                started_at=_utc_now(),
                started_perf=time.perf_counter(),
                metadata=dict(self.metadata),
            )
        context.metadata = {
            **context.metadata,
            "token_usage_status": "reported" if usage is not None else "not_reported",
        }
        model = self._response_model(response) or context.model
        latency = max(0.0, time.perf_counter() - context.started_perf)
        self._record(
            context,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_seconds=latency,
            status="completed",
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **kwargs: Any,
    ) -> None:
        key = str(run_id)
        with self._lock:
            context = self._runs.pop(key, None)
        if context is None:
            context = _RunContext(
                call_id=str(uuid.uuid4()),
                run_id=key,
                parent_run_id=str(parent_run_id) if parent_run_id is not None else None,
                model=self.default_model,
                started_at=_utc_now(),
                started_perf=time.perf_counter(),
                metadata=dict(self.metadata),
            )
        self._record(
            context,
            model=context.model,
            prompt_tokens=0,
            completion_tokens=0,
            latency_seconds=max(0.0, time.perf_counter() - context.started_perf),
            status="failed",
            error=sanitize_error(error),
        )

    def record_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        *,
        model: str | None = None,
        latency_seconds: float = 0.0,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TokenUsage:
        """Registra uso manual de clientes que no emiten callbacks LangChain."""

        now = _utc_now()
        context = _RunContext(
            call_id=str(uuid.uuid4()),
            run_id=run_id or str(uuid.uuid4()),
            parent_run_id=None,
            model=model or self.default_model,
            started_at=now,
            started_perf=time.perf_counter(),
            metadata={**self.metadata, **dict(metadata or {})},
        )
        return self._record(
            context,
            model=context.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_seconds=max(0.0, latency_seconds),
            status="completed",
        )

    def _record(
        self,
        context: _RunContext,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_seconds: float,
        status: str,
        error: str | None = None,
    ) -> TokenUsage:
        billing_mode = str(context.metadata.get("billing_mode") or "").casefold()
        subscription_call = billing_mode in {"subscription", "chatgpt_subscription"}
        self_hosted_call = billing_mode in {"self_hosted", "self-hosted", "compute"}
        usage_missing = context.metadata.get("token_usage_status") == "not_reported"
        if usage_missing and not subscription_call and not self_hosted_call:
            cost_usd, cost_clp = 0.0, 0.0
            context.metadata = {
                **context.metadata,
                "pricing_status": "not_evaluated",
                "api_cost_status": "unavailable_no_usage",
                "cost_status": "not_calculated",
            }
        elif self_hosted_call:
            # El coste del endpoint por token es cero, pero CPU/GPU, memoria y energía
            # siguen teniendo coste. Se mantienen fuera de ``cost_usd`` para no mezclar
            # una estimación de infraestructura con una tarifa exacta del proveedor.
            cost_usd, cost_clp = 0.0, 0.0
            configured_rate = context.metadata.get("compute_usd_per_hour")
            if configured_rate is None:
                configured_rate = getattr(
                    self.settings, "self_hosted_compute_usd_per_hour", None
                )
            compute_metadata: dict[str, Any] = {
                "pricing_status": "not_applicable",
                "api_cost_status": "not_applicable",
            }
            if configured_rate is None:
                compute_metadata["compute_cost_status"] = "external_not_configured"
            else:
                rate = max(0.0, float(configured_rate))
                estimated_usd = rate * max(0.0, latency_seconds) / 3_600.0
                compute_metadata.update(
                    {
                        "compute_cost_status": "estimated_from_runtime",
                        "compute_usd_per_hour": rate,
                        "estimated_compute_cost_usd": estimated_usd,
                        "estimated_compute_cost_clp": estimated_usd * self.usd_to_clp,
                    }
                )
            context.metadata = {**context.metadata, **compute_metadata}
        else:
            try:
                cost_usd, cost_clp = calculate_cost(
                    prompt_tokens,
                    completion_tokens,
                    model,
                    pricing=self.pricing,
                    usd_to_clp=self.usd_to_clp,
                )
            except KeyError as exc:
                # La observabilidad nunca convierte una respuesta LLM valida en un
                # fallo. Un alias desconocido queda pendiente de reconciliación.
                cost_usd, cost_clp = 0.0, 0.0
                if subscription_call:
                    context.metadata = {
                        **context.metadata,
                        "pricing_status": "not_applicable",
                        "cost_status": "included_not_attributable",
                    }
                else:
                    context.metadata = {
                        **context.metadata,
                        "pricing_status": "unknown",
                        "pricing_error": sanitize_error(exc),
                    }
            else:
                if subscription_call:
                    # El cero almacenado es compatibilidad del esquema, no una
                    # estimacion del coste economico de la suscripcion.
                    cost_usd, cost_clp = 0.0, 0.0
                    context.metadata = {
                        **context.metadata,
                        "pricing_status": "not_applicable",
                        "cost_status": "included_not_attributable",
                    }
                else:
                    context.metadata = {
                        **context.metadata,
                        "pricing_status": "configured",
                        "api_cost_status": "attributable",
                    }
        usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            cost_clp=cost_clp,
        )
        finished_at = _utc_now()
        call = {
            "id": context.call_id,
            "execution_id": self.execution_id,
            "step_id": self.step_id,
            "run_id": context.run_id,
            "parent_run_id": context.parent_run_id,
            "model": model,
            "status": status,
            "started_at": context.started_at,
            "finished_at": finished_at,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": cost_usd,
            "cost_clp": cost_clp,
            "latency_seconds": latency_seconds,
            "metadata": context.metadata,
            "error": sanitize_error(error) if error else None,
        }
        with self._lock:
            self._prompt_tokens += prompt_tokens
            self._completion_tokens += completion_tokens
            self._cost_usd += cost_usd
            self._cost_clp += cost_clp
            self._latency_seconds += latency_seconds
            self._call_count += 1
            self._calls.append(dict(call))
        if self.database is not None:
            try:
                self.database.record_llm_call(call)
            except Exception as persistence_error:  # callback no debe romper el LLM
                message = sanitize_error(persistence_error)
                with self._lock:
                    self.persistence_errors.append(message)
                if self.raise_on_persistence_error:
                    raise
        return usage

    @property
    def prompt_tokens(self) -> int:
        with self._lock:
            return self._prompt_tokens

    @property
    def completion_tokens(self) -> int:
        with self._lock:
            return self._completion_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_usd(self) -> float:
        with self._lock:
            return self._cost_usd

    @property
    def cost_clp(self) -> float:
        with self._lock:
            return self._cost_clp

    @property
    def latency_seconds(self) -> float:
        with self._lock:
            return self._latency_seconds

    @property
    def call_count(self) -> int:
        with self._lock:
            return self._call_count

    @property
    def metrics(self) -> ExecutionMetrics:
        with self._lock:
            return ExecutionMetrics(
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._completion_tokens,
                cost_usd=self._cost_usd,
                cost_clp=self._cost_clp,
                latency_seconds=self._latency_seconds,
                call_count=self._call_count,
            )

    @property
    def calls(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(call) for call in self._calls]

    def snapshot(self) -> dict[str, int | float]:
        """Retorna metricas serializables para el panel Streamlit."""

        return self.metrics.to_dict()

    def persist(self) -> dict[str, int | float]:
        """Punto explicito de sincronizacion; las llamadas ya se guardan al finalizar."""

        if self.persistence_errors and self.raise_on_persistence_error:
            raise RuntimeError("; ".join(self.persistence_errors))
        return self.snapshot()

    def reset(self) -> None:
        with self._lock:
            if self._runs:
                raise RuntimeError("No se puede reiniciar con llamadas LLM en curso")
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._cost_usd = 0.0
            self._cost_clp = 0.0
            self._latency_seconds = 0.0
            self._call_count = 0
            self._calls.clear()
            self.persistence_errors.clear()

    def close_execution(
        self,
        *,
        status: str = "completed",
        error: str | None = None,
    ) -> None:
        """Finaliza la ejecucion persistida usando la latencia agregada."""

        if self.database is not None and self.execution_id is not None:
            self.database.finish_execution(
                self.execution_id,
                status=status,
                error=sanitize_error(error) if error else None,
                latency_seconds=self.latency_seconds,
            )

    def __enter__(self) -> "CostTrackingCallback":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> bool:
        self.close_execution(
            status="failed" if exc_value is not None else "completed",
            error=(sanitize_error(exc_value) if exc_type and exc_value else None),
        )
        return False


ObservabilityCallback = CostTrackingCallback
TokenCostCallback = CostTrackingCallback
TokenTrackingCallback = CostTrackingCallback
TokenUsageCallback = CostTrackingCallback


__all__ = [
    "CostTrackingCallback",
    "ExecutionMetrics",
    "ObservabilityCallback",
    "TokenCostCallback",
    "TokenTrackingCallback",
    "TokenUsage",
    "TokenUsageCallback",
    "calculate_cost",
    "sanitize_error",
]
