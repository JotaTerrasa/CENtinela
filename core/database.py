"""Persistencia SQLite thread-safe para CENtinela.

La clase :class:`Database` abre una conexion corta por transaccion. Este patron
evita compartir cursores entre los hilos que Streamlit crea al atender sesiones
y, combinado con WAL y ``busy_timeout``, permite lectores concurrentes y
escrituras serializadas por SQLite.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

from .config import Settings, business_today, get_settings


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _as_iso(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat(timespec="milliseconds")
    return value.isoformat() if isinstance(value, date) else str(value)


class Database:
    """Repositorio SQLite de usuarios, noticias, alertas y trazas de agentes."""

    _schema_lock = threading.RLock()

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        settings: Settings | None = None,
        initialize: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.path = Path(path or self.settings.database_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        if initialize:
            self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Entrega una conexion y garantiza commit/rollback y cierre."""

        connection = self._connect()
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Crea el esquema idempotente y, si procede, el administrador inicial."""

        with self._schema_lock, self.connection(write=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    email TEXT COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    pbkdf2_iterations INTEGER NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );

                CREATE TABLE IF NOT EXISTS news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    summary TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    category TEXT,
                    published_at TEXT,
                    fetched_at TEXT NOT NULL,
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    content_hash TEXT NOT NULL,
                    is_fallback INTEGER NOT NULL DEFAULT 0 CHECK (is_fallback IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS idx_news_published_at
                    ON news(published_at DESC);
                CREATE INDEX IF NOT EXISTS idx_news_source ON news(source);

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id, enabled);

                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    workflow TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    cost_clp REAL NOT NULL DEFAULT 0,
                    latency_seconds REAL NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_executions_started
                    ON executions(started_at DESC);

                CREATE TABLE IF NOT EXISTS execution_steps (
                    id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
                    step_name TEXT NOT NULL,
                    model TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    cost_clp REAL NOT NULL DEFAULT 0,
                    latency_seconds REAL NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_steps_execution
                    ON execution_steps(execution_id, started_at);

                CREATE TABLE IF NOT EXISTS llm_calls (
                    id TEXT PRIMARY KEY,
                    execution_id TEXT REFERENCES executions(id) ON DELETE CASCADE,
                    step_id TEXT REFERENCES execution_steps(id) ON DELETE SET NULL,
                    run_id TEXT,
                    parent_run_id TEXT,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    cost_clp REAL NOT NULL,
                    latency_seconds REAL NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_llm_calls_execution
                    ON llm_calls(execution_id, started_at);

                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY,
                    execution_id TEXT REFERENCES executions(id) ON DELETE SET NULL,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    report_date TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reports_date
                    ON reports(report_date DESC, created_at DESC);

                CREATE TABLE IF NOT EXISTS daily_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_key TEXT NOT NULL,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    memory_date TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner_key, memory_date)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_owner_date
                    ON daily_memory(owner_key, memory_date DESC);
                """
            )

        username = self.settings.default_admin_username
        password = self.settings.default_admin_password
        if username and password and self.get_user_by_username(username) is None:
            try:
                self.create_user(
                    username,
                    password.get_secret_value(),
                    is_admin=True,
                )
            except sqlite3.IntegrityError:
                # Otro worker pudo crear el mismo administrador entre consulta e insert.
                if self.get_user_by_username(username) is None:
                    raise

    # -- Usuarios y autenticacion -------------------------------------------------
    def _password_digest(
        self,
        password: str,
        salt: bytes,
        iterations: int,
    ) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
        )

    def create_user(
        self,
        username: str,
        password: str,
        *,
        email: str | None = None,
        is_admin: bool = False,
        is_active: bool = True,
    ) -> int:
        username = username.strip()
        if len(username) < 3:
            raise ValueError("El nombre de usuario debe tener al menos 3 caracteres")
        if len(password) < 8:
            raise ValueError("La contrasena debe tener al menos 8 caracteres")
        normalized_email = email.strip() if email else None
        salt = secrets.token_bytes(16)
        iterations = self.settings.password_pbkdf2_iterations
        digest = self._password_digest(password, salt, iterations)
        now = _utc_now()
        with self.connection(write=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO users(
                    username, email, password_hash, password_salt,
                    pbkdf2_iterations, is_admin, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    normalized_email,
                    base64.b64encode(digest).decode("ascii"),
                    base64.b64encode(salt).decode("ascii"),
                    iterations,
                    int(is_admin),
                    int(is_active),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def authenticate_user(self, username: str, password: str) -> dict[str, Any] | None:
        """Verifica PBKDF2 en tiempo constante y devuelve el usuario sin secretos."""

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                (username.strip(),),
            ).fetchone()
        if row is None:
            # Reduce diferencias observables entre usuario ausente y clave erronea.
            self._password_digest(password, b"\0" * 16, self.settings.password_pbkdf2_iterations)
            return None
        expected = base64.b64decode(row["password_hash"])
        salt = base64.b64decode(row["password_salt"])
        actual = self._password_digest(password, salt, int(row["pbkdf2_iterations"]))
        if not bool(row["is_active"]) or not hmac.compare_digest(actual, expected):
            return None
        now = _utc_now()
        with self.connection(write=True) as connection:
            connection.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, row["id"]),
            )
        user = self._public_user(dict(row))
        user["last_login_at"] = now
        return user

    authenticate = authenticate_user

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._public_user(dict(row)) if row else None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                (username.strip(),),
            ).fetchone()
        return self._public_user(dict(row)) if row else None

    @staticmethod
    def _public_user(user: dict[str, Any]) -> dict[str, Any]:
        user.pop("password_hash", None)
        user.pop("password_salt", None)
        user.pop("pbkdf2_iterations", None)
        user["is_admin"] = bool(user.get("is_admin"))
        user["is_active"] = bool(user.get("is_active"))
        return user

    # -- Noticias ----------------------------------------------------------------
    def upsert_news(self, article: Mapping[str, Any]) -> int:
        title = str(article.get("title") or "").strip()
        url = str(article.get("url") or "").strip()
        source = str(article.get("source") or "").strip()
        if not title or not url or not source:
            raise ValueError("Una noticia requiere source, title y url")
        summary = str(article.get("summary") or article.get("description") or "")
        content = str(article.get("content") or "")
        content_hash = hashlib.sha256(
            f"{title}\n{summary}\n{content}".encode("utf-8")
        ).hexdigest()
        fetched_at = _as_iso(
            article.get("fetched_at") or article.get("retrieved_at")
        ) or _utc_now()
        keywords = article.get("keywords") or article.get("topics") or []
        metadata = dict(article.get("metadata") or {})
        for key in ("id", "source_url", "fallback_reason"):
            if article.get(key) is not None and key not in metadata:
                metadata[key] = article[key]
        with self.connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO news(
                    source, title, url, summary, content, category,
                    published_at, fetched_at, keywords_json, metadata_json,
                    content_hash, is_fallback
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    source = excluded.source,
                    title = excluded.title,
                    summary = excluded.summary,
                    content = excluded.content,
                    category = excluded.category,
                    published_at = excluded.published_at,
                    fetched_at = excluded.fetched_at,
                    keywords_json = excluded.keywords_json,
                    metadata_json = excluded.metadata_json,
                    content_hash = excluded.content_hash,
                    is_fallback = excluded.is_fallback
                """,
                (
                    source,
                    title,
                    url,
                    summary,
                    content,
                    article.get("category"),
                    _as_iso(article.get("published_at") or article.get("date")),
                    fetched_at,
                    _json_dump(list(keywords)),
                    _json_dump(metadata),
                    content_hash,
                    int(bool(article.get("is_fallback", False))),
                ),
            )
            row = connection.execute("SELECT id FROM news WHERE url = ?", (url,)).fetchone()
            return int(row["id"])

    def save_news(self, articles: Sequence[Mapping[str, Any]]) -> list[int]:
        return [self.upsert_news(article) for article in articles]

    def list_news(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        sources: Sequence[str] | None = None,
        query: str | None = None,
        since: date | datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 10_000 or offset < 0:
            raise ValueError("limit debe estar entre 1 y 10000 y offset no puede ser negativo")
        clauses: list[str] = []
        parameters: list[Any] = []
        if sources:
            placeholders = ",".join("?" for _ in sources)
            clauses.append(f"source IN ({placeholders})")
            parameters.extend(sources)
        if query:
            clauses.append("(title LIKE ? OR summary LIKE ? OR content LIKE ?)")
            like = f"%{query.strip()}%"
            parameters.extend((like, like, like))
        if since is not None:
            clauses.append("COALESCE(published_at, fetched_at) >= ?")
            parameters.append(_as_iso(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM news {where}
                ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [self._decode_news(dict(row)) for row in rows]

    get_news = list_news
    get_recent_news = list_news

    @staticmethod
    def _decode_news(row: dict[str, Any]) -> dict[str, Any]:
        row["keywords"] = _json_load(row.pop("keywords_json", None), [])
        row["metadata"] = _json_load(row.pop("metadata_json", None), {})
        row["is_fallback"] = bool(row.get("is_fallback"))
        return row

    # -- Alertas -----------------------------------------------------------------
    def create_alert(
        self,
        user_id: int,
        name: str,
        keywords: Sequence[str],
        *,
        sources: Sequence[str] | None = None,
        enabled: bool = True,
    ) -> int:
        normalized = sorted({word.strip() for word in keywords if word.strip()})
        if not name.strip() or not normalized:
            raise ValueError("Una alerta requiere nombre y al menos una palabra clave")
        now = _utc_now()
        with self.connection(write=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO alerts(
                    user_id, name, keywords_json, sources_json,
                    enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    name.strip(),
                    _json_dump(normalized),
                    _json_dump(sorted(set(sources or []))),
                    int(enabled),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def update_alert(
        self,
        alert_id: int,
        user_id: int,
        *,
        name: str | None = None,
        keywords: Sequence[str] | None = None,
        sources: Sequence[str] | None = None,
        enabled: bool | None = None,
    ) -> bool:
        assignments = ["updated_at = ?"]
        parameters: list[Any] = [_utc_now()]
        if name is not None:
            if not name.strip():
                raise ValueError("El nombre no puede estar vacio")
            assignments.append("name = ?")
            parameters.append(name.strip())
        if keywords is not None:
            normalized = sorted({word.strip() for word in keywords if word.strip()})
            if not normalized:
                raise ValueError("La alerta requiere al menos una palabra clave")
            assignments.append("keywords_json = ?")
            parameters.append(_json_dump(normalized))
        if sources is not None:
            assignments.append("sources_json = ?")
            parameters.append(_json_dump(sorted(set(sources))))
        if enabled is not None:
            assignments.append("enabled = ?")
            parameters.append(int(enabled))
        parameters.extend((alert_id, user_id))
        with self.connection(write=True) as connection:
            cursor = connection.execute(
                f"UPDATE alerts SET {', '.join(assignments)} WHERE id = ? AND user_id = ?",
                parameters,
            )
            return cursor.rowcount == 1

    def delete_alert(self, alert_id: int, user_id: int) -> bool:
        with self.connection(write=True) as connection:
            cursor = connection.execute(
                "DELETE FROM alerts WHERE id = ? AND user_id = ?",
                (alert_id, user_id),
            )
            return cursor.rowcount == 1

    def list_alerts(self, user_id: int, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        condition = " AND enabled = 1" if enabled_only else ""
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM alerts WHERE user_id = ?{condition} ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            row["keywords"] = _json_load(row.pop("keywords_json", None), [])
            row["sources"] = _json_load(row.pop("sources_json", None), [])
            row["enabled"] = bool(row["enabled"])
            result.append(row)
        return result

    get_alerts = list_alerts

    # -- Ejecuciones, pasos y llamadas LLM ---------------------------------------
    def start_execution(
        self,
        workflow: str = "daily_report",
        *,
        user_id: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        execution_id: str | None = None,
    ) -> str:
        identifier = execution_id or str(uuid.uuid4())
        with self.connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO executions(
                    id, user_id, workflow, status, started_at, metadata_json
                ) VALUES (?, ?, ?, 'running', ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (identifier, user_id, workflow, _utc_now(), _json_dump(dict(metadata or {}))),
            )
        return identifier

    create_execution = start_execution

    def finish_execution(
        self,
        execution_id: str,
        *,
        status: str = "completed",
        error: str | None = None,
        latency_seconds: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        assignments = ["status = ?", "finished_at = ?", "error = ?"]
        parameters: list[Any] = [status, _utc_now(), error]
        if latency_seconds is not None:
            assignments.append("latency_seconds = ?")
            parameters.append(max(0.0, float(latency_seconds)))
        if metadata is not None:
            assignments.append("metadata_json = ?")
            parameters.append(_json_dump(dict(metadata)))
        parameters.append(execution_id)
        with self.connection(write=True) as connection:
            cursor = connection.execute(
                f"UPDATE executions SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )
            return cursor.rowcount == 1

    update_execution = finish_execution

    def start_step(
        self,
        execution_id: str,
        step_name: str,
        *,
        model: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        step_id: str | None = None,
    ) -> str:
        identifier = step_id or str(uuid.uuid4())
        with self.connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO execution_steps(
                    id, execution_id, step_name, model, status,
                    started_at, metadata_json
                ) VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    identifier,
                    execution_id,
                    step_name,
                    model,
                    _utc_now(),
                    _json_dump(dict(metadata or {})),
                ),
            )
        return identifier

    create_execution_step = start_step

    def finish_step(
        self,
        step_id: str,
        *,
        status: str = "completed",
        error: str | None = None,
        latency_seconds: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        assignments = ["status = ?", "finished_at = ?", "error = ?"]
        parameters: list[Any] = [status, _utc_now(), error]
        if latency_seconds is not None:
            assignments.append("latency_seconds = ?")
            parameters.append(max(0.0, float(latency_seconds)))
        if metadata is not None:
            assignments.append("metadata_json = ?")
            parameters.append(_json_dump(dict(metadata)))
        parameters.append(step_id)
        with self.connection(write=True) as connection:
            cursor = connection.execute(
                f"UPDATE execution_steps SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )
            return cursor.rowcount == 1

    update_execution_step = finish_step

    def record_llm_call(self, call: Mapping[str, Any]) -> str:
        """Persiste una llamada y suma sus metricas una sola vez a padres."""

        identifier = str(call.get("id") or uuid.uuid4())
        execution_id = str(call["execution_id"]) if call.get("execution_id") else None
        step_id = str(call["step_id"]) if call.get("step_id") else None
        prompt_tokens = int(call.get("prompt_tokens", 0))
        completion_tokens = int(call.get("completion_tokens", 0))
        cost_usd = float(call.get("cost_usd", 0.0))
        cost_clp = float(call.get("cost_clp", 0.0))
        latency = max(0.0, float(call.get("latency_seconds", 0.0)))
        with self.connection(write=True) as connection:
            if execution_id:
                connection.execute(
                    """
                    INSERT INTO executions(id, workflow, status, started_at)
                    VALUES (?, 'llm', 'running', ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (execution_id, _utc_now()),
                )
            cursor = connection.execute(
                """
                INSERT INTO llm_calls(
                    id, execution_id, step_id, run_id, parent_run_id, model,
                    status, started_at, finished_at, prompt_tokens,
                    completion_tokens, cost_usd, cost_clp, latency_seconds,
                    metadata_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    identifier,
                    execution_id,
                    step_id,
                    str(call["run_id"]) if call.get("run_id") else None,
                    str(call["parent_run_id"]) if call.get("parent_run_id") else None,
                    str(call.get("model") or "unknown"),
                    str(call.get("status") or "completed"),
                    _as_iso(call.get("started_at")) or _utc_now(),
                    _as_iso(call.get("finished_at")) or _utc_now(),
                    prompt_tokens,
                    completion_tokens,
                    cost_usd,
                    cost_clp,
                    latency,
                    _json_dump(dict(call.get("metadata") or {})),
                    call.get("error"),
                ),
            )
            if cursor.rowcount == 1 and execution_id:
                connection.execute(
                    """
                    UPDATE executions SET
                        prompt_tokens = prompt_tokens + ?,
                        completion_tokens = completion_tokens + ?,
                        cost_usd = cost_usd + ?,
                        cost_clp = cost_clp + ?,
                        latency_seconds = latency_seconds + ?
                    WHERE id = ?
                    """,
                    (prompt_tokens, completion_tokens, cost_usd, cost_clp, latency, execution_id),
                )
            if cursor.rowcount == 1 and step_id:
                connection.execute(
                    """
                    UPDATE execution_steps SET
                        prompt_tokens = prompt_tokens + ?,
                        completion_tokens = completion_tokens + ?,
                        cost_usd = cost_usd + ?,
                        cost_clp = cost_clp + ?,
                        latency_seconds = latency_seconds + ?
                    WHERE id = ?
                    """,
                    (prompt_tokens, completion_tokens, cost_usd, cost_clp, latency, step_id),
                )
        return identifier

    def get_execution(
        self,
        execution_id: str,
        *,
        include_details: bool = True,
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM executions WHERE id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                return None
            result = self._decode_metadata(dict(row))
            if include_details:
                steps = connection.execute(
                    "SELECT * FROM execution_steps WHERE execution_id = ? ORDER BY started_at",
                    (execution_id,),
                ).fetchall()
                calls = connection.execute(
                    "SELECT * FROM llm_calls WHERE execution_id = ? ORDER BY started_at",
                    (execution_id,),
                ).fetchall()
                result["steps"] = [self._decode_metadata(dict(item)) for item in steps]
                result["llm_calls"] = [self._decode_metadata(dict(item)) for item in calls]
        return result

    def list_executions(
        self,
        *,
        limit: int = 50,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE user_id = ?" if user_id is not None else ""
        parameters: tuple[Any, ...] = (
            (user_id, max(1, min(int(limit), 1000)))
            if user_id is not None
            else (max(1, min(int(limit), 1000)),)
        )
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM executions {where} ORDER BY started_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._decode_metadata(dict(row)) for row in rows]

    def get_observability_summary(self, *, execution_id: str | None = None) -> dict[str, Any]:
        where = "WHERE id = ?" if execution_id else ""
        parameters: tuple[Any, ...] = (execution_id,) if execution_id else ()
        with self.connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS executions,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd,
                       COALESCE(SUM(cost_clp), 0) AS cost_clp,
                       COALESCE(SUM(latency_seconds), 0) AS latency_seconds
                FROM executions {where}
                """,
                parameters,
            ).fetchone()
        result = dict(row)
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
        return result

    get_metrics = get_observability_summary

    # -- Informes y memoria diaria -----------------------------------------------
    def save_report(
        self,
        report_date: date | str,
        title: str,
        content: str,
        *,
        execution_id: str | None = None,
        user_id: int | None = None,
        citations: Sequence[Mapping[str, Any] | str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        report_id: str | None = None,
    ) -> str:
        identifier = report_id or str(uuid.uuid4())
        with self.connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO reports(
                    id, execution_id, user_id, report_date, title, content,
                    citations_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    execution_id,
                    user_id,
                    _as_iso(report_date),
                    title.strip(),
                    content,
                    _json_dump(list(citations or [])),
                    _json_dump(dict(metadata or {})),
                    _utc_now(),
                ),
            )
        return identifier

    def list_reports(
        self,
        *,
        limit: int = 30,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE user_id = ?" if user_id is not None else ""
        parameters: tuple[Any, ...] = (
            (user_id, max(1, min(limit, 1000)))
            if user_id is not None
            else (max(1, min(limit, 1000)),)
        )
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM reports {where} ORDER BY report_date DESC, created_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._decode_report(dict(row)) for row in rows]

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return self._decode_report(dict(row)) if row else None

    @staticmethod
    def _decode_report(row: dict[str, Any]) -> dict[str, Any]:
        row["citations"] = _json_load(row.pop("citations_json", None), [])
        row["metadata"] = _json_load(row.pop("metadata_json", None), {})
        return row

    def save_daily_memory(
        self,
        memory_date: date | str,
        content: str,
        *,
        user_id: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        owner_key = f"user:{user_id}" if user_id is not None else "global"
        now = _utc_now()
        with self.connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO daily_memory(
                    owner_key, user_id, memory_date, content,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_key, memory_date) DO UPDATE SET
                    content = excluded.content,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_key,
                    user_id,
                    _as_iso(memory_date),
                    content,
                    _json_dump(dict(metadata or {})),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM daily_memory WHERE owner_key = ? AND memory_date = ?",
                (owner_key, _as_iso(memory_date)),
            ).fetchone()
            return int(row["id"])

    save_memory = save_daily_memory

    def get_daily_memory(
        self,
        memory_date: date | str,
        *,
        user_id: int | None = None,
    ) -> dict[str, Any] | None:
        owner_key = f"user:{user_id}" if user_id is not None else "global"
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM daily_memory WHERE owner_key = ? AND memory_date = ?",
                (owner_key, _as_iso(memory_date)),
            ).fetchone()
        return self._decode_metadata(dict(row)) if row else None

    def get_previous_day_memory(
        self,
        reference_date: date | datetime | str | None = None,
        *,
        user_id: int | None = None,
        latest_fallback: bool = False,
    ) -> dict[str, Any] | None:
        if reference_date is None:
            current = business_today(self.settings.business_timezone)
        elif isinstance(reference_date, datetime):
            if reference_date.tzinfo is None:
                reference_date = reference_date.replace(
                    tzinfo=ZoneInfo(self.settings.business_timezone)
                )
            current = reference_date.astimezone(
                ZoneInfo(self.settings.business_timezone)
            ).date()
        elif isinstance(reference_date, date):
            current = reference_date
        else:
            current = date.fromisoformat(str(reference_date)[:10])
        previous = current - timedelta(days=1)
        result = self.get_daily_memory(previous, user_id=user_id)
        if result is not None or not latest_fallback:
            return result
        owner_key = f"user:{user_id}" if user_id is not None else "global"
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM daily_memory
                WHERE owner_key = ? AND memory_date < ?
                ORDER BY memory_date DESC LIMIT 1
                """,
                (owner_key, current.isoformat()),
            ).fetchone()
        return self._decode_metadata(dict(row)) if row else None

    get_previous_memory = get_previous_day_memory

    @staticmethod
    def _decode_metadata(row: dict[str, Any]) -> dict[str, Any]:
        row["metadata"] = _json_load(row.pop("metadata_json", None), {})
        return row


DatabaseManager = Database


def init_db(
    path: str | Path | None = None,
    *,
    settings: Settings | None = None,
) -> Database:
    """Crea/actualiza el esquema y devuelve el repositorio."""

    return Database(path, settings=settings, initialize=True)


_database_instances: dict[Path, Database] = {}
_database_instances_lock = threading.Lock()


def get_database(
    path: str | Path | None = None,
    *,
    settings: Settings | None = None,
) -> Database:
    """Devuelve una instancia compartida por ruta (sus conexiones no se comparten)."""

    resolved_settings = settings or get_settings()
    resolved_path = Path(path or resolved_settings.database_path).expanduser().resolve()
    with _database_instances_lock:
        if resolved_path not in _database_instances:
            _database_instances[resolved_path] = Database(
                resolved_path,
                settings=resolved_settings,
            )
        return _database_instances[resolved_path]


__all__ = ["Database", "DatabaseManager", "get_database", "init_db"]
