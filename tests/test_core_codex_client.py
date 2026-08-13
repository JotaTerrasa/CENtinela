from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from core.codex_client import (
    CODEX_PERMISSION_PROFILE,
    CodexClient,
    CodexExecutionError,
    CodexOutputError,
    CodexTimeoutError,
)
from core.config import Settings
from core.observability import CostTrackingCallback


def completed(stdout: str, *, stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["codex"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def jsonl(*events: dict[str, Any]) -> str:
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


def test_text_response_uses_safe_noninteractive_flags_and_extracts_usage(
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_runner(command: list[str], **kwargs: Any):
        captured.update(command=command, kwargs=kwargs)
        return completed(
            jsonl(
                {"type": "thread.started", "thread_id": "thread-123"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1",
                        "type": "agent_message",
                        "text": "Informe regulatorio listo.",
                    },
                },
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 120,
                        "cached_input_tokens": 20,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 4,
                    },
                },
            )
        )

    ticks = iter((10.0, 10.75))
    client = CodexClient(
        executable="codex-test",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=45,
        workdir=tmp_path,
        runner=fake_runner,
        clock=lambda: next(ticks),
    )
    result = client.invoke_text("Resume estas normas")

    assert result.text == result.content == "Informe regulatorio listo."
    assert result.data is None
    assert result.thread_id == "thread-123"
    assert result.model == "gpt-5.6-terra"
    assert result.latency_seconds == pytest.approx(0.75)
    assert result.cost_usd == result.cost_clp == 0
    assert result.metadata["billing_mode"] == "subscription"
    assert result.metadata["cost_attribution"] == "not_attributable"
    assert result.usage is not None
    assert result.usage.prompt_tokens == result.usage.input_tokens == 120
    assert result.usage.completion_tokens == result.usage.output_tokens == 30
    assert result.usage.cached_prompt_tokens == 20
    assert result.usage.reasoning_tokens == 4
    assert result.usage.total_tokens == 150

    command = captured["command"]
    assert command[:2] == ["codex-test", "exec"]
    assert "--json" in command
    assert "--ephemeral" in command
    assert "--strict-config" in command
    assert "--sandbox" not in command
    assert "--skip-git-repo-check" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--model") + 1] == "gpt-5.6-terra"
    configs = [
        command[index + 1]
        for index, argument in enumerate(command)
        if argument == "--config"
    ]
    assert f'default_permissions="{CODEX_PERMISSION_PROFILE}"' in configs
    filesystem = next(
        value for value in configs if ".filesystem=" in value
    )
    assert '":minimal"="read"' in filesystem
    assert '"/app"="deny"' in filesystem
    assert f'{json.dumps(str(client.codex_home))}="deny"' in filesystem
    assert f'{json.dumps(str(tmp_path.resolve()))}="read"' in filesystem
    assert f"permissions.{CODEX_PERMISSION_PROFILE}.network.enabled=false" in configs
    assert 'forced_login_method="chatgpt"' in configs
    assert 'approval_policy="never"' in configs
    assert 'shell_environment_policy.inherit="none"' in configs
    assert "shell_environment_policy.ignore_default_excludes=false" in configs
    assert (
        'shell_environment_policy.set={"PATH"="/usr/local/bin:/usr/bin:/bin"}'
        in configs
    )
    assert 'model_reasoning_effort="medium"' in configs
    assert command[-1] == "-"
    assert captured["kwargs"]["input"] == "Resume estas normas"
    assert captured["kwargs"]["timeout"] == 45
    assert captured["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert captured["kwargs"]["check"] is False
    assert captured["kwargs"]["shell"] is False


def test_structured_response_passes_temporary_schema_and_parses_json() -> None:
    schema = {
        "type": "object",
        "properties": {"approved": {"type": "boolean"}},
        "required": ["approved"],
        "additionalProperties": False,
    }
    captured_schema_path: Path | None = None

    def fake_runner(command: list[str], **kwargs: Any):
        nonlocal captured_schema_path
        captured_schema_path = Path(command[command.index("--output-schema") + 1])
        assert captured_schema_path.is_file()
        assert json.loads(captured_schema_path.read_text(encoding="utf-8")) == schema
        return completed(
            jsonl(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": '{"approved":true}',
                    },
                },
                {"type": "turn.completed", "usage": {"input_tokens": 5}},
            )
        )

    result = CodexClient(runner=fake_runner).invoke_json(
        "Evalua el informe",
        schema,
        model="gpt-5.6-sol",
        reasoning_effort="high",
    )

    assert result.data == result.structured_output == {"approved": True}
    assert result.text == '{"approved":true}'
    assert captured_schema_path is not None
    assert not captured_schema_path.exists()


