from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import Settings, business_today, resolve_codex_executable


def test_settings_prepare_paths_and_hide_secrets(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db" / "centinela.sqlite3",
        chroma_path=tmp_path / "vectors",
        reports_path=tmp_path / "reports",
        codex_workdir=tmp_path / "codex-work",
        app_env="test",
        log_level="debug",
    )

    assert settings.database_path.parent.is_dir()
    assert settings.chroma_path.is_dir()
    assert settings.reports_path.is_dir()
    assert settings.codex_workdir.is_dir()
    assert settings.log_level == "DEBUG"
    assert settings.planning_model == "gpt-5.6-luna"
    assert settings.final_model == "gpt-5.6-sol"
    assert settings.rag_top_k == 5
    assert settings.price_for_model("gpt-5.6-luna").input_per_million == 0
    assert settings.embedding_model == "local-hash-1536"
    assert "default_admin_password" not in settings.public_dict()


def test_admin_credentials_must_be_configured_together(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_path=tmp_path / "db.sqlite3",
            chroma_path=tmp_path / "chroma",
            reports_path=tmp_path / "reports",
            default_admin_username="admin",
        )


def test_empty_optional_admin_credentials_are_treated_as_unset(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db.sqlite3",
        chroma_path=tmp_path / "chroma",
        reports_path=tmp_path / "reports",
        default_admin_username="",
        default_admin_password="",
    )
    assert settings.default_admin_username is None
    assert settings.default_admin_password is None


def test_business_date_uses_santiago_and_subscription_models_are_portable(
    tmp_path: Path,
) -> None:
    instant = datetime(2026, 8, 14, 1, 30, tzinfo=timezone.utc)
    assert business_today("America/Santiago", now=instant).isoformat() == "2026-08-13"

    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db.sqlite3",
        chroma_path=tmp_path / "chroma",
        reports_path=tmp_path / "reports",
        planner_model="modelo-codex-del-workspace",
    )
    assert settings.planner_model == "modelo-codex-del-workspace"
    with pytest.raises(KeyError, match="No existe pricing"):
        settings.price_for_model(settings.planner_model)

    with pytest.raises(ValidationError, match="940 CLP"):
        Settings(
            _env_file=None,
            database_path=tmp_path / "db-2.sqlite3",
            chroma_path=tmp_path / "chroma-2",
            reports_path=tmp_path / "reports-2",
            usd_to_clp=950,
        )


def test_codex_executable_uses_bundled_fallback_only_for_default_name(
    tmp_path: Path,
) -> None:
    bundled = tmp_path / "codex"
    bundled.write_text("#!/bin/sh\n", encoding="utf-8")
    bundled.chmod(0o700)

    resolved = resolve_codex_executable(
        "codex",
        executable_finder=lambda value: None,
        bundled_resolver=lambda: bundled,
    )

    assert resolved == str(bundled.resolve())
    assert (
        resolve_codex_executable(
            "codex-corporativo",
            executable_finder=lambda value: None,
            bundled_resolver=lambda: bundled,
        )
        == "codex-corporativo"
    )


def test_codex_executable_prefers_path_over_bundled(tmp_path: Path) -> None:
    discovered = tmp_path / "global" / "codex"
    resolved = resolve_codex_executable(
        "codex",
        executable_finder=lambda value: str(discovered),
        bundled_resolver=lambda: tmp_path / "bundled" / "codex",
    )
    assert resolved == str(discovered.resolve())
