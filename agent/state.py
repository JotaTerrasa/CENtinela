"""Estado JSON-serializable compartido por el grafo regulatorio."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal, TypedDict
from zoneinfo import ZoneInfo

from core.config import DEFAULT_BUSINESS_TIMEZONE, business_today


class ResearchPlan(TypedDict, total=False):
    """Plan acotado que el planner entrega al nodo de captura."""

    objective: str
    report_date: str
    lookback_days: int
    sources: list[str]
    keywords: list[str]
    max_items_per_source: int
    rationale: str
    mode: Literal["deterministic", "llm"]


class CitationRecord(TypedDict):
    """Fuente primaria que respalda una salida del sistema."""

    source: str
    url: str
    title: str


class JudgeResult(TypedDict, total=False):
    """Resultado combinado del control determinista y LLM-as-Judge."""

    approved: bool
    score: float
    relevance: float
    coverage: float
    clarity: float
    traceability: float
    deterministic_valid: bool
    missing_citation_lines: list[str]
    unknown_citations: list[str]
    observations: list[str]
    model: str
    mode: Literal["llm", "llm_revised", "deterministic_fallback"]


class AgentState(TypedDict, total=False):
    """Contrato de estado de ``START`` a ``END``.

    Todos los valores son tipos simples para facilitar checkpoints futuros y la
    serializacion del resultado en la interfaz.
    """

    execution_id: str
    user_id: int | None
    request: str
    report_date: str
    keywords: list[str]
    alert_rules: list[dict[str, Any]]
    plan: ResearchPlan
    documents: list[dict[str, Any]]
    filtered_documents: list[dict[str, Any]]
    previous_report: str
    report: str
    citations: list[CitationRecord]
    judge: JudgeResult
    errors: list[str]
    started_at: str
    finished_at: str
    report_id: str
    index_stats: dict[str, Any]
    capture_stats: dict[str, Any]
    report_mode: Literal["llm", "llm_revised", "deterministic_fallback"]
    quality_status: Literal["pending", "approved", "rejected"]


DEFAULT_KEYWORDS: tuple[str, ...] = (
    "solar",
    "BESS",
    "almacenamiento",
    "hidrogeno verde",
    "hidrógeno verde",
    "data center",
    "precios de nudo",
    "transmision",
    "transmisión",
)


def initial_agent_state(
    *,
    execution_id: str,
    user_id: int | None = None,
    keywords: list[str] | tuple[str, ...] | None = None,
    alert_rules: list[dict[str, Any]] | None = None,
    report_date: date | datetime | str | None = None,
    request: str | None = None,
    previous_report: str = "",
    business_timezone: str = DEFAULT_BUSINESS_TIMEZONE,
) -> AgentState:
    """Crea un estado completo y seguro para invocar el grafo.

    La funcion evita listas compartidas y normaliza las palabras clave sin
    distinguir mayusculas, preservando el primer texto recibido.
    """

    if isinstance(report_date, datetime):
        if report_date.tzinfo is None:
            report_date = report_date.replace(tzinfo=ZoneInfo(business_timezone))
        resolved_date = (
            report_date.astimezone(ZoneInfo(business_timezone)).date().isoformat()
        )
    elif isinstance(report_date, date):
        resolved_date = report_date.isoformat()
    elif report_date:
        resolved_date = str(report_date)[:10]
    else:
        resolved_date = business_today(business_timezone).isoformat()

    requested_keywords = keywords or DEFAULT_KEYWORDS
    normalized_keywords: list[str] = []
    seen: set[str] = set()
    for raw_keyword in requested_keywords:
        keyword = str(raw_keyword).strip()
        folded = keyword.casefold()
        if keyword and folded not in seen:
            normalized_keywords.append(keyword)
            seen.add(folded)

    return AgentState(
        execution_id=str(execution_id),
        user_id=user_id,
        request=(
            request.strip()
            if request and request.strip()
            else "Preparar el informe regulatorio diario del SEN chileno"
        ),
        report_date=resolved_date,
        keywords=normalized_keywords,
        alert_rules=[dict(rule) for rule in alert_rules or []],
        plan=ResearchPlan(),
        documents=[],
        filtered_documents=[],
        previous_report=previous_report,
        report="",
        citations=[],
        judge=JudgeResult(),
        errors=[],
        started_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        capture_stats={},
        report_mode="deterministic_fallback",
        quality_status="pending",
    )


__all__ = [
    "AgentState",
    "CitationRecord",
    "DEFAULT_KEYWORDS",
    "JudgeResult",
    "ResearchPlan",
    "initial_agent_state",
]
