import json
from pathlib import Path

import pytest

from app import (
    _ask_rag_observed,
    _index_local_news,
    _refresh_sources,
    _run_daily_report,
    build_report_exports,
)
from core.config import PROJECT_ROOT, Settings
from core.demo import (
    DemoReadOnlyError,
    demo_rag_messages,
    demo_report_payload,
    ensure_demo_dataset,
    get_demo_repository,
    load_demo_bundle,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="staging",
        public_demo_mode=True,
        database_path=tmp_path / "must-not-exist" / "centinela.db",
        chroma_path=tmp_path / "chroma",
        reports_path=tmp_path / "reports",
        codex_workdir=tmp_path / "codex-work",
    )


def test_demo_bundle_hashes_and_payload_provenance_are_verified() -> None:
    bundle = load_demo_bundle()
    report = demo_report_payload()
    messages = demo_rag_messages()

    assert bundle["manifest"]["schema_version"] == 1
    assert report["evaluation"]["approved"] is True
    assert report["artifact_kind"] == "acceptance_artifact_replay"
    assert report["evidence_replay"] is True
    assert report["execution_id"] == "76dbc034-963f-4c01-9af0-cb7dfe064d6a"
    assert report["origin_sha256"].startswith("sha256:")
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert "https://" in messages[1]["content"]
    assert len(messages[1]["sources"]) == 2
    assert all("excerpt" not in source for source in messages[1]["sources"])


def test_demo_repository_is_static_truthful_and_read_only() -> None:
    repository = get_demo_repository()
    news = repository.list_news(limit=100)
    snapshot = repository.acceptance_snapshot

    assert len(news) == 34
    assert len({item["source"] for item in news}) == 6
    assert not any("Coordinador Eléctrico" in item["source"] for item in news)
    assert all(item["published_at"] is None for item in news)
    assert all(item["fetched_at"] is None for item in news)
    assert all(item["is_fallback"] is None for item in news)
    assert all(item["metadata"]["capture_method"] == "unknown" for item in news)
    assert snapshot["publications_in_dashboard"] == 53
    assert snapshot["sources_recovered"] == 7
    assert snapshot["citation_catalog_entries"] == 34
    assert load_demo_bundle()["manifest"]["ui_simulation"]["evidence_replay"] is False
    alerts = repository.list_alerts(0)
    assert len(alerts) == 1
    assert alerts[0]["artifact_kind"] == "ui_simulation"
    assert alerts[0]["metadata"]["evidence_replay"] is False

    with pytest.raises(DemoReadOnlyError):
        repository.create_alert(0, "No", ["BESS"])
    with pytest.raises(DemoReadOnlyError):
        ensure_demo_dataset(repository, "session")


def test_demo_telemetry_matches_captured_trace_exactly() -> None:
    repository = get_demo_repository()
    executions = repository.list_executions(limit=20, user_id=0)
    assert len(executions) == 2

    report = repository.get_execution("76dbc034-963f-4c01-9af0-cb7dfe064d6a")
    assert report is not None
    assert report["prompt_tokens"] == 35_203
    assert report["completion_tokens"] == 3_702
    assert report["latency_seconds"] == pytest.approx(77.85753036900041)
    assert [
        (call["prompt_tokens"], call["completion_tokens"], call["latency_seconds"])
        for call in report["llm_calls"]
    ] == [
        (17_191, 3_144, 62.7314069869999),
        (18_012, 558, 14.405822715000795),
    ]
    assert report["metadata"]["judge"]["approved"] is True

    rag = repository.get_execution("221ed6a0-7819-4d12-9fd7-b668ae576967")
    assert rag is not None
    assert rag["latency_seconds"] == pytest.approx(8.808233837999978)
    assert rag["llm_calls"][0]["latency_seconds"] == pytest.approx(
        8.441093170000386
    )


def test_demo_exports_remain_labelled_outside_the_ui() -> None:
    markdown, raw_json, safe_date, execution_id = build_report_exports(
        demo_report_payload()
    )
    exported = json.loads(raw_json)

    assert markdown.startswith(
        b"<!-- artifact_kind: acceptance_artifact_replay -->"
    )
    assert b"no es una ejecuci\xc3\xb3n nueva" in markdown
    assert exported["artifact_kind"] == "acceptance_artifact_replay"
    assert exported["evidence_replay"] is True
    assert exported["validated_at"] == "2026-08-13"
    assert exported["origin_sha256"].startswith("sha256:")
    assert exported["metrics"]["billing_mode"] == "subscription"
    assert exported["metrics"]["cost_attribution"] == "not_attributable"
    assert b"coste por llamada no atribuible" in markdown
    assert safe_date == "2026-08-13"
    assert execution_id == "76dbc034-963f-4c01-9af0-cb7dfe064d6a"


def test_demo_action_entrypoints_are_blocked_before_side_effects(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    repository = get_demo_repository()
    user = {"id": 0}

    with pytest.raises(DemoReadOnlyError):
        _refresh_sources(settings, repository)
    with pytest.raises(DemoReadOnlyError):
        _run_daily_report(settings, user, repository)
    with pytest.raises(DemoReadOnlyError):
        _ask_rag_observed(settings, repository, user, "consulta")
    with pytest.raises(DemoReadOnlyError):
        _index_local_news(object(), repository)


def test_demo_screenshots_are_real_png_files() -> None:
    screenshots = sorted((PROJECT_ROOT / "docs" / "demo" / "screenshots").glob("*.png"))
    assert len(screenshots) == 9
    for screenshot in screenshots:
        assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), screenshot.name
