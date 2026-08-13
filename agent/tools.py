"""Herramientas puras de normalizacion, filtrado y trazabilidad.

Estas funciones no realizan llamadas de red. Separarlas de los nodos permite
probar las reglas que protegen las citas sin consumir tokens.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.observability import sanitize_error

from .state import CitationRecord, JudgeResult


CITATION_PATTERN = re.compile(
    r"\[(?P<source>[^\[\]|\r\n]+?)\s*\|\s*(?P<url>https?://[^\]\s]+)\]",
    flags=re.IGNORECASE,
)
JSON_FENCE_PATTERN = re.compile(
    r"```(?:json)?\s*(?P<body>.*?)\s*```",
    flags=re.IGNORECASE | re.DOTALL,
)
TOKEN_PATTERN = re.compile(r"[a-z0-9áéíóúüñ]{3,}", flags=re.IGNORECASE)


def _plain_document(document: Any) -> dict[str, Any]:
    if isinstance(document, Mapping):
        return dict(document)
    to_dict = getattr(document, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    if is_dataclass(document):
        return asdict(document)
    raise TypeError(f"Documento no soportado: {type(document).__name__}")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return re.sub(r"\s+", " ", str(value)).strip()


def _as_topics(value: Any) -> list[str]:
    if value is None:
        return []
    candidates = value if isinstance(value, (list, tuple, set)) else [value]
    topics: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        topic = _as_text(candidate)
        folded = topic.casefold()
        if topic and folded not in seen:
            topics.append(topic)
            seen.add(folded)
    return topics


def valid_public_url(url: str) -> bool:
    """Acepta exclusivamente URLs HTTP(S) absolutas con host."""

    try:
        parsed = urlsplit(url.strip())
    except (TypeError, ValueError):
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def canonical_url(url: str) -> str:
    """Normaliza solo esquema/host y elimina fragmentos, sin perder la ruta fuente."""

    clean = _as_text(url)
    if not valid_public_url(clean):
        return clean
    parsed = urlsplit(clean)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def safe_source_name(source: str) -> str:
    """Evita que el nombre de organismo rompa el formato de cita."""

    return re.sub(r"[\[\]|\r\n]+", " ", _as_text(source)).strip() or "Fuente oficial"


def citation_for(source: str, url: str) -> str:
    """Construye una cita contractual ``[Fuente | URL]``."""

    clean_url = _as_text(url)
    if not valid_public_url(clean_url):
        raise ValueError(f"No se puede citar una URL no publica: {url!r}")
    return f"[{safe_source_name(source)} | {clean_url}]"


def normalize_document(document: Any) -> dict[str, Any]:
    """Convierte la salida de scraper/SQLite a un contrato unico y trazable."""

    raw = _plain_document(document)
    title = _as_text(raw.get("title") or raw.get("headline") or raw.get("name"))
    source = safe_source_name(
        str(raw.get("source") or raw.get("agency") or raw.get("organism") or "")
    )
    url = _as_text(raw.get("url") or raw.get("link") or raw.get("source_url"))
    if not title:
        raise ValueError("El documento regulatorio no tiene titulo")
    if not raw.get("source") and source == "Fuente oficial":
        raise ValueError("El documento regulatorio no identifica la fuente")
    if not valid_public_url(url):
        raise ValueError(f"El documento regulatorio no tiene una URL HTTP(S) valida: {url!r}")

    summary = _as_text(raw.get("summary") or raw.get("description") or raw.get("excerpt"))
    content = _as_text(raw.get("content") or raw.get("body") or summary)
    source_url = _as_text(raw.get("source_url") or url)
    if not valid_public_url(source_url):
        source_url = url
    topics = _as_topics(raw.get("topics") or raw.get("keywords") or raw.get("tags"))
    published_at = _as_text(raw.get("published_at") or raw.get("date"))
    retrieved_at = _as_text(raw.get("retrieved_at") or raw.get("fetched_at"))

    normalized: dict[str, Any] = {
        "title": title,
        "summary": summary,
        "content": content or summary or title,
        "url": url,
        "source": source,
        "source_url": source_url,
        "published_at": published_at,
        "retrieved_at": retrieved_at,
        "topics": topics,
        "keywords": topics,
        "is_fallback": bool(raw.get("is_fallback", False)),
    }
    if raw.get("fallback_reason"):
        normalized["fallback_reason"] = _as_text(raw["fallback_reason"])
    if isinstance(raw.get("metadata"), Mapping):
        normalized["metadata"] = dict(raw["metadata"])
    return normalized


def normalize_documents(documents: Iterable[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Normaliza y deduplica por URL; devuelve errores de filas descartadas."""

    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    positions: dict[str, int] = {}
    for index, document in enumerate(documents):
        try:
            candidate = normalize_document(document)
        except (TypeError, ValueError) as exc:
            errors.append(f"documento {index + 1}: {sanitize_error(exc)}")
            continue
        identity = canonical_url(candidate["url"])
        if identity in positions:
            previous = normalized[positions[identity]]
            if len(candidate.get("content", "")) > len(previous.get("content", "")):
                normalized[positions[identity]] = candidate
            continue
        positions[identity] = len(normalized)
        normalized.append(candidate)
    return normalized, errors


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def source_identity(value: str) -> str:
    """Clave estable para etiquetas institucionales y aliases de interfaz."""

    folded = _fold(str(value)).strip()
    if folded in {"cen", "coordinador", "coordinador_electrico"} or "coordinador" in folded:
        return "cen"
    if folded == "cne" or "comision nacional de energia" in folded:
        return "cne"
    if folded in {"minenergia", "ministerio", "energia"} or "ministerio de energia" in folded:
        return "minenergia"
    if folded == "sec" or "superintendencia de electricidad" in folded:
        return "sec"
    if folded == "sea" or "servicio de evaluacion ambiental" in folded:
        return "sea"
    if folded == "senado" or "senado" in folded:
        return "senado"
    if folded in {"camara", "diputados"} or "camara de diputadas" in folded:
        return "camara"
    return folded


