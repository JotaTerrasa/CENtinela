"""Ingesta robusta y trazable de fuentes regulatorias chilenas.

La capa usa exclusivamente publicaciones publicas y vivas. Para cada organismo
se consulta primero el canal oficial mas estable disponible (RSS, sitemap o
listado HTML). Si la fuente oficial bloquea o no responde, se intenta otro
endpoint oficial y, como ultimo recurso, un proxy de lectura que recupera en
ese momento la misma URL oficial. Los resultados de este ultimo camino quedan
marcados con ``is_fallback`` y ``fallback_reason``; nunca se inyectan noticias
estaticas ni inventadas.

Los conectores estan deliberadamente encapsulados en esta unica unidad para que
Streamlit y LangGraph consuman un contrato JSON uniforme sin conocer el HTML de
cada institucion.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.observability import sanitize_error

LOGGER = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "CENtinela/1.0 (regulatory-intelligence-research; "
    "+https://github.com/JotaTerrasa/CENtinela)"
)
READING_PROXY_PREFIX = "https://r.jina.ai/https://"
CHILE_TIMEZONE = ZoneInfo("America/Santiago")


class ScraperError(RuntimeError):
    """Error recuperable de una fuente individual."""


@dataclass(frozen=True, slots=True)
class ScraperConfig:
    """Politica de red, volumen y enriquecimiento del scraper."""

    timeout_seconds: float = 15.0
    max_items_per_source: int = 8
    retries: int = 0
    backoff_factor: float = 0.6
    user_agent: str = DEFAULT_USER_AGENT
    hydrate_articles: bool = False
    max_content_characters: int = 20_000
    max_workers: int = 7

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds debe ser mayor que cero")
        if self.max_items_per_source <= 0:
            raise ValueError("max_items_per_source debe ser mayor que cero")
        if self.retries < 0:
            raise ValueError("retries no puede ser negativo")
        if self.max_content_characters < 500:
            raise ValueError("max_content_characters debe ser al menos 500")
        if self.max_workers <= 0:
            raise ValueError("max_workers debe ser mayor que cero")


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    """Endpoints y metadatos verificables de una institucion."""

    key: str
    name: str
    source_url: str
    primary_urls: tuple[str, ...]
    parser: str
    category: str
    allowed_hosts: tuple[str, ...]
    fallback_url: str | None = None


@dataclass(slots=True)
class RegulatoryDocument:
    """Registro normalizado listo para SQLite, ChromaDB y la interfaz."""

    title: str
    summary: str
    content: str
    url: str
    source: str
    source_url: str
    published_at: str | None
    retrieved_at: str
    topics: list[str] = field(default_factory=list)
    category: str = "regulatory"
    is_fallback: bool = False
    fallback_reason: str | None = None
    id: str = ""

    def __post_init__(self) -> None:
        self.title = _clean_text(self.title)
        self.summary = _clean_text(self.summary)
        self.content = _clean_text(self.content)
        self.url = _canonical_url(self.url)
        self.source_url = _canonical_url(self.source_url)
        self.topics = sorted({_clean_text(topic) for topic in self.topics if _clean_text(topic)})
        if not self.title or not self.url or not self.source:
            raise ValueError("title, url y source son obligatorios")
        if not self.content:
            self.content = self.summary or self.title
        if not self.summary:
            self.summary = _summarize(self.content)
        if not self.id:
            identity = f"{self.source}\0{self.url}".encode("utf-8")
            self.id = hashlib.sha256(identity).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Devuelve tipos exclusivamente JSON-serializables."""

        return asdict(self)


