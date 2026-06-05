from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .collector import snapshot
from .storage import init_storage, paths
from .util import utc_now_iso


def append_log(path: Path, message: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{utc_now_iso()} {message.rstrip()}\n")


def is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not is_zombie(pid)


def is_zombie(pid: int) -> bool:
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        stat = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return False

    return parse_linux_stat_state(stat) == "Z"


def parse_linux_stat_state(stat: str) -> str | None:
    try:
        state = stat.rsplit(")", 1)[1].strip().split()[0]
    except IndexError:
        return None
    return state


def remove_pid_file(pid_path: Path) -> None:
    pid_path.unlink(missing_ok=True)


def wait_until_stopped(pid: int, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not is_running(pid):
            return True
        time.sleep(0.2)
    return not is_running(pid)


def terminate(pid: int, timeout_seconds: int) -> bool:
    os.kill(pid, signal.SIGTERM)
    return wait_until_stopped(pid, timeout_seconds)


def kill(pid: int, timeout_seconds: int) -> bool:
    os.kill(pid, signal.SIGKILL)
    return wait_until_stopped(pid, timeout_seconds)


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
    stop_event = threading.Event()

    def handle_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    first = True
    while not stop_event.is_set():
        if first or once_first:
            try:
                status, snapshot_hash = snapshot(out_dir, force=force)
                append_log(p["collector_log"], f"snapshot {status} hash={snapshot_hash}")
            except Exception as exc:
                append_log(p["collector_log"], f"snapshot error {exc}")
        first = False
        if stop_event.is_set():
            break
        stop_event.wait(interval_seconds)
    append_log(p["collector_log"], f"loop stopped pid={os.getpid()}")


def start(repo: Path, out_dir: Path, interval_seconds: int, force: bool = False) -> tuple[int, Path]:
    p = init_storage(out_dir)
    old_pid = read_pid(p["pid"])
    if old_pid and is_running(old_pid):
        raise RuntimeError(f"collector is already running with pid {old_pid}")
    if old_pid:
        remove_pid_file(p["pid"])

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
    with log_path.open("a", encoding="utf-8") as log_fh:
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
        remove_pid_file(p["pid"])
        return False, f"stale pid file removed for pid {pid}"
    if terminate(pid, timeout_seconds):
        remove_pid_file(p["pid"])
        return True, f"stopped pid {pid}"
    if kill(pid, 2):
        remove_pid_file(p["pid"])
        return True, f"killed pid {pid}"
    return False, f"sent SIGTERM and SIGKILL to pid {pid}, but it is still running"


def status(out_dir: Path) -> tuple[bool, int | None, str]:
    p = paths(out_dir)
    pid = read_pid(p["pid"])
    if pid is None:
        return False, None, "not running"
    if is_running(pid):
        return True, pid, "running"
    return False, pid, "stale pid file"
