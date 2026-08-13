"""Adaptador seguro para ejecutar Codex CLI de forma no interactiva.

El cliente delega autenticacion por completo en el ejecutable ``codex``. No
lee, copia ni expone archivos de credenciales: la sesion que ya administra el
CLI es la unica fuente de autenticacion. Todas las ejecuciones son efimeras,
usan un perfil de permisos minimo y reciben el prompt mediante ``stdin``.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from langchain_core.outputs import LLMResult

from .config import PROJECT_ROOT, resolve_codex_executable
from .observability import sanitize_error


JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]
CODEX_PERMISSION_PROFILE = "centinela_runtime"
MINIMAL_SHELL_PATH = "/usr/local/bin:/usr/bin:/bin"


class CodexClientError(RuntimeError):
    """Error base del adaptador Codex con mensajes aptos para observabilidad."""


class CodexExecutionError(CodexClientError):
    """El proceso Codex termino con un codigo distinto de cero."""

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class CodexTimeoutError(CodexClientError):
    """La ejecucion supero el limite de tiempo configurado."""


class CodexOutputError(CodexClientError):
    """Codex termino, pero no produjo una respuesta consumible."""


@dataclass(frozen=True, slots=True)
class CodexUsage:
    """Uso reportado por ``turn.completed`` en el flujo JSONL de Codex.

    Los nombres ``prompt_tokens`` y ``completion_tokens`` mantienen
    compatibilidad con la observabilidad existente en CENtinela. Los alias
    ``input_tokens`` y ``output_tokens`` reflejan la terminologia del CLI.
    """

    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def input_tokens(self) -> int:
        return self.prompt_tokens

    @property
    def output_tokens(self) -> int:
        return self.completion_tokens

    @property
    def cached_input_tokens(self) -> int:
        return self.cached_prompt_tokens

    @property
    def reasoning_output_tokens(self) -> int:
        return self.reasoning_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, int]:
        values = asdict(self)
        values["total_tokens"] = self.total_tokens
        return values


@dataclass(frozen=True, slots=True)
class CodexResult:
    """Respuesta normalizada de una invocacion al CLI."""

    text: str
    data: JsonValue | None
    usage: CodexUsage | None
    model: str | None
    thread_id: str | None
    latency_seconds: float
    metadata: Mapping[str, Any]

    @property
    def cost_usd(self) -> float:
        """Coste API atribuible; siempre cero para la suscripcion Codex."""

        return 0.0

    @property
    def cost_clp(self) -> float:
        return 0.0

    @property
    def content(self) -> str:
        """Alias compatible con consumidores que esperan ``message.content``."""

        return self.text

    @property
    def structured_output(self) -> JsonValue | None:
        return self.data


def _positive_timeout(value: float) -> float:
    timeout = float(value)
    if timeout <= 0:
        raise ValueError("timeout_seconds debe ser positivo")
    return timeout


def _optional_cli_value(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} no puede estar vacio")
    if "\x00" in normalized or "\n" in normalized or "\r" in normalized:
        raise ValueError(f"{name} contiene caracteres no permitidos")
    return normalized


def _toml_inline_table(values: Mapping[str, str]) -> str:
    """Serializa un mapa plano como tabla inline TOML sin interpolar claves."""

    pairs = (
        f"{json.dumps(str(key), ensure_ascii=False)}="
        f"{json.dumps(str(value), ensure_ascii=False)}"
        for key, value in values.items()
    )
    return "{" + ",".join(pairs) + "}"


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    raw_path = Path(configured) if configured else Path.home() / ".codex"
    return raw_path.expanduser().resolve()


def _validate_runtime_workdir(workdir: Path, *, codex_home: Path) -> Path:
    resolved = workdir.expanduser().resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError("CODEX_WORKDIR no puede ser la raiz del sistema")
    if resolved in {Path("/app"), PROJECT_ROOT.resolve()}:
        raise ValueError("CODEX_WORKDIR debe ser un subdirectorio dedicado")
    if resolved == codex_home or codex_home in resolved.parents:
        raise ValueError("CODEX_WORKDIR no puede estar dentro de CODEX_HOME")
    return resolved


def _security_config_overrides(
    *,
    workdir: Path,
    codex_home: Path,
) -> list[str]:
    """Configuracion inline fail-closed para cada ``codex exec``.

    El orden importa: una ruta mas especifica puede reabrir el directorio de
    trabajo dentro del ``/app`` denegado, tal como define el modelo de perfiles
    de Codex. Ninguna ruta recibe permisos de escritura.
    """

    filesystem: dict[str, str] = {":minimal": "read"}
    for protected in (Path("/app"), PROJECT_ROOT.resolve(), codex_home):
        filesystem[str(protected)] = "deny"
    filesystem[str(workdir)] = "read"
    profile = CODEX_PERMISSION_PROFILE
    return [
        f"default_permissions={json.dumps(profile)}",
        (
            f"permissions.{profile}.description="
            f"{json.dumps('CENtinela read-only runtime')}"
        ),
        f"permissions.{profile}.filesystem={_toml_inline_table(filesystem)}",
        f"permissions.{profile}.network.enabled=false",
        'approval_policy="never"',
        'shell_environment_policy.inherit="none"',
        "shell_environment_policy.ignore_default_excludes=false",
        (
            "shell_environment_policy.set="
            f"{_toml_inline_table({'PATH': MINIMAL_SHELL_PATH})}"
        ),
        'forced_login_method="chatgpt"',
    ]


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def _parse_jsonl(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    plain_lines: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            plain_lines.append(line)
            continue
        if isinstance(parsed, dict) and "type" in parsed:
            events.append(parsed)
        else:
            plain_lines.append(line)
    return events, plain_lines


def _message_text(item: Mapping[str, Any]) -> str | None:
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    content = item.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        fragments: list[str] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_text = block.get("text")
            if isinstance(block_text, str) and block_text.strip():
                fragments.append(block_text.strip())
        if fragments:
            return "\n".join(fragments)
    return None


def _final_message(events: Sequence[Mapping[str, Any]]) -> str | None:
    messages: list[str] = []
    for event in events:
        if event.get("type") not in {"item.completed", "item.updated"}:
            continue
        item = event.get("item")
        if not isinstance(item, Mapping) or item.get("type") != "agent_message":
            continue
        text = _message_text(item)
        if text:
            messages.append(text)
    return messages[-1] if messages else None


def _extract_usage(events: Sequence[Mapping[str, Any]]) -> CodexUsage | None:
    for event in reversed(events):
        usage = event.get("usage")
        if event.get("type") != "turn.completed" or not isinstance(usage, Mapping):
            continue
        return CodexUsage(
            prompt_tokens=_nonnegative_int(
                usage.get("input_tokens", usage.get("prompt_tokens"))
            ),
            completion_tokens=_nonnegative_int(
                usage.get("output_tokens", usage.get("completion_tokens"))
            ),
            cached_prompt_tokens=_nonnegative_int(
                usage.get("cached_input_tokens", usage.get("cached_prompt_tokens"))
            ),
            reasoning_tokens=_nonnegative_int(
                usage.get(
                    "reasoning_output_tokens",
                    usage.get("reasoning_tokens"),
                )
            ),
        )
    return None


def _extract_thread_id(events: Sequence[Mapping[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return None


def _event_error(events: Sequence[Mapping[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") not in {"error", "turn.failed"}:
            continue
        error = event.get("error")
        if isinstance(error, Mapping):
            for key in ("message", "detail", "code"):
                if error.get(key):
                    return str(error[key])
        if isinstance(error, str) and error:
            return error
        if event.get("message"):
            return str(event["message"])
    return None


def _safe_error_detail(value: BaseException | str | None) -> str:
    if value is None or not str(value).strip():
        return "sin detalle disponible"
    return sanitize_error(value, max_length=500)


def _configured_callbacks(config: Mapping[str, Any] | None) -> list[Any]:
    if not config:
        return []
    configured = config.get("callbacks")
    if configured is None:
        return []
    if isinstance(configured, Sequence) and not isinstance(configured, (str, bytes)):
        return list(configured)
    return [configured]


def _callback_metadata(
    config: Mapping[str, Any] | None,
    *,
    requested_model: str | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if config and isinstance(config.get("metadata"), Mapping):
        metadata.update(dict(config["metadata"]))
    metadata.update(
        {
            "provider": "codex_cli",
            "billing_mode": "subscription",
            "cost_attribution": "not_attributable",
            "attributable_cost_usd": 0.0,
            "auth_method": "chatgpt",
            "permission_profile": CODEX_PERMISSION_PROFILE,
        }
    )
    if requested_model:
        metadata["requested_model"] = requested_model
    return metadata


def _accounting_model(requested_model: str | None) -> str:
    # El prefijo evita aplicar accidentalmente tarifas API a una sesion Codex.
    return f"codex-subscription/{requested_model or 'default'}"


def _notify_start(
    callbacks: Sequence[Any],
    *,
    prompt: str,
    run_id: uuid.UUID,
    model: str,
    metadata: Mapping[str, Any],
) -> None:
    for callback in callbacks:
        handler = getattr(callback, "on_llm_start", None)
        if not callable(handler):
            continue
        try:
            handler(
                {"name": "CodexCLI"},
                [prompt],
                run_id=run_id,
                invocation_params={"model": model},
                metadata=dict(metadata),
            )
        except Exception:
            # La telemetria no debe convertir una respuesta valida en un fallo.
            continue


def _notify_end(
    callbacks: Sequence[Any],
    *,
    run_id: uuid.UUID,
    model: str,
    usage: CodexUsage | None,
) -> None:
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0
    response = LLMResult(
        generations=[],
        llm_output={
            "model_name": model,
            "token_usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    )
    for callback in callbacks:
        handler = getattr(callback, "on_llm_end", None)
        if not callable(handler):
            continue
        try:
            handler(response, run_id=run_id)
        except Exception:
            continue


def _notify_error(
    callbacks: Sequence[Any],
    *,
    run_id: uuid.UUID,
    error: BaseException,
) -> None:
    for callback in callbacks:
        handler = getattr(callback, "on_llm_error", None)
        if not callable(handler):
            continue
        try:
            handler(error, run_id=run_id)
        except Exception:
            continue


class CodexClient:
    """Cliente sin estado para texto y JSON estructurado mediante ``codex exec``."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: float = 180.0,
        workdir: str | Path | None = None,
        runner: SubprocessRunner = subprocess.run,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        configured_executable = _optional_cli_value(executable, name="executable") or "codex"
        self.executable = resolve_codex_executable(configured_executable)
        self.model = _optional_cli_value(model, name="model")
        self.reasoning_effort = _optional_cli_value(
            reasoning_effort,
            name="reasoning_effort",
        )
        self.timeout_seconds = _positive_timeout(timeout_seconds)
        self.codex_home = _default_codex_home()
        self.workdir = (
            _validate_runtime_workdir(Path(workdir), codex_home=self.codex_home)
            if workdir
            else None
        )
        self._runner = runner
        self._clock = clock

    def _command(
        self,
        *,
        model: str | None,
        reasoning_effort: str | None,
        schema_path: Path | None,
        workdir: Path,
    ) -> list[str]:
        command = [
            self.executable,
            "exec",
            "--json",
            "--ephemeral",
            "--strict-config",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--color",
            "never",
        ]
        for override in _security_config_overrides(
            workdir=workdir,
            codex_home=self.codex_home,
        ):
            command.extend(("--config", override))
        if model:
            command.extend(("--model", model))
        if reasoning_effort:
            # JSON string syntax is also valid TOML and prevents config injection.
            command.extend(
                ("--config", f"model_reasoning_effort={json.dumps(reasoning_effort)}")
            )
        if schema_path is not None:
            command.extend(("--output-schema", str(schema_path)))
        command.append("-")
        return command

    def invoke(
        self,
        prompt: str,
        config: Mapping[str, Any] | None = None,
        *,
        output_schema: Mapping[str, Any] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CodexResult:
        """Ejecuta un turno y devuelve texto, datos estructurados y uso.

        ``output_schema`` activa ``--output-schema`` y hace que ``data``
        contenga el JSON deserializado. El prompt nunca forma parte de los
        argumentos del proceso ni de los mensajes de error del adaptador.
        """

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt debe ser un texto no vacio")

        effective_model = (
            self.model
            if model is None
            else _optional_cli_value(model, name="model")
        )
        effective_reasoning = (
            self.reasoning_effort
            if reasoning_effort is None
            else _optional_cli_value(reasoning_effort, name="reasoning_effort")
        )
        effective_timeout = (
            self.timeout_seconds
            if timeout_seconds is None
            else _positive_timeout(timeout_seconds)
        )

        if output_schema is not None:
            try:
                serialized_schema = json.dumps(
                    dict(output_schema),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("output_schema debe ser serializable como JSON") from exc
        else:
            serialized_schema = None

        callbacks = _configured_callbacks(config)
        run_id = uuid.uuid4()
        callback_model = _accounting_model(effective_model)
        _notify_start(
            callbacks,
            prompt=prompt,
            run_id=run_id,
            model=callback_model,
            metadata=_callback_metadata(config, requested_model=effective_model),
        )

        try:
            with tempfile.TemporaryDirectory(prefix="centinela-codex-") as temp_dir:
                schema_path: Path | None = None
                if serialized_schema is not None:
                    schema_path = Path(temp_dir) / "output-schema.json"
                    schema_path.write_text(serialized_schema, encoding="utf-8")

                execution_workdir = self.workdir or Path(temp_dir).resolve()
                command = self._command(
                    model=effective_model,
                    reasoning_effort=effective_reasoning,
                    schema_path=schema_path,
                    workdir=execution_workdir,
                )
                started = self._clock()
                process = self._runner(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=effective_timeout,
                    # Un directorio temporal vacio evita que el agente pueda
                    # explorar accidentalmente el repositorio anfitrion.
                    cwd=str(execution_workdir),
                    shell=False,
                )
                latency = max(self._clock() - started, 0.0)

            stdout = process.stdout or ""
            stderr = process.stderr or ""
            events, plain_lines = _parse_jsonl(stdout)

            if process.returncode != 0:
                detail = _event_error(events) or stderr
                raise CodexExecutionError(
                    "Codex termino con error "
                    f"(codigo {process.returncode}): {_safe_error_detail(detail)}",
                    returncode=process.returncode,
                )

            final_text = _final_message(events)
            if final_text is None and plain_lines:
                # Compatibilidad con versiones que impriman solo el mensaje final.
                final_text = "\n".join(plain_lines).strip()
            if not final_text:
                raise CodexOutputError("Codex termino sin producir un mensaje final")

            structured: JsonValue | None = None
            if output_schema is not None:
                try:
                    structured = json.loads(final_text)
                except json.JSONDecodeError:
                    raise CodexOutputError(
                        "Codex produjo una respuesta que no es JSON valido"
                    ) from None
        except subprocess.TimeoutExpired as exc:
            detail = _safe_error_detail(exc.stderr)
            error = CodexTimeoutError(
                f"Codex excedio el timeout de {effective_timeout:g}s ({detail})"
            )
            _notify_error(callbacks, run_id=run_id, error=error)
            raise error from None
        except FileNotFoundError:
            error = CodexExecutionError(
                f"No se encontro el ejecutable Codex: {self.executable!r}"
            )
            _notify_error(callbacks, run_id=run_id, error=error)
            raise error from None
        except OSError as exc:
            error = CodexExecutionError(
                f"No se pudo iniciar Codex: {_safe_error_detail(exc)}"
            )
            _notify_error(callbacks, run_id=run_id, error=error)
            raise error from None
        except CodexClientError as error:
            _notify_error(callbacks, run_id=run_id, error=error)
            raise

        usage = _extract_usage(events)
        _notify_end(
            callbacks,
            run_id=run_id,
            model=callback_model,
            usage=usage,
        )
        return CodexResult(
            text=final_text,
            data=structured,
            usage=usage,
            model=effective_model,
            thread_id=_extract_thread_id(events),
            latency_seconds=latency,
            metadata=_callback_metadata(config, requested_model=effective_model),
        )

    def invoke_text(self, prompt: str, **kwargs: Any) -> CodexResult:
        """Atajo explicito para una respuesta de texto."""

        if "output_schema" in kwargs:
            raise TypeError("invoke_text no acepta output_schema")
        return self.invoke(prompt, **kwargs)

    def invoke_json(
        self,
        prompt: str,
        output_schema: Mapping[str, Any],
        **kwargs: Any,
    ) -> CodexResult:
        """Atajo para una respuesta JSON validada por Codex contra un schema."""

        return self.invoke(prompt, output_schema=output_schema, **kwargs)


__all__ = [
    "CodexClient",
    "CodexClientError",
    "CodexExecutionError",
    "CodexOutputError",
    "CodexResult",
    "CodexTimeoutError",
    "CodexUsage",
    "CODEX_PERMISSION_PROFILE",
]
