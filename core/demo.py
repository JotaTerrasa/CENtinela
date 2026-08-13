"""Replay público, inmutable y sin persistencia de los artefactos de aceptación.

Este módulo no abre SQLite ni crea usuarios. Expone un repositorio de solo
lectura con el subconjunto que realmente quedó conservado en ``docs/demo``.
Cuando el artefacto no contiene una fecha, un extracto o una fuente, el replay
mantiene el valor vacío en lugar de reconstruirlo.
"""

from __future__ import annotations

import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT


DEMO_REPORT_PATH = PROJECT_ROOT / "docs" / "demo" / "sample-report.json"
DEMO_RAG_PATH = PROJECT_ROOT / "docs" / "demo" / "sample-rag.json"
DEMO_VALIDATION_PATH = PROJECT_ROOT / "docs" / "demo" / "validation-summary.json"
DEMO_MANIFEST_PATH = PROJECT_ROOT / "docs" / "demo" / "replay-manifest.json"


class DemoReadOnlyError(RuntimeError):
    """Una acción con efectos laterales se intentó ejecutar en el replay."""


@lru_cache(maxsize=4)
def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError(f"El artefacto demo {path.name} debe ser un objeto JSON")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_artifacts(manifest: Mapping[str, Any]) -> None:
    paths = {
        "sample-report.json": DEMO_REPORT_PATH,
        "sample-rag.json": DEMO_RAG_PATH,
        "validation-summary.json": DEMO_VALIDATION_PATH,
    }
    declared = manifest.get("artifacts")
    if not isinstance(declared, Mapping):
        raise ValueError("El manifest del replay no declara hashes de artefactos")
    for name, path in paths.items():
        expected = str(declared.get(name) or "").removeprefix("sha256:")
        if not expected or _sha256(path) != expected:
            raise ValueError(f"La integridad del artefacto {name} no es válida")


@lru_cache(maxsize=1)
def load_demo_bundle() -> dict[str, Any]:
    """Carga y valida una vez el bundle inmutable del replay."""

    manifest = copy.deepcopy(_load_json(DEMO_MANIFEST_PATH))
    _verify_artifacts(manifest)
    report = copy.deepcopy(_load_json(DEMO_REPORT_PATH))
    rag = copy.deepcopy(_load_json(DEMO_RAG_PATH))
    validation = copy.deepcopy(_load_json(DEMO_VALIDATION_PATH))
    return {
        "manifest": manifest,
        "report": report,
        "rag": rag,
        "validation": validation,
    }


def demo_report_payload() -> dict[str, Any]:
    """Normaliza el informe sin alterar los identificadores históricos."""

    bundle = load_demo_bundle()
    payload = copy.deepcopy(bundle["report"])
    payload["evaluation"] = copy.deepcopy(payload.get("judge") or {})
    payload["status"] = "completed"
    payload["artifact_kind"] = "acceptance_artifact_replay"
    payload["evidence_replay"] = True
    payload["evidence_origin"] = "docs/demo/sample-report.json"
    payload["validated_at"] = str(bundle["manifest"]["validated_at"])
    payload["origin_sha256"] = str(
        bundle["manifest"]["artifacts"]["sample-report.json"]
    )
    return payload


def demo_rag_messages() -> list[dict[str, Any]]:
    """Convierte la consulta RAG conservada en mensajes renderizables."""

    payload = copy.deepcopy(load_demo_bundle()["rag"])
    lines: list[str] = []
    sources: list[dict[str, str]] = []
    for raw in payload.get("answer") or []:
        if not isinstance(raw, Mapping):
            continue
        citation = raw.get("citation")
        citation = citation if isinstance(citation, Mapping) else {}
        source = str(citation.get("source") or "Fuente oficial")
        url = str(citation.get("url") or "")
        answer = str(raw.get("text") or "").strip()
        reference = f"[{source} | {url}]" if url else f"[{source}]"
        if answer:
            lines.append(f"- {answer} {reference}")
        # ``sample-rag.json`` conserva la respuesta y la cita, pero no el
        # pasaje recuperado. No reutilizamos la redacción generada como excerpt.
        sources.append({"source": source, "url": url})
    return [
        {"role": "user", "content": str(payload.get("question") or "")},
        {
            "role": "assistant",
            "content": "\n".join(lines),
            "sources": sources,
            "artifact_kind": "acceptance_artifact_replay",
            "evidence_replay": True,
        },
    ]


def _call_metadata() -> dict[str, Any]:
    return {
        "provider": "codex_cli",
        "billing_mode": "subscription",
        "cost_attribution": "not_attributable",
        "token_usage_status": "reported",
        "artifact_kind": "acceptance_artifact_replay",
        "evidence_replay": True,
    }


