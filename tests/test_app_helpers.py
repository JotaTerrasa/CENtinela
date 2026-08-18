from types import SimpleNamespace

import pytest

from app import (
    _billing_mode,
    _captured_evidence,
    _economic_cost_label,
    _execution_display_status,
    _format_timestamp,
    _index_local_news,
    _news_frame,
    _news_page,
    _safe_external_url,
    _source_link_html,
    _subscription_imputation,
    article_matches_keywords,
    articles_matching_alerts,
    codex_runtime_status,
    dashboard_snapshot,
    flatten_alert_keywords,
    normalize_article,
    normalize_rag_result,
    normalize_report_result,
    report_quality_status,
)
from agent.graph import ReportQualityError


def test_report_quality_error_is_a_distinct_public_failure() -> None:
    error = ReportQualityError("rechazado")
    assert isinstance(error, RuntimeError)
    assert "rechazado" in str(error)


def test_codex_runtime_status_uses_safe_status_command(tmp_path) -> None:
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout="Logged in using ChatGPT",
            stderr="",
        )

    settings = SimpleNamespace(
        codex_cli_path="codex",
        codex_workdir=tmp_path,
        codex_timeout_seconds=240,
    )
    result = codex_runtime_status(
        settings,
        runner=runner,
        executable_finder=lambda executable: "/opt/bin/codex",
    )

    assert result == {
        "available": True,
        "authenticated": True,
        "reason": "ready",
    }
    assert captured["command"] == ["/opt/bin/codex", "login", "status"]
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 10.0
    assert "auth" not in " ".join(captured["command"]).casefold()


def test_codex_runtime_status_does_not_expose_cli_output(tmp_path) -> None:
    secret_like_output = "Not logged in; internal diagnostic SHOULD-NOT-LEAK"

    def runner(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout=secret_like_output, stderr="")

    settings = SimpleNamespace(
        codex_cli_path="codex",
        codex_workdir=tmp_path,
        codex_timeout_seconds=4,
    )
    result = codex_runtime_status(
        settings,
        runner=runner,
        executable_finder=lambda executable: "/opt/bin/codex",
    )

    assert result["authenticated"] is False
    assert result["reason"] == "not_authenticated"
    assert secret_like_output not in repr(result)


def test_codex_runtime_status_rejects_api_key_authentication(tmp_path) -> None:
    def runner(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="Logged in using an API key",
            stderr="",
        )

    settings = SimpleNamespace(
        codex_cli_path="codex",
        codex_workdir=tmp_path,
        codex_timeout_seconds=4,
    )
    result = codex_runtime_status(
        settings,
        runner=runner,
        executable_finder=lambda executable: "/opt/bin/codex",
    )

    assert result == {
        "available": True,
        "authenticated": False,
        "reason": "api_key_auth_not_allowed",
    }


def test_codex_runtime_status_requires_explicit_chatgpt_confirmation(tmp_path) -> None:
    def runner(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout="Login active", stderr="")

    settings = SimpleNamespace(
        codex_cli_path="codex",
        codex_workdir=tmp_path,
        codex_timeout_seconds=4,
    )
    result = codex_runtime_status(
        settings,
        runner=runner,
        executable_finder=lambda executable: "/opt/bin/codex",
    )

    assert result["authenticated"] is False
    assert result["reason"] == "unsupported_auth_mode"


def test_codex_runtime_status_stops_before_process_when_cli_is_missing(tmp_path) -> None:
    def forbidden_runner(command, **kwargs):
        raise AssertionError("no debe lanzar un proceso")

    settings = SimpleNamespace(
        codex_cli_path="codex",
        codex_workdir=tmp_path,
        codex_timeout_seconds=4,
    )
    result = codex_runtime_status(
        settings,
        runner=forbidden_runner,
        executable_finder=lambda executable: None,
    )

    assert result == {
        "available": False,
        "authenticated": False,
        "reason": "executable_not_found",
    }


def test_billing_mode_prefers_codex_subscription_metadata() -> None:
    assert (
        _billing_mode(
            {
                "model": "gpt-5.6-luna",
                "cost_usd": 0,
                "metadata": {"billing_mode": "subscription", "provider": "codex_cli"},
            }
        )
        == "subscription"
    )
    assert _billing_mode({"model": "legacy-api-model", "cost_usd": 0.01}) == (
        "legacy_api"
    )


def test_economic_cost_label_keeps_billing_categories_separate() -> None:
    assert _economic_cost_label({"subscription"}) == "Codex N/A"
    assert "US$0.010000" in _economic_cost_label(
        {"api"}, api_usd=0.01, api_clp=9.4
    )
    assert _economic_cost_label({"self_hosted"}) == "Cómputo N/D"
    assert _economic_cost_label(set()) == "N/D"


