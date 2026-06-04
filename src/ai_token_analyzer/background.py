from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .collector import snapshot
from .storage import init_storage, paths
from .util import utc_now_iso


def append_log(path: Path, message: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{utc_now_iso()} {message.rstrip()}\n")


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def run_loop(out_dir: Path, interval_seconds: int, force: bool = False, once_first: bool = True) -> None:
    if interval_seconds < 1:
        raise ValueError("interval_seconds must be at least 1")
    p = init_storage(out_dir)
    append_log(p["collector_log"], f"loop started pid={os.getpid()} interval_seconds={interval_seconds}")
    stopping = False

    def handle_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    first = True
    while not stopping:
        if first or once_first:
            try:
                status, snapshot_hash = snapshot(out_dir, force=force)
                append_log(p["collector_log"], f"snapshot {status} hash={snapshot_hash}")
            except Exception as exc:
                append_log(p["collector_log"], f"snapshot error {exc}")
        first = False
        if stopping:
            break
        time.sleep(interval_seconds)
    append_log(p["collector_log"], f"loop stopped pid={os.getpid()}")


def start(repo: Path, out_dir: Path, interval_seconds: int, force: bool = False) -> tuple[int, Path]:
    p = init_storage(out_dir)
    old_pid = read_pid(p["pid"])
    if old_pid and is_running(old_pid):
        raise RuntimeError(f"collector is already running with pid {old_pid}")
    if old_pid:
        p["pid"].unlink(missing_ok=True)

    repo_abs = repo.resolve()
    out_abs = out_dir.resolve()
    log_path = p["collector_log"].resolve()
    cmd = [
        sys.executable,
        "-m",
        "ai_token_analyzer.cli",
        "run-loop",
        "--out-dir",
        str(out_abs),
        "--interval-seconds",
        str(interval_seconds),
    ]
    if force:
        cmd.append("--force")
    log_fh = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=repo_abs,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    p["pid"].write_text(f"{proc.pid}\n", encoding="utf-8")
    append_log(log_path, f"background collector started pid={proc.pid} interval_seconds={interval_seconds}")
    return proc.pid, log_path


def stop(out_dir: Path, timeout_seconds: int = 10) -> tuple[bool, str]:
    p = paths(out_dir)
    pid = read_pid(p["pid"])
    if pid is None:
        return False, "no pid file found"
    if not is_running(pid):
        p["pid"].unlink(missing_ok=True)
        return False, f"stale pid file removed for pid {pid}"
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not is_running(pid):
            p["pid"].unlink(missing_ok=True)
            return True, f"stopped pid {pid}"
        time.sleep(0.2)
    return False, f"sent SIGTERM to pid {pid}, but it is still running"


def status(out_dir: Path) -> tuple[bool, int | None, str]:
    p = paths(out_dir)
    pid = read_pid(p["pid"])
    if pid is None:
        return False, None, "not running"
    if is_running(pid):
        return True, pid, "running"
    return False, pid, "stale pid file"
