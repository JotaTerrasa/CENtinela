from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.graph import MANDATORY_SOURCES, RegulatoryAgent, ReportQualityError
from core.config import Settings, business_today
from core.database import Database


URL = "https://www.cne.cl/normativa/almacenamiento"
SOURCE = "Comisión Nacional de Energía (CNE)"


class FakeDatabase:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.news: list[dict[str, Any]] = []
        self.report: dict[str, Any] | None = None
        self.execution_status = ""
        self.execution_finish: dict[str, Any] = {}

    def start_execution(self, workflow: str, **kwargs: Any) -> str:
        return str(kwargs.get("execution_id") or "execution")

    def finish_execution(self, execution_id: str, **kwargs: Any) -> bool:
        self.execution_status = str(kwargs.get("status"))
        self.execution_finish = dict(kwargs)
        return True

    def start_step(self, execution_id: str, step_name: str, **kwargs: Any) -> str:
        self.steps.append({"name": step_name, "model": kwargs.get("model")})
        return f"step-{len(self.steps)}"

    def finish_step(self, step_id: str, **kwargs: Any) -> bool:
        return True

    def save_news(self, documents: list[dict[str, Any]]) -> list[int]:
        self.news.extend(documents)
        return list(range(len(documents)))

    def list_news(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.news)

    def get_previous_day_memory(self, **kwargs: Any) -> None:
        return None

    def save_report(self, report_date: str, title: str, content: str, **kwargs: Any) -> str:
        self.report = {"date": report_date, "title": title, "content": content, **kwargs}
        return "report-1"

    def save_daily_memory(self, report_date: str, content: str, **kwargs: Any) -> int:
        return 1


class FakeScraper:
    def scrape_all(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "title": "Norma técnica de almacenamiento",
                "summary": "La CNE publicó exigencias para BESS.",
                "content": "La norma establece exigencias técnicas para BESS.",
                "source": SOURCE,
                "url": URL,
                "source_url": "https://www.cne.cl/prensa/",
                "published_at": business_today().isoformat(),
                "topics": ["BESS"],
            }
        ]


class FakeVectorEngine:
    def __init__(self) -> None:
        self.indexed: list[dict[str, Any]] = []

    def index_documents(self, documents: list[dict[str, Any]]) -> dict[str, Any]:
        self.indexed.extend(documents)
        return {"status": "completed", "documents_indexed": len(documents), "errors": []}


class FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[str] = []

    def invoke(self, prompt: str, config: Any | None = None) -> SimpleNamespace:
        self.calls.append(prompt)
        return SimpleNamespace(content=self.content)


class FailingLLM:
    def __init__(self, message: str = "Codex no disponible") -> None:
        self.message = message
        self.calls: list[str] = []

    def invoke(self, prompt: str, config: Any | None = None) -> None:
        self.calls.append(prompt)
        raise RuntimeError(self.message)


@dataclass
class FakeMetrics:
    def to_dict(self) -> dict[str, int]:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


class FakeCallback:
    execution_id: str | None = None
    step_id: str | None = None
    default_model: str | None = None

    def snapshot(self) -> FakeMetrics:
        return FakeMetrics()


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        planner_model="gpt-5.6-luna",
        filter_model="gpt-5.6-luna",
        report_model="gpt-5.6-sol",
        judge_model="gpt-5.6-terra",
        planner_reasoning_effort="low",
        filter_reasoning_effort="low",
        report_reasoning_effort="high",
        judge_reasoning_effort="medium",
        codex_cli_path="codex-inexistente-para-test",
        codex_timeout_seconds=1,
        codex_workdir=None,
        scraper_max_articles=8,
    )


def make_agent() -> tuple[RegulatoryAgent, FakeDatabase, FakeVectorEngine, list[FakeLLM]]:
    database = FakeDatabase()
    vector = FakeVectorEngine()
    planner = FakeLLM(
        '{"objective":"Revisar BESS","lookback_days":7,"keywords":["BESS"],'
        '"max_items_per_source":5,"rationale":"Prioridad operativa"}'
    )
    executor = FakeLLM(
        f"# Informe\n\n## Resumen\n\nLa CNE publicó exigencias para BESS. [{SOURCE} | {URL}]"
    )
    judge = FakeLLM(
        '{"approved":true,"score":95,"relevance":95,"coverage":90,'
        '"clarity":95,"traceability":100,"observations":["Trazable"]}'
    )
    filter_llm = FakeLLM('{"keep":[1]}')
    agent = RegulatoryAgent(
        settings=settings(),
        database=database,
        scraper=FakeScraper(),
        vector_engine=vector,
        callback=FakeCallback(),
        planner_llm=planner,
        filter_llm=filter_llm,
        executor_llm=executor,
        judge_llm=judge,
    )
    return agent, database, vector, [planner, filter_llm, executor, judge]