def test_subscription_imputation_is_separate_and_deterministic() -> None:
    result = _subscription_imputation(20, 100, 3)
    assert result["per_execution_usd"] == pytest.approx(0.2)
    assert result["per_execution_clp"] == pytest.approx(188)
    assert result["observed_total_usd"] == pytest.approx(0.6)
    assert result["observed_total_clp"] == pytest.approx(564)


def test_report_quality_distinguishes_approval_rejection_and_degradation() -> None:
    assert report_quality_status({"evaluation": {"approved": True}})["level"] == (
        "approved"
    )
    rejected = report_quality_status(
        {"evaluation": {"approved": False, "score": 68}}
    )
    assert rejected["level"] == "rejected"
    assert rejected["score"] == 68
    degraded = report_quality_status(
        {
            "evaluation": {"approved": True, "mode": "llm"},
            "errors": ["executor fallback: barrera de citas"],
        }
    )
    assert degraded["level"] == "degraded"


def test_observability_status_does_not_call_rejected_report_completed() -> None:
    execution = {
        "workflow": "daily_report",
        "status": "completed",
        "metadata": {"judge": {"approved": False, "score": 68}},
    }
    assert _execution_display_status(execution) == "Rechazado por Judge"


def test_dashboard_snapshot_exposes_focus_freshness_and_partial_coverage() -> None:
    news = [
        {
            "source": "Comisión Nacional de Energía (CNE)",
            "title": "Norma BESS",
            "published_at": "2026-08-12T10:00:00Z",
            "fetched_at": "2026-08-13T11:00:00Z",
        },
        {
            "source": "Coordinador Eléctrico Nacional (CEN)",
            "title": "Operación SEN",
            "published_at": "2026-08-11T10:00:00Z",
            "fetched_at": "2026-08-13T12:00:00Z",
        },
    ]
    snapshot = dashboard_snapshot(news, [news[0]], ["BESS"])
    assert snapshot["coverage_count"] == 2
    assert snapshot["coverage_partial"] is True
    assert "Cámara" in snapshot["missing_sources"]
    assert snapshot["focus_source"] == "Comisión Nacional de Energía (CNE)"
    assert snapshot["focus_count"] == 1
    assert snapshot["next_focus"] == "Norma BESS"
    assert snapshot["latest_capture"] == "2026-08-13T12:00:00Z"


def test_news_frame_preserves_traceability_and_captured_evidence() -> None:
    frame = _news_frame(
        [
            {
                "source": "Comisión Nacional de Energía (CNE)",
                "title": "Norma técnica",
                "url": "https://www.cne.cl/prensa/norma-tecnica/",
                "summary": "Resumen regulatorio.",
                "content": "Contenido regulatorio capturado con mayor detalle.",
                "fetched_at": "2026-08-13T12:00:00Z",
                "is_fallback": True,
                "metadata": {"fallback_reason": "HTTP 403 en el endpoint directo"},
            }
        ]
    )

    assert frame.loc[0, "Dominio oficial"] == "www.cne.cl"
    assert frame.loc[0, "URL"] == "https://www.cne.cl/prensa/norma-tecnica/"
    assert frame.loc[0, "Texto normalizado conservado"] == (
        "Contenido regulatorio capturado con mayor detalle."
    )
    assert frame.loc[0, "Capturado el"] == "13/08/2026 08:00"
    assert frame.loc[0, "Método de captura"] == "Fallback"
    assert frame.loc[0, "Huella SHA-256"] == ""


def test_captured_evidence_is_compact_and_never_fabricates_missing_text() -> None:
    assert _captured_evidence({"summary": "", "content": ""}) == ""
    assert _captured_evidence({"summary": "resumen", "content": "texto completo"}) == (
        "texto completo"
    )
    assert _captured_evidence({"content": "uno dos tres cuatro"}, max_chars=12) == (
        "uno dos tres…"
    )
    with pytest.raises(ValueError, match="positivo"):
        _captured_evidence({"content": "texto"}, max_chars=0)


@pytest.mark.parametrize(
    "value",
    ["javascript:alert(1)", "data:text/plain,contenido", "/ruta/relativa", ""],
)
def test_safe_external_url_rejects_non_http_destinations(value: str) -> None:
    assert _safe_external_url(value) == ""


def test_source_link_html_opens_in_same_tab_and_escapes_metadata() -> None:
    markup = _source_link_html(
        "https://www.cne.cl/prensa/norma?tema=bess&region=sur",
        source='CNE "Chile"',
        title="Norma <BESS>",
    )

    assert (
        'href="https://www.cne.cl/prensa/norma?tema=bess&amp;region=sur"' in markup
    )
    assert 'target="_self"' in markup
    assert 'rel="noopener noreferrer"' in markup
    assert "CNE &quot;Chile&quot;" in markup
    assert "Norma &lt;BESS&gt;" in markup
    assert "Abrir fuente" in markup


