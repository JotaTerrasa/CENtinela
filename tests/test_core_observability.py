from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.outputs import LLMResult

from core.config import Settings
from core.database import Database
from core.observability import CostTrackingCallback, calculate_cost, sanitize_error


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp_path / "metrics.sqlite3",
        chroma_path=tmp_path / "chroma",
        reports_path=tmp_path / "reports",
        app_env="test",
    )


def test_exact_cost_formula_and_codex_subscription_models() -> None:
    custom = {"test-model": {"input_per_million": 2.5, "output_per_million": 10.0}}
    api_usd, api_clp = calculate_cost(
        1_000_000, 1_000_000, "test-model", pricing=custom
    )
    codex_usd, codex_clp = calculate_cost(
        1_000_000, 1_000_000, "gpt-5.6-sol"
    )

    assert api_usd == pytest.approx(12.5)
    assert api_clp == pytest.approx(11_750.0)
    assert codex_usd == codex_clp == 0


def test_langchain_callback_extracts_and_persists_exact_tokens(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings=settings)
    execution_id = database.start_execution("daily_report")
    step_id = database.start_step(execution_id, "planner", model="gpt-5.6-luna")
    callback = CostTrackingCallback(
        database=database,
        execution_id=execution_id,
        step_id=step_id,
        model="gpt-5.6-luna",
        settings=settings,
    )
    run_id = uuid4()
    callback.on_llm_start(
        {"name": "CodexCLI"},
        ["Analiza la regulacion"],
        run_id=run_id,
        invocation_params={"model": "gpt-5.6-luna"},
    )
    callback.on_llm_end(
        LLMResult(
            generations=[],
            llm_output={
                "model_name": "gpt-5.6-luna",
                "token_usage": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 250,
                    "total_tokens": 1_250,
                },
            },
        ),
        run_id=run_id,
    )

    assert callback.prompt_tokens == 1_000
    assert callback.completion_tokens == 250
    assert callback.total_tokens == 1_250
    assert callback.cost_usd == 0
    assert callback.cost_clp == 0
    assert callback.latency_seconds >= 0

    execution = database.get_execution(execution_id)
    assert execution is not None
    assert execution["prompt_tokens"] == 1_000
    assert execution["completion_tokens"] == 250
    assert execution["llm_calls"][0]["cost_clp"] == 0


def test_manual_usage_accumulates_without_rounding(tmp_path: Path) -> None:
    callback = CostTrackingCallback(
        model="gpt-5.6-sol",
        settings=make_settings(tmp_path),
    )
    callback.record_usage(10, 5, latency_seconds=0.4)
    callback.record_usage(20, 7, latency_seconds=0.6)

    assert callback.prompt_tokens == 30
    assert callback.completion_tokens == 12
    assert callback.call_count == 2
    assert callback.latency_seconds == pytest.approx(1.0)
    assert callback.snapshot()["total_tokens"] == 42


def test_error_sanitizer_redacts_secrets_and_prompt_payloads() -> None:
    error = RuntimeError(
        'Authorization: Bearer abc123 prompt="contenido confidencial" '
        "https://api.example.test?api_key=visible"
    )
    safe = sanitize_error(error)
    assert "abc123" not in safe
    assert "contenido confidencial" not in safe
    assert "api_key=visible" not in safe
    assert "[REDACTED" in safe


def test_unknown_provider_alias_does_not_break_a_successful_call(tmp_path: Path) -> None:
    callback = CostTrackingCallback(model="gpt-5.6-luna", settings=make_settings(tmp_path))
    usage = callback.record_usage(12, 3, model="provider-alias-sin-pricing")

    assert usage.prompt_tokens == 12
    assert usage.completion_tokens == 3
    assert usage.cost_usd == 0
    assert callback.calls[0]["metadata"]["pricing_status"] == "unknown"