def test_graph_has_fixed_planner_executor_sequence() -> None:
    agent, _, _, _ = make_agent()
    graph = agent.graph.get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert ("__start__", "planner") in edges
    assert ("planner", "scraper") in edges
    assert ("scraper", "executor") in edges
    assert ("executor", "evaluator") in edges
    assert ("evaluator", "__end__") in edges
    assert len(edges) == 5


def test_daily_report_routes_models_and_persists_cited_output() -> None:
    agent, database, vector, llms = make_agent()
    result = agent.run_daily_report(user_id=None, keywords=["BESS"])

    assert result["judge"]["approved"] is True
    assert f"[{SOURCE} | {URL}]" in result["report"]
    assert [step["name"] for step in database.steps] == [
        "planner",
        "scraper",
        "executor",
        "evaluator",
    ]
    assert [step["model"] for step in database.steps] == [
        "gpt-5.6-luna",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    ]
    assert database.execution_status == "completed"
    assert database.report and database.report["content"] == result["report"]
    assert len(vector.indexed) == 1
    assert [len(llm.calls) for llm in llms] == [1, 1, 1, 1]


def test_codex_unavailable_path_reaches_judge_and_fails_closed() -> None:
    database = FakeDatabase()
    agent = RegulatoryAgent(
        settings=settings(),
        database=database,
        scraper=FakeScraper(),
        vector_engine=FakeVectorEngine(),
        callback=FakeCallback(),
        executor_llm=FailingLLM("Executor no disponible"),
        judge_llm=FailingLLM("Judge no disponible"),
    )

    with pytest.raises(ReportQualityError, match="rechazado"):
        agent.run_daily_report(keywords=["BESS"])

    assert [step["name"] for step in database.steps][-1] == "evaluator"
    assert database.execution_status == "rejected"
    assert database.report is None
    judge = database.execution_finish["metadata"]["judge"]
    assert judge["approved"] is False
    assert judge["deterministic_valid"] is True
    assert judge["mode"] == "deterministic_fallback"


def test_real_repository_persists_downloadable_artifacts(tmp_path: Path) -> None:
    configured = Settings(
        _env_file=None,
        database_path=tmp_path / "centinela.db",
        chroma_path=tmp_path / "chroma",
        reports_path=tmp_path / "reports",
        app_env="test",
    )
    database = Database(settings=configured)
    user_id = database.create_user("analista", "clave-segura-123")
    agent = RegulatoryAgent(
        settings=configured,
        database=database,
        scraper=FakeScraper(),
        vector_engine=FakeVectorEngine(),
        executor_llm=FakeLLM(
            f"# Informe\n\n## Resumen\n\nLa CNE publicó exigencias para BESS. "
            f"[{SOURCE} | {URL}]"
        ),
        judge_llm=FakeLLM(
            '{"approved":true,"score":95,"relevance":95,"coverage":90,'
            '"clarity":95,"traceability":100,"observations":["Trazable"]}'
        ),
    )

    result = agent.run_daily_report(user_id=user_id, keywords=["BESS"])

    markdown = Path(result["artifacts"]["markdown"])
    json_file = Path(result["artifacts"]["json"])
    assert markdown.read_text(encoding="utf-8") == result["report"]
    assert json_file.is_file()
    assert database.get_execution(result["execution_id"])["status"] == "completed"


def test_rejected_judge_is_not_persisted_or_marked_completed() -> None:
    agent, database, _, llms = make_agent()
    rejected = FakeLLM(
        '{"approved":false,"score":55,"relevance":70,"coverage":40,'
        '"clarity":75,"traceability":80,"observations":["Cobertura insuficiente"]}'
    )
    agent._judge_llm = rejected

    with pytest.raises(ReportQualityError, match="rechazado"):
        agent.run_daily_report(keywords=["BESS"])

    assert database.execution_status == "rejected"
    assert database.report is None
    assert database.execution_finish["metadata"]["quality_status"] == "rejected"
    assert len(rejected.calls) == 2
    assert len(llms[2].calls) == 1


