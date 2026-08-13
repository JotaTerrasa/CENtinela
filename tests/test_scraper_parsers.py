from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scrapers.chile_regulatory import ChileRegulatoryScraper, ScraperConfig


def NOW() -> datetime:
    return datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def scraper() -> ChileRegulatoryScraper:
    return ChileRegulatoryScraper(
        config=ScraperConfig(max_items_per_source=10, hydrate_articles=False), now=NOW
    )


def test_rss_extracts_traceability_date_topics_and_content(scraper):
    xml = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Norma técnica para BESS y generación solar</title>
      <link>https://www.cne.cl/prensa/norma-bess/</link>
      <pubDate>Wed, 12 Aug 2026 14:00:00 +0000</pubDate>
      <description><![CDATA[<p>La CNE abrió la consulta sobre almacenamiento.</p>]]></description>
      </item></channel></rss>"""
    source = scraper._normalize_sources(["cne"])[0]
    documents = scraper._parse_feed(__import__("scrapers.chile_regulatory", fromlist=["SOURCE_REGISTRY"]).SOURCE_REGISTRY[source], xml)

    assert len(documents) == 1
    data = documents[0].to_dict()
    assert data["source_url"] == "https://www.cne.cl/"
    assert data["published_at"] == "2026-08-12T14:00:00+00:00"
    assert data["retrieved_at"] == "2026-08-13T12:00:00+00:00"
    assert {"BESS", "Solar"}.issubset(data["topics"])
    assert data["content"] == "La CNE abrió la consulta sobre almacenamiento."
    assert data["id"]


@pytest.mark.parametrize(
    ("key", "html", "expected_title", "expected_date"),
    [
        (
            "cen",
            """<article class="news-card"><div class="news-category-badge">SISTEMA ELÉCTRICO</div>
            <span class="news-date">12 de agosto de 2026</span><h3><a href="/novedades/estudio-bess/">
            Estudio del Coordinador sobre BESS</a></h3><div class="news-content"><p>Monitoreo del SEN.</p></div></article>""",
            "Estudio del Coordinador sobre BESS",
            "2026-08-12T16:00:00+00:00",
        ),
        (
            "cne",
            """<div class="col-md-4"><span class="fecha">11/08/2026</span>
            <h3 class="entry-title"><a href="https://www.cne.cl/prensa/norma/">Nueva norma técnica</a></h3>
            <p>Consulta de transmisión.</p></div>""",
            "Nueva norma técnica",
            "2026-08-11T16:00:00+00:00",
        ),
        (
            "minenergia",
            """<div class="thumbnail card"><div class="tag">Nacional</div><div class="caption">
            <p class="date-text">10 Ago 2026</p><a href="/noticias/nacional/plan-h2v"><h3>Plan de Hidrógeno Verde</h3></a>
            <p>Hoja de ruta H2V para Chile.</p></div></div>""",
            "Plan de Hidrógeno Verde",
            "2026-08-10T16:00:00+00:00",
        ),
        (
            "sec",
            """<article><time datetime="2026-08-09T10:00:00-04:00"></time><h3>
            <a href="https://www.sec.cl/sec-fiscaliza-baterias/">SEC fiscaliza instalaciones de baterías</a></h3>
            <p class="entry-summary">Fiscalización eléctrica.</p></article>""",
            "SEC fiscaliza instalaciones de baterías",
            "2026-08-09T14:00:00+00:00",
        ),
        (
            "sea",
            """<div class="views-row"><div class="views-field-field-shared-created">
            <time datetime="2026-08-08T14:11:43Z">Vie</time></div><div class="views-field-title">
            <a href="/noticias/proyecto-fotovoltaico">SEA evalúa proyecto fotovoltaico</a></div></div>""",
            "SEA evalúa proyecto fotovoltaico",
            "2026-08-08T14:11:43+00:00",
        ),
    ],
)
def test_official_html_parsers(scraper, key, html, expected_title, expected_date):
    source = __import__("scrapers.chile_regulatory", fromlist=["SOURCE_REGISTRY"]).SOURCE_REGISTRY[key]
    parser = getattr(scraper, f"_parse_{source.parser}_html")
    documents = parser(source, html, source.primary_urls[0])

    assert len(documents) == 1
    assert documents[0].title == expected_title
    assert documents[0].published_at == expected_date
    assert documents[0].url.startswith("http")


def test_senate_table_and_chamber_cards_are_real_legislative_records(scraper):
    senate_html = """<table id="PIniciados"><tbody><tr>
      <td class="td_boletin">18571-08</td><td>Obligaciones para exportadores de cobre</td>
      <td></td><td>En tramitación</td><td>11/08/2026</td></tr></tbody></table>"""
    chamber_html = """<div class="project-card"><span>11 Ago. 2026</span>
      <h3><a href="/legislacion/ProyectosDeLey/tramitacion.aspx?prmID=19255&amp;prmBOLETIN=18571-08">
      Establece obligaciones para exportadores de concentrado de cobre</a></h3>
      <p>Estado: En tramitación</p></div>"""
    registry = __import__("scrapers.chile_regulatory", fromlist=["SOURCE_REGISTRY"]).SOURCE_REGISTRY

    senate = scraper._parse_senado_html(registry["senado"], senate_html, registry["senado"].primary_urls[0])
    chamber = scraper._parse_camara_html(registry["camara"], chamber_html, registry["camara"].primary_urls[0])

    assert senate[0].title.startswith("Boletín 18571-08")
    assert chamber[0].title.startswith("Boletín 18571-08")
    assert senate[0].published_at == chamber[0].published_at == "2026-08-11T16:00:00+00:00"
    assert "Estado: En tramitación" in chamber[0].summary


def test_chamber_homepage_markdown_recovers_official_latest_projects(scraper):
    markdown = """
