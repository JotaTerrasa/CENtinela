from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from core.config import Settings
from core.database import Database


def make_database(tmp_path: Path) -> Database:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "centinela.sqlite3",
        chroma_path=tmp_path / "chroma",
        reports_path=tmp_path / "reports",
        app_env="test",
        password_pbkdf2_iterations=100_000,
    )
    return Database(settings=settings)


def test_users_alerts_news_reports_and_previous_memory(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    user_id = database.create_user(
        "arquitecta",
        "una-clave-segura",
        email="ai@example.com",
        is_admin=True,
    )

    assert database.authenticate_user("arquitecta", "incorrecta") is None
    authenticated = database.authenticate_user("ARQUITECTA", "una-clave-segura")
    assert authenticated is not None
    assert authenticated["id"] == user_id
    assert authenticated["is_admin"] is True
    assert "password_hash" not in authenticated

    news_id = database.upsert_news(
        {
            "source": "CNE",
            "title": "Nueva norma BESS",
            "url": "https://example.cl/norma-bess",
            "summary": "Regulacion de almacenamiento",
            "published_at": "2026-08-12T10:00:00-04:00",
            "keywords": ["BESS", "almacenamiento"],
        }
    )
    assert news_id > 0
    assert database.list_news(query="almacenamiento")[0]["keywords"] == [
        "BESS",
        "almacenamiento",
    ]

    alert_id = database.create_alert(user_id, "Baterias", ["BESS", "BESS"])
    assert database.list_alerts(user_id)[0]["keywords"] == ["BESS"]
    assert database.update_alert(alert_id, user_id, enabled=False)
    assert database.list_alerts(user_id, enabled_only=True) == []

    execution_id = database.start_execution("daily_report", user_id=user_id)
    step_id = database.start_step(execution_id, "planner", model="gpt-4o-mini")
    database.record_llm_call(
        {
            "id": "call-1",
            "execution_id": execution_id,
            "step_id": step_id,
            "model": "gpt-4o-mini",
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cost_usd": 0.000027,
            "cost_clp": 0.02538,
            "latency_seconds": 0.2,
        }
    )
    # Mismo id no duplica los agregados.
    database.record_llm_call(
        {
            "id": "call-1",
            "execution_id": execution_id,
            "step_id": step_id,
            "model": "gpt-4o-mini",
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cost_usd": 0.000027,
            "cost_clp": 0.02538,
            "latency_seconds": 0.2,
        }
    )
    database.finish_step(step_id)
    database.finish_execution(execution_id)
    execution = database.get_execution(execution_id)
    assert execution is not None
    assert execution["prompt_tokens"] == 100
    assert execution["completion_tokens"] == 20
    assert len(execution["llm_calls"]) == 1
    assert database.list_executions(user_id=user_id)[0]["id"] == execution_id
    other_user = database.create_user("otra", "otra-clave-segura")
    assert database.list_executions(user_id=other_user) == []

    report_id = database.save_report(
        date(2026, 8, 13),
        "Informe diario",
        "Cambio regulatorio [CNE | https://example.cl/norma-bess]",
        execution_id=execution_id,
        user_id=user_id,
        citations=[{"source": "CNE", "url": "https://example.cl/norma-bess"}],
    )
    assert database.get_report(report_id)["citations"][0]["source"] == "CNE"

    database.save_daily_memory(date(2026, 8, 12), "Ayer se publico la norma")
    previous = database.get_previous_day_memory(date(2026, 8, 13))
    assert previous is not None
    assert previous["content"] == "Ayer se publico la norma"


def test_database_supports_concurrent_streamlit_workers(tmp_path: Path) -> None:
    database = make_database(tmp_path)

    def save(index: int) -> int:
        return database.upsert_news(
            {
                "source": "CEN",
                "title": f"Noticia {index}",
                "url": f"https://example.cl/news/{index}",
            }
        )

    with ThreadPoolExecutor(max_workers=8) as workers:
        identifiers = list(workers.map(save, range(24)))

    assert len(set(identifiers)) == 24
    assert len(database.list_news(limit=100)) == 24