def test_judge_catalogue_always_contains_every_cited_document() -> None:
    database = FakeDatabase()
    judge = FakeLLM(
        '{"approved":true,"score":95,"relevance":95,"coverage":90,'
        '"clarity":95,"traceability":100,"observations":["Trazable"]}'
    )
    agent = RegulatoryAgent(
        settings=settings(),
        database=database,
        scraper=FakeScraper(),
        vector_engine=FakeVectorEngine(),
        callback=FakeCallback(),
        judge_llm=judge,
    )
    documents = [
        {
            "title": f"Evidencia {index}",
            "summary": f"Resumen oficial {index}",
            "content": f"Contenido oficial {index}",
            "source": SOURCE,
            "url": f"https://www.cne.cl/evidencia/{index}",
            "published_at": "2026-08-13",
            "topics": ["BESS"],
        }
        for index in range(1, 21)
    ]
    last = documents[-1]
    report = (
        "# Informe\n\n## Resumen ejecutivo\n\n"
        f"La evidencia veinte es prioritaria. [{SOURCE} | {last['url']}]"
    )
    baseline = {
        "approved": True,
        "score": 100.0,
        "relevance": 100.0,
        "coverage": 100.0,
        "clarity": 100.0,
        "traceability": 100.0,
        "deterministic_valid": True,
        "missing_citation_lines": [],
        "unknown_citations": [],
        "observations": [],
        "model": "gpt-5.6-terra",
        "mode": "deterministic_fallback",
    }

    agent._judge_with_llm(judge, report, documents, baseline, mode="llm")  # type: ignore[arg-type]

    assert last["url"] in judge.calls[0]


def test_fresh_snapshot_avoids_repeating_live_scrape() -> None:
    class ScrapeMustNotRun:
        def fetch_all(self, **kwargs: Any) -> list[dict[str, Any]]:
            raise AssertionError("no debe repetirse una captura fresca")

    source_names = {
        "cen": "Coordinador Eléctrico Nacional (CEN)",
        "cne": "Comisión Nacional de Energía (CNE)",
        "minenergia": "Ministerio de Energía de Chile",
        "sec": "Superintendencia de Electricidad y Combustibles (SEC)",
        "sea": "Servicio de Evaluación Ambiental (SEA)",
        "senado": "Senado de la República de Chile",
        "camara": "Cámara de Diputadas y Diputados de Chile",
    }
    database = FakeDatabase()
    retrieved = datetime.now(timezone.utc).isoformat()
    database.news = [
        {
            "title": f"Publicación {key}",
            "summary": "Evidencia oficial",
            "content": "Evidencia oficial",
            "source": source_names[key],
            "url": f"https://{key}.example.cl/noticia",
            "source_url": f"https://{key}.example.cl/",
            "published_at": "2026-08-13",
            "fetched_at": retrieved,
            "topics": [],
        }
        for key in MANDATORY_SOURCES
    ]
    agent = RegulatoryAgent(
        settings=settings(),
        database=database,
        scraper=ScrapeMustNotRun(),
        vector_engine=FakeVectorEngine(),
        callback=FakeCallback(),
    )

    documents, capture = agent._scrape(
        {"sources": list(MANDATORY_SOURCES), "max_items_per_source": 1}
    )

    assert len(documents) == len(MANDATORY_SOURCES)
    assert capture["mode"] == "snapshot"
    assert capture["live_sources"] == []


def test_scraper_node_applies_plan_lookback_to_report_evidence() -> None:
    class DatedScraper:
        last_errors: dict[str, str] = {}

        def fetch_all(self, **kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    **FakeScraper().scrape_all()[0],
                    "title": "Dentro de ventana",
                    "url": "https://www.cne.cl/normativa/reciente",
                    "published_at": "2026-08-12",
                },
                {
                    **FakeScraper().scrape_all()[0],
                    "title": "Fuera de ventana",
                    "url": "https://www.cne.cl/normativa/antigua",
                    "published_at": "2026-07-01",
                },
            ]

    database = FakeDatabase()
    agent = RegulatoryAgent(
        settings=settings(),
        database=database,
        scraper=DatedScraper(),
        vector_engine=FakeVectorEngine(),
        callback=FakeCallback(),
    )
    state = {
        "execution_id": "lookback-test",
        "report_date": "2026-08-13",
        "plan": {
            "sources": ["cne"],
            "max_items_per_source": 5,
            "lookback_days": 7,
            "keywords": [],
        },
        "keywords": [],
        "alert_rules": [],
        "errors": [],
    }

    result = agent.scraper_node(state)  # type: ignore[arg-type]

    assert [item["title"] for item in result["filtered_documents"]] == [
        "Dentro de ventana"
    ]
    assert result["capture_stats"]["documents_outside_window"] == 1
