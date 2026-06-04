from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from . import analysis, background, collector
from .storage import init_storage, paths

app = typer.Typer(help="Snapshot and analyze Codex token usage.")
analyze_app = typer.Typer(help="Analyze collected token usage.")
app.add_typer(analyze_app, name="analyze")
console = Console()


@app.command("init-storage")
def init_storage_cmd(out_dir: Path = typer.Option(Path("."), "--out-dir", help="Storage output directory.")) -> None:
    created = init_storage(out_dir)
    for key in ("raw", "latest", "db", "logs", "locks", "sqlite"):
        console.print(f"{key}: {created[key]}")


@app.command()
def snapshot(
    out_dir: Path = typer.Option(Path("."), "--out-dir", help="Storage output directory."),
    force: bool = typer.Option(False, "--force", help="Store even when the snapshot hash is unchanged."),
    print_status: bool = typer.Option(False, "--print-status", help="Print changed/skipped/error status."),
) -> None:
    try:
        status, snapshot_hash = collector.snapshot(out_dir, force=force)
        if print_status:
            console.print(f"{status}: {snapshot_hash}")
    except Exception as exc:
        if print_status:
            console.print(f"error: {exc}")
        raise typer.Exit(1) from exc


@app.command("run-loop")
def run_loop(
    out_dir: Path = typer.Option(Path("."), "--out-dir", help="Storage output directory."),
    interval_seconds: int = typer.Option(60, "--interval-seconds", min=1, help="Seconds to sleep between snapshots."),
    force: bool = typer.Option(False, "--force", help="Store every snapshot even when unchanged."),
) -> None:
    try:
        background.run_loop(out_dir, interval_seconds=interval_seconds, force=force)
    except Exception as exc:
        console.print(f"error: {exc}")
        raise typer.Exit(1) from exc


@app.command("start-collector")
def start_collector(
    out_dir: Path = typer.Option(Path("."), "--out-dir", help="Storage output directory."),
    repo: Path = typer.Option(Path.cwd(), "--repo", help="Repository directory for the background process."),
    interval_seconds: int = typer.Option(60, "--interval-seconds", min=1, help="Seconds to sleep between snapshots."),
    force: bool = typer.Option(False, "--force", help="Store every snapshot even when unchanged."),
) -> None:
    try:
        pid, log_path = background.start(repo, out_dir, interval_seconds=interval_seconds, force=force)
    except RuntimeError as exc:
        console.print(f"error: {exc}")
        raise typer.Exit(1) from exc
    console.print(f"started collector pid={pid}")
    console.print(f"log: {log_path}")


@app.command("stop-collector")
def stop_collector(out_dir: Path = typer.Option(Path("."), "--out-dir", help="Storage output directory.")) -> None:
    stopped, message = background.stop(out_dir)
    console.print(message)
    if not stopped and "still running" in message:
        raise typer.Exit(1)


@app.command("collector-status")
def collector_status(out_dir: Path = typer.Option(Path("."), "--out-dir", help="Storage output directory.")) -> None:
    running, pid, message = background.status(out_dir)
    console.print(f"status: {message}")
    if pid is not None:
        console.print(f"pid: {pid}")
    p = paths(out_dir)
    for label, path in (("collector.log", p["collector_log"]), ("snapshot.log", p["snapshot_log"])):
        console.print(f"\n{label}: {path}")
        if not path.exists():
            console.print("(missing)")
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]
        console.print("\n".join(lines) if lines else "(empty)")


@analyze_app.command("summary")
def analyze_summary(out_dir: Path = typer.Option(Path("."), "--out-dir", help="Storage output directory.")) -> None:
    analysis.summary(out_dir)


@analyze_app.command("sessions")
def analyze_sessions(
    out_dir: Path = typer.Option(Path("."), "--out-dir", help="Storage output directory."),
    top: int = typer.Option(20, "--top", help="Number of sessions to show."),
    sort: str = typer.Option("cost", "--sort", help="cost|tokens|last_activity|duration"),
    repo: Optional[str] = typer.Option(None, "--repo", help="Filter by repo/cwd/project text."),
    model: Optional[str] = typer.Option(None, "--model", help="Filter by model text."),
) -> None:
    if sort not in {"cost", "tokens", "last_activity", "duration"}:
        raise typer.BadParameter("sort must be cost, tokens, last_activity, or duration")
    analysis.sessions(out_dir, top, sort, repo, model)


@analyze_app.command("models")
def analyze_models(out_dir: Path = typer.Option(Path("."), "--out-dir", help="Storage output directory.")) -> None:
    analysis.models(out_dir)


@analyze_app.command("timeline")
def analyze_timeline(
    out_dir: Path = typer.Option(Path("."), "--out-dir", help="Storage output directory."),
    bucket: str = typer.Option("hour", "--bucket", help="minute|hour|day|month"),
) -> None:
    if bucket not in {"minute", "hour", "day", "month"}:
        raise typer.BadParameter("bucket must be minute, hour, day, or month")
    analysis.timeline(out_dir, bucket)


@analyze_app.command("latest")
def analyze_latest(out_dir: Path = typer.Option(Path("."), "--out-dir", help="Storage output directory.")) -> None:
    analysis.latest(out_dir)


@app.command("export")
def export_cmd(
    out_dir: Path = typer.Option(Path("."), "--out-dir", help="Storage output directory."),
    output_format: str = typer.Option("csv", "--format", help="csv|json"),
    table: str = typer.Option(..., "--table", help="sessions|observations|model_observations|snapshot_deltas"),
    out: Path = typer.Option(..., "--out", help="Output file path."),
) -> None:
    if output_format not in {"csv", "json"}:
        raise typer.BadParameter("format must be csv or json")
    if table not in {"sessions", "observations", "model_observations", "snapshot_deltas"}:
        raise typer.BadParameter("unsupported table")
    analysis.export(out_dir, table, output_format, out)


if __name__ == "__main__":
    app()
