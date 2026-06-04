from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from rich.console import Console
from rich.table import Table

from .storage import connect, paths

console = Console()
NUMERIC = (
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


LATEST_SESSIONS = """
WITH ranked AS (
  SELECT *,
         COALESCE(last_activity, last_seen, first_seen, snapshot_ts) AS activity_ts,
         ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY snapshot_ts DESC, snapshot_hash DESC) AS rn,
         COUNT(*) OVER (PARTITION BY session_id) AS updates_count
  FROM session_observations
)
SELECT * FROM ranked WHERE rn = 1
"""

LATEST_MODELS = """
WITH ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY session_id, model ORDER BY snapshot_ts DESC, snapshot_hash DESC) AS rn
  FROM model_observations
)
SELECT * FROM ranked WHERE rn = 1
"""


def fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def render_table(title: str, columns: Iterable[str], rows: Iterable[Iterable[Any]]) -> None:
    table = Table(title=title)
    for column in columns:
        justify = "right" if any(token in column for token in ("cost", "tokens", "count", "sessions")) else "left"
        table.add_column(column, justify=justify)
    for row in rows:
        table.add_row(*(fmt(value) for value in row))
    console.print(table)


def latest_session_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(LATEST_SESSIONS))


def latest_model_rows_allocated(out_dir: Path) -> list[dict[str, Any]]:
    with connect(out_dir) as conn:
        model_rows = [dict(row) for row in conn.execute(LATEST_MODELS)]
        session_rows = {row["session_id"]: dict(row) for row in conn.execute(LATEST_SESSIONS)}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in model_rows:
        grouped[row["session_id"]].append(row)
    for session_id, rows in grouped.items():
        session_cost = session_rows.get(session_id, {}).get("cost_usd")
        if session_cost is None:
            continue
        known_cost = sum((row["cost_usd"] or 0) for row in rows)
        missing = [row for row in rows if row["cost_usd"] is None]
        if not missing:
            continue
        remaining_cost = max(session_cost - known_cost, 0)
        token_total = sum((row["total_tokens"] or 0) for row in missing)
        for row in missing:
            if token_total > 0:
                row["cost_usd"] = remaining_cost * ((row["total_tokens"] or 0) / token_total)
            else:
                row["cost_usd"] = remaining_cost / len(missing)
    return model_rows


def activity_span(session_rows: list[sqlite3.Row]) -> tuple[str | None, str | None]:
    activity_values = [activity_ts(row) for row in session_rows if activity_ts(row)]
    if not activity_values:
        return (None, None)
    return (min(activity_values), max(activity_values))


def summary(out_dir: Path) -> None:
    p = paths(out_dir)
    with connect(out_dir) as conn:
        sessions = latest_session_rows(conn)
        models = latest_model_rows_allocated(out_dir)
    first_activity, last_activity = activity_span(sessions)
    totals = {key: sum((row[key] or 0) for row in sessions) for key in NUMERIC}
    most_expensive = max(sessions, key=lambda row: row["cost_usd"] or 0, default=None)
    most_active = max(sessions, key=lambda row: row["total_tokens"] or 0, default=None)
    latest = max(sessions, key=lambda row: row["activity_ts"] or "", default=None)
    top_model_cost = max(models, key=lambda row: row["cost_usd"] or 0, default=None)
    top_model_tokens = max(models, key=lambda row: row["total_tokens"] or 0, default=None)
    rows = [
        ("first activity time", first_activity),
        ("last activity time", last_activity),
        ("unique sessions", len(sessions)),
        ("total cost estimate", totals["cost_usd"]),
        ("total input tokens", totals["input_tokens"]),
        ("total output tokens", totals["output_tokens"]),
        ("total cache creation tokens", totals["cache_creation_tokens"]),
        ("total cache read tokens", totals["cache_read_tokens"]),
        ("total reasoning output tokens", totals["reasoning_output_tokens"]),
        ("total tokens", totals["total_tokens"]),
        ("most expensive session", most_expensive["session_id"] if most_expensive else None),
        ("most active session by total tokens", most_active["session_id"] if most_active else None),
        ("latest active session", latest["session_id"] if latest else None),
        ("top model by cost", top_model_cost["model"] if top_model_cost else None),
        ("top model by tokens", top_model_tokens["model"] if top_model_tokens else None),
        ("database", p["sqlite"]),
    ]
    render_table("Summary", ("metric", "value"), rows)


