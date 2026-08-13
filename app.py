"""Interfaz Streamlit de CENtinela.

La aplicacion no hace I/O de red al importarse. Las acciones externas solo se
ejecutan tras una interaccion explicita del usuario, lo que hace los reruns de
Streamlit predecibles y permite probar los helpers sin consumir creditos.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import unicodedata
from time import perf_counter
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
import streamlit as st

from agent.tools import source_identity
from core.config import Settings, business_today, get_settings
from core.database import Database, get_database
from core.observability import sanitize_error


LOGGER = logging.getLogger("centinela.ui")

CANONICAL_SOURCES = [
    "Coordinador Eléctrico Nacional (CEN)",
    "Comisión Nacional de Energía (CNE)",
    "Ministerio de Energía de Chile",
    "Superintendencia de Electricidad y Combustibles (SEC)",
    "Servicio de Evaluación Ambiental (SEA)",
    "Senado de la República de Chile",
    "Cámara de Diputadas y Diputados de Chile",
]

ALERT_KEYWORDS = [
    "BESS",
    "almacenamiento",
    "precios de nudo",
    "solar",
    "fotovoltaico",
    "hidrógeno verde",
    "data center",
    "transmisión",
    "vertimiento",
    "potencia",
    "servicios complementarios",
    "permisos ambientales",
    "RCA",
    "PMGD",
    "licitación",
    "tarifas",
]

CODEX_DOCKER_LOGIN_COMMAND = (
    "docker compose exec centinela codex login --device-auth"
)

SOURCE_SHORT_NAMES = {
    "cen": "CEN",
    "cne": "CNE",
    "minenergia": "Ministerio de Energía",
    "sec": "SEC",
    "sea": "SEA",
    "senado": "Senado",
    "camara": "Cámara",
}


def _plain(value: str) -> str:
    """Normaliza texto para comparaciones de alertas sin perder el original."""

    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _probe_codex_runtime(
    executable: str,
    workdir: str,
    timeout_seconds: float,
    *,
    runner: Any = subprocess.run,
    executable_finder: Any = shutil.which,
) -> dict[str, Any]:
    """Comprueba el CLI y su sesion sin leer ni exponer credenciales.

    El comando se ejecuta sin ``shell`` y la respuesta publica solo contiene un
    estado normalizado. La salida del CLI se usa en memoria para reconocer
    ``not logged in``, pero nunca se devuelve ni se persiste.
    """

    resolved = executable_finder(executable)
    if not resolved:
        return {
            "available": False,
            "authenticated": False,
            "reason": "executable_not_found",
        }
    timeout = max(1.0, min(float(timeout_seconds), 10.0))
    try:
        process = runner(
            [resolved, "login", "status"],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "available": True,
            "authenticated": False,
            "reason": "timeout",
        }
    except OSError:
        return {
            "available": False,
            "authenticated": False,
            "reason": "runtime_error",
        }

    diagnostic = f"{process.stdout or ''}\n{process.stderr or ''}".casefold()
    logged_out = "not logged in" in diagnostic or "logged out" in diagnostic
    api_key_login = "api key" in diagnostic or "api-key" in diagnostic
    chatgpt_login = "logged in using chatgpt" in diagnostic
    if process.returncode != 0 or logged_out:
        reason = "not_authenticated"
    elif api_key_login:
        # El perfil de entrega es deliberadamente Codex-only con identidad
        # ChatGPT. Una API key valida para el CLI no autoriza este producto.
        reason = "api_key_auth_not_allowed"
    elif chatgpt_login:
        reason = "ready"
    else:
        reason = "unsupported_auth_mode"
    return {
        "available": True,
        "authenticated": reason == "ready",
        "reason": reason,
    }


@st.cache_data(show_spinner=False, ttl=30)
def _cached_codex_runtime_status(
    executable: str,
    workdir: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _probe_codex_runtime(executable, workdir, timeout_seconds)


def codex_runtime_status(
    settings: Settings,
    *,
    runner: Any | None = None,
    executable_finder: Any | None = None,
) -> dict[str, Any]:
    """Estado cacheado de Codex; las dependencias inyectadas evitan procesos en tests."""

    executable = str(settings.codex_cli_path)
    workdir = str(settings.codex_workdir)
    timeout = float(settings.codex_timeout_seconds)
    if runner is not None or executable_finder is not None:
        return _probe_codex_runtime(
            executable,
            workdir,
            timeout,
            runner=runner if runner is not None else subprocess.run,
            executable_finder=(
                executable_finder if executable_finder is not None else shutil.which
            ),
        )
    return _cached_codex_runtime_status(executable, workdir, timeout)


def _codex_unavailable_message(runtime: Mapping[str, Any], capability: str) -> str:
    """Explica por qué una capacidad generativa está deshabilitada."""

    reason = str(runtime.get("reason") or "")
    if reason == "api_key_auth_not_allowed":
        return (
            f"{capability} está deshabilitado: el CLI usa una API key y este perfil "
            "solo admite una sesión ChatGPT/Codex. Cierra esa sesión con "
            f"`docker compose exec centinela codex logout` y ejecuta "
            f"`{CODEX_DOCKER_LOGIN_COMMAND}`."
        )
    if reason == "unsupported_auth_mode":
        return (
            f"{capability} está deshabilitado: `codex login status` no confirmó "
            "explícitamente una sesión ChatGPT. Vuelve a autenticar con "
            f"`{CODEX_DOCKER_LOGIN_COMMAND}`."
        )
    if not bool(runtime.get("available")):
        return (
            f"{capability} está deshabilitado porque el runtime Codex no está "
            "disponible en el contenedor."
        )
    return (
        f"{capability} requiere una sesión ChatGPT/Codex en este contenedor. "
        f"Ejecuta `{CODEX_DOCKER_LOGIN_COMMAND}`. Dashboard, scraping, alertas e "
        "índice local siguen disponibles; el fallback no sustituye el login y solo "
        "protege una ejecución generativa ya iniciada."
    )


PROVIDER_LABELS = {
    "codex": "Codex · ChatGPT",
    "openai": "OpenAI API",
    "ollama": "Ollama · self-hosted",
    "vllm": "vLLM · self-hosted",
}


def _settings_secret(settings: Settings, name: str) -> str | None:
    value = getattr(settings, name, None)
    if value is None:
        return None
    reveal = getattr(value, "get_secret_value", None)
    normalized = str(reveal() if callable(reveal) else value).strip()
    return normalized or None


def provider_runtime_status(
    settings: Settings,
    role: str = "executor",
    *,
    client_factory: Any | None = None,
) -> dict[str, Any]:
    """Comprueba el proveedor efectivo sin ejecutar una generación facturable."""

    provider_resolver = getattr(settings, "provider_for_role", None)
    provider = (
        str(provider_resolver(role))
        if callable(provider_resolver)
        else str(getattr(settings, "ai_provider", "codex"))
    )
    model_resolver = getattr(settings, "model_for_role", None)
    model = (
        str(model_resolver(role))
        if callable(model_resolver)
        else str(getattr(settings, "report_model", ""))
    )
    if provider == "codex" and client_factory is None:
        codex_status = codex_runtime_status(settings)
        return {
            **codex_status,
            "ready": bool(codex_status.get("authenticated")),
            "endpoint_reachable": None,
            "provider": "codex",
            "label": PROVIDER_LABELS["codex"],
            "model": model,
        }

    if provider == "openai" and not _settings_secret(settings, "openai_api_key"):
        return {
            "available": False,
            "authenticated": False,
            "ready": False,
            "endpoint_reachable": None,
            "reason": "missing_api_key",
            "provider": provider,
            "label": PROVIDER_LABELS[provider],
            "model": model,
        }

    if client_factory is None:
        from core.providers import create_generation_client

        client_factory = create_generation_client
    effort_resolver = getattr(settings, "reasoning_effort_for_role", None)
    effort = str(effort_resolver(role)) if callable(effort_resolver) else None
    if provider == "openai" and model.startswith("gpt-4o"):
        effort = None
    try:
        client = client_factory(
            provider,
            model=model,
            reasoning_effort=effort,
            timeout_seconds=(
                float(getattr(settings, "codex_timeout_seconds", 240.0))
                if provider == "codex"
                else float(getattr(settings, "provider_timeout_seconds", 240.0))
            ),
            api_key=_settings_secret(settings, f"{provider}_api_key"),
            base_url=(
                None
                if provider == "codex"
                else str(getattr(settings, f"{provider}_base_url"))
            ),
            codex_executable=str(getattr(settings, "codex_cli_path", "codex")),
            codex_workdir=getattr(settings, "codex_workdir", None),
        )
        health = client.health(
            timeout_seconds=float(
                getattr(settings, "provider_health_timeout_seconds", 5.0)
            )
        )
        payload = health.to_dict() if hasattr(health, "to_dict") else dict(health)
        ready = bool(payload.get("available")) and payload.get("authenticated") is not False
        return {
            **payload,
            "available": ready,
            "ready": ready,
            "endpoint_reachable": bool(payload.get("reachable")),
            "reason": "ready" if ready else "model_or_endpoint_unavailable",
            "provider": provider,
            "label": PROVIDER_LABELS.get(provider, provider),
            "model": model,
        }
    except Exception as exc:
        LOGGER.warning("Health check de proveedor fallido: %s", sanitize_error(exc))
        return {
            "available": False,
            "authenticated": False,
            "ready": False,
            "endpoint_reachable": False,
            "reason": "runtime_error",
            "provider": provider,
            "label": PROVIDER_LABELS.get(provider, provider),
            "model": model,
        }


def _provider_unavailable_message(runtime: Mapping[str, Any], capability: str) -> str:
    provider = str(runtime.get("provider") or "codex")
    if provider == "codex":
        return _codex_unavailable_message(runtime, capability)
    if runtime.get("reason") == "missing_api_key":
        return (
            f"{capability} requiere `OPENAI_API_KEY`. La API se factura por separado "
            "de la suscripción ChatGPT; inyéctala como secreto del despliegue."
        )
    model = str(runtime.get("model") or "configurado")
    endpoint_hint = "el endpoint privado" if provider == "vllm" else "Ollama"
    return (
        f"{capability} está deshabilitado: {endpoint_hint} no está listo o no publica "
        f"el modelo `{model}` en `/v1/models`."
    )


def provider_execution_metadata(settings: Settings, role: str) -> dict[str, Any]:
    resolver = getattr(settings, "provider_for_role", None)
    provider = str(resolver(role)) if callable(resolver) else str(settings.ai_provider)
    if provider == "codex":
        return {
            "provider": "codex_cli",
            "billing_mode": "subscription",
            "cost_attribution": "not_attributable",
        }
    if provider == "openai":
        return {
            "provider": "openai_api",
            "billing_mode": "api",
            "cost_attribution": "token_pricing",
        }
    return {
        "provider": provider,
        "billing_mode": "self_hosted",
        "cost_attribution": "external_compute",
    }


def report_quality_status(report: Mapping[str, Any]) -> dict[str, Any]:
    """Resume aprobación del Judge y degradación técnica sin ocultar ninguna."""

    evaluation = report.get("evaluation") or report.get("judge") or {}
    evaluation = evaluation if isinstance(evaluation, Mapping) else {}
    raw_approved = evaluation.get("approved")
    if raw_approved is None:
        raw_approved = evaluation.get("passed")
    approved = raw_approved if isinstance(raw_approved, bool) else None
    mode = str(evaluation.get("mode") or "").casefold()
    errors = [str(value) for value in report.get("errors") or []]
    status = str(report.get("status") or "").casefold()
    degraded = (
        status == "degraded"
        or mode in {"deterministic", "fallback", "degraded"}
        or any(
            marker in error.casefold()
            for error in errors
            for marker in ("executor fallback", "judge fallback", "modo degradado")
        )
    )
    if approved is False:
        level = "rejected"
    elif degraded:
        level = "degraded"
    elif approved is True:
        level = "approved"
    else:
        level = "unreviewed"
    score = evaluation.get("score")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return {
        "level": level,
        "approved": approved,
        "degraded": degraded,
        "score": score,
        "evaluation": dict(evaluation),
    }


def _execution_display_status(execution: Mapping[str, Any]) -> str:
    """Distingue éxito técnico de aprobación editorial para informes."""

    technical = str(execution.get("status") or "—")
    if str(execution.get("workflow") or "") != "daily_report":
        return technical
    metadata = execution.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    quality = report_quality_status(
        {
            "evaluation": metadata.get("judge") or {},
            "errors": metadata.get("errors") or [],
            "status": technical,
        }
    )
    return {
        "approved": "Aprobado por Judge",
        "rejected": "Rechazado por Judge",
        "degraded": "Generado en modo degradado",
        "unreviewed": technical,
    }[quality["level"]]


def _subscription_imputation(
    monthly_cost_usd: float,
    planned_executions: int,
    observed_executions: int,
    *,
    usd_to_clp: float = 940.0,
) -> dict[str, float]:
    """Imputación interna opcional; nunca se confunde con tarifa del proveedor."""

    monthly = max(float(monthly_cost_usd), 0.0)
    planned = max(int(planned_executions), 1)
    observed = max(int(observed_executions), 0)
    per_execution_usd = monthly / planned
    total_usd = per_execution_usd * observed
    return {
        "per_execution_usd": per_execution_usd,
        "per_execution_clp": per_execution_usd * float(usd_to_clp),
        "observed_total_usd": total_usd,
        "observed_total_clp": total_usd * float(usd_to_clp),
    }


def flatten_alert_keywords(alerts: Iterable[Mapping[str, Any]]) -> list[str]:
    """Devuelve palabras unicas de alertas activas conservando su escritura."""

    seen: set[str] = set()
    result: list[str] = []
    for alert in alerts:
        if not bool(alert.get("enabled", True)):
            continue
        for value in alert.get("keywords") or []:
            keyword = str(value).strip()
            identity = _plain(keyword)
            if keyword and identity not in seen:
                seen.add(identity)
                result.append(keyword)
    return result


def article_matches_keywords(article: Mapping[str, Any], keywords: Sequence[str]) -> list[str]:
    haystack = _plain(
        " ".join(
            str(article.get(field) or "")
            for field in ("title", "summary", "content", "category")
        )
    )
    return [keyword for keyword in keywords if _plain(str(keyword)) in haystack]


def article_matches_alert(
    article: Mapping[str, Any], alert: Mapping[str, Any]
) -> list[str]:
    """Aplica conjuntamente palabras clave y restriccion opcional por organismo."""

    if not bool(alert.get("enabled", True)):
        return []
    allowed_sources = {
        source_identity(str(source))
        for source in alert.get("sources") or []
        if str(source).strip()
    }
    article_source = source_identity(str(article.get("source") or ""))
    if allowed_sources and article_source not in allowed_sources:
        return []
    return article_matches_keywords(article, list(alert.get("keywords") or []))


def articles_matching_alerts(
    articles: Sequence[Mapping[str, Any]], alerts: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Devuelve cada articulo una sola vez si satisface alguna regla completa."""

    return [
        dict(article)
        for article in articles
        if any(article_matches_alert(article, alert) for alert in alerts)
    ]


