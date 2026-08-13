from __future__ import annotations

from agent.tools import (
    citation_for,
    deterministic_report,
    ensure_report_citations,
    filter_documents_by_alerts,
    filter_documents_by_lookback,
    normalize_documents,
    prioritize_documents_by_alerts,
    validate_report_citations,
)


DOCUMENTS = [
    {
        "title": "Norma técnica de almacenamiento",
        "summary": "La CNE publicó una norma para sistemas de almacenamiento.",
        "content": "La norma define exigencias aplicables a sistemas BESS.",
        "source": "Comisión Nacional de Energía (CNE)",
        "url": "https://www.cne.cl/normativa/almacenamiento",
        "source_url": "https://www.cne.cl/prensa/",
        "published_at": "2026-08-13",
        "topics": ["BESS", "almacenamiento"],
    }
]


def test_normalize_documents_deduplicates_by_canonical_url() -> None:
    richer = {**DOCUMENTS[0], "url": f"{DOCUMENTS[0]['url']}#detalle", "content": "x" * 500}
    normalized, errors = normalize_documents([DOCUMENTS[0], richer])

    assert errors == []
    assert len(normalized) == 1
    assert normalized[0]["content"] == "x" * 500
    assert normalized[0]["source_url"] == DOCUMENTS[0]["source_url"]


def test_validator_requires_known_citation_on_each_material_line() -> None:
    report = "# Informe\n\nLa CNE publicó una nueva norma sin referencia."
    validation = validate_report_citations(report, DOCUMENTS)

    assert validation["valid"] is False
    assert validation["missing_citation_lines"] == [
        "La CNE publicó una nueva norma sin referencia."
    ]


def test_citation_barrier_removes_unknown_url_without_assigning_support() -> None:
    report = (
        "# Informe\n\nLa norma regula BESS "
        "[Sitio inventado | https://example.com/noticia]."
    )
    repaired = ensure_report_citations(report, DOCUMENTS)

    expected = citation_for(DOCUMENTS[0]["source"], DOCUMENTS[0]["url"])
    assert "example.com" not in repaired
    assert expected not in repaired
    assert validate_report_citations(repaired, DOCUMENTS)["valid"] is False


def test_deterministic_report_has_only_traceable_material_lines() -> None:
    report = deterministic_report(
        DOCUMENTS,
        report_date="2026-08-13",
        keywords=["BESS"],
    )

    validation = validate_report_citations(report, DOCUMENTS)
    assert validation["valid"] is True
    assert validation["unknown_citations"] == []
    expected = (
        "[Comisión Nacional de Energía (CNE) | "
        "https://www.cne.cl/normativa/almacenamiento]"
    )
    assert expected in report
    assert "## Resumen ejecutivo" in report
    assert "## Novedades por organismo" in report
    assert "## Impacto potencial por activo" in report
    assert "## Vigilancia recomendada" in report
    assert "## Fuentes" in report


def test_alert_filter_combines_keyword_and_source_without_relaxing() -> None:
    sea = {**DOCUMENTS[0], "source": "SEA", "url": "https://sea.gob.cl/bess"}
    rules = [{"enabled": True, "keywords": ["BESS"], "sources": ["SEA"]}]
    assert filter_documents_by_alerts([DOCUMENTS[0], sea], rules) == [sea]
    assert filter_documents_by_alerts(
        [DOCUMENTS[0]],
        [{"enabled": True, "keywords": ["hidrógeno"], "sources": ["CNE"]}],
    ) == []


def test_planner_lookback_excludes_old_and_future_but_marks_undated() -> None:
    recent = {**DOCUMENTS[0], "published_at": "2026-08-10"}
    old = {
        **DOCUMENTS[0],
        "title": "Norma histórica",
        "url": "https://www.cne.cl/normativa/historica",
        "published_at": "2026-07-01",
    }
    future = {
        **DOCUMENTS[0],
        "title": "Norma futura",
        "url": "https://www.cne.cl/normativa/futura",
        "published_at": "2026-08-14",
    }
    undated = {
        **DOCUMENTS[0],
        "title": "Proyecto sin fecha",
        "url": "https://www.camara.cl/legislacion/proyecto-sin-fecha",
        "published_at": None,
    }

    selected = filter_documents_by_lookback(
        [recent, old, future, undated],
        report_date="2026-08-13",
        lookback_days=7,
    )

    assert [item["title"] for item in selected] == [recent["title"], undated["title"]]
    assert selected[0]["metadata"]["temporal_status"] == "within_window"
    assert selected[1]["metadata"]["temporal_status"] == "undated"
    assert "metadata" not in recent


def test_alerts_prioritize_matches_without_hiding_other_sources() -> None:
    cne = {
        **DOCUMENTS[0],
        "title": "Nueva metodología tarifaria",
        "summary": "Actualización de precios de nudo.",
        "content": "Actualización de precios de nudo.",
        "topics": ["Precios de nudo"],
    }
    sea = {
        **DOCUMENTS[0],
        "title": "Evaluación ambiental solar",
        "summary": "Ingreso de un proyecto fotovoltaico.",
        "content": "Ingreso de un proyecto fotovoltaico.",
        "source": "Servicio de Evaluación Ambiental (SEA)",
        "url": "https://www.sea.gob.cl/noticias/proyecto-solar",
        "topics": ["Solar"],
    }
    sec_bess = {
        **DOCUMENTS[0],
        "title": "Fiscalización BESS",
        "source": "Superintendencia de Electricidad y Combustibles (SEC)",
        "url": "https://www.sec.cl/fiscalizacion-bess",
    }

    selected = prioritize_documents_by_alerts(
        [cne, sea, sec_bess],
        [{"enabled": True, "keywords": ["BESS"], "sources": ["SEC"]}],
        keywords=["BESS"],
        limit=3,
    )

    assert selected[0]["url"] == sec_bess["url"]
    assert {item["source"] for item in selected} == {
        cne["source"],
        sea["source"],
        sec_bess["source"],
    }
    assert selected[0]["metadata"]["alert_matches"] == ["BESS"]