def sessions(out_dir: Path, top: int, sort: str, repo: str | None, model: str | None) -> None:
    order = {"cost": "cost_usd", "tokens": "total_tokens", "last_activity": "activity_ts", "duration": "first_seen"}.get(sort, "cost_usd")
    with connect(out_dir) as conn:
        rows = latest_session_rows(conn)
    if repo:
        rows = [row for row in rows if repo in str(row["repo"] or row["cwd"] or row["project"] or "")]
    if model:
        rows = [row for row in rows if model in models_for_session(out_dir, row["session_id"])]
    reverse = sort != "duration"
    rows = sorted(rows, key=lambda row: row[order] or 0, reverse=reverse)[:top]
    table_rows = []
    for row in rows:
        table_rows.append(
            (
                row["session_id"],
                row["first_seen"],
                row["last_seen"],
                row["activity_ts"],
                row["cost_usd"],
                row["total_tokens"],
                row["input_tokens"],
                row["output_tokens"],
                row["cache_creation_tokens"],
                row["cache_read_tokens"],
                row["reasoning_output_tokens"],
                models_for_session(out_dir, row["session_id"]),
                row["cwd"] or row["project"] or row["repo"],
                row["updates_count"],
            )
        )
    render_table(
        "Sessions",
        ("session_id", "first_seen", "last_seen", "activity_time", "cost_usd", "total_tokens", "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens", "reasoning_output_tokens", "models", "cwd/project/repo", "updates_count"),
        table_rows,
    )


def models_for_session(out_dir: Path, session_id: str) -> str:
    with connect(out_dir) as conn:
        rows = conn.execute(f"SELECT model FROM ({LATEST_MODELS}) WHERE session_id = ? ORDER BY model", (session_id,)).fetchall()
    return ",".join(row["model"] for row in rows)


def models(out_dir: Path) -> None:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"session_ids": set(), **{key: 0 for key in NUMERIC}})
    for row in latest_model_rows_allocated(out_dir):
        item = grouped[row["model"]]
        item["session_ids"].add(row["session_id"])
        for key in NUMERIC:
            item[key] += row[key] or 0
    rows = []
    for model_name, item in grouped.items():
        rows.append(
            (
                model_name,
                len(item["session_ids"]),
                item["cost_usd"],
                item["input_tokens"],
                item["output_tokens"],
                item["cache_creation_tokens"],
                item["cache_read_tokens"],
                item["reasoning_output_tokens"],
                item["total_tokens"],
            )
        )
    rows.sort(key=lambda row: (row[2] or 0, row[8] or 0), reverse=True)
    render_table(
        "Models",
        ("model", "sessions count", "cost_usd", "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens", "reasoning_output_tokens", "total_tokens"),
        rows,
    )


def bucket_ts(ts: str, bucket: str) -> str:
    if bucket == "minute":
        return ts[:16]
    if bucket == "day":
        return ts[:10]
    if bucket == "month":
        return ts[:7]
    return ts[:13]


def activity_ts(row: dict[str, Any] | sqlite3.Row) -> str:
    return (
        row["last_activity"]
        or row["last_seen"]
        or row["first_seen"]
        or row["snapshot_ts"]
        or ""
    )


def delta_rows(out_dir: Path) -> list[dict[str, Any]]:
    with connect(out_dir) as conn:
        rows = conn.execute("SELECT * FROM session_observations ORDER BY session_id, snapshot_ts").fetchall()
    previous: dict[str, sqlite3.Row] = {}
    deltas = []
    for row in rows:
        prev = previous.get(row["session_id"])
        delta = {"session_id": row["session_id"], "snapshot_ts": row["snapshot_ts"], "snapshot_hash": row["snapshot_hash"]}
        for key in NUMERIC:
            old = prev[key] if prev else 0
            new = row[key] or 0
            delta[key] = max(new - (old or 0), 0)
        deltas.append(delta)
        previous[row["session_id"]] = row
    return deltas


