from __future__ import annotations

import fcntl
import json
import subprocess
from pathlib import Path
from typing import Any

from .storage import CCUSAGE_COMMAND, connect, init_storage, insert_event, paths
from .util import canonical_json, get_first, get_float, get_int, sha256_text, utc_now_iso

SESSION_ID_KEYS = ("sessionId", "session_id", "id", "conversationId", "conversation_id")
COST_KEYS = ("costUSD", "cost_usd", "totalCost", "total_cost", "cost")
INPUT_KEYS = ("inputTokens", "input_tokens")
OUTPUT_KEYS = ("outputTokens", "output_tokens")
CACHE_CREATE_KEYS = ("cacheCreationTokens", "cache_creation_tokens")
CACHE_READ_KEYS = ("cacheReadTokens", "cache_read_tokens", "cachedInputTokens", "cached_input_tokens")
REASONING_KEYS = ("reasoningOutputTokens", "reasoning_output_tokens", "reasoningTokens", "reasoning_tokens")
TOTAL_KEYS = ("totalTokens", "total_tokens", "tokens")
FIRST_KEYS = ("firstSeen", "first_seen", "createdAt", "created_at", "startTime", "start_time")
LAST_KEYS = ("lastSeen", "last_seen", "updatedAt", "updated_at")
ACTIVITY_KEYS = ("lastActivity", "last_activity", "endTime", "end_time")
MODEL_KEYS = ("models", "modelBreakdowns", "model_breakdowns", "modelUsage", "model_usage")


def run_ccusage() -> Any:
    proc = subprocess.run(CCUSAGE_COMMAND.split(), text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ccusage command failed").strip())
    return json.loads(proc.stdout)


def extract_sessions(report: Any) -> list[dict[str, Any]]:
    if isinstance(report, list):
        items = report
    elif isinstance(report, dict):
        data = report.get("data")
        sessions = report.get("sessions")
        if isinstance(data, list):
            items = data
        elif isinstance(sessions, list):
            items = sessions
        else:
            items = []
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def normalize_session(item: dict[str, Any], snapshot_hash: str, snapshot_ts: str) -> dict[str, Any] | None:
    session_id = get_first(item, SESSION_ID_KEYS)
    if session_id is None:
        return None
    input_tokens = get_int(item, INPUT_KEYS)
    output_tokens = get_int(item, OUTPUT_KEYS)
    cache_creation_tokens = get_int(item, CACHE_CREATE_KEYS)
    cache_read_tokens = get_int(item, CACHE_READ_KEYS)
    reasoning_output_tokens = get_int(item, REASONING_KEYS)
    total_tokens = get_int(item, TOTAL_KEYS)
    if total_tokens is None:
        parts = [input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens]
        total_tokens = sum(v for v in parts if v is not None) if any(v is not None for v in parts) else None
    return {
        "session_id": str(session_id),
        "snapshot_hash": snapshot_hash,
        "snapshot_ts": snapshot_ts,
        "first_seen": get_first(item, FIRST_KEYS),
        "last_seen": get_first(item, LAST_KEYS),
        "last_activity": get_first(item, ACTIVITY_KEYS),
        "cost_usd": get_float(item, COST_KEYS),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": total_tokens,
        "cwd": get_first(item, ("cwd", "directory")),
        "project": get_first(item, ("project", "projectName")),
        "repo": get_first(item, ("repo", "repository", "gitRepo")),
        "branch": get_first(item, ("branch", "gitBranch")),
        "raw_json": canonical_json(item),
    }


def extract_model_rows(item: dict[str, Any], session_id: str, snapshot_hash: str, snapshot_ts: str) -> list[dict[str, Any]]:
    data = get_first(item, MODEL_KEYS)
    if not data:
        return []
    rows: list[dict[str, Any]] = []
    if isinstance(data, dict):
        iterable = []
        for model, value in data.items():
            if isinstance(value, dict):
                value = {"model": model, **value}
            else:
                value = {"model": model, "totalTokens": value}
            iterable.append(value)
    elif isinstance(data, list):
        iterable = [value for value in data if isinstance(value, dict)]
    else:
        return []
    for value in iterable:
        model = get_first(value, ("model", "name", "modelName", "id"))
        if model is None:
            continue
        input_tokens = get_int(value, INPUT_KEYS)
        output_tokens = get_int(value, OUTPUT_KEYS)
        cache_creation_tokens = get_int(value, CACHE_CREATE_KEYS)
        cache_read_tokens = get_int(value, CACHE_READ_KEYS)
        reasoning_output_tokens = get_int(value, REASONING_KEYS)
        total_tokens = get_int(value, TOTAL_KEYS)
        if total_tokens is None:
            parts = [input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens]
            total_tokens = sum(v for v in parts if v is not None) if any(v is not None for v in parts) else None
        rows.append(
            {
                "session_id": session_id,
                "model": str(model),
                "snapshot_hash": snapshot_hash,
                "snapshot_ts": snapshot_ts,
                "cost_usd": get_float(value, COST_KEYS),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_tokens": cache_creation_tokens,
                "cache_read_tokens": cache_read_tokens,
                "reasoning_output_tokens": reasoning_output_tokens,
                "total_tokens": total_tokens,
                "raw_json": canonical_json(value),
            }
        )
    return rows


def log_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip() + "\n")