# URLs comprobadas el 13-08-2026. Senado y Camara se mantienen separados para
# conservar procedencia, aunque la interfaz pueda agruparlos como Congreso.
SOURCE_REGISTRY: dict[str, SourceDefinition] = {
    "cen": SourceDefinition(
        key="cen",
        name="Coordinador Eléctrico Nacional (CEN)",
        source_url="https://www.coordinador.cl/",
        primary_urls=(
            "https://www.coordinador.cl/novedades/",
            "https://www.coordinador.cl/sitemap.rss",
        ),
        parser="cen",
        category="system-operator",
        allowed_hosts=("coordinador.cl", "www.coordinador.cl"),
        fallback_url="https://www.coordinador.cl/novedades/",
    ),
    "cne": SourceDefinition(
        key="cne",
        name="Comisión Nacional de Energía (CNE)",
        source_url="https://www.cne.cl/",
        primary_urls=(
            "https://www.cne.cl/prensa/",
            "https://www.cne.cl/feed/",
            "https://www.cne.cl/wp-sitemap.xml",
        ),
        parser="cne",
        category="regulator",
        allowed_hosts=("cne.cl", "www.cne.cl"),
        fallback_url="https://www.cne.cl/prensa/",
    ),
    "minenergia": SourceDefinition(
        key="minenergia",
        name="Ministerio de Energía de Chile",
        source_url="https://energia.gob.cl/",
        primary_urls=("https://energia.gob.cl/noticias",),
        parser="minenergia",
        category="government",
        allowed_hosts=("energia.gob.cl",),
        fallback_url="https://energia.gob.cl/noticias",
    ),
    "sec": SourceDefinition(
        key="sec",
        name="Superintendencia de Electricidad y Combustibles (SEC)",
        source_url="https://www.sec.cl/",
        primary_urls=(
            "https://www.sec.cl/categoria/noticias/feed/",
            "https://www.sec.cl/categoria/noticias/",
        ),
        parser="sec",
        category="supervisor",
        allowed_hosts=("sec.cl", "www.sec.cl"),
        fallback_url="https://www.sec.cl/categoria/noticias/",
    ),
    "sea": SourceDefinition(
        key="sea",
        name="Servicio de Evaluación Ambiental (SEA)",
        source_url="https://www.sea.gob.cl/",
        primary_urls=(
            "https://www.sea.gob.cl/noticias",
            "https://www.sea.gob.cl/rss.xml",
        ),
        parser="sea",
        category="environmental",
        allowed_hosts=("sea.gob.cl", "www.sea.gob.cl"),
        fallback_url="https://www.sea.gob.cl/noticias",
    ),
    "senado": SourceDefinition(
        key="senado",
        name="Senado de la República de Chile",
        source_url="https://www.senado.cl/actividad-legislativa",
        primary_urls=(
            "https://tramitacion.senado.cl/appsenado/templates/tramitacion/index.php",
            "https://portallegislativo.senado.cl/proyectos-ley",
        ),
        parser="senado",
        category="legislation",
        allowed_hosts=(
            "senado.cl",
            "www.senado.cl",
            "tramitacion.senado.cl",
            "portallegislativo.senado.cl",
        ),
        fallback_url="https://tramitacion.senado.cl/appsenado/templates/tramitacion/index.php",
    ),
    "camara": SourceDefinition(
        key="camara",
        name="Cámara de Diputadas y Diputados de Chile",
        source_url="https://www.camara.cl/legislacion/ProyectosDeLey/proyectos_ley.aspx",
        primary_urls=(
            "https://www.camara.cl/legislacion/ProyectosDeLey/proyectos_ley.aspx",
        ),
        parser="camara",
        category="legislation",
        allowed_hosts=("camara.cl", "www.camara.cl"),
        # La portada oficial expone los ultimos proyectos ingresados y sigue
        # siendo legible a traves del proxy cuando el listado devuelve 403.
        fallback_url="https://www.camara.cl/index.aspx",
    ),
}

SOURCE_ALIASES = {
    "coordinador": "cen",
    "coordinador_electrico": "cen",
    "ministerio": "minenergia",
    "energia": "minenergia",
    "ministerio_energia": "minenergia",
    "congreso": "congreso",
    "diputados": "camara",
    "cámara": "camara",
}

TOPIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (topic, re.compile(pattern, re.IGNORECASE))
    for topic, pattern in {
        "BESS": r"\b(?:bess|bater[ií]as?|almacenamiento(?:\s+de\s+energ[ií]a)?)\b",
        "Solar": r"\b(?:solar|fotovoltaic[oa]s?|pv)\b",
        "Hidrógeno Verde": r"\b(?:hidr[oó]geno\s+verde|h2v|electr[oó]lisis|electrolizador)\b",
        "Data Centers": r"\b(?:data\s*cent(?:er|re)s?|centros?\s+de\s+datos?)\b",
        "Precios de nudo": r"\b(?:precio(?:s)?\s+de\s+nudo|pncp|pnp)\b",
        "Transmisión": r"\b(?:transmisi[oó]n|subestaci[oó]n|l[ií]nea\s+el[eé]ctrica)\b",
        "PMGD": r"\bpmgd\b",
        "Licitación": r"\b(?:licitaci[oó]n|bases\s+de\s+licitaci[oó]n)\b",
        "Regulación": r"\b(?:regulaci[oó]n|reglament[oa]|norma\s+t[eé]cnica|resoluci[oó]n)\b",
        "Evaluación ambiental": r"\b(?:sea|seia|impacto\s+ambiental|calificaci[oó]n\s+ambiental|rca)\b",
        "Sistema Eléctrico Nacional": r"\b(?:sen|sistema\s+el[eé]ctrico\s+nacional)\b",
        "Descarbonización": r"\b(?:descarbonizaci[oó]n|transici[oó]n\s+energ[eé]tica)\b",
    }.items()
)

SPANISH_MONTHS = {
    "ene": 1,
    "enero": 1,
    "feb": 2,
    "febrero": 2,
    "mar": 3,
    "marzo": 3,
    "abr": 4,
    "abril": 4,
    "may": 5,
    "mayo": 5,
    "jun": 6,
    "junio": 6,
    "jul": 7,
    "julio": 7,
    "ago": 8,
    "agosto": 8,
    "sep": 9,
    "sept": 9,
    "septiembre": 9,
    "oct": 10,
    "octubre": 10,
    "nov": 11,
    "noviembre": 11,
    "dic": 12,
    "diciembre": 12,
}