def model_delta_rows(out_dir: Path) -> list[dict[str, Any]]:
    session_cost_deltas = {(row["session_id"], row["snapshot_hash"]): row["cost_usd"] for row in delta_rows(out_dir)}
    with connect(out_dir) as conn:
        rows = conn.execute("SELECT * FROM model_observations ORDER BY session_id, model, snapshot_ts").fetchall()
    previous: dict[tuple[str, str], sqlite3.Row] = {}
    deltas = []
    for row in rows:
        prev_key = (row["session_id"], row["model"])
        prev = previous.get(prev_key)
        delta = {
            "session_id": row["session_id"],
            "model": row["model"],
            "snapshot_ts": row["snapshot_ts"],
            "snapshot_hash": row["snapshot_hash"],
        }
        for key in NUMERIC:
            old = prev[key] if prev else 0
            new = row[key] or 0
            delta[key] = max(new - (old or 0), 0)
        deltas.append(delta)
        previous[prev_key] = row
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in deltas:
        grouped[(row["session_id"], row["snapshot_hash"])].append(row)
    for key, rows_for_snapshot in grouped.items():
        session_cost = session_cost_deltas.get(key)
        if session_cost is None:
            continue
        known_cost = sum((row["cost_usd"] or 0) for row in rows_for_snapshot)
        missing = [row for row in rows_for_snapshot if not row["cost_usd"]]
        if not missing:
            continue
        remaining_cost = max(session_cost - known_cost, 0)
        token_total = sum((row["total_tokens"] or 0) for row in missing)
        for row in missing:
            if token_total > 0:
                row["cost_usd"] = remaining_cost * ((row["total_tokens"] or 0) / token_total)
            else:
                row["cost_usd"] = remaining_cost / len(missing)
    return deltas


def timeline(out_dir: Path, bucket: str) -> None:
    grouped: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"sessions": set(), **{key: 0 for key in NUMERIC}})
    with connect(out_dir) as conn:
        session_rows = {row["session_id"]: dict(row) for row in latest_session_rows(conn)}
    source_rows = latest_model_rows_allocated(out_dir)
    if not source_rows:
        source_rows = [{**row, "model": "(session total)"} for row in session_rows.values()]
    for row in source_rows:
        session_row = session_rows.get(row["session_id"], row)
        key = (bucket_ts(activity_ts(session_row), bucket), row["model"])
        grouped[key]["sessions"].add(row["session_id"])
        for num in NUMERIC:
            grouped[key][num] += row[num] or 0
    rows = []
    for key in sorted(grouped):
        item = grouped[key]
        bucket_value, model = key
        rows.append(
            (
                bucket_value,
                model,
                item["reasoning_output_tokens"],
                item["total_tokens"],
                item["cost_usd"],
                item["input_tokens"],
                item["output_tokens"],
                item["cache_creation_tokens"],
                item["cache_read_tokens"],
                len(item["sessions"]),
            )
        )
    render_table(
        "Timeline",
        (
            "bucket timestamp",
            "model",
            "reasoning tokens",
            "total tokens",
            "cost",
            "input tokens",
            "output tokens",
            "cache creation tokens",
            "cache read tokens",
            "sessions count",
        ),
        rows,
    )


def latest(out_dir: Path) -> None:
    p = paths(out_dir)
    with connect(out_dir) as conn:
        sessions = latest_session_rows(conn)
    latest_session = max(sessions, key=lambda row: row["activity_ts"] or "", default=None)
    render_table(
        "Latest",
        ("metric", "value"),
        (
            ("latest activity time", latest_session["activity_ts"] if latest_session else None),
            ("latest active session", latest_session["session_id"] if latest_session else None),
            ("latest session models", models_for_session(out_dir, latest_session["session_id"]) if latest_session else None),
            ("sessions tracked", len(sessions)),
            ("latest raw JSON path", p["latest_json"]),
            ("SQLite DB path", p["sqlite"]),
            ("collector log path", p["collector_log"]),
            ("snapshot log path", p["snapshot_log"]),
        ),
    )


def export(out_dir: Path, table: str, output_format: str, out: Path) -> None:
    with connect(out_dir) as conn:
        if table == "sessions":
            rows = [dict(row) for row in conn.execute(LATEST_SESSIONS)]
        elif table == "observations":
            rows = [dict(row) for row in conn.execute("SELECT * FROM session_observations ORDER BY snapshot_ts")]
        elif table == "model_observations":
            rows = [dict(row) for row in conn.execute("SELECT * FROM model_observations ORDER BY snapshot_ts")]
        else:
            rows = delta_rows(out_dir)
    if output_format == "json":
        out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    else:
        with out.open("w", newline="", encoding="utf-8") as fh:
            fieldnames = list(rows[0].keys()) if rows else []
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    console.print(f"exported {len(rows)} rows to {out}")