def dashboard_snapshot(
    news: Sequence[Mapping[str, Any]],
    matches: Sequence[Mapping[str, Any]],
    alert_keywords: Sequence[str],
) -> dict[str, Any]:
    """Lectura ejecutiva determinista del corpus para el primer screen."""

    present_sources = {
        source_identity(str(item.get("source") or ""))
        for item in news
        if str(item.get("source") or "").strip()
    }
    expected_sources = set(SOURCE_SHORT_NAMES)
    missing_sources = [
        SOURCE_SHORT_NAMES[identity]
        for identity in SOURCE_SHORT_NAMES
        if identity not in present_sources
    ]
    scope = list(matches) if matches else list(news)
    source_counts: dict[str, int] = {}
    source_labels: dict[str, str] = {}
    for item in scope:
        label = str(item.get("source") or "Sin organismo")
        identity = source_identity(label)
        source_counts[identity] = source_counts.get(identity, 0) + 1
        source_labels[identity] = label
    if source_counts:
        focus_identity = max(source_counts, key=source_counts.get)
        focus_source = source_labels[focus_identity]
        focus_count = source_counts[focus_identity]
    else:
        focus_source = "Sin evidencia"
        focus_count = 0

    latest_capture = max(
        (str(item.get("fetched_at") or "") for item in news),
        default="",
    )
    latest_evidence = max(
        (
            str(item.get("published_at") or item.get("fetched_at") or "")
            for item in news
        ),
        default="",
    )
    if matches:
        next_focus = str(matches[0].get("title") or "Abrir la primera coincidencia")
    elif alert_keywords:
        next_focus = "Revisar términos sin coincidencias y ampliar el horizonte."
    else:
        next_focus = "Crear una alerta para priorizar activos y riesgos concretos."
    return {
        "coverage_count": len(present_sources & expected_sources),
        "coverage_partial": bool(expected_sources - present_sources),
        "missing_sources": missing_sources,
        "latest_capture": latest_capture,
        "latest_evidence": latest_evidence,
        "focus_source": focus_source,
        "focus_count": focus_count,
        "next_focus": next_focus,
    }


def normalize_article(article: Mapping[str, Any]) -> dict[str, Any]:
    """Adapta el contrato rico del scraper al repositorio SQLite sin perder datos."""

    item = dict(article)
    topics = list(item.get("topics") or item.get("keywords") or [])
    metadata = dict(item.get("metadata") or {})
    for key in ("source_url", "capture_method", "retrieval_status"):
        if item.get(key) is not None:
            metadata[key] = item[key]
    item["keywords"] = topics
    item["fetched_at"] = item.get("retrieved_at") or item.get("fetched_at")
    item["published_at"] = item.get("published_at") or item.get("date")
    item["category"] = item.get("category") or (topics[0] if topics else "Regulación")
    item["metadata"] = metadata
    return item