class ChileRegulatoryScraper:
    """Cliente sin estado persistente para las siete fuentes soportadas."""

    def __init__(
        self,
        settings: Any | None = None,
        *,
        config: ScraperConfig | None = None,
        session: requests.Session | None = None,
        now: Any | None = None,
    ) -> None:
        if config is None:
            defaults = ScraperConfig()
            config = ScraperConfig(
                timeout_seconds=float(
                    getattr(settings, "scraper_timeout_seconds", defaults.timeout_seconds)
                ),
                max_items_per_source=int(
                    getattr(settings, "scraper_max_articles", defaults.max_items_per_source)
                ),
            )
        self.config = config
        self.session = session or self._build_session(config)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.last_errors: dict[str, str] = {}

    @staticmethod
    def _build_session(config: ScraperConfig) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=config.retries,
            connect=config.retries,
            read=config.retries,
            status=config.retries,
            backoff_factor=config.backoff_factor,
            status_forcelist=(408, 425, 429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=config.max_workers,
            pool_maxsize=config.max_workers,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(
            {
                "User-Agent": config.user_agent,
                "Accept": (
                    "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
                    "text/html;q=0.8, */*;q=0.5"
                ),
                "Accept-Language": "es-CL,es;q=0.9,en;q=0.5",
            }
        )
        return session

    def fetch_all(
        self,
        max_per_source: int | None = None,
        *,
        sources: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Recupera y deduplica fuentes; un fallo aislado no cancela el lote."""

        return self.scrape_all(sources=sources, max_items_per_source=max_per_source)

    def scrape_all(
        self,
        sources: Sequence[str] | None = None,
        *,
        max_items_per_source: int | None = None,
    ) -> list[dict[str, Any]]:
        limit = self._validated_limit(max_items_per_source)
        keys = self._normalize_sources(sources)
        if not keys:
            self.last_errors = {}
            return []
        documents: list[RegulatoryDocument] = []
        errors: dict[str, str] = {}
        workers = min(self.config.max_workers, len(keys))
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="centinela-source",
        ) as executor:
            futures = {
                key: executor.submit(self._scrape_source_documents, key, limit)
                for key in keys
            }
            # Se recoge en el orden estable del registro aunque las peticiones
            # ocurran en paralelo; asi la UI y los tests siguen siendo reproducibles.
            for key in keys:
                try:
                    documents.extend(futures[key].result())
                except Exception as exc:  # una institucion no tumba el informe diario
                    safe_error = sanitize_error(exc)
                    errors[key] = safe_error
                    LOGGER.warning("Fuente %s no disponible: %s", key, safe_error)
        self.last_errors = errors
        return [item.to_dict() for item in self._deduplicate(documents)]

    def scrape_source(
        self,
        source: str,
        limit: int | None = None,
        *,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """Recupera una fuente concreta con el mismo esquema que ``fetch_all``."""

        effective = max_items if max_items is not None else limit
        checked_limit = self._validated_limit(effective)
        keys = self._normalize_sources([source])
        documents: list[RegulatoryDocument] = []
        self.last_errors = {}
        for key in keys:
            try:
                documents.extend(self._scrape_source_documents(key, checked_limit))
            except Exception as exc:
                self.last_errors[key] = sanitize_error(exc)
                raise
        return [item.to_dict() for item in self._deduplicate(documents)]

    def _validated_limit(self, limit: int | None) -> int:
        value = self.config.max_items_per_source if limit is None else int(limit)
        if value <= 0:
            raise ValueError("max_items_per_source debe ser mayor que cero")
        return value

    def _normalize_sources(self, sources: Sequence[str] | None) -> list[str]:
        if sources is None:
            return list(SOURCE_REGISTRY)
        result: list[str] = []
        for value in sources:
            key = _slug(str(value))
            key = SOURCE_ALIASES.get(key, key)
            expanded = ["senado", "camara"] if key == "congreso" else [key]
            for candidate in expanded:
                if candidate not in SOURCE_REGISTRY:
                    valid = ", ".join(SOURCE_REGISTRY)
                    raise ValueError(f"Fuente desconocida {value!r}. Opciones: {valid}, congreso")
                if candidate not in result:
                    result.append(candidate)
        return result

    def _scrape_source_documents(
        self, key: str, limit: int
    ) -> list[RegulatoryDocument]:
        source = SOURCE_REGISTRY[key]
        failures: list[str] = []
        for endpoint in source.primary_urls:
            try:
                response = self._get(endpoint)
                documents = self._parse_payload(
                    source, response.text, response.url, response.headers.get("Content-Type", "")
                )
                if documents:
                    return self._finalize(documents, source, limit)
                failures.append(f"{endpoint}: respuesta sin registros")
            except Exception as exc:
                failures.append(f"{endpoint}: {sanitize_error(exc)}")

        if source.fallback_url:
            reason = "; ".join(failures)[-1500:]
            try:
                payload = self._get_reading_proxy(source.fallback_url)
                documents = self._parse_markdown_fallback(source, payload)
                if documents:
                    for document in documents:
                        document.is_fallback = True
                        document.fallback_reason = (
                            "Proxy de lectura en vivo de la URL oficial tras fallar endpoints "
                            f"directos: {reason}"
                        )
                    return self._finalize(documents, source, limit, hydrate=False)
                failures.append("fallback: respuesta sin registros")
            except Exception as exc:
                failures.append(f"fallback: {sanitize_error(exc)}")
        raise ScraperError(" | ".join(failures))

    def _get(self, url: str) -> requests.Response:
        response = self.session.get(url, timeout=self.config.timeout_seconds)
        response.raise_for_status()
        if not response.content:
            raise ScraperError(f"respuesta vacia desde {url}")
        return response

    def _get_reading_proxy(self, official_url: str) -> str:
        parts = urlsplit(official_url)
        if parts.scheme not in {"http", "https"}:
            raise ScraperError("fallback solo admite HTTP(S)")
        proxy_url = READING_PROXY_PREFIX + official_url.removeprefix("https://").removeprefix(
            "http://"
        )
        response = self._get(proxy_url)
        return response.text

    def _parse_payload(
        self,
        source: SourceDefinition,
        payload: str,
        endpoint: str,
        content_type: str,
    ) -> list[RegulatoryDocument]:
        probe = payload.lstrip()[:500].lower()
        is_feed = (
            "xml" in content_type.lower()
            or probe.startswith("<?xml")
            or probe.startswith("<rss")
            or probe.startswith("<feed")
        )
        if is_feed:
            feed_documents = self._parse_feed(source, payload)
            if feed_documents:
                return feed_documents
        parser = getattr(self, f"_parse_{source.parser}_html")
        return parser(source, payload, endpoint)

    def _parse_feed(
        self, source: SourceDefinition, payload: str
    ) -> list[RegulatoryDocument]:
        parsed = feedparser.parse(payload)
        documents: list[RegulatoryDocument] = []
        for entry in parsed.entries:
            title = _clean_text(entry.get("title", ""))
            url = _canonical_url(entry.get("link", ""))
            if not title or not self._valid_source_url(source, url):
                continue
            raw_summary = entry.get("summary") or entry.get("description") or ""
            raw_content = " ".join(
                value.get("value", "") for value in entry.get("content", []) if value
            )
            content = _html_to_text(raw_content or raw_summary)
            summary = _summarize(_html_to_text(raw_summary) or content)
            published = (
                entry.get("published")
                or entry.get("updated")
                or entry.get("created")
            )
            documents.append(
                self._document(
                    source,
                    title=title,
                    summary=summary,
                    content=content,
                    url=url,
                    published_at=published,
                )
            )
        return documents

    def _parse_cen_html(
        self, source: SourceDefinition, payload: str, endpoint: str
    ) -> list[RegulatoryDocument]:
        soup = BeautifulSoup(payload, "html.parser")
        documents: list[RegulatoryDocument] = []
        for card in soup.select("article.news-card, .news-card"):
            anchor = card.select_one("h3 a[href]") or card.select_one("a.read-more-link[href]")
            if not anchor:
                continue
            category_node = card.select_one(".news-category-badge")
            documents.append(
                self._document_from_card(
                    source,
                    card,
                    anchor,
                    date_selectors=(".news-date", "time"),
                    summary_selectors=(".news-content > p", "p"),
                    category=_text(category_node) or source.category,
                )
            )
        return documents

    def _parse_cne_html(
        self, source: SourceDefinition, payload: str, endpoint: str
    ) -> list[RegulatoryDocument]:
        soup = BeautifulSoup(payload, "html.parser")
        documents: list[RegulatoryDocument] = []
        seen_cards: set[int] = set()
        for heading in soup.select("h3.entry-title"):
            anchor = heading.find("a", href=True)
            card = heading.find_parent(["article", "div", "li"])
            if not anchor or not card or id(card) in seen_cards:
                continue
            seen_cards.add(id(card))
            documents.append(
                self._document_from_card(
                    source,
                    card,
                    anchor,
                    date_selectors=(".fecha", "time"),
                    summary_selectors=("p",),
                )
            )
        return documents

    def _parse_minenergia_html(
        self, source: SourceDefinition, payload: str, endpoint: str
    ) -> list[RegulatoryDocument]:
        soup = BeautifulSoup(payload, "html.parser")
        documents: list[RegulatoryDocument] = []
        candidates = soup.select(".thumbnail.card, .view-content .row, .views-row")
        for card in candidates:
            anchor = card.select_one('a[href*="/noticias/"] h3')
            if anchor:
                anchor = anchor.parent
            else:
                anchor = card.select_one('h2 + p + a[href*="/noticias/"]') or card.select_one(
                    'a[href*="/noticias/"][title]'
                )
            if not anchor:
                continue
            title_node = card.select_one("h2, h3")
            title = _text(title_node) or anchor.get("title", "")
            if not title:
                continue
            date_node = card.select_one(".date-text, time")
            summary_node = next(
                (node for node in card.select("p") if "date-text" not in node.get("class", [])),
                None,
            )
            tag_node = card.select_one(".tag")
            documents.append(
                self._document(
                    source,
                    title=title,
                    summary=_text(summary_node),
                    content=_text(summary_node),
                    url=urljoin(endpoint, anchor.get("href", "")),
                    published_at=_text(date_node),
                    category=_text(tag_node) or source.category,
                )
            )
        return documents

    def _parse_sec_html(
        self, source: SourceDefinition, payload: str, endpoint: str
    ) -> list[RegulatoryDocument]:
        soup = BeautifulSoup(payload, "html.parser")
        documents: list[RegulatoryDocument] = []
        anchors = soup.select(
            'h2 a[href], h3 a[href], article a[href*="sec.cl/"], .post a[href]'
        )
        for anchor in anchors:
            title = _text(anchor)
            url = urljoin(endpoint, anchor.get("href", ""))
            if len(title) < 12 or not self._valid_source_url(source, url):
                continue
            card = anchor.find_parent(["article", "li", "div"]) or anchor
            documents.append(
                self._document_from_card(
                    source,
                    card,
                    anchor,
                    date_selectors=("time", ".date", ".entry-date"),
                    summary_selectors=(".entry-summary", ".excerpt", "p"),
                )
            )
        return documents

    def _parse_sea_html(
        self, source: SourceDefinition, payload: str, endpoint: str
    ) -> list[RegulatoryDocument]:
        soup = BeautifulSoup(payload, "html.parser")
        documents: list[RegulatoryDocument] = []
        for card in soup.select(".views-row"):
            anchor = card.select_one('.views-field-title a[href*="/noticias/"]')
            if not anchor:
                continue
            documents.append(
                self._document_from_card(
                    source,
                    card,
                    anchor,
                    date_selectors=("time[datetime]", ".datetime"),
                    summary_selectors=(".views-field-body", "p"),
                )
            )
        return documents

    def _parse_senado_html(
        self, source: SourceDefinition, payload: str, endpoint: str
    ) -> list[RegulatoryDocument]:
        soup = BeautifulSoup(payload, "html.parser")
        documents: list[RegulatoryDocument] = []
        table = soup.select_one("table#PIniciados")
        if table:
            for row in table.select("tbody tr"):
                cells = row.find_all("td")
                if len(cells) < 5:
                    continue
                bulletin = _clean_text(cells[0].get_text(" ", strip=True))
                title = _clean_text(cells[1].get_text(" ", strip=True))
                status = _clean_text(cells[3].get_text(" ", strip=True))
                published = _clean_text(cells[4].get_text(" ", strip=True))
                if not bulletin or not title:
                    continue
                url = (
                    "https://tramitacion.senado.cl/appsenado/templates/tramitacion/"
                    f"index.php?boletin_ini={bulletin}"
                )
                documents.append(
                    self._document(
                        source,
                        title=f"Boletín {bulletin}: {title}",
                        summary=f"Estado: {status or 'sin estado informado'}.",
                        content=f"{title}. Estado legislativo: {status or 'sin estado informado'}.",
                        url=url,
                        published_at=published,
                    )
                )
        if documents:
            return documents

        # Portal nuevo: su HTML puede ser renderizado por servidor o contener tarjetas.
        for card in soup.select("article, .card, tr"):
            text = _text(card)
            match = re.search(r"\b(\d{3,6}-\d{2})\b", text)
            if not match:
                continue
            anchor = card.find("a", href=True)
            title_node = card.select_one("h2, h3, td:nth-of-type(2)")
            title = _text(title_node)
            if not title:
                continue
            bulletin = match.group(1)
            url = urljoin(endpoint, anchor.get("href", "")) if anchor else (
                "https://tramitacion.senado.cl/appsenado/templates/tramitacion/"
                f"index.php?boletin_ini={bulletin}"
            )
            documents.append(
                self._document(
                    source,
                    title=f"Boletín {bulletin}: {title}",
                    summary=_summarize(text),
                    content=text,
                    url=url,
                    published_at=_text(card.select_one("time, .date, td:last-child")),
                )
            )
        return documents

    def _parse_camara_html(
        self, source: SourceDefinition, payload: str, endpoint: str
    ) -> list[RegulatoryDocument]:
        soup = BeautifulSoup(payload, "html.parser")
        documents: list[RegulatoryDocument] = []
        anchors = soup.select('a[href*="tramitacion.aspx"][href*="prmBOLETIN"]')
        for anchor in anchors:
            title = _text(anchor)
            if not title or title.lower().startswith("n° bolet"):
                continue
            card = anchor.find_parent(["article", "li", "div"])
            text = _text(card or anchor.parent)
            match = re.search(r"\b(\d{3,6}-\d{2})\b", anchor.get("href", "") + " " + text)
            if not match:
                continue
            bulletin = match.group(1)
            published = _extract_date_text(text)
            status_match = re.search(r"Estado\s*:?\s*([^\n|]+)", text, re.IGNORECASE)
            status = _clean_text(status_match.group(1)) if status_match else "En tramitación"
            documents.append(
                self._document(
                    source,
                    title=f"Boletín {bulletin}: {title}",
                    summary=f"Estado: {status}.",
                    content=f"{title}. Estado legislativo: {status}.",
                    url=urljoin(endpoint, anchor.get("href", "")),
                    published_at=published,
                )
            )
        return documents

    def _parse_markdown_fallback(
        self, source: SourceDefinition, payload: str
    ) -> list[RegulatoryDocument]:
        if source.key == "camara":
            return self._parse_camara_markdown(source, payload)
        if source.key == "senado":
            # El proxy conserva tablas Markdown; BeautifulSoup no ayuda aqui.
            return self._parse_senado_markdown(source, payload)

        documents: list[RegulatoryDocument] = []
        link_pattern = re.compile(
            r"(?<!!)\[\*{0,2}([^\]\n]{12,})\*{0,2}\]\((https?://[^\s)]+)"
        )
        matches = list(link_pattern.finditer(payload))
        for index, match in enumerate(matches):
            title = _clean_markdown(match.group(1))
            url = _canonical_url(match.group(2).split(' "', 1)[0])
            if not self._valid_source_url(source, url) or _is_navigation_title(title):
                continue
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(payload)
            nearby = payload[match.end() : min(next_start, match.end() + 1200)]
            summary = _clean_fallback_excerpt(nearby, title=title)
            documents.append(
                self._document(
                    source,
                    title=title,
                    summary=summary,
                    content=summary,
                    url=url,
                    published_at=_extract_date_text(nearby),
                )
            )
        return documents

    def _parse_camara_markdown(
        self, source: SourceDefinition, payload: str
    ) -> list[RegulatoryDocument]:
        documents: list[RegulatoryDocument] = []
        pattern = re.compile(
            r"###\s+\[([^\]]+)\]\((https://www\.camara\.cl/legislacion/"
            r"ProyectosDeLey/tramitacion\.aspx\?[^)]+prmBOLETIN=(\d{3,6}-\d{2})[^)]*)\)"
            r"(.*?)(?=\n\d{3,6}-\d{2}\n|\n###\s+\[|\Z)",
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(payload):
            title, url, bulletin, block = match.groups()
            status_match = re.search(r"Estado:\s*\n?\*?\s*([^\n]+)", block, re.IGNORECASE)
            status = _clean_markdown(status_match.group(1)) if status_match else "En tramitación"
            documents.append(
                self._document(
                    source,
                    title=f"Boletín {bulletin}: {_clean_markdown(title)}",
                    summary=f"Estado: {status}.",
                    content=f"{_clean_markdown(title)}. Estado legislativo: {status}.",
                    url=url,
                    published_at=_extract_date_text(block),
                )
            )
        # La portada de Camara publica el titulo seguido del enlace al boletin
        # en una unica linea Markdown. Es una alternativa oficial verificable
        # cuando el buscador historico bloquea peticiones automatizadas.
        inline_pattern = re.compile(
            r"^(?P<title>.{15,}?)\s+\[(?P<bulletin>\d{3,6}-\d{2})\]"
            r"\((?P<target>https://www\.camara\.cl/legislacion/"
            r"proyectosdeley/tramitacion\.aspx\?[^)]*)\)\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        for match in inline_pattern.finditer(payload):
            title = _clean_markdown(match.group("title")).lstrip("-• ")
            bulletin = match.group("bulletin")
            url = _canonical_url(match.group("target").split(' "', 1)[0])
            if not title or bulletin not in url or not self._valid_source_url(source, url):
                continue
            documents.append(
                self._document(
                    source,
                    title=f"Boletín {bulletin}: {title}",
                    summary="Proyecto ingresado; estado disponible en la ficha oficial.",
                    content=(
                        f"{title}. Proyecto ingresado; el estado legislativo se consulta "
                        "en la ficha oficial."
                    ),
                    url=url,
                    published_at=None,
                )
            )
        return documents

    def _parse_senado_markdown(
        self, source: SourceDefinition, payload: str
    ) -> list[RegulatoryDocument]:
        documents: list[RegulatoryDocument] = []
        for line in payload.splitlines():
            match = re.search(
                r"\b(\d{3,6}-\d{2})\s*\|\s*([^|]{12,})\|(?:\s*[^|]*)?\|\s*([^|]+)\|\s*([^|]+)",
                line,
            )
            if not match:
                continue
            bulletin, title, status, published = (_clean_markdown(x) for x in match.groups())
            documents.append(
                self._document(
                    source,
                    title=f"Boletín {bulletin}: {title}",
                    summary=f"Estado: {status}.",
                    content=f"{title}. Estado legislativo: {status}.",
                    url=(
                        "https://tramitacion.senado.cl/appsenado/templates/tramitacion/"
                        f"index.php?boletin_ini={bulletin}"
                    ),
                    published_at=published,
                )
            )
        return documents

    def _document_from_card(
        self,
        source: SourceDefinition,
        card: Tag,
        anchor: Tag,
        *,
        date_selectors: Sequence[str],
        summary_selectors: Sequence[str],
        category: str | None = None,
    ) -> RegulatoryDocument:
        date_node = next((card.select_one(selector) for selector in date_selectors if card.select_one(selector)), None)
        summary_node = next(
            (card.select_one(selector) for selector in summary_selectors if card.select_one(selector)),
            None,
        )
        published = ""
        if date_node:
            published = date_node.get("datetime", "") or _text(date_node)
        return self._document(
            source,
            title=_text(anchor),
            summary=_text(summary_node),
            content=_text(summary_node),
            url=urljoin(source.source_url, anchor.get("href", "")),
            published_at=published,
            category=category,
        )

    def _document(
        self,
        source: SourceDefinition,
        *,
        title: str,
        summary: str,
        content: str,
        url: str,
        published_at: Any = None,
        category: str | None = None,
    ) -> RegulatoryDocument:
        combined = f"{title}\n{summary}\n{content}"
        return RegulatoryDocument(
            title=title,
            summary=_summarize(summary or content),
            content=content or summary or title,
            url=url,
            source=source.name,
            source_url=source.source_url,
            published_at=_iso_datetime(published_at),
            retrieved_at=self._now().astimezone(timezone.utc).isoformat(),
            topics=_detect_topics(combined),
            category=category or source.category,
        )

    def _finalize(
        self,
        documents: Sequence[RegulatoryDocument],
        source: SourceDefinition,
        limit: int,
        *,
        hydrate: bool | None = None,
    ) -> list[RegulatoryDocument]:
        valid = [doc for doc in documents if self._valid_source_url(source, doc.url)]
        deduped = self._deduplicate(valid)
        deduped.sort(key=lambda item: item.published_at or "", reverse=True)
        selected = deduped[:limit]
        should_hydrate = self.config.hydrate_articles if hydrate is None else hydrate
        if should_hydrate:
            for document in selected:
                self._hydrate_document(document, source)
        return selected

    def _hydrate_document(
        self, document: RegulatoryDocument, source: SourceDefinition
    ) -> None:
        if document.url.lower().endswith(".pdf"):
            return
        try:
            response = self._get(document.url)
            soup = BeautifulSoup(response.text, "html.parser")
            for node in soup.select("script, style, nav, header, footer, aside, form"):
                node.decompose()
            title = _first_text(
                soup,
                (
                    "h1.entry-title",
                    "h1.page-title",
                    "article h1",
                    "main h1",
                    "h1",
                    'meta[property="og:title"]',
                ),
                attribute="content",
            )
            content = _first_text(
                soup,
                (
                    ".entry-content",
                    ".post-content",
                    ".field-name-field-shared-body",
                    ".field--name-field-shared-body",
                    "article",
                    "main",
                ),
            )
            published = _first_text(
                soup,
                (
                    'meta[property="article:published_time"]',
                    "time[datetime]",
                    ".entry-date",
                    ".date",
                ),
                attribute="content",
            )
            if title and "…" not in title and "..." not in title:
                document.title = title
            if content and len(content) > len(document.content):
                document.content = content[: self.config.max_content_characters]
                document.summary = _summarize(document.content)
            parsed_date = _iso_datetime(published)
            if parsed_date:
                document.published_at = parsed_date
            document.topics = _detect_topics(
                f"{document.title}\n{document.summary}\n{document.content}"
            )
        except Exception as exc:
            LOGGER.debug(
                "No se pudo enriquecer %s: %s", document.url, sanitize_error(exc)
            )

    @staticmethod
    def _valid_source_url(source: SourceDefinition, url: str) -> bool:
        if not url:
            return False
        parts = urlsplit(url)
        return parts.scheme in {"http", "https"} and parts.hostname in source.allowed_hosts

    @staticmethod
    def _deduplicate(
        documents: Iterable[RegulatoryDocument],
    ) -> list[RegulatoryDocument]:
        by_url: dict[str, RegulatoryDocument] = {}
        by_fingerprint: dict[str, str] = {}
        for document in documents:
            url_key = _canonical_url(document.url)
            fingerprint = _fingerprint(f"{document.source}\0{document.title}")
            existing_key = by_fingerprint.get(fingerprint)
            if url_key in by_url:
                current = by_url[url_key]
                if len(document.content) > len(current.content):
                    by_url[url_key] = document
                continue
            if existing_key and existing_key in by_url:
                current = by_url[existing_key]
                if len(document.content) > len(current.content):
                    by_url.pop(existing_key)
                    by_url[url_key] = document
                    by_fingerprint[fingerprint] = url_key
                continue
            by_url[url_key] = document
            by_fingerprint[fingerprint] = url_key
        return list(by_url.values())


def fetch_regulatory_updates(
    settings: Any | None = None,
    *,
    sources: Sequence[str] | None = None,
    max_per_source: int | None = None,
) -> list[dict[str, Any]]:
    """Atajo funcional para Streamlit, scripts y tools de LangGraph."""

    return ChileRegulatoryScraper(settings=settings).fetch_all(
        max_per_source=max_per_source, sources=sources
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Smoke test online operable mediante ``python -m``."""

    parser = argparse.ArgumentParser(
        description="Recupera publicaciones regulatorias oficiales de Chile."
    )
    parser.add_argument(
        "--source",
        action="append",
        choices=[*SOURCE_REGISTRY, "congreso"],
        help="Fuente a consultar; se puede repetir. Por defecto consulta las siete.",
    )
    parser.add_argument(
        "--max-per-source",
        type=int,
        default=2,
        help="Máximo de publicaciones por organismo (por defecto: 2).",
    )
    args = parser.parse_args(argv)
    scraper = ChileRegulatoryScraper()
    documents = scraper.fetch_all(
        sources=args.source,
        max_per_source=args.max_per_source,
    )
    print(
        json.dumps(
            {
                "documents_retrieved": len(documents),
                "source_errors": scraper.last_errors,
                "documents": documents,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if documents else 2


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = text.replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip()


def _text(node: Tag | None) -> str:
    return _clean_text(node.get_text(" ", strip=True)) if node else ""


def _html_to_text(value: Any) -> str:
    return _clean_text(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True))


def _clean_markdown(value: str) -> str:
    value = re.sub(r"[*_`#>|]", " ", value)
    value = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", value)
    return _clean_text(value)


def _clean_fallback_excerpt(value: str, *, title: str = "") -> str:
    """Limpia el ruido estructural que añade una pagina Markdown proxificada."""

    text = str(value or "")
    text = re.sub(r'^\s*"[^"\n]{0,240}"\)\s*', " ", text)
    text = re.sub(r"!\[[^]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[(?:\s*…\s*|\s*\.\.\.\s*)\]", " ", text)
    text = re.sub(
        r"(?im)^\s*(?:Title|URL Source|Published Time|Markdown Content)\s*:.*$",
        " ",
        text,
    )
    text = _clean_markdown(text)
    clean_title = _clean_text(title)
    if clean_title and text.casefold().startswith(clean_title.casefold()):
        text = text[len(clean_title) :]
    text = text.lstrip(" .,:;–—-\"'()[]")
    return _summarize(text)


def _summarize(value: str, max_characters: int = 500) -> str:
    text = _clean_text(value)
    if len(text) <= max_characters:
        return text
    shortened = text[: max_characters + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return shortened + "…"


def _canonical_url(value: Any) -> str:
    raw = _clean_text(value)
    if not raw:
        return ""
    parts = urlsplit(raw)
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid", "_ga", "output", "view_full_site"}
    ]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), "")
    )


def _fingerprint(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", _clean_text(value).lower())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")


def _detect_topics(value: str) -> list[str]:
    return [topic for topic, pattern in TOPIC_PATTERNS if pattern.search(value)]


def _iso_datetime(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, time.struct_time):
        parsed = datetime(*value[:6], tzinfo=timezone.utc)
        return parsed.isoformat()
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _clean_text(value)
        date_only = _is_date_only(text)
        if re.match(r"^\d{4}-\d{2}-\d{2}(?:T|\s|$)", text):
            try:
                parsed = date_parser.isoparse(text)
            except (TypeError, ValueError, OverflowError):
                return None
        else:
            spanish = _parse_spanish_date(text)
            if spanish:
                parsed = spanish
            else:
                try:
                    parsed = parsedate_to_datetime(text)
                except (TypeError, ValueError, OverflowError):
                    try:
                        parsed = date_parser.parse(text, dayfirst=True, fuzzy=False)
                    except (TypeError, ValueError, OverflowError):
                        return None
    if parsed.tzinfo is None:
        # Las paginas oficiales expresan fechas/horas en Chile. Para una fecha
        # sin hora usamos mediodia local: al normalizar a UTC nunca retrocede al
        # dia anterior en la UI, evitando afirmar una hora de publicacion falsa.
        if "date_only" in locals() and date_only:
            parsed = parsed.replace(hour=12)
        parsed = parsed.replace(tzinfo=CHILE_TIMEZONE)
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_spanish_date(value: str) -> datetime | None:
    normalized = unicodedata.normalize("NFKD", value.lower())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"\b(?:lun|mar|mie|jue|vie|sab|dom)(?:tes|rcoles|ves|rnes|ado|ingo)?[,.]?\s*", "", normalized)
    match = re.search(
        r"\b(\d{1,2})(?:\s+de|[\s./-]+)\s*([a-z]{3,10}|\d{1,2})(?:\s+de|[\s./-]+)\s*(\d{4})"
        r"(?:\s*[-,]?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        normalized,
    )
    if not match:
        return None
    day, month_raw, year, hour, minute, second = match.groups()
    month = int(month_raw) if month_raw.isdigit() else SPANISH_MONTHS.get(month_raw[:3])
    if not month:
        return None
    try:
        return datetime(
            int(year),
            month,
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
        )
    except ValueError:
        return None