def test_usage_is_optional_when_cli_does_not_report_it() -> None:
    spawned_cwd: Path | None = None

    def fake_runner(command: list[str], **kwargs: Any):
        nonlocal spawned_cwd
        spawned_cwd = Path(kwargs["cwd"])
        assert spawned_cwd.is_dir()
        return completed(
            jsonl(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Respuesta"},
                }
            )
        )

    result = CodexClient(runner=fake_runner).invoke("Pregunta")

    assert result.text == "Respuesta"
    assert result.usage is None
    assert spawned_cwd is not None
    assert not spawned_cwd.exists()


def test_langchain_config_callback_receives_exact_usage_with_subscription_cost(
    tmp_path: Path,
) -> None:
    def fake_runner(command: list[str], **kwargs: Any):
        return completed(
            jsonl(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Respuesta"},
                },
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 321, "output_tokens": 45},
                },
            )
        )

    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "metrics.db",
        chroma_path=tmp_path / "chroma",
        reports_path=tmp_path / "reports",
        app_env="test",
    )
    callback = CostTrackingCallback(model="codex-subscription", settings=settings)
    client = CodexClient(model="gpt-5.6-luna", runner=fake_runner)

    client.invoke(
        "Pregunta",
        config={"callbacks": [callback], "metadata": {"step": "planner"}},
    )

    assert callback.prompt_tokens == 321
    assert callback.completion_tokens == 45
    assert callback.cost_usd == callback.cost_clp == 0
    assert callback.calls[0]["model"] == "codex-subscription/gpt-5.6-luna"
    assert callback.calls[0]["metadata"] == {
        "step": "planner",
        "provider": "codex_cli",
        "billing_mode": "subscription",
        "cost_attribution": "not_attributable",
        "attributable_cost_usd": 0.0,
        "auth_method": "chatgpt",
        "permission_profile": CODEX_PERMISSION_PROFILE,
        "requested_model": "gpt-5.6-luna",
        "pricing_status": "not_applicable",
        "cost_status": "included_not_attributable",
    }


def test_nonzero_exit_uses_event_error_and_redacts_secrets() -> None:
    def fake_runner(command: list[str], **kwargs: Any):
        return completed(
            jsonl(
                {
                    "type": "turn.failed",
                    "error": {
                        "message": "token=test-secret-123 prompt='dato privado'"
                    },
                }
            ),
            stderr="Authorization: Bearer abc123",
            returncode=7,
        )

    with pytest.raises(CodexExecutionError) as raised:
        CodexClient(runner=fake_runner).invoke("Este prompt tampoco debe filtrarse")

    message = str(raised.value)
    assert raised.value.returncode == 7
    assert "codigo 7" in message
    assert "test-secret-123" not in message
    assert "abc123" not in message
    assert "dato privado" not in message
    assert "Este prompt" not in message
    assert "[REDACTED" in message


def test_timeout_is_specific_and_sanitizes_stderr() -> None:
    def fake_runner(command: list[str], **kwargs: Any):
        raise subprocess.TimeoutExpired(
            command,
            timeout=kwargs["timeout"],
            stderr="api_key=timeout-canary",
        )

    with pytest.raises(CodexTimeoutError) as raised:
        CodexClient(runner=fake_runner, timeout_seconds=2.5).invoke("Pregunta")

    assert "2.5s" in str(raised.value)
    assert "timeout-canary" not in str(raised.value)
    assert "[REDACTED" in str(raised.value)


def test_missing_binary_has_a_clear_error() -> None:
    def fake_runner(command: list[str], **kwargs: Any):
        raise FileNotFoundError("codex no existe")

    with pytest.raises(CodexExecutionError, match="No se encontro el ejecutable"):
        CodexClient(executable="codex-ausente", runner=fake_runner).invoke("Pregunta")


@pytest.mark.parametrize(
    "stdout",
    [
        jsonl({"type": "turn.completed", "usage": {"input_tokens": 2}}),
        jsonl(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "no es JSON"},
            }
        ),
    ],
)
def test_missing_or_invalid_structured_output_is_rejected(stdout: str) -> None:
    def fake_runner(command: list[str], **kwargs: Any):
        return completed(stdout)

    schema = {"type": "object"}
    with pytest.raises(CodexOutputError):
        CodexClient(runner=fake_runner).invoke_json("Pregunta", schema)


def test_input_validation_happens_before_spawning_process() -> None:
    called = False

    def fake_runner(command: list[str], **kwargs: Any):
        nonlocal called
        called = True
        return completed("")

    client = CodexClient(runner=fake_runner)
    with pytest.raises(ValueError, match="prompt"):
        client.invoke("   ")
    with pytest.raises(ValueError, match="timeout_seconds"):
        client.invoke("Pregunta", timeout_seconds=0)
    with pytest.raises(ValueError, match="caracteres no permitidos"):
        client.invoke("Pregunta", reasoning_effort="high\nmalicioso")
    assert called is False


def test_codex_workdir_rejects_broad_or_credential_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "auth-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(ValueError, match="subdirectorio dedicado"):
        CodexClient(workdir=Path(__file__).resolve().parents[1])
    with pytest.raises(ValueError, match="dentro de CODEX_HOME"):
        CodexClient(workdir=codex_home / "work")