class DemoRepository:
    """Adaptador de lectura con la interfaz mínima consumida por Streamlit."""

    is_read_only_demo = True

    def __init__(self) -> None:
        bundle = load_demo_bundle()
        report = demo_report_payload()
        manifest = bundle["manifest"]
        citations = list(report.get("citations") or [])
        self._news = [
            {
                "id": index,
                "source": str(citation.get("source") or ""),
                "title": str(citation.get("title") or ""),
                "url": str(citation.get("url") or ""),
                "summary": "",
                "content": "",
                "category": "Catálogo de citas",
                "published_at": None,
                "fetched_at": None,
                "keywords": [],
                "metadata": {
                    "artifact_kind": "acceptance_artifact_replay",
                    "origin": "docs/demo/sample-report.json",
                    "capture_method": "unknown",
                },
                # El catálogo conservado no registra si la cita llegó por
                # scraping directo o fallback; exponer False inventaría ese dato.
                "is_fallback": None,
            }
            for index, citation in enumerate(citations, start=1)
            if isinstance(citation, Mapping)
        ]
        simulation = manifest.get("ui_simulation") or {}
        self._alerts = []
        for raw_alert in simulation.get("alerts") or []:
            if not isinstance(raw_alert, Mapping):
                continue
            alert = copy.deepcopy(raw_alert)
            alert.update(
                user_id=0,
                metadata={
                    "artifact_kind": "ui_simulation",
                    "evidence_replay": False,
                    "evidence_origin": (
                        "docs/demo/replay-manifest.json#/ui_simulation/alerts"
                    ),
                },
            )
            self._alerts.append(alert)
        self._report = report
        self._stored_report = {
            "id": str(report["report_id"]),
            "execution_id": str(report["execution_id"]),
            "user_id": 0,
            "report_date": str(report["report_date"]),
            "title": "Informe regulatorio diario · replay de aceptación",
            "content": str(report.get("report") or ""),
            "citations": citations,
            "metadata": {
                "judge": copy.deepcopy(report.get("evaluation") or {}),
                "errors": [],
                "metrics": copy.deepcopy(report.get("metrics") or {}),
                "artifact_kind": "acceptance_artifact_replay",
                "evidence_replay": True,
                "evidence_origin": report["evidence_origin"],
                "validated_at": report["validated_at"],
                "origin_sha256": report["origin_sha256"],
            },
        }
        self._executions: dict[str, dict[str, Any]] = {}
        for key in ("daily_report", "rag"):
            trace = manifest[key]
            execution = copy.deepcopy(trace["execution"])
            execution.update(
                user_id=0,
                metadata={
                    "provider": "codex_cli",
                    "billing_mode": "subscription",
                    "cost_attribution": "not_attributable",
                    "artifact_kind": "acceptance_artifact_replay",
                    "evidence_replay": True,
                    "judge": (
                        copy.deepcopy(report.get("evaluation") or {})
                        if key == "daily_report"
                        else {}
                    ),
                    "errors": [],
                },
            )
            steps = []
            for raw_step in trace.get("steps") or []:
                step = copy.deepcopy(raw_step)
                step.update(
                    execution_id=execution["id"],
                    cost_usd=0.0,
                    cost_clp=0.0,
                    metadata={
                        "artifact_kind": "acceptance_artifact_replay",
                        "evidence_replay": True,
                    },
                )
                steps.append(step)
            calls = []
            for raw_call in trace.get("llm_calls") or []:
                call = copy.deepcopy(raw_call)
                call.update(
                    execution_id=execution["id"],
                    cost_usd=0.0,
                    cost_clp=0.0,
                    metadata=_call_metadata(),
                )
                calls.append(call)
            execution["steps"] = steps
            execution["llm_calls"] = calls
            self._executions[str(execution["id"])] = execution

    @property
    def acceptance_snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(load_demo_bundle()["manifest"]["acceptance_snapshot"])

    def list_news(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        sources: Sequence[str] | None = None,
        query: str | None = None,
        since: Any = None,
    ) -> list[dict[str, Any]]:
        del since
        rows = self._news
        if sources:
            allowed = set(sources)
            rows = [row for row in rows if row["source"] in allowed]
        if query:
            needle = query.casefold().strip()
            rows = [
                row
                for row in rows
                if needle in f"{row['title']} {row['summary']}".casefold()
            ]
        return copy.deepcopy(rows[offset : offset + limit])

    def list_alerts(
        self, user_id: int, *, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        del user_id
        rows = self._alerts
        if enabled_only:
            rows = [row for row in rows if row["enabled"]]
        return copy.deepcopy(rows)

    def list_reports(
        self, *, limit: int = 30, user_id: int | None = None
    ) -> list[dict[str, Any]]:
        del user_id
        return [copy.deepcopy(self._stored_report)][:limit]

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        return (
            copy.deepcopy(self._stored_report)
            if report_id == self._stored_report["id"]
            else None
        )

    def list_executions(
        self, *, limit: int = 50, user_id: int | None = None
    ) -> list[dict[str, Any]]:
        del user_id
        daily_id = str(self._report["execution_id"])
        ordered = [
            self._executions[daily_id],
            *(
                value
                for key, value in self._executions.items()
                if key != daily_id
            ),
        ]
        return [
            {key: copy.deepcopy(value) for key, value in item.items() if key not in {"steps", "llm_calls"}}
            for item in ordered[:limit]
        ]

    def get_execution(
        self, execution_id: str, *, include_details: bool = True
    ) -> dict[str, Any] | None:
        item = self._executions.get(execution_id)
        if item is None:
            return None
        result = copy.deepcopy(item)
        if not include_details:
            result.pop("steps", None)
            result.pop("llm_calls", None)
        return result

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("create_", "save_", "update_", "delete_", "start_", "finish_", "record_")):
            raise DemoReadOnlyError(
                f"{name} está bloqueado: el replay público es de solo lectura"
            )
        raise AttributeError(name)


@lru_cache(maxsize=1)
def get_demo_repository() -> DemoRepository:
    return DemoRepository()


def ensure_demo_dataset(*_args: Any, **_kwargs: Any) -> None:
    """Compatibilidad defensiva: el antiguo seeding persistente está prohibido."""

    raise DemoReadOnlyError(
        "El replay ya no crea datasets persistentes; usa get_demo_repository()"
    )


__all__ = [
    "DemoReadOnlyError",
    "DemoRepository",
    "demo_rag_messages",
    "demo_report_payload",
    "ensure_demo_dataset",
    "get_demo_repository",
    "load_demo_bundle",
]
