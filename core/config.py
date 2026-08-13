"""Configuracion tipada y centralizada de CENtinela."""

from __future__ import annotations

import os
import shutil
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUSINESS_TIMEZONE = "America/Santiago"


def resolve_codex_executable(
    configured: str = "codex",
    *,
    executable_finder: Callable[[str], str | None] = shutil.which,
    bundled_resolver: Callable[[], str | os.PathLike[str]] | None = None,
) -> str:
    """Resuelve el CLI oficial y usa el binario empaquetado como fallback.

    Una ruta configurada de forma explicita nunca se sustituye silenciosamente
    por otro ejecutable. El fallback de ``openai-codex`` solo se activa para el
    valor portable ``codex`` cuando no existe en ``PATH``.
    """

    normalized = str(configured).strip()
    if not normalized:
        raise ValueError("CODEX_CLI_PATH no puede estar vacio")
    if any(character in normalized for character in ("\x00", "\n", "\r")):
        raise ValueError("CODEX_CLI_PATH contiene caracteres no permitidos")

    discovered = executable_finder(normalized)
    if discovered:
        return str(Path(discovered).expanduser().resolve())
    if normalized != "codex":
        return normalized

    try:
        if bundled_resolver is None:
            from codex_cli_bin import bundled_codex_path

            bundled_resolver = bundled_codex_path
        bundled = Path(bundled_resolver()).expanduser().resolve()
    except (ImportError, ModuleNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        return normalized
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    return normalized


def business_today(
    timezone_name: str = DEFAULT_BUSINESS_TIMEZONE,
    *,
    now: datetime | None = None,
) -> date:
    """Fecha civil del negocio; los timestamps de auditoria siguen en UTC."""

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(ZoneInfo(timezone_name)).date()


class ModelPricing(BaseModel):
    """Precio en USD por un millon de tokens de entrada y salida."""

    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)


DEFAULT_MODEL_PRICING: dict[str, dict[str, float]] = {
    # Codex autenticado con ChatGPT consume la cuota de la suscripcion. El CLI
    # informa tokens, pero no atribuye un precio API por llamada. Mantener cero
    # evita presentar como exacto un coste USD/CLP que no existe en este modo.
    "gpt-5.6-luna": {"input_per_million": 0.0, "output_per_million": 0.0},
    "gpt-5.6-terra": {"input_per_million": 0.0, "output_per_million": 0.0},
    "gpt-5.6-sol": {"input_per_million": 0.0, "output_per_million": 0.0},
}