def _tokens(value: str) -> set[str]:
    return {match.group(0) for match in TOKEN_PATTERN.finditer(_fold(value))}


def _document_date(value: Any) -> date | None:
    """Interpreta fechas ISO normalizadas sin asumir una fecha cuando falta."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _as_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None


def filter_documents_by_lookback(
    documents: Sequence[Mapping[str, Any]],
    *,
    report_date: date | datetime | str,
    lookback_days: int,
    include_undated: bool = True,
) -> list[dict[str, Any]]:
    """Aplica de forma efectiva la ventana temporal definida por el Planner.

    Las publicaciones fechadas fuera de ``[report_date-lookback, report_date]``
    se excluyen. Un registro sin fecha puede conservarse porque varios portales
    oficiales (especialmente sus fallbacks) no publican ese dato; queda marcado
    como ``undated`` para que el informe no lo presente como novedad fechada.
    """

    if isinstance(report_date, datetime):
        reference = report_date.date()
    elif isinstance(report_date, date):
        reference = report_date
    else:
        reference = date.fromisoformat(str(report_date)[:10])
    days = int(lookback_days)
    if not 1 <= days <= 365:
        raise ValueError("lookback_days debe estar entre 1 y 365")
    cutoff = reference - timedelta(days=days)

    selected: list[dict[str, Any]] = []
    for raw in documents:
        document = dict(raw)
        published = _document_date(document.get("published_at"))
        metadata = dict(document.get("metadata") or {})
        if published is None:
            if not include_undated:
                continue
            metadata["temporal_status"] = "undated"
        elif cutoff <= published <= reference:
            metadata["temporal_status"] = "within_window"
        else:
            continue
        metadata["lookback_cutoff"] = cutoff.isoformat()
        metadata["lookback_reference"] = reference.isoformat()
        document["metadata"] = metadata
        selected.append(document)
    return selected


def _ranked_with_source_coverage(
    ranked: Sequence[tuple[int, int, dict[str, Any]]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Selecciona por puntuacion reservando una evidencia por organismo."""

    ordered = sorted(ranked, key=lambda item: (item[0], item[1]), reverse=True)
    best_by_source: dict[str, tuple[int, int, dict[str, Any]]] = {}
    for item in ordered:
        identity = source_identity(_as_text(item[2].get("source")))
        best_by_source.setdefault(identity, item)

    selected: list[tuple[int, int, dict[str, Any]]] = []
    seen_urls: set[str] = set()
    if limit >= len(best_by_source):
        for item in best_by_source.values():
            identity = canonical_url(_as_text(item[2].get("url")))
            if identity not in seen_urls:
                selected.append(item)
                seen_urls.add(identity)
    for item in ordered:
        if len(selected) >= limit:
            break
        identity = canonical_url(_as_text(item[2].get("url")))
        if identity in seen_urls:
            continue
        selected.append(item)
        seen_urls.add(identity)
    selected.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [document for _, _, document in selected]