def test_source_link_html_rejects_unsafe_destinations() -> None:
    assert (
        _source_link_html(
            "javascript:alert(1)",
            source="Fuente",
            title="Contenido",
        )
        == ""
    )


def test_news_page_is_bounded_and_stable_when_filters_shrink() -> None:
    frame = _news_frame(
        [
            {
                "source": "Servicio de Evaluación Ambiental (SEA)",
                "title": f"Proyecto {index}",
                "url": f"https://www.sea.gob.cl/noticias/proyecto-{index}",
            }
            for index in range(23)
        ]
    )

    first, page, total = _news_page(frame, 1)
    assert (len(first), page, total) == (10, 1, 3)
    assert first.iloc[0]["Titular"] == "Proyecto 0"

    last, page, total = _news_page(frame, 99)
    assert (len(last), page, total) == (3, 3, 3)
    assert last.iloc[-1]["Titular"] == "Proyecto 22"

    filtered, page, total = _news_page(frame.iloc[:2], 3)
    assert (len(filtered), page, total) == (2, 1, 1)


def test_news_page_rejects_non_positive_page_size() -> None:
    with pytest.raises(ValueError, match="positivo"):
        _news_page(_news_frame([]), 1, page_size=0)


def test_alert_matching_is_case_and_accent_insensitive() -> None:
    article = {
        "title": "Nueva regulación para transmisión y almacenamiento",
        "summary": "El régimen aplica a sistemas BESS.",
    }
    matches = article_matches_keywords(article, ["TRANSMISION", "bess", "hidrógeno"])
    assert matches == ["TRANSMISION", "bess"]


def test_flatten_alert_keywords_only_uses_enabled_unique_values() -> None:
    alerts = [
        {"enabled": True, "keywords": ["BESS", "Transmisión"]},
        {"enabled": True, "keywords": ["bess", "solar"]},
        {"enabled": False, "keywords": ["hidrógeno"]},
    ]
    assert flatten_alert_keywords(alerts) == ["BESS", "Transmisión", "solar"]


def test_alert_matching_honours_source_restrictions() -> None:
    articles = [
        {"title": "Proyecto BESS", "source": "SEA"},
        {"title": "Norma BESS", "source": "CNE"},
    ]
    alerts = [
        {"enabled": True, "keywords": ["BESS"], "sources": ["SEA"]},
    ]
    assert articles_matching_alerts(articles, alerts) == [articles[0]]


def test_alert_source_aliases_and_chilean_timestamp() -> None:
    articles = [
        {"title": "Proyecto BESS", "source": "Ministerio de Energía de Chile"},
    ]
    alerts = [
        {"enabled": True, "keywords": ["BESS"], "sources": ["Ministerio de Energía"]},
    ]
    assert articles_matching_alerts(articles, alerts) == articles
    assert _format_timestamp("2026-08-13T12:00:00Z") == "13/08/2026 08:00"
    assert _format_timestamp("2026-08-13") == "13/08/2026"


def test_normalize_article_preserves_traceability_fields() -> None:
    result = normalize_article(
        {
            "source": "CNE",
            "title": "Norma técnica",
            "url": "https://www.cne.cl/norma",
            "source_url": "https://www.cne.cl/prensa/",
            "retrieved_at": "2026-08-13T12:00:00Z",
            "topics": ["BESS"],
            "capture_method": "html",
        }
    )
    assert result["fetched_at"] == "2026-08-13T12:00:00Z"
    assert result["keywords"] == ["BESS"]
    assert result["metadata"]["source_url"] == "https://www.cne.cl/prensa/"
    assert result["metadata"]["capture_method"] == "html"


def test_normalizers_accept_graph_and_rag_contracts() -> None:
    report = normalize_report_result(
        {"final_report": "Texto", "judge_result": {"passed": True}}
    )
    rag = normalize_rag_result(
        {"response": "Respuesta", "citations": [{"source": "CEN"}]}
    )
    assert report["report"] == "Texto"
    assert report["evaluation"]["passed"] is True
    assert rag["answer"] == "Respuesta"
    assert rag["sources"][0]["source"] == "CEN"


def test_index_helper_never_reports_failed_documents_as_indexed() -> None:
    class DatabaseStub:
        def list_news(self, limit: int):
            return [{"title": "BESS"}]

    class EngineStub:
        def index_news(self, news):
            return {"status": "skipped", "documents_indexed": 0, "errors": ["sin clave"]}

    with pytest.raises(RuntimeError, match="sin clave"):
        _index_local_news(EngineStub(), DatabaseStub())
