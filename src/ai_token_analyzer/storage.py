from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .util import ensure_dir

CCUSAGE_COMMAND = "npx -y ccusage@latest codex session --json"


def paths(out_dir: Path) -> dict[str, Path]:
    root = out_dir
    return {
        "root": root,
        "raw": root / "raw",
        "latest": root / "latest",
        "db": root / "db",
        "logs": root / "logs",
        "locks": root / "locks",
        "raw_jsonl": root / "raw" / "codex_session_snapshots.jsonl",
        "latest_json": root / "latest" / "latest_codex_session.json",
        "latest_hash": root / "latest" / "latest_codex_session.sha256",
        "sqlite": root / "db" / "codex_usage.sqlite",
        "snapshot_log": root / "logs" / "snapshot.log",
        "collector_log": root / "logs" / "collector.log",
        "pid": root / "locks" / "collector.pid",
        "lock": root / "locks" / "collector.lock",
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL UNIQUE,
    ccusage_command TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_observations (
    session_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    snapshot_ts TEXT NOT NULL,
    first_seen TEXT,
    last_seen TEXT,
    last_activity TEXT,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_creation_tokens INTEGER,
    cache_read_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    total_tokens INTEGER,
    cwd TEXT,
    project TEXT,
    repo TEXT,
    branch TEXT,
    source TEXT DEFAULT 'codex',
    raw_json TEXT NOT NULL,
    PRIMARY KEY (session_id, snapshot_hash)
);

CREATE TABLE IF NOT EXISTS model_observations (
    session_id TEXT NOT NULL,
    model TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    snapshot_ts TEXT NOT NULL,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_creation_tokens INTEGER,
    cache_read_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    total_tokens INTEGER,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (session_id, model, snapshot_hash)
);

CREATE TABLE IF NOT EXISTS snapshot_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT,
    snapshot_hash TEXT,
    extra_json TEXT
);
"""


def init_storage(out_dir: Path) -> dict[str, Path]:
    p = paths(out_dir)
    for key in ("raw", "latest", "db", "logs", "locks"):
        ensure_dir(p[key])
    with connect(out_dir) as conn:
        conn.executescript(SCHEMA)
        ensure_column(conn, "session_observations", "reasoning_output_tokens", "INTEGER")
        ensure_column(conn, "model_observations", "reasoning_output_tokens", "INTEGER")
    return p


def connect(out_dir: Path) -> sqlite3.Connection:
    p = paths(out_dir)
    ensure_dir(p["db"])
    conn = sqlite3.connect(p["sqlite"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate_existing_schema(conn)
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def migrate_existing_schema(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "session_observations" in tables:
        ensure_column(conn, "session_observations", "reasoning_output_tokens", "INTEGER")
        backfill_token_columns(conn, "session_observations", ("session_id", "snapshot_hash"))
    if "model_observations" in tables:
        ensure_column(conn, "model_observations", "reasoning_output_tokens", "INTEGER")
        backfill_token_columns(conn, "model_observations", ("session_id", "model", "snapshot_hash"))
    conn.commit()


def json_int(raw_json: str, keys: tuple[str, ...]) -> int | None:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
    return None


def backfill_token_columns(conn: sqlite3.Connection, table: str, pk_columns: tuple[str, ...]) -> None:
    rows = conn.execute(
        f"""
        SELECT {", ".join(pk_columns)}, raw_json, cache_read_tokens, reasoning_output_tokens
        FROM {table}
        WHERE cache_read_tokens IS NULL OR reasoning_output_tokens IS NULL
        """
    ).fetchall()
    for row in rows:
        cache_read_tokens = row["cache_read_tokens"]
        reasoning_output_tokens = row["reasoning_output_tokens"]
        if cache_read_tokens is None:
            cache_read_tokens = json_int(row["raw_json"], ("cacheReadTokens", "cache_read_tokens", "cachedInputTokens", "cached_input_tokens"))
        if reasoning_output_tokens is None:
            reasoning_output_tokens = json_int(row["raw_json"], ("reasoningOutputTokens", "reasoning_output_tokens", "reasoningTokens", "reasoning_tokens"))
        where_clause = " AND ".join(f"{column} = ?" for column in pk_columns)
        conn.execute(
            f"UPDATE {table} SET cache_read_tokens = ?, reasoning_output_tokens = ? WHERE {where_clause}",
            (cache_read_tokens, reasoning_output_tokens, *(row[column] for column in pk_columns)),
        )


def insert_event(
    conn: sqlite3.Connection,
    ts: str,
    event_type: str,
    message: str | None = None,
    snapshot_hash: str | None = None,
    extra_json: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO snapshot_events (ts, event_type, message, snapshot_hash, extra_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ts, event_type, message, snapshot_hash, extra_json),
    )


def query_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params))
