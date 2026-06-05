from pathlib import Path

from ai_token_analyzer import background


def test_parse_linux_stat_state_handles_parentheses_in_command() -> None:
    stat = "42613 (python (worker)) Z 1 42613 42613 0 -1 4228100"

    assert background.parse_linux_stat_state(stat) == "Z"


def test_stop_removes_stale_pid_file(tmp_path: Path, monkeypatch) -> None:
    locks = tmp_path / "locks"
    locks.mkdir()
    pid_path = locks / "collector.pid"
    pid_path.write_text("42613\n", encoding="utf-8")

    monkeypatch.setattr(background, "is_running", lambda _pid: False)

    stopped, message = background.stop(tmp_path)

    assert stopped is False
    assert message == "stale pid file removed for pid 42613"
    assert not pid_path.exists()
