from pathlib import Path

from streamlit.testing.v1 import AppTest

from core.config import PROJECT_ROOT, get_settings


def test_public_demo_renders_every_view_without_sqlite(
    monkeypatch, tmp_path: Path
) -> None:
    database_path = tmp_path / "runtime" / "must-not-exist.db"
    environment = {
        "PUBLIC_DEMO_MODE": "true",
        "APP_ENV": "staging",
        "DEFAULT_ADMIN_USERNAME": "",
        "DEFAULT_ADMIN_PASSWORD": "",
        "OPENAI_API_KEY": "",
        "OLLAMA_API_KEY": "",
        "VLLM_API_KEY": "",
        "DATABASE_PATH": str(database_path),
        "CHROMA_PATH": str(tmp_path / "chroma"),
        "REPORTS_PATH": str(tmp_path / "reports"),
        "CODEX_WORKDIR": str(tmp_path / "codex-work"),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    runtime_paths = (
        database_path.parent,
        Path(environment["CHROMA_PATH"]),
        Path(environment["REPORTS_PATH"]),
        Path(environment["CODEX_WORKDIR"]),
    )
    assert not any(path.exists() for path in runtime_paths)

    app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=30)
    app.run()
    assert not app.exception
    assert [title.value for title in app.title] == ["Radar regulatorio"]

    expected = {
        "Informe diario": "Informe regulatorio diario",
        "Alertas": "Alertas personalizadas",
        "Chat RAG": "Chat RAG",
        "Observabilidad": "Observabilidad y tokenomics",
        "Arquitectura": "Arquitectura y controles",
    }
    for page, title in expected.items():
        app.radio[0].set_value(page).run()
        assert not app.exception
        assert [item.value for item in app.title] == [title]

    assert not database_path.exists()
    assert not any(path.exists() for path in runtime_paths)
    get_settings.cache_clear()