def prioritize_documents_by_alerts(
    documents: Sequence[Mapping[str, Any]],
    alerts: Sequence[Mapping[str, Any]],
    *,
    keywords: Sequence[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Prioriza coincidencias personales sin convertirlas en un filtro ciego.

    Las alertas aportan una puntuacion alta y respetan su restriccion opcional de
    organismo. El resultado conserva al menos una publicacion por fuente cuando
    el limite lo permite, de modo que una preferencia BESS no oculte una novedad
    regulatoria relevante de otro organismo.
    """

    if limit < 1:
        return []
    active_rules = [rule for rule in alerts if bool(rule.get("enabled", True))]
    keyword_tokens = _tokens(" ".join(str(value) for value in keywords or []))
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for position, raw in enumerate(documents):
        document = dict(raw)
        source = source_identity(_as_text(document.get("source")))
        haystack_text = " ".join(
            (
                _as_text(document.get("title")),
                _as_text(document.get("summary")),
                _as_text(document.get("content")),
                " ".join(_as_topics(document.get("topics") or document.get("keywords"))),
            )
        )
        folded_haystack = _fold(haystack_text)
        alert_matches: list[str] = []
        best_alert_score = 0
        for rule in active_rules:
            allowed_sources = {
                source_identity(_as_text(value))
                for value in rule.get("sources") or []
                if _as_text(value)
            }
            if allowed_sources and source not in allowed_sources:
                continue
            matches = [
                _as_text(keyword)
                for keyword in rule.get("keywords") or []
                if _as_text(keyword) and _fold(_as_text(keyword)) in folded_haystack
            ]
            if matches:
                alert_matches.extend(matches)
                best_alert_score = max(best_alert_score, len(matches))
        lexical_score = len(keyword_tokens & _tokens(haystack_text))
        metadata = dict(document.get("metadata") or {})
        metadata["alert_matches"] = list(dict.fromkeys(alert_matches))
        document["metadata"] = metadata
        # Una coincidencia de alerta domina el ranking; keywords generales
        # desempatan y la posicion conserva el orden fresco del scraper.
        score = best_alert_score * 100 + lexical_score
        ranked.append((score, -position, document))
    return _ranked_with_source_coverage(ranked, limit=limit)


def filter_documents(
    documents: Sequence[Mapping[str, Any]],
    keywords: Sequence[str] | None,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Ordena evidencia por coincidencia determinista y conserva cobertura.

    Si ningun documento coincide, devuelve la evidencia disponible en vez de
    fabricar un resultado vacio. El planner puede priorizar; no puede ocultar que
    una fuente oficial fue capturada.
    """

    if limit < 1:
        return []
    keyword_tokens = _tokens(" ".join(str(keyword) for keyword in keywords or []))
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for position, raw in enumerate(documents):
        document = dict(raw)
        haystack = " ".join(
            (
                _as_text(document.get("title")),
                _as_text(document.get("summary")),
                _as_text(document.get("content")),
                " ".join(_as_topics(document.get("topics") or document.get("keywords"))),
            )
        )
        score = len(keyword_tokens & _tokens(haystack)) if keyword_tokens else 0
        ranked.append((score, -position, document))
    if any(score > 0 for score, _, _ in ranked):
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [document for _, _, document in ranked[:limit]]


def filter_documents_by_alerts(
    documents: Sequence[Mapping[str, Any]],
    alerts: Sequence[Mapping[str, Any]],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Filtra por reglas completas: keyword y, si existe, organismo permitido.

    A diferencia del ranking general, una regla personalizada no se relaja cuando
    no hay coincidencias: el resultado vacio es informacion valida para el usuario.
    """

    if limit < 1:
        return []
    active_rules = [rule for rule in alerts if bool(rule.get("enabled", True))]
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for position, raw in enumerate(documents):
        document = dict(raw)
        source = source_identity(_as_text(document.get("source")))
        haystack = _fold(
            " ".join(
                (
                    _as_text(document.get("title")),
                    _as_text(document.get("summary")),
                    _as_text(document.get("content")),
                    " ".join(
                        _as_topics(document.get("topics") or document.get("keywords"))
                    ),
                )
            )
        )
        best_score = 0
        for rule in active_rules:
            allowed_sources = {
                source_identity(_as_text(value))
                for value in rule.get("sources") or []
                if _as_text(value)
            }
            if allowed_sources and source not in allowed_sources:
                continue
            matches = sum(
                1
                for keyword in rule.get("keywords") or []
                if _fold(_as_text(keyword)) in haystack
            )
            best_score = max(best_score, matches)
        if best_score:
            ranked.append((best_score, -position, document))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [document for _, _, document in ranked[:limit]]


def format_evidence_catalog(
    documents: Sequence[Mapping[str, Any]],
    *,
    max_documents: int = 30,
    max_chars_per_document: int = 2_400,
) -> str:
    """Serializa evidencia delimitada y con su unica cita permitida."""

    blocks: list[str] = []
    for index, document in enumerate(documents[:max_documents], start=1):
        source = safe_source_name(_as_text(document.get("source")))
        url = _as_text(document.get("url"))
        if not valid_public_url(url):
            continue
        content = _as_text(document.get("content") or document.get("summary"))[
            :max_chars_per_document
        ]
        payload = {
            "evidence_id": index,
            "title": _as_text(document.get("title")),
            "source": source,
            "url": url,
            "published_at": _as_text(document.get("published_at")),
            "topics": _as_topics(document.get("topics") or document.get("keywords")),
            "temporal_status": (document.get("metadata") or {}).get(
                "temporal_status", "unknown"
            ),
            "alert_matches": (document.get("metadata") or {}).get("alert_matches", []),
            "content": content,
            "allowed_citation": citation_for(source, url),
        }
        blocks.append(
            f"<EVIDENCE_{index}>\n{json.dumps(payload, ensure_ascii=False)}\n</EVIDENCE_{index}>"
        )
    return "\n\n".join(blocks)


def message_text(message: Any) -> str:
    """Extrae texto de ``AIMessage`` y de dobles de prueba."""

    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray, str)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and block.get("text"):
                parts.append(str(block["text"]))
        return "\n".join(parts).strip()
    return _as_text(content)


def parse_json_object(value: Any) -> dict[str, Any]:
    """Tolera JSON cercado o texto explicativo, sin usar ``eval``."""

    text = message_text(value)
    fenced = JSON_FENCE_PATTERN.search(text)
    candidates = [fenced.group("body")] if fenced else []
    candidates.append(text)
    first = text.find("{")
    last = text.rfind("}")
    if 0 <= first < last:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def extract_citations(text: str) -> list[dict[str, str]]:
    """Extrae citas exactas y deduplicadas en orden de aparicion."""

    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in CITATION_PATTERN.finditer(text or ""):
        source = safe_source_name(match.group("source"))
        url = match.group("url").strip()
        identity = (source.casefold(), canonical_url(url))
        if identity not in seen:
            results.append({"source": source, "url": url, "text": match.group(0)})
            seen.add(identity)
    return results


def citations_from_documents(
    documents: Sequence[Mapping[str, Any]],
) -> list[CitationRecord]:
    """Crea el catalogo de fuentes primarias sin duplicados de URL."""

    citations: list[CitationRecord] = []
    seen: set[str] = set()
    for document in documents:
        url = _as_text(document.get("url"))
        identity = canonical_url(url)
        if not valid_public_url(url) or identity in seen:
            continue
        citations.append(
            CitationRecord(
                source=safe_source_name(_as_text(document.get("source"))),
                url=url,
                title=_as_text(document.get("title")),
            )
        )
        seen.add(identity)
    return citations


def _is_material_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("<!--"):
        return False
    if stripped.startswith("```") or re.fullmatch(r"[-|: ]+", stripped):
        return False
    without_prefix = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+|>\s*)", "", stripped)
    labels = (
        "fecha del informe:",
        "periodo analizado:",
        "fuentes consultadas:",
        "nota metodologica:",
        "nota metodológica:",
        "palabras clave priorizadas:",
        "sin evidencia suficiente",
    )
    if _fold(without_prefix).startswith(tuple(_fold(label) for label in labels)):
        return False
    return len(_tokens(without_prefix)) >= 2


def validate_report_citations(
    report: str,
    documents: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Comprueba cobertura por linea y pertenencia al catalogo capturado."""

    known: dict[str, str] = {}
    for document in documents:
        url = _as_text(document.get("url"))
        if valid_public_url(url):
            known[canonical_url(url)] = safe_source_name(_as_text(document.get("source")))

    unknown: list[str] = []
    extracted = extract_citations(report)
    for citation in extracted:
        expected_source = known.get(canonical_url(citation["url"]))
        if expected_source is None or expected_source.casefold() != citation["source"].casefold():
            unknown.append(citation["text"])

    missing: list[str] = []
    material_count = 0
    cited_material_count = 0
    for line in report.splitlines():
        if not _is_material_line(line):
            continue
        material_count += 1
        line_has_known = any(
            canonical_url(citation["url"]) in known
            and known[canonical_url(citation["url"])].casefold()
            == citation["source"].casefold()
            for citation in extract_citations(line)
        )
        if line_has_known:
            cited_material_count += 1
        else:
            missing.append(line.strip())

    coverage = (
        cited_material_count / material_count
        if material_count
        else (1.0 if not report else 0.0)
    )
    return {
        "valid": bool(report.strip()) and not missing and not unknown,
        "coverage": coverage,
        "material_lines": material_count,
        "cited_material_lines": cited_material_count,
        "missing_citation_lines": missing,
        "unknown_citations": list(dict.fromkeys(unknown)),
        "citations": extracted,
    }


def ensure_report_citations(
    report: str,
    documents: Sequence[Mapping[str, Any]],
) -> str:
    """Elimina citas ajenas al catalogo, pero nunca asigna respaldo por heuristica.

    Una afirmacion sin cita queda deliberadamente sin ella para que la validacion
    falle y active el fallback extractivo. La procedencia no se infiere por simple
    solapamiento lexico.
    """

    if not report.strip() or not documents:
        return report.strip()
    known = {
        canonical_url(_as_text(document.get("url"))): document
        for document in documents
        if valid_public_url(_as_text(document.get("url")))
    }
    repaired: list[str] = []
    for original_line in report.splitlines():
        line = original_line.rstrip()
        if not _is_material_line(line):
            repaired.append(line)
            continue
        citations = extract_citations(line)
        valid_citations = [
            citation
            for citation in citations
            if canonical_url(citation["url"]) in known
            and safe_source_name(
                _as_text(known[canonical_url(citation["url"])].get("source"))
            ).casefold()
            == citation["source"].casefold()
        ]
        invalid_citations = [citation for citation in citations if citation not in valid_citations]
        for citation in invalid_citations:
            line = line.replace(citation["text"], "").rstrip()
        repaired.append(line)
    return "\n".join(repaired).strip()


def _short_summary(document: Mapping[str, Any], *, max_chars: int = 420) -> str:
    text = _as_text(document.get("summary") or document.get("content") or document.get("title"))
    title = _as_text(document.get("title"))
    if title and _fold(text).startswith(_fold(title)):
        text = text[len(title) :].lstrip(' .,:;–—-"\'()[]')
    if not text:
        text = title
    if len(text) <= max_chars:
        return text
    clipped = text[: max_chars - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{clipped}…"


def deterministic_report(
    documents: Sequence[Mapping[str, Any]],
    *,
    report_date: str,
    keywords: Sequence[str] | None = None,
) -> str:
    """Informe ejecutivo extractivo: cada linea material viaja con su fuente."""

    lines = [
        f"# Informe regulatorio diario CENtinela — {report_date}",
        "",
        f"Fecha del informe: {report_date}",
        "Nota metodológica: síntesis extractiva de evidencia pública; requiere revisión especializada.",
    ]
    if not documents:
        lines.extend(("", "## Resumen ejecutivo", ""))
        lines.append(
            "Sin evidencia suficiente: la captura no devolvió documentos oficiales; "
            "no se emiten conclusiones regulatorias."
        )
        return "\n".join(lines)

    # Un informe corto conserva la primera evidencia de cada organismo y completa
    # con los documentos ya priorizados por alertas/relevancia.
    representatives: list[Mapping[str, Any]] = []
    seen_sources: set[str] = set()
    seen_urls: set[str] = set()
    for document in documents:
        source = source_identity(_as_text(document.get("source")))
        if source in seen_sources:
            continue
        representatives.append(document)
        seen_sources.add(source)
        seen_urls.add(canonical_url(_as_text(document.get("url"))))
    for document in documents:
        if len(representatives) >= 12:
            break
        identity = canonical_url(_as_text(document.get("url")))
        if identity not in seen_urls:
            representatives.append(document)
            seen_urls.add(identity)

    lines.extend(("", "## Resumen ejecutivo", ""))
    for document in representatives[:3]:
        citation = citation_for(_as_text(document.get("source")), _as_text(document.get("url")))
        title = _as_text(document.get("title"))
        summary = _short_summary(document, max_chars=280)
        lines.append(f"- **{title}.** {summary} {citation}")

    lines.extend(("", "## Novedades por organismo", ""))
    for document in representatives:
        citation = citation_for(_as_text(document.get("source")), _as_text(document.get("url")))
        title = _as_text(document.get("title"))
        summary = _short_summary(document)
        published = _as_text(document.get("published_at"))
        date_fragment = published[:10] if published else "fecha no informada"
        source = safe_source_name(_as_text(document.get("source")))
        lines.append(
            f"- **{source} · {date_fragment}: {title}.** {summary} {citation}"
        )

    thematic_groups = (
        ("BESS y almacenamiento", ("bess", "almacenamiento", "bateria")),
        ("Solar", ("solar", "fotovolta")),
        ("Hidrógeno verde", ("hidrogeno verde", "h2v", "electrol")),
        ("Data centers", ("data center", "centro de datos")),
        ("Transmisión y operación", ("transmision", "subestacion", "operacion")),
        ("Evaluación ambiental", ("ambiental", "seia", "rca")),
        ("Tarifas y precios", ("tarifa", "precio de nudo", "licitacion")),
    )
    thematic_lines: list[str] = []
    for label, needles in thematic_groups:
        match = next(
            (
                document
                for document in representatives
                if any(
                    needle in _fold(
                        " ".join(
                            (
                                _as_text(document.get("title")),
                                _as_text(document.get("summary")),
                                " ".join(
                                    _as_topics(
                                        document.get("topics") or document.get("keywords")
                                    )
                                ),
                            )
                        )
                    )
                    for needle in needles
                )
            ),
            None,
        )
        if match is None:
            continue
        citation = citation_for(_as_text(match.get("source")), _as_text(match.get("url")))
        thematic_lines.append(
            f"- **{label}:** la evidencia prioritaria incluye «{_as_text(match.get('title'))}»; "
            f"su aplicabilidad al portafolio debe validarse contra el texto oficial. {citation}"
        )
    if not thematic_lines:
        reference = representatives[0]
        citation = citation_for(
            _as_text(reference.get("source")), _as_text(reference.get("url"))
        )
        thematic_lines.append(
            "- La evidencia capturada no identifica de forma explícita un impacto por "
            "tecnología; cualquier aplicabilidad al portafolio requiere revisar el texto "
            f"oficial. {citation}"
        )
    lines.extend(("", "## Impacto potencial por activo", "", *thematic_lines))

    lines.extend(("", "## Vigilancia recomendada", ""))
    for document in representatives[:3]:
        citation = citation_for(_as_text(document.get("source")), _as_text(document.get("url")))
        lines.append(
            f"- Revisar la publicación «{_as_text(document.get('title'))}» y documentar "
            f"responsable, plazo e impacto antes de adoptar una decisión. {citation}"
        )

    lines.extend(("", "## Fuentes", ""))
    for document in representatives:
        citation = citation_for(_as_text(document.get("source")), _as_text(document.get("url")))
        lines.append(f"- {_as_text(document.get('title'))}. {citation}")

    if keywords:
        lines.extend(
            (
                "",
                "## Alcance del análisis",
                "",
                f"Palabras clave priorizadas: {', '.join(str(item) for item in keywords)}.",
            )
        )
    return ensure_report_citations("\n".join(lines), documents)


def deterministic_judgement(
    report: str,
    documents: Sequence[Mapping[str, Any]],
    *,
    model: str = "gpt-5.6-terra",
) -> JudgeResult:
    """Rúbrica local usada junto al LLM-as-Judge y como fallback operativo."""

    validation = validate_report_citations(report, documents)
    has_evidence = bool(documents)
    traceability = float(validation["coverage"])
    coverage = min(1.0, len(extract_citations(report)) / max(1, min(len(documents), 5)))
    relevance = 1.0 if has_evidence and len(report) >= 120 else (0.5 if report else 0.0)
    clarity = 1.0 if "# " in report and "## " in report else (0.7 if report else 0.0)
    score = round(
        (traceability * 0.4 + coverage * 0.2 + relevance * 0.25 + clarity * 0.15)
        * 100,
        1,
    )
    observations: list[str] = []
    if not has_evidence:
        observations.append("No hubo evidencia oficial para evaluar el contenido regulatorio.")
    if validation["missing_citation_lines"]:
        observations.append("Hay afirmaciones materiales sin una cita valida en la misma linea.")
    if validation["unknown_citations"]:
        observations.append("El informe contiene citas ajenas al catalogo capturado.")
    if not observations:
        observations.append(
            "La validacion determinista confirma formato y procedencia de las citas."
        )
    return JudgeResult(
        approved=bool(validation["valid"] and has_evidence and score >= 70),
        score=score,
        relevance=round(relevance * 100, 1),
        coverage=round(coverage * 100, 1),
        clarity=round(clarity * 100, 1),
        traceability=round(traceability * 100, 1),
        deterministic_valid=bool(validation["valid"]),
        missing_citation_lines=list(validation["missing_citation_lines"]),
        unknown_citations=list(validation["unknown_citations"]),
        observations=observations,
        model=model,
        mode="deterministic_fallback",
    )


__all__ = [
    "CITATION_PATTERN",
    "canonical_url",
    "citation_for",
    "citations_from_documents",
    "deterministic_judgement",
    "deterministic_report",
    "ensure_report_citations",
    "extract_citations",
    "filter_documents",
    "filter_documents_by_alerts",
    "filter_documents_by_lookback",
    "format_evidence_catalog",
    "message_text",
    "normalize_document",
    "normalize_documents",
    "parse_json_object",
    "prioritize_documents_by_alerts",
    "safe_source_name",
    "source_identity",
    "valid_public_url",
    "validate_report_citations",
]