Modifica la ley eléctrica para reforzar el almacenamiento de energía [18580-07](https://www.camara.cl/legislacion/proyectosdeley/tramitacion.aspx?prmID=19270&prmBOLETIN=18580-07 "Ver Boletín 18580-07")
"""
    registry = __import__(
        "scrapers.chile_regulatory", fromlist=["SOURCE_REGISTRY"]
    ).SOURCE_REGISTRY

    documents = scraper._parse_camara_markdown(registry["camara"], markdown)

    assert len(documents) == 1
    assert documents[0].title.startswith("Boletín 18580-07")
    assert "almacenamiento de energía" in documents[0].content
    assert documents[0].url.startswith("https://www.camara.cl/legislacion/")
    assert documents[0].published_at is None


def test_deduplication_uses_canonical_url_then_normalized_title(scraper):
    registry = __import__("scrapers.chile_regulatory", fromlist=["SOURCE_REGISTRY"]).SOURCE_REGISTRY
    source = registry["cen"]
    first = scraper._document(
        source,
        title="Nueva norma de transmisión",
        summary="Breve",
        content="Breve",
        url="https://www.coordinador.cl/novedades/norma/?utm_source=test",
    )
    richer = scraper._document(
        source,
        title="Nueva norma de transmisión",
        summary="Más detalle",
        content="Contenido mucho más detallado sobre el sistema eléctrico nacional.",
        url="https://www.coordinador.cl/novedades/norma/",
    )
    same_title_other_url = scraper._document(
        source,
        title="NUEVA NORMA DE TRANSMISIÓN",
        summary="Duplicada",
        content="Duplicada",
        url="https://www.coordinador.cl/novedades/otra-url/",
    )

    result = scraper._deduplicate([first, richer, same_title_other_url])

    assert len(result) == 1
    assert "mucho más detallado" in result[0].content


def test_congreso_alias_expands_both_chambers_and_unknown_source_is_rejected(scraper):
    assert scraper._normalize_sources(["congreso"]) == ["senado", "camara"]
    assert scraper._normalize_sources(["ministerio", "coordinador"]) == ["minenergia", "cen"]
    with pytest.raises(ValueError, match="Fuente desconocida"):
        scraper._normalize_sources(["diario-ficticio"])


def test_civil_date_keeps_same_calendar_day_when_rendered_in_chile(scraper):
    from zoneinfo import ZoneInfo

    registry = __import__("scrapers.chile_regulatory", fromlist=["SOURCE_REGISTRY"]).SOURCE_REGISTRY
    document = scraper._document(
        registry["cne"],
        title="Resolución eléctrica",
        summary="Publicación oficial",
        content="Publicación oficial",
        url="https://www.cne.cl/prensa/resolucion/",
        published_at="13/08/2026",
    )

    rendered = datetime.fromisoformat(document.published_at).astimezone(ZoneInfo("America/Santiago"))
    assert rendered.date().isoformat() == "2026-08-13"