def snapshot(out_dir: Path, force: bool = False) -> tuple[str, str]:
    p = init_storage(out_dir)
    with p["lock"].open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        snapshot_ts = utc_now_iso()
        try:
            report = run_ccusage()
            raw_json = canonical_json(report)
            snapshot_hash = sha256_text(raw_json)
            previous_hash = p["latest_hash"].read_text(encoding="utf-8").strip() if p["latest_hash"].exists() else None
            if previous_hash == snapshot_hash and not force:
                with connect(out_dir) as conn:
                    insert_event(conn, snapshot_ts, "skipped", "snapshot unchanged", snapshot_hash)
                    conn.commit()
                log_line(p["snapshot_log"], f"{snapshot_ts} skipped hash={snapshot_hash}")
                return ("skipped", snapshot_hash)

            sessions = extract_sessions(report)
            session_rows = []
            model_rows = []
            for item in sessions:
                row = normalize_session(item, snapshot_hash, snapshot_ts)
                if row is None:
                    continue
                session_rows.append(row)
                model_rows.extend(extract_model_rows(item, row["session_id"], snapshot_hash, snapshot_ts))

            with connect(out_dir) as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO snapshots
                        (snapshot_ts, snapshot_hash, ccusage_command, raw_json, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (snapshot_ts, snapshot_hash, CCUSAGE_COMMAND, raw_json, snapshot_ts),
                    )
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO session_observations
                        (session_id, snapshot_hash, snapshot_ts, first_seen, last_seen, last_activity,
                         cost_usd, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                         reasoning_output_tokens,
                         total_tokens, cwd, project, repo, branch, raw_json)
                        VALUES (:session_id, :snapshot_hash, :snapshot_ts, :first_seen, :last_seen,
                                :last_activity, :cost_usd, :input_tokens, :output_tokens,
                                :cache_creation_tokens, :cache_read_tokens, :reasoning_output_tokens,
                                :total_tokens, :cwd,
                                :project, :repo, :branch, :raw_json)
                        """,
                        session_rows,
                    )
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO model_observations
                        (session_id, model, snapshot_hash, snapshot_ts, cost_usd, input_tokens,
                         output_tokens, cache_creation_tokens, cache_read_tokens, reasoning_output_tokens,
                         total_tokens, raw_json)
                        VALUES (:session_id, :model, :snapshot_hash, :snapshot_ts, :cost_usd,
                                :input_tokens, :output_tokens, :cache_creation_tokens,
                                :cache_read_tokens, :reasoning_output_tokens, :total_tokens, :raw_json)
                        """,
                        model_rows,
                    )
                    insert_event(
                        conn,
                        snapshot_ts,
                        "stored",
                        f"stored {len(session_rows)} sessions and {len(model_rows)} model rows",
                        snapshot_hash,
                    )
            p["latest_json"].write_text(raw_json + "\n", encoding="utf-8")
            p["latest_hash"].write_text(snapshot_hash + "\n", encoding="utf-8")
            with p["raw_jsonl"].open("a", encoding="utf-8") as fh:
                fh.write(canonical_json({"snapshot_ts": snapshot_ts, "snapshot_hash": snapshot_hash, "report": report}) + "\n")
            log_line(p["snapshot_log"], f"{snapshot_ts} stored hash={snapshot_hash} sessions={len(session_rows)} models={len(model_rows)}")
            return ("stored", snapshot_hash)
        except Exception as exc:
            with connect(out_dir) as conn:
                insert_event(conn, snapshot_ts, "error", str(exc))
                conn.commit()
            log_line(p["snapshot_log"], f"{snapshot_ts} error {exc}")
            raise