def normalize_report_result(result: Any) -> dict[str, Any]:
    """Normaliza el estado de LangGraph para render y descarga estable."""

    if hasattr(result, "model_dump"):
        result = result.model_dump(mode="json")
    if isinstance(result, str):
        return {"report": result, "evaluation": {}, "citations": []}
    if not isinstance(result, Mapping):
        return {"report": str(result), "evaluation": {}, "citations": []}
    payload = dict(result)
    report = (
        payload.get("final_report")
        or payload.get("report")
        or payload.get("report_content")
        or ""
    )
    if isinstance(report, Mapping):
        report = report.get("content") or report.get("text") or json.dumps(
            report, ensure_ascii=False
        )
    evaluation = (
        payload.get("evaluation")
        or payload.get("judge_result")
        or payload.get("judge")
        or {}
    )
    citations = payload.get("citations") or payload.get("sources") or []
    payload.update(
        report=str(report),
        evaluation=evaluation,
        citations=citations,
    )
    return payload


def normalize_rag_result(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        result = result.model_dump(mode="json")
    if isinstance(result, str):
        return {"answer": result, "sources": []}
    if not isinstance(result, Mapping):
        return {"answer": str(result), "sources": []}
    payload = dict(result)
    payload["answer"] = str(
        payload.get("answer") or payload.get("response") or payload.get("text") or ""
    )
    payload["sources"] = list(payload.get("sources") or payload.get("citations") or [])
    return payload


@st.cache_resource(show_spinner=False)
def services() -> tuple[Settings, Database]:
    settings = get_settings()
    database = get_database(settings=settings)
    return settings, database


def scraper_service() -> Any:
    from scrapers.chile_regulatory import ChileRegulatoryScraper

    settings, _ = services()
    return ChileRegulatoryScraper(settings=settings)


def vector_service() -> Any:
    from rag.vector_engine import VectorEngine

    settings, _ = services()
    return VectorEngine(settings=settings)


def agent_service() -> Any:
    """Crea un agente aislado por ejecucion para no compartir callbacks entre sesiones."""

    from agent.graph import RegulatoryAgent

    settings, database = services()
    return RegulatoryAgent(
        settings=settings,
        database=database,
        scraper=scraper_service(),
        vector_engine=vector_service(),
    )


def _set_user(user: Mapping[str, Any] | None) -> None:
    previous = st.session_state.get("user")
    previous_id = previous.get("id") if isinstance(previous, Mapping) else None
    next_id = user.get("id") if isinstance(user, Mapping) else None
    if previous_id != next_id:
        for key in (
            "rag_messages",
            "latest_report",
            "latest_report_user_id",
            "dashboard_sources",
            "dashboard_sources_compact",
            "enable_subscription_imputation",
        ):
            st.session_state.pop(key, None)
    if user is None:
        st.session_state.pop("user", None)
    else:
        st.session_state.user = dict(user)


def render_auth(database: Database) -> None:
    st.markdown(
        """
        <div class="hero">
          <div class="eyebrow">INTELIGENCIA REGULATORIA · CHILE</div>
          <h1>CEN<span>tinela</span></h1>
          <p>Evidencia oficial, análisis trazable y consumo observable para el SEN.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    left, center, right = st.columns([1, 1.35, 1])
    with center:
        login_tab, register_tab = st.tabs(["Iniciar sesión", "Crear cuenta"])
        with login_tab:
            with st.form("login_form", clear_on_submit=False):
                username = st.text_input("Usuario", autocomplete="username")
                password = st.text_input(
                    "Contraseña", type="password", autocomplete="current-password"
                )
                submitted = st.form_submit_button(
                    "Entrar en CENtinela", type="primary", width="stretch"
                )
            if submitted:
                user = database.authenticate_user(username, password)
                if user:
                    _set_user(user)
                    st.rerun()
                st.error("Usuario o contraseña incorrectos.")
        with register_tab:
            st.caption("Registro local habilitado para la demostración del MVP.")
            with st.form("register_form", clear_on_submit=True):
                new_username = st.text_input(
                    "Nuevo usuario", key="register_username", autocomplete="username"
                )
                email = st.text_input("Email (opcional)", autocomplete="email")
                new_password = st.text_input(
                    "Contraseña (mínimo 8 caracteres)",
                    type="password",
                    key="register_password",
                    autocomplete="new-password",
                )
                confirmation = st.text_input(
                    "Repite la contraseña",
                    type="password",
                    key="register_confirmation",
                    autocomplete="new-password",
                )
                register = st.form_submit_button(
                    "Crear cuenta", width="stretch"
                )
            if register:
                if new_password != confirmation:
                    st.error("Las contraseñas no coinciden.")
                else:
                    try:
                        user_id = database.create_user(
                            new_username, new_password, email=email or None
                        )
                        _set_user(database.get_user(user_id))
                        st.rerun()
                    except Exception as exc:
                        safe_error = sanitize_error(exc)
                        LOGGER.warning("No se pudo registrar usuario: %s", safe_error)
                        if "UNIQUE" in safe_error.upper():
                            message = "El usuario o email ya existe."
                        elif isinstance(exc, ValueError):
                            message = str(exc)
                        else:
                            message = "No se pudo crear la cuenta. Revisa los logs del servicio."
                        st.error(message)


def _refresh_sources(settings: Settings, database: Database) -> tuple[int, list[str]]:
    if settings.public_demo_mode or getattr(database, "is_read_only_demo", False):
        from core.demo import DemoReadOnlyError

        raise DemoReadOnlyError(
            "La captura de fuentes está bloqueada en el replay público"
        )
    scraper = scraper_service()
    articles = scraper.fetch_all(max_per_source=settings.scraper_max_articles)
    normalized = [normalize_article(item) for item in articles]
    database.save_news(normalized)
    warnings: list[str] = []
    raw_errors = getattr(scraper, "last_errors", None) or getattr(scraper, "errors", None)
    if isinstance(raw_errors, Mapping):
        warnings = [f"{source}: {error}" for source, error in raw_errors.items()]
    elif raw_errors:
        warnings = [str(value) for value in raw_errors]
    if normalized:
        try:
            engine = vector_service()
            if hasattr(engine, "index_news"):
                index_result = engine.index_news(normalized)
            elif hasattr(engine, "add_documents"):
                index_result = engine.add_documents(normalized)
            else:
                index_result = None
            if isinstance(index_result, Mapping):
                warnings.extend(
                    f"Índice RAG: {sanitize_error(str(message))}"
                    for message in index_result.get("errors") or []
                )
        except Exception as exc:
            LOGGER.error("Fallo de indexacion tras scraping: %s", sanitize_error(exc))
            warnings.append(f"Índice RAG: {sanitize_error(exc)}")
    return len(normalized), warnings


def _format_timestamp(value: Any) -> str:
    if not value:
        return "—"
    try:
        raw = str(value).strip()
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            return pd.to_datetime(raw).strftime("%d/%m/%Y")
        timestamp = pd.to_datetime(value, utc=True)
        return timestamp.tz_convert("America/Santiago").strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _news_frame(news: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for item in news:
        topics = item.get("keywords") or item.get("topics") or []
        fallback = item.get("is_fallback")
        capture_method = (
            "N/D" if fallback is None else ("Fallback" if fallback else "Directa")
        )
        rows.append(
            {
                "Fecha": _format_timestamp(item.get("published_at") or item.get("fetched_at")),
                "Organismo": item.get("source") or "—",
                "Titular": item.get("title") or "—",
                "Temas": ", ".join(str(value) for value in topics),
                "Método de captura": capture_method,
                "URL": item.get("url") or "",
            }
        )
    return pd.DataFrame(rows)


def render_dashboard(settings: Settings, database: Database, user: Mapping[str, Any]) -> None:
    header, action = st.columns([3, 1])
    with header:
        st.title("Radar regulatorio")
        st.caption(
            "Catálogo conservado de citas oficiales enlazadas a su fuente primaria."
            if settings.public_demo_mode
            else "Publicaciones oficiales recuperadas y enlazadas a su evidencia primaria."
        )
    with action:
        st.write("")
        refresh = st.button(
            "Actualizar fuentes",
            type="primary",
            width="stretch",
            disabled=settings.public_demo_mode,
        )
        if settings.public_demo_mode:
            st.caption("Bloqueado en la demo pública")
    if refresh:
        with st.spinner("Consultando organismos oficiales…"):
            try:
                count, warnings = _refresh_sources(settings, database)
                st.success(f"Actualización completada: {count} publicaciones normalizadas.")
                st.session_state.last_scrape_warnings = warnings
            except Exception as exc:
                LOGGER.error(
                    "Actualizacion regulatoria fallida: %s", sanitize_error(exc)
                )
                st.error(
                    "No se pudo completar la actualización. Revisa los logs del servicio."
                )

    news = database.list_news(limit=1000)
    alerts = database.list_alerts(int(user["id"]), enabled_only=True)
    alert_keywords = flatten_alert_keywords(alerts)
    matches = articles_matching_alerts(news, alerts)
    sources = sorted({str(item.get("source")) for item in news if item.get("source")})
    fallback_count = sum(bool(item.get("is_fallback")) for item in news)
    snapshot = dashboard_snapshot(news, matches, alert_keywords)
    latest_display = _format_timestamp(snapshot["latest_evidence"]).split(" ")[0]
    if len(latest_display) == 10 and latest_display.count("/") == 2:
        latest_display = f"{latest_display[:6]}{latest_display[-2:]}"

    if settings.public_demo_mode:
        acceptance = getattr(database, "acceptance_snapshot", {})
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Catálogo de citas", len(news))
        c2.metric("Organismos citados", snapshot["coverage_count"])
        c3.metric(
            "Snapshot de aceptación",
            int(acceptance.get("publications_in_dashboard") or 0),
        )
        c4.metric(
            "Fuentes recuperadas",
            f"{int(acceptance.get('sources_recovered') or 0)}/7",
        )
    else:
        direct_count = len(news) - fallback_count
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Publicaciones", len(news))
        c2.metric("Fuentes", f"{snapshot['coverage_count']}/7")
        c3.metric("Evidencia directa", direct_count)
        c4.metric("Fallback", fallback_count)
        c5.metric("Coincidencias", len(matches))
    if settings.public_demo_mode:
        st.caption(
            "Bundle de replay validado el 13/08/2026. El catálogo conservado no "
            "incluye fechas de publicación ni de captura por artículo; se muestran N/D."
        )
    else:
        st.caption(f"Evidencia oficial más reciente: {latest_display}.")

    if not news:
        st.info(
            "Todavía no hay publicaciones locales. Pulsa **Actualizar fuentes** para "
            "construir el primer snapshot con datos públicos reales."
        )
        return

    st.subheader("Lectura ejecutiva")
    what_col, where_col, next_col = st.columns(3)
    with what_col:
        with st.container(border=True):
            st.caption("QUÉ OCURRE")
            if matches:
                st.write(
                    f"{len(matches)} de {len(news)} "
                    f"{'citas' if settings.public_demo_mode else 'publicaciones'} "
                    "coinciden con la configuración de alertas."
                )
            else:
                st.write(
                    f"Hay {len(news)} "
                    f"{'citas' if settings.public_demo_mode else 'publicaciones'} "
                    "en el radar y ninguna "
                    "coincidencia activa."
                )
    with where_col:
        with st.container(border=True):
            st.caption("DÓNDE MIRAR")
            focus_unit = "coincidencia" if snapshot["focus_count"] == 1 else "coincidencias"
            st.write(
                f"{snapshot['focus_source']} concentra {snapshot['focus_count']} "
                f"{focus_unit if matches else ('citas' if settings.public_demo_mode else 'publicaciones')} "
                "del foco actual."
            )
    with next_col:
        with st.container(border=True):
            st.caption("SIGUIENTE FOCO")
            next_focus = str(snapshot["next_focus"])
            st.write(next_focus if len(next_focus) <= 145 else f"{next_focus[:142]}…")

    if not settings.public_demo_mode:
        st.caption(
            "Frescura de captura: "
            f"{_format_timestamp(snapshot['latest_capture'])} · "
            "Evidencia más reciente: "
            f"{_format_timestamp(snapshot['latest_evidence'])}."
        )
    if snapshot["coverage_partial"]:
        if settings.public_demo_mode:
            st.warning(
                f"Catálogo conservado: {snapshot['coverage_count']}/7 organismos de "
                "referencia. No representados en sus 34 citas: "
                + ", ".join(snapshot["missing_sources"])
                + ". El resumen histórico 53/7 se muestra por separado."
            )
        else:
            st.warning(
                f"Cobertura parcial {snapshot['coverage_count']}/7. Sin evidencia en el "
                "snapshot actual: " + ", ".join(snapshot["missing_sources"]) + "."
            )

    warnings = st.session_state.get("last_scrape_warnings") or []
    if warnings:
        with st.expander(f"Incidencias de captura ({len(warnings)})"):
            for warning in warnings:
                st.warning(warning)

    filter_left, filter_right = st.columns([1, 2])
    selected_sources = filter_left.multiselect(
        "Filtrar fuentes",
        sources,
        default=[],
        placeholder="Todas las fuentes",
        key="dashboard_sources_compact",
    )
    query = filter_right.text_input(
        "Buscar en titulares y resúmenes", placeholder="Ej.: almacenamiento o transmisión"
    )
    visible = (
        [item for item in news if item.get("source") in selected_sources]
        if selected_sources
        else list(news)
    )
    if query:
        visible = [item for item in visible if article_matches_keywords(item, [query])]

    insight_tab, alert_tab, coverage_tab = st.tabs(
        ["Novedades", "Mis alertas", "Cobertura"]
    )
    with insight_tab:
        if visible:
            frame = _news_frame(visible)
            st.dataframe(
                frame,
                hide_index=True,
                width="stretch",
                column_config={
                    "URL": st.column_config.LinkColumn(
                        "Fuente oficial", display_text="Abrir ↗"
                    ),
                    "Titular": st.column_config.TextColumn(width="large"),
                },
            )
        else:
            filter_description = f" para «{query}»" if query else ""
            st.info(
                f"No hay {'citas' if settings.public_demo_mode else 'publicaciones'} "
                f"que coincidan{filter_description} con "
                "los filtros seleccionados."
            )
    with alert_tab:
        if not alert_keywords:
            st.info("Configura al menos una alerta para ver coincidencias personalizadas.")
        else:
            st.caption("Filtro activo: " + " · ".join(alert_keywords))
            st.dataframe(
                _news_frame(matches),
                hide_index=True,
                width="stretch",
                column_config={"URL": st.column_config.LinkColumn(display_text="Abrir ↗")},
            )
    with coverage_tab:
        counts = pd.DataFrame(
            [
                {
                    "Organismo": source,
                    ("Citas" if settings.public_demo_mode else "Publicaciones"): sum(
                        item.get("source") == source for item in news
                    ),
                    "Última captura": max(
                        (
                            item.get("fetched_at") or ""
                            for item in news
                            if item.get("source") == source
                        ),
                        default="",
                    ),
                }
                for source in sources
            ]
        )
        if not counts.empty:
            counts["Última captura"] = counts["Última captura"].map(_format_timestamp)
        st.dataframe(counts, hide_index=True, width="stretch")
        if settings.public_demo_mode:
            st.caption(
                "Cobertura del catálogo conservado: 34 citas de 6 organismos. "
                "La aceptación original registró, por separado, 53 publicaciones "
                "y 7/7 fuentes recuperadas."
            )
        else:
            st.caption(f"Registros obtenidos mediante fallback en vivo: {fallback_count}.")


def render_alerts(
    settings: Settings,
    database: Database,
    user: Mapping[str, Any],
) -> None:
    st.title("Alertas personalizadas")
    st.caption(
        "Configuración UI simulada y no persistente; no forma parte de la evidencia histórica."
        if settings.public_demo_mode
        else "Cada regla pertenece al usuario autenticado y se aplica al corpus local."
    )
    if settings.public_demo_mode:
        st.info(
            "Vista de demostración en solo lectura. En el modo interactivo, cada "
            "usuario puede crear, editar y eliminar sus propias reglas."
        )
        alerts = database.list_alerts(int(user["id"]))
        for alert in alerts:
            with st.container(border=True):
                st.markdown(f"### {alert['name']}")
                st.write("**Palabras clave:** " + " · ".join(alert["keywords"]))
                st.write(
                    "**Organismos:** "
                    + (" · ".join(alert["sources"]) if alert["sources"] else "Todos")
                )
                st.caption("Activa" if alert["enabled"] else "Pausada")
                st.caption(
                    "SIMULACIÓN UI · Procedencia: replay-manifest.json · No es evidencia histórica"
                )
        return
    existing_sources = sorted(
        {item["source"] for item in database.list_news(limit=1000) if item.get("source")}
    ) or CANONICAL_SOURCES

    with st.form("new_alert_form", clear_on_submit=True):
        name = st.text_input("Nombre de la alerta", placeholder="Riesgos BESS y transmisión")
        selected = st.multiselect(
            "Palabras clave",
            ALERT_KEYWORDS,
            placeholder="Selecciona uno o varios términos",
        )
        custom = st.text_input(
            "Términos adicionales", placeholder="Separados por coma: capacidad, conexión"
        )
        sources = st.multiselect(
            "Limitar a organismos (vacío = todos)",
            existing_sources,
            placeholder="Todos los organismos",
        )
        create = st.form_submit_button("Guardar alerta", type="primary")
    if create:
        extra = [value.strip() for value in custom.split(",") if value.strip()]
        try:
            database.create_alert(
                int(user["id"]), name, [*selected, *extra], sources=sources
            )
            st.success("Alerta creada.")
            st.rerun()
        except Exception as exc:
            safe_error = sanitize_error(exc)
            LOGGER.warning("No se pudo crear la alerta: %s", safe_error)
            if "UNIQUE" in safe_error.upper():
                message = "Ya existe una alerta con ese nombre."
            elif isinstance(exc, ValueError):
                message = str(exc)
            else:
                message = "No se pudo guardar la alerta."
            st.error(message)

    alerts = database.list_alerts(int(user["id"]))
    if not alerts:
        st.info("Aún no tienes alertas. Crea la primera regla en el formulario superior.")
        return
    st.subheader("Reglas guardadas")
    for alert in alerts:
        with st.expander(
            f"{'●' if alert['enabled'] else '○'} {alert['name']}", expanded=False
        ):
            with st.form(f"alert_edit_{alert['id']}"):
                edit_name = st.text_input(
                    "Nombre", value=alert["name"], key=f"alert_name_{alert['id']}"
                )
                edit_keywords = st.multiselect(
                    "Palabras clave",
                    sorted(set(ALERT_KEYWORDS + list(alert["keywords"]))),
                    default=list(alert["keywords"]),
                    key=f"alert_keywords_{alert['id']}",
                )
                edit_sources = st.multiselect(
                    "Organismos",
                    sorted(set(existing_sources + list(alert["sources"]))),
                    default=list(alert["sources"]),
                    key=f"alert_sources_{alert['id']}",
                )
                enabled = st.checkbox(
                    "Activa", value=alert["enabled"], key=f"alert_enabled_{alert['id']}"
                )
                save = st.form_submit_button("Aplicar cambios")
            if save:
                try:
                    database.update_alert(
                        int(alert["id"]),
                        int(user["id"]),
                        name=edit_name,
                        keywords=edit_keywords,
                        sources=edit_sources,
                        enabled=enabled,
                    )
                    st.success("Alerta actualizada.")
                    st.rerun()
                except Exception as exc:
                    LOGGER.warning(
                        "No se pudo actualizar la alerta: %s", sanitize_error(exc)
                    )
                    st.error(
                        str(exc)
                        if isinstance(exc, ValueError)
                        else "No se pudo actualizar la alerta."
                    )
            if st.button("Eliminar alerta", key=f"delete_alert_{alert['id']}"):
                database.delete_alert(int(alert["id"]), int(user["id"]))
                st.rerun()


def _run_daily_report(
    settings: Settings, user: Mapping[str, Any], database: Database
) -> dict[str, Any]:
    if settings.public_demo_mode or getattr(database, "is_read_only_demo", False):
        from core.demo import DemoReadOnlyError

        raise DemoReadOnlyError(
            "La generación de informes está bloqueada en el replay público"
        )
    alerts = database.list_alerts(int(user["id"]), enabled_only=True)
    keywords = flatten_alert_keywords(alerts)
    agent = agent_service()
    result = agent.run_daily_report(
        user_id=int(user["id"]), keywords=keywords, alert_rules=alerts
    )
    return normalize_report_result(result)


def build_report_exports(report: Mapping[str, Any]) -> tuple[bytes, bytes, str, str]:
    """Construye exports autocontenidos y conserva la procedencia del replay."""

    content = str(report.get("report") or "")
    execution_id = str(report.get("execution_id") or "sin-id")
    safe_date = str(report.get("report_date") or business_today().isoformat())[:10]
    is_replay = bool(report.get("evidence_replay")) or str(
        report.get("artifact_kind") or ""
    ) == "acceptance_artifact_replay"
    provenance = {
        "artifact_kind": (
            "acceptance_artifact_replay" if is_replay else "live_execution"
        ),
        "evidence_replay": is_replay,
        "evidence_origin": report.get("evidence_origin"),
        "validated_at": report.get("validated_at"),
        "origin_sha256": report.get("origin_sha256"),
    }
    metrics = dict(report.get("metrics") or {})
    if is_replay:
        metrics.update(
            billing_mode="subscription",
            cost_attribution="not_attributable",
        )
    export_payload = {
        "schema_version": 1,
        **provenance,
        "report_id": report.get("report_id"),
        "execution_id": execution_id,
        "report_date": safe_date,
        "report": content,
        "citations": list(report.get("citations") or []),
        "evaluation": dict(report.get("evaluation") or report.get("judge") or {}),
        "metrics": metrics,
    }
    markdown = content
    if is_replay:
        markdown = (
            "<!-- artifact_kind: acceptance_artifact_replay -->\n"
            "# Replay histórico de aceptación — no es una ejecución nueva\n\n"
            f"- Validado: {provenance['validated_at'] or 'N/D'}\n"
            f"- Origen: `{provenance['evidence_origin'] or 'N/D'}`\n"
            f"- SHA-256 de origen: `{provenance['origin_sha256'] or 'N/D'}`\n\n"
            "- Facturación: suscripción Codex; coste por llamada no atribuible "
            "(N/A)\n\n"
            "---\n\n"
            f"{content}"
        )
    return (
        markdown.encode("utf-8"),
        json.dumps(export_payload, ensure_ascii=False, indent=2, default=str).encode(
            "utf-8"
        ),
        safe_date,
        execution_id,
    )


def _render_report_downloads(report: Mapping[str, Any]) -> None:
    markdown, payload, safe_date, execution_id = build_report_exports(report)
    left, right = st.columns(2)
    left.download_button(
        "Descargar Markdown",
        data=markdown,
        file_name=f"centinela-{safe_date}-{execution_id[:8]}.md",
        mime="text/markdown",
        width="stretch",
    )
    right.download_button(
        "Descargar JSON",
        data=payload,
        file_name=f"centinela-{safe_date}-{execution_id[:8]}.json",
        mime="application/json",
        width="stretch",
    )


def _render_report_quality_banner(report: Mapping[str, Any]) -> dict[str, Any]:
    quality = report_quality_status(report)
    score = quality["score"]
    score_label = f" · {score:.0f}/100" if score is not None else ""
    if quality["level"] == "approved":
        st.success(
            f"Aprobado por el LLM-as-Judge{score_label}. Listo para revisión humana."
        )
    elif quality["level"] == "rejected":
        st.error(
            f"Rechazado por el LLM-as-Judge{score_label}. No debe distribuirse sin "
            "revisión y corrección."
        )
    elif quality["level"] == "degraded":
        st.warning(
            f"Informe generado en modo degradado{score_label}. Conserva trazabilidad, "
            "pero no equivale a una ejecución plenamente aprobada."
        )
    else:
        st.warning(
            "Informe generado sin una aprobación verificable del LLM-as-Judge. "
            "Requiere revisión humana."
        )
    return quality


def render_report(settings: Settings, database: Database, user: Mapping[str, Any]) -> None:
    st.title("Informe regulatorio diario")
    st.caption(
        "Planner → Scraper → Executor → LLM-as-Judge. Cada afirmación material exige cita."
    )
    if settings.public_demo_mode:
        runtime = {"ready": False, "label": "Evidencia reproducida"}
        judge_runtime = {"ready": False}
        report_ready = False
        st.info(
            "Esta vista reproduce un informe generado y validado durante la "
            "aceptación. No ejecuta un modelo nuevo ni consume cuota. Despliega "
            "el modo interactivo y conecta Codex, OpenAI, Ollama o vLLM para "
            "habilitar una ejecución en vivo."
        )
    else:
        runtime = provider_runtime_status(settings, "executor")
        judge_runtime = provider_runtime_status(settings, "evaluator")
        report_ready = bool(runtime["ready"] and judge_runtime["ready"])
        if not runtime["ready"]:
            st.warning(_provider_unavailable_message(runtime, "El informe generativo"))
        if not judge_runtime["ready"]:
            st.warning(
                _provider_unavailable_message(
                    judge_runtime, "La barrera LLM-as-Judge"
                )
            )
    alerts = database.list_alerts(int(user["id"]), enabled_only=True)
    keywords = flatten_alert_keywords(alerts)
    if keywords:
        st.info(
            (
                "Términos de la simulación UI: "
                if settings.public_demo_mode
                else "Prioridades de tus alertas: "
            )
            + " · ".join(keywords)
        )
    else:
        st.info("Sin alertas activas: el agente cubrirá la taxonomía energética completa.")

    if st.button(
        "Preparar el informe regulatorio de hoy",
        type="primary",
        disabled=not report_ready,
    ):
        with st.status("Orquestando el informe…", expanded=True) as status:
            try:
                planning_mode = (
                    "Planner/filtro deterministas"
                    if settings.provider_for_role("planner") == "codex"
                    and settings.provider_for_role("filter") == "codex"
                    else (
                        f"Planner {settings.model_for_role('planner')} · "
                        f"filtro {settings.model_for_role('filter')}"
                    )
                )
                st.write(
                    f"{planning_mode} · "
                    f"Judge {settings.model_for_role('evaluator')} · "
                    f"reporte {settings.model_for_role('executor')} · "
                    f"proveedor {runtime['label']}."
                )
                result = _run_daily_report(settings, user, database)
                st.session_state.latest_report = result
                st.session_state.latest_report_user_id = int(user["id"])
                quality = report_quality_status(result)
                if quality["level"] == "approved":
                    status.update(
                        label="Informe aprobado por el Judge",
                        state="complete",
                        expanded=False,
                    )
                elif quality["level"] == "rejected":
                    status.update(
                        label="Informe generado, pero rechazado por el Judge",
                        state="error",
                        expanded=True,
                    )
                elif quality["level"] == "degraded":
                    status.update(
                        label="Informe generado en modo degradado",
                        state="error",
                        expanded=True,
                    )
                else:
                    status.update(
                        label="Informe generado sin aprobación verificable",
                        state="error",
                        expanded=True,
                    )
            except Exception as exc:
                LOGGER.error("Fallo del agente regulatorio: %s", sanitize_error(exc))
                status.update(label="La ejecución ha fallado", state="error")
                st.error(
                    "El informe no pudo completarse. Consulta Observabilidad y los "
                    "logs del servicio."
                )

    report = (
        st.session_state.get("latest_report")
        if st.session_state.get("latest_report_user_id") == int(user["id"])
        else None
    )
    if report:
        st.subheader("Resultado")
        _render_report_quality_banner(report)
        st.markdown(str(report.get("report") or ""))
        _render_report_downloads(report)
        with st.expander("Evaluación LLM-as-Judge", expanded=True):
            st.json(report.get("evaluation") or {"status": "sin evaluación"})
        citations = report.get("citations") or []
        if citations:
            with st.expander(f"Catálogo de citas ({len(citations)})"):
                st.json(citations)

    stored = database.list_reports(limit=20, user_id=int(user["id"]))
    if stored:
        st.divider()
        st.subheader("Historial")
        labels = {
            f"{item['report_date']} · {item['title']} · {item['id'][:8]}": item
            for item in stored
        }
        selected = st.selectbox("Abrir informe anterior", list(labels), index=None)
        if selected:
            historical = labels[selected]
            metadata = historical.get("metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            _render_report_quality_banner(
                {
                    "evaluation": metadata.get("judge") or {},
                    "errors": metadata.get("errors") or [],
                }
            )
            st.markdown(historical["content"])
            _render_report_downloads(
                {
                    "report_id": historical.get("id"),
                    "report": historical["content"],
                    "execution_id": historical.get("execution_id") or historical["id"],
                    "report_date": historical.get("report_date"),
                    "citations": historical.get("citations") or [],
                    "evaluation": metadata.get("judge") or {},
                    "metrics": metadata.get("metrics") or {},
                    "artifact_kind": metadata.get("artifact_kind"),
                    "evidence_replay": metadata.get("evidence_replay"),
                    "evidence_origin": metadata.get("evidence_origin"),
                    "validated_at": metadata.get("validated_at"),
                    "origin_sha256": metadata.get("origin_sha256"),
                }
            )
            historical_judge = metadata.get("judge") or {}
            if historical_judge:
                with st.expander("Evaluación LLM-as-Judge", expanded=True):
                    st.json(historical_judge)
            historical_citations = historical.get("citations") or []
            if historical_citations:
                with st.expander(
                    f"Catálogo de citas ({len(historical_citations)})"
                ):
                    st.json(historical_citations)


def _index_local_news(engine: Any, database: Database) -> int:
    if getattr(database, "is_read_only_demo", False):
        from core.demo import DemoReadOnlyError

        raise DemoReadOnlyError(
            "La indexación está bloqueada en el replay público"
        )
    news = database.list_news(limit=5000)
    if not news:
        return 0
    if hasattr(engine, "index_news"):
        result = engine.index_news(news)
    else:
        result = engine.add_documents(news)
    if isinstance(result, int):
        return result
    if isinstance(result, Mapping):
        indexed = int(
            result.get("documents_indexed")
            or result.get("indexed")
            or result.get("count")
            or 0
        )
        errors = list(result.get("errors") or [])
        if errors and indexed == 0:
            raise RuntimeError("; ".join(sanitize_error(str(error)) for error in errors))
        return indexed
    return len(news)


def _ask_rag_observed(
    settings: Settings,
    database: Database,
    user: Mapping[str, Any],
    prompt: str,
) -> dict[str, Any]:
    """Ejecuta una pregunta con traza aislada y callback propio por usuario."""

    if settings.public_demo_mode or getattr(database, "is_read_only_demo", False):
        from core.demo import DemoReadOnlyError

        raise DemoReadOnlyError(
            "Las consultas generativas están bloqueadas en el replay público"
        )

    from core.observability import CostTrackingCallback
    from rag.vector_engine import VectorEngine

    execution_id = database.start_execution(
        "rag_chat",
        user_id=int(user["id"]),
        metadata={
            "interface": "streamlit",
            **provider_execution_metadata(settings, "filter"),
        },
    )
    step_id = database.start_step(
        execution_id, "rag_answer", model=settings.model_for_role("filter")
    )
    callback = CostTrackingCallback(
        database=database,
        execution_id=execution_id,
        step_id=step_id,
        model=settings.model_for_role("filter"),
        settings=settings,
        auto_start_execution=False,
    )
    started = perf_counter()
    try:
        engine = VectorEngine(settings=settings, callback=callback)
        result = engine.ask(prompt, k=settings.rag_top_k)
        elapsed = perf_counter() - started
        degraded = isinstance(result, Mapping) and bool(result.get("error"))
        final_status = "degraded" if degraded else "completed"
        database.finish_step(step_id, status=final_status, latency_seconds=elapsed)
        database.finish_execution(
            execution_id, status=final_status, latency_seconds=elapsed
        )
        payload = normalize_rag_result(result)
        payload["execution_id"] = execution_id
        return payload
    except Exception as exc:
        elapsed = perf_counter() - started
        error = sanitize_error(exc)
        database.finish_step(
            step_id, status="failed", error=error, latency_seconds=elapsed
        )
        database.finish_execution(
            execution_id, status="failed", error=error, latency_seconds=elapsed
        )
        raise


def render_chat(
    settings: Settings,
    database: Database,
    user: Mapping[str, Any],
) -> None:
    st.title("Chat RAG")
    st.caption(
        "Recuperación trazable en ChromaDB y redacción multiproveedor; la respuesta "
        "conserva fuentes y URLs."
    )
    if settings.public_demo_mode:
        runtime = {"ready": False}
        st.info(
            "La conversación visible es una captura RAG verificada con fuentes "
            "oficiales. El cuadro de consulta permanece deshabilitado en este replay; "
            "para consultar, despliega el modo interactivo y conecta un proveedor de IA."
        )
    else:
        runtime = provider_runtime_status(settings, "filter")
        if not runtime["ready"]:
            st.warning(_provider_unavailable_message(runtime, "El chat generativo"))
    news_count = len(database.list_news(limit=5000))
    if not news_count:
        st.info("Actualiza primero las fuentes desde el Dashboard.")
        return
    if st.button(
        "Sincronizar índice",
        width="content",
        disabled=settings.public_demo_mode,
    ):
        try:
            with st.spinner("Calculando embeddings de documentos nuevos…"):
                indexed = _index_local_news(vector_service(), database)
            st.success(f"Índice sincronizado: {indexed} documentos procesados.")
        except Exception as exc:
            LOGGER.error("Sincronizacion RAG fallida: %s", sanitize_error(exc))
            st.error("No se pudo sincronizar el índice. Revisa los logs del servicio.")

    messages = st.session_state.setdefault("rag_messages", [])
    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message.get("sources"):
                with st.expander("Fuentes recuperadas"):
                    _render_sources(message["sources"])
    prompt = st.chat_input(
        "Pregunta sobre regulación eléctrica chilena",
        disabled=not runtime["ready"],
    )
    if prompt:
        messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            try:
                with st.spinner("Recuperando evidencia y redactando…"):
                    answer = _ask_rag_observed(
                        settings, database, user, prompt
                    )
                st.markdown(answer["answer"])
                if answer["sources"]:
                    with st.expander("Fuentes recuperadas", expanded=True):
                        _render_sources(answer["sources"])
                messages.append(
                    {
                        "role": "assistant",
                        "content": answer["answer"],
                        "sources": answer["sources"],
                    }
                )
            except Exception as exc:
                LOGGER.error("Consulta RAG fallida: %s", sanitize_error(exc))
                st.error(
                    "La consulta no pudo completarse. Revisa Observabilidad y vuelve a intentarlo."
                )


def _render_sources(sources: Sequence[Any]) -> None:
    for source in sources:
        if isinstance(source, str):
            st.markdown(f"- {source}")
            continue
        if not isinstance(source, Mapping):
            st.markdown(f"- {source}")
            continue
        label = source.get("source") or source.get("title") or "Fuente"
        url = source.get("url") or source.get("source_url") or ""
        excerpt = source.get("excerpt") or source.get("document") or ""
        if url:
            st.markdown(f"- [{label}]({url})")
        else:
            st.markdown(f"- {label}")
        if excerpt:
            st.caption(str(excerpt)[:300])


def _billing_mode(record: Mapping[str, Any]) -> str:
    """Clasifica una llamada usando primero la metadata de facturacion."""

    metadata = record.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    configured = str(metadata.get("billing_mode") or "").casefold()
    provider = str(metadata.get("provider") or "").casefold()
    model = str(record.get("model") or "").casefold()
    if configured == "subscription" or provider == "codex_cli" or model.startswith(
        "codex-subscription/"
    ):
        return "subscription"
    if (
        configured in {"self_hosted", "self-hosted", "compute"}
        or provider in {"ollama", "vllm"}
        or model.startswith("self-hosted/")
    ):
        return "self_hosted"
    if configured in {"api", "payg", "usage"}:
        return "api"
    if provider in {"openai", "openai_api"}:
        return "api"
    if model or float(record.get("cost_usd") or 0) > 0:
        return "legacy_api"
    return "none"


def _billing_label(modes: set[str]) -> str:
    has_codex = "subscription" in modes
    has_api = bool({"api", "legacy_api"} & modes)
    has_self_hosted = "self_hosted" in modes
    if sum((has_codex, has_api, has_self_hosted)) > 1:
        return "Mixta · por llamada"
    if has_codex:
        return "Codex · suscripción"
    if has_api:
        return "API · atribuible"
    if has_self_hosted:
        return "Self-hosted · cómputo"
    return "Sin llamada generativa"


def _economic_cost_label(
    modes: set[str],
    *,
    api_usd: float = 0.0,
    api_clp: float = 0.0,
    compute_usd: float | None = None,
) -> str:
    """Resume atribución económica sin usar ``None`` ni mezclar categorías."""

    labels: list[str] = []
    if "subscription" in modes:
        labels.append("Codex N/A")
    if {"api", "legacy_api"} & modes:
        labels.append(f"API US${api_usd:,.6f} · CLP ${api_clp:,.2f}")
    if "self_hosted" in modes:
        labels.append(
            f"Cómputo US${compute_usd:,.6f}"
            if compute_usd is not None
            else "Cómputo N/D"
        )
    return " · ".join(labels) or "N/D"


def render_observability(database: Database, user: Mapping[str, Any]) -> None:
    st.title("Observabilidad y tokenomics")
    st.caption(
        "Tokens reportados por cada backend, coste API exacto y estimación de cómputo "
        "self-hosted separados por llamada, nodo y ejecución."
    )
    is_admin = bool(user.get("is_admin"))
    executions = database.list_executions(
        limit=200,
        user_id=None if is_admin else int(user["id"]),
    )
    if getattr(database, "is_read_only_demo", False):
        st.info(
            "Replay histórico: las cifras proceden de la traza de aceptación "
            "conservada; esta visita no ejecuta modelos ni escribe telemetría. "
            "La fila de ejecución usa tiempo de pared y el detalle, latencia por llamada."
        )
    prompt_tokens = sum(int(item.get("prompt_tokens") or 0) for item in executions)
    completion_tokens = sum(int(item.get("completion_tokens") or 0) for item in executions)
    execution_views: list[dict[str, Any]] = []
    codex_calls = 0
    codex_reports = 0
    api_cost_usd = 0.0
    api_cost_clp = 0.0
    api_call_count = 0
    self_hosted_calls = 0
    compute_estimate_usd = 0.0
    for item in executions:
        detail = database.get_execution(str(item["id"])) or dict(item)
        calls = list(detail.get("llm_calls") or [])
        modes = {_billing_mode(call) for call in calls}
        modes.discard("none")
        if not modes:
            execution_mode = _billing_mode(item)
            if execution_mode != "none":
                modes.add(execution_mode)
        subscription_calls = [
            call for call in calls if _billing_mode(call) == "subscription"
        ]
        api_calls = [
            call for call in calls if _billing_mode(call) in {"api", "legacy_api"}
        ]
        hosted_calls = [call for call in calls if _billing_mode(call) == "self_hosted"]
        codex_calls += len(subscription_calls)
        api_call_count += len(api_calls)
        self_hosted_calls += len(hosted_calls)
        codex_reports += int(
            item.get("workflow") == "daily_report" and "subscription" in modes
        )
        if api_calls:
            item_api_usd = sum(float(call.get("cost_usd") or 0) for call in api_calls)
            item_api_clp = sum(float(call.get("cost_clp") or 0) for call in api_calls)
        elif {"api", "legacy_api"} & modes:
            item_api_usd = float(item.get("cost_usd") or 0)
            item_api_clp = float(item.get("cost_clp") or 0)
        else:
            item_api_usd = 0.0
            item_api_clp = 0.0
        item_compute_usd = sum(
            float((call.get("metadata") or {}).get("estimated_compute_cost_usd") or 0)
            for call in hosted_calls
            if isinstance(call.get("metadata"), Mapping)
        )
        api_cost_usd += item_api_usd
        api_cost_clp += item_api_clp
        compute_estimate_usd += item_compute_usd
        execution_views.append(
            {
                "execution": item,
                "detail": detail,
                "modes": modes,
                "billing": _billing_label(modes),
                "display_status": _execution_display_status(item),
                "api_usd": item_api_usd,
                "api_clp": item_api_clp,
                "compute_usd": item_compute_usd,
            }
        )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tokens de entrada", f"{prompt_tokens:,}")
    c2.metric("Tokens de salida", f"{completion_tokens:,}")
    c3.metric(
        "Llamadas generativas",
        f"{codex_calls + api_call_count + self_hosted_calls:,}",
    )
    c4.metric("Coste API atribuible", f"US${api_cost_usd:,.4f}")
    st.caption(f"Conversión contractual: CLP ${api_cost_clp:,.2f} · 1 USD = 940 CLP.")
    if codex_calls:
        st.info(
            "Codex usa cuota de suscripción: su coste por llamada es N/A, no cero. "
            "La imputación opcional se mantiene separada."
        )
    if self_hosted_calls:
        compute_text = (
            f"Estimación configurada: US${compute_estimate_usd:,.6f}."
            if compute_estimate_usd
            else "Coste de infraestructura aún no configurado."
        )
        st.info(
            f"{self_hosted_calls} llamadas self-hosted: coste API 0; {compute_text}"
        )

    with st.expander("Imputación interna opcional de la suscripción"):
        st.caption(
            "Simulación financiera interna; no es una tarifa del proveedor, no se "
            "persiste y no modifica la telemetría exacta."
        )
        enable_imputation = st.toggle(
            "Calcular una imputación orientativa",
            value=False,
            key="enable_subscription_imputation",
        )
        if enable_imputation:
            input_left, input_right = st.columns(2)
            monthly_cost = input_left.number_input(
                "Coste mensual asignado (USD)",
                min_value=0.0,
                value=0.0,
                step=1.0,
            )
            planned_runs = input_right.number_input(
                "Informes previstos al mes",
                min_value=1,
                value=100,
                step=1,
            )
            estimate = _subscription_imputation(
                monthly_cost,
                int(planned_runs),
                codex_reports,
            )
            estimate_left, estimate_right = st.columns(2)
            estimate_left.metric(
                "Imputación por informe",
                f"US${estimate['per_execution_usd']:,.4f}",
                delta=f"CLP ${estimate['per_execution_clp']:,.2f}",
                delta_color="off",
            )
            estimate_right.metric(
                f"Imputación de {codex_reports} informes observados",
                f"US${estimate['observed_total_usd']:,.4f}",
                delta=f"CLP ${estimate['observed_total_clp']:,.2f}",
                delta_color="off",
            )
            st.caption(
                "Fórmula interna: coste mensual asignado / informes previstos. "
                "Conversión ilustrativa: 1 USD = 940 CLP."
            )
    if not executions:
        st.info("Aún no hay ejecuciones instrumentadas.")
        return
    frame = pd.DataFrame(
        [
            {
                "Inicio": _format_timestamp(item.get("started_at")),
                "Flujo": item.get("workflow"),
                "Estado": view["display_status"],
                "Prompt": item.get("prompt_tokens"),
                "Completion": item.get("completion_tokens"),
                "Facturación": view["billing"],
                "Atribución económica": _economic_cost_label(
                    view["modes"],
                    api_usd=view["api_usd"],
                    api_clp=view["api_clp"],
                    compute_usd=(
                        view["compute_usd"]
                        if view["compute_usd"] or "self_hosted" not in view["modes"]
                        else None
                    ),
                ),
                "Latencia (s)": item.get("latency_seconds"),
                "Ejecución": str(item.get("id") or "")[:8],
            }
            for view in execution_views
            for item in [view["execution"]]
        ]
    )
    st.dataframe(frame, hide_index=True, width="stretch")
    labels = {
        (
            f"{_format_timestamp(item['started_at'])} · "
            f"{item['workflow']} · {item['id'][:8]}"
        ): item["id"]
        for item in executions
    }
    selected = st.selectbox("Detalle de ejecución", list(labels))
    detail = database.get_execution(labels[selected]) if selected else None
    if detail:
        left, right = st.tabs(["Pasos", "Llamadas LLM"])
        with left:
            st.dataframe(
                pd.DataFrame(detail.get("steps") or []),
                hide_index=True,
                width="stretch",
            )
        with right:
            calls = detail.get("llm_calls") or []
            if calls:
                call_rows = [
                    {
                        "Modelo": call.get("model"),
                        "Estado": call.get("status"),
                        "Facturación": _billing_label({_billing_mode(call)}),
                        "Prompt": call.get("prompt_tokens"),
                        "Completion": call.get("completion_tokens"),
                        "Atribución económica": _economic_cost_label(
                            {_billing_mode(call)},
                            api_usd=float(call.get("cost_usd") or 0),
                            api_clp=float(call.get("cost_clp") or 0),
                            compute_usd=(
                                float(
                                    (call.get("metadata") or {}).get(
                                        "estimated_compute_cost_usd"
                                    )
                                )
                                if _billing_mode(call) == "self_hosted"
                                and isinstance(call.get("metadata"), Mapping)
                                and (
                                    call.get("metadata") or {}
                                ).get("estimated_compute_cost_usd")
                                is not None
                                else None
                            ),
                        ),
                        "Latencia (s)": call.get("latency_seconds"),
                    }
                    for call in calls
                ]
                st.dataframe(
                    pd.DataFrame(call_rows), hide_index=True, width="stretch"
                )
            else:
                st.info("La ejecución no contiene llamadas LLM.")


def render_architecture(settings: Settings) -> None:
    st.title("Arquitectura y controles")
    st.markdown(
        """
```mermaid
flowchart LR
    U[Usuario / scheduler] --> G[LangGraph: Planner → Scraper → Executor → Judge]
    G --> I[Informe citado y evaluado]
    G --> D[(SQLite + ChromaDB)]
    D --> R[Chat RAG trazable]
    F[Provider Factory: Codex · OpenAI · Ollama · vLLM] --> G
    F --> R
    G --> O[Tokens · latencia · costes]
    R --> O
```
"""
    )
    st.subheader(
        "Routing compatible (no ejecutado en este replay)"
        if settings.public_demo_mode
        else "Model routing efectivo"
    )
    if settings.public_demo_mode:
        st.info(
            "La tabla describe la configuración portable de CENtinela. El replay "
            "solo presenta una ejecución histórica y no contacta ningún proveedor."
        )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Rol": "Planner",
                    "Proveedor": settings.provider_for_role("planner"),
                    "Modelo": (
                        "Determinista · perfil Codex"
                        if settings.provider_for_role("planner") == "codex"
                        else settings.model_for_role("planner")
                    ),
                },
                {
                    "Rol": "Filtro",
                    "Proveedor": settings.provider_for_role("filter"),
                    "Modelo": (
                        "Determinista · perfil Codex"
                        if settings.provider_for_role("filter") == "codex"
                        else settings.model_for_role("filter")
                    ),
                },
                {
                    "Rol": "Reporte final",
                    "Proveedor": settings.provider_for_role("executor"),
                    "Modelo": settings.model_for_role("executor"),
                },
                {
                    "Rol": "LLM-as-Judge",
                    "Proveedor": settings.provider_for_role("evaluator"),
                    "Modelo": settings.model_for_role("evaluator"),
                },
                {
                    "Rol": "Embeddings",
                    "Proveedor": settings.embedding_provider,
                    "Modelo": settings.embedding_model_for_provider(),
                },
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.subheader("Reglas de confianza")
    st.markdown(
        """
- El contenido web se trata como datos no confiables, nunca como instrucciones.
- Solo se aceptan citas con URL presente en el catálogo de la ejecución.
- Una cita demuestra procedencia; la interpretación requiere revisión regulatoria.
- Los fallos por fuente son visibles y no se sustituyen por noticias inventadas.
- Prompts, contraseñas y credenciales de proveedores no se guardan en las trazas.
- Los health checks consultan `codex login status` o `/v1/models`; nunca exponen secretos.
- OpenAI API, suscripción Codex y cómputo self-hosted se contabilizan por separado.
"""
    )


def _inject_styles() -> None:
    st.markdown(
        """
<style>
  .stApp { background: linear-gradient(180deg, #f7faf8 0%, #ffffff 45%); }
  .block-container { max-width: 1440px; padding-left: clamp(1rem, 2vw, 2rem); padding-right: clamp(1rem, 2vw, 2rem); }
  .hero { padding: 4.5rem 1rem 1.5rem; text-align: center; }
  .hero .eyebrow { color: #2f735c; font-size: .78rem; font-weight: 750; letter-spacing: .17em; }
  .hero h1 { margin: .2rem 0; color: #0c3330; font-size: 4.4rem; letter-spacing: -.065em; }
  .hero h1 span { color: #24a660; }
  .hero p { color: #52716a; font-size: 1.12rem; }
  [data-testid="stMetric"] { background: #fff; border: 1px solid #dce9e2; border-radius: 14px; padding: 1rem; }
  [data-testid="stSidebar"] { border-right: 1px solid #dce9e2; }
  .stButton > button[kind="primary"] { font-weight: 700; }
  [data-testid="stHeaderActionElements"] a[aria-label^="Link to heading"] { display: none !important; }
  [data-testid="stAppDeployButton"], .stAppDeployButton, #MainMenu, footer { display: none !important; }
</style>
""",
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(
        page_title="CENtinela · Inteligencia Regulatoria",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_styles()
    settings = get_settings()
    if settings.public_demo_mode:
        from core.demo import (
            demo_rag_messages,
            demo_report_payload,
            get_demo_repository,
        )

        database = get_demo_repository()
    else:
        settings, database = services()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if settings.public_demo_mode and not st.session_state.get("demo_initialized"):
        demo_user = {
            "id": 0,
            "username": "public-replay",
            "is_admin": False,
            "is_active": True,
        }
        _set_user(demo_user)
        st.session_state.latest_report = demo_report_payload()
        st.session_state.latest_report_user_id = 0
        st.session_state.rag_messages = demo_rag_messages()
        st.session_state.demo_initialized = True
    if "user" not in st.session_state:
        render_auth(database)
        return
    user = st.session_state.user
    runtime = (
        {"ready": False, "provider": "demo", "label": "Replay validado"}
        if settings.public_demo_mode
        else provider_runtime_status(settings, "executor")
    )
    with st.sidebar:
        st.markdown("## CEN<span style='color:#24a660'>tinela</span>", unsafe_allow_html=True)
        st.caption("SEN · Chile")
        st.divider()
        st.markdown(
            "**Invitado de demostración**"
            if settings.public_demo_mode
            else f"**{user['username']}**"
        )
        st.caption(
            "Solo lectura · sin persistencia por visitante"
            if settings.public_demo_mode
            else ("Administrador" if user.get("is_admin") else "Analista")
        )
        if settings.public_demo_mode:
            st.success("Demo pública · evidencia lista", icon="✅")
            st.caption("Replay inmutable; no introduzcas información sensible.")
        elif runtime["ready"]:
            st.success(f"{runtime['label']} · listo", icon="✅")
        elif runtime["provider"] == "codex" and runtime["reason"] == "api_key_auth_not_allowed":
            st.error("Codex con API key · sesión rechazada", icon="⛔")
        elif runtime["provider"] == "codex" and runtime["reason"] == "unsupported_auth_mode":
            st.error("Codex · identidad no confirmada", icon="⛔")
        elif runtime["provider"] == "codex" and runtime["available"]:
            st.warning("Codex instalado · falta sesión", icon="⚠️")
        else:
            st.error(f"{runtime['label']} · no disponible", icon="⛔")
        page = st.radio(
            "Navegación",
            [
                "Dashboard",
                "Informe diario",
                "Alertas",
                "Chat RAG",
                "Observabilidad",
                "Arquitectura",
            ],
            label_visibility="collapsed",
        )
        st.divider()
        if settings.public_demo_mode:
            if st.button("Reiniciar vista", width="stretch"):
                _set_user(None)
                for key in (
                    "demo_initialized",
                    "latest_report",
                    "latest_report_user_id",
                    "rag_messages",
                ):
                    st.session_state.pop(key, None)
                st.rerun()
        elif st.button("Cerrar sesión", width="stretch"):
            _set_user(None)
            st.rerun()

    if settings.public_demo_mode:
        st.warning(
            "**Replay público:** informe, RAG y telemetría son artefactos históricos; "
            "la alerta está marcada como simulación UI. No es una ejecución nueva. "
            "Las acciones con efectos laterales y las llamadas a modelos están "
            "bloqueadas; no se inicializa SQLite ni se crean rutas de runtime."
        )

    if page == "Dashboard":
        render_dashboard(settings, database, user)
    elif page == "Informe diario":
        render_report(settings, database, user)
    elif page == "Alertas":
        render_alerts(settings, database, user)
    elif page == "Chat RAG":
        render_chat(settings, database, user)
    elif page == "Observabilidad":
        render_observability(database, user)
    else:
        render_architecture(settings)


if __name__ == "__main__":
    main()