class Settings(BaseSettings):
    """Configuracion de aplicacion obtenida de entorno o archivo ``.env``.

    Los secretos usan :class:`SecretStr`, por lo que no aparecen en ``repr`` ni
    en los volcados publicos. Las rutas relativas y ``.env`` se resuelven desde
    la raiz del proyecto para que el resultado no dependa del directorio desde
    el que se invoque Streamlit.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    database_path: Path = Path("data/centinela.db")
    chroma_path: Path = Path("data/chroma")
    reports_path: Path = Path("reports")
    codex_workdir: Path = Path("data/codex-work")

    app_name: str = "CENtinela"
    app_env: Literal["development", "test", "staging", "production"] = (
        "development"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    business_timezone: str = DEFAULT_BUSINESS_TIMEZONE

    scraper_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    scraper_max_articles: int = Field(default=8, gt=0, le=1000)
    rag_top_k: int = Field(default=5, gt=0, le=20)
    codex_cli_path: str = "codex"
    codex_timeout_seconds: float = Field(default=240.0, gt=0, le=900)

    usd_to_clp: float = Field(default=940.0, gt=0)
    planner_model: str = "gpt-5.6-luna"
    filter_model: str = "gpt-5.6-luna"
    judge_model: str = "gpt-5.6-terra"
    report_model: str = "gpt-5.6-sol"
    embedding_model: str = "local-hash-1536"
    planner_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    filter_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    judge_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    report_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    model_pricing: dict[str, ModelPricing] = Field(
        default_factory=lambda: {
            name: ModelPricing(**values)
            for name, values in DEFAULT_MODEL_PRICING.items()
        }
    )

    default_admin_username: str | None = None
    default_admin_password: SecretStr | None = None
    password_pbkdf2_iterations: int = Field(default=600_000, ge=100_000)

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("business_timezone")
    @classmethod
    def validate_business_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Zona horaria IANA desconocida: {normalized!r}") from exc
        return normalized

    @field_validator("usd_to_clp")
    @classmethod
    def enforce_contractual_exchange_rate(cls, value: float) -> float:
        if float(value) != 940.0:
            raise ValueError("USD_TO_CLP es fijo para la prueba: 1 USD = 940 CLP")
        return 940.0

    @field_validator("codex_cli_path", mode="before")
    @classmethod
    def resolve_configured_codex_cli(cls, value: Any) -> str:
        return resolve_codex_executable(str(value or "codex"))

    @field_validator(
        "planner_model",
        "filter_model",
        "judge_model",
        "report_model",
        mode="before",
    )
    @classmethod
    def validate_model_name(cls, value: Any) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("El nombre de modelo Codex no puede estar vacio")
        if any(character in normalized for character in ("\x00", "\n", "\r")):
            raise ValueError("El nombre de modelo contiene caracteres no permitidos")
        return normalized

    @field_validator("default_admin_username", mode="before")
    @classmethod
    def normalize_optional_username(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("default_admin_password", mode="before")
    @classmethod
    def normalize_optional_secret(cls, value: Any) -> Any:
        if value is None:
            return None
        revealed = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        return revealed.strip() or None

    @model_validator(mode="after")
    def validate_admin_and_prepare_paths(self) -> "Settings":
        has_username = self.default_admin_username is not None
        has_password = self.default_admin_password is not None
        if has_username != has_password:
            raise ValueError(
                "DEFAULT_ADMIN_USERNAME y DEFAULT_ADMIN_PASSWORD deben definirse juntos"
            )
        if self.app_env == "production" and has_username:
            raise ValueError(
                "Las credenciales bootstrap no se permiten con APP_ENV=production"
            )

        self.database_path = self._absolute_path(self.database_path)
        self.chroma_path = self._absolute_path(self.chroma_path)
        self.reports_path = self._absolute_path(self.reports_path)
        self.codex_workdir = self._absolute_path(self.codex_workdir)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.chroma_path.mkdir(parents=True, exist_ok=True)
        self.reports_path.mkdir(parents=True, exist_ok=True)
        self.codex_workdir.mkdir(parents=True, exist_ok=True)
        return self

    @staticmethod
    def _absolute_path(path: Path) -> Path:
        expanded = path.expanduser()
        return expanded if expanded.is_absolute() else (PROJECT_ROOT / expanded).resolve()

    @property
    def planning_model(self) -> str:
        """Alias semantico usado por algunos consumidores del grafo."""

        return self.planner_model

    @property
    def final_model(self) -> str:
        """Alias semantico para el modelo de redaccion del informe."""

        return self.report_model

    def price_for_model(self, model_name: str) -> ModelPricing:
        """Devuelve pricing exacto o por prefijo para modelos versionados."""

        if model_name in self.model_pricing:
            return self.model_pricing[model_name]
        matches = [name for name in self.model_pricing if model_name.startswith(name)]
        if matches:
            return self.model_pricing[max(matches, key=len)]
        raise KeyError(f"No existe pricing configurado para el modelo {model_name!r}")

    def public_dict(self) -> dict[str, Any]:
        """Configuracion serializable sin claves ni contrasenas."""

        values = self.model_dump(exclude={"default_admin_password"}, mode="json")
        values["secrets_configured"] = {
            "default_admin_password": bool(
                self.default_admin_password
                and self.default_admin_password.get_secret_value()
            ),
        }
        return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retorna una unica instancia validada por proceso."""

    return Settings()


__all__ = [
    "DEFAULT_BUSINESS_TIMEZONE",
    "DEFAULT_MODEL_PRICING",
    "ModelPricing",
    "PROJECT_ROOT",
    "Settings",
    "business_today",
    "get_settings",
    "resolve_codex_executable",
]