def _extract_date_text(value: str) -> str:
    patterns = (
        r"\b\d{1,2}\s+(?:de\s+)?[A-Za-zÁÉÍÓÚÜÑáéíóúüñ.]+(?:\s+de)?\s+\d{4}(?:\s*-\s*\d{1,2}:\d{2})?",
        r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(0)
    return ""


def _is_date_only(value: str) -> bool:
    """Distingue una fecha civil de un timestamp con hora explicita."""

    return not bool(re.search(r"(?:T|\s)\d{1,2}:\d{2}", value))


def _first_text(
    soup: BeautifulSoup,
    selectors: Sequence[str],
    *,
    attribute: str | None = None,
) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        if attribute:
            value = node.get(attribute) or node.get("datetime")
            if value:
                return _clean_text(value)
        value = _text(node)
        if value:
            return value
    return ""


def _is_navigation_title(value: str) -> bool:
    normalized = _slug(value)
    if re.match(r"^image_?\d+", normalized):
        return True
    return normalized in {
        "leer_mas",
        "ver_mas",
        "inicio",
        "noticias",
        "prensa",
        "mapa_del_sitio",
        "contacto",
        "actividad_legislativa",
        "proyectos_de_ley",
    }


__all__ = [
    "SOURCE_REGISTRY",
    "ChileRegulatoryScraper",
    "RegulatoryDocument",
    "ScraperConfig",
    "ScraperError",
    "SourceDefinition",
    "fetch_regulatory_updates",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
