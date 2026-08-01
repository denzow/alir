"""ループ制御(一時停止・ハートビート・イベントログ)のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import control, db, driver, registry
from alir.driver import RunResult

URL = "https://github.com/denzow/alir/issues/12"


@pytest.fixture
def dbdir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def test_paused_flag_roundtrip(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    assert control.is_paused(conn) is False
    control.set_paused(conn, True)
    assert control.is_paused(conn) is True
    control.set_paused(conn, False)
    assert control.is_paused(conn) is False


def test_driver_alive_by_heartbeat(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    assert control.driver_alive(conn) is False
    control.heartbeat(conn)
    assert control.driver_alive(conn) is True
    assert control.driver_alive(conn, stale_after_seconds=0.0) is False


def test_log_event_returns_recent_first(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    control.log_event(conn, "one")
    control.log_event(conn, "two")
    events = control.recent_events(conn)
    assert [e.message for e in events] == ["two", "one"]


def test_log_event_rotates(dbdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control, "MAX_EVENTS", 3)
    conn = db.connect(dbdir)
    for i in range(5):
        control.log_event(conn, f"e{i}")
    events = control.recent_events(conn)
    assert [e.message for e in events] == ["e4", "e3", "e2"]


def _fake_worktree(issue: registry.Issue) -> tuple[Path, str]:
    return Path("/tmp/wt"), f"alir/issue-{issue.number}"


def test_run_loop_respects_manual_pause(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")
    control.set_paused(conn, True)

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        raise AssertionError("must not run while paused")

    logs: list[str] = []
    driver.run_loop(dbdir, once=True, runner=runner, worktree=_fake_worktree, log=logs.append)
    conn = db.connect(dbdir)
    assert registry.list_issues(conn)[0].status == registry.STATUS_QUEUED
    assert any("pause: paused manually" in line for line in logs)


def test_run_loop_records_heartbeat_and_events(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        return RunResult(exit_code=0, session_id=None, output="ok")

    driver.run_loop(dbdir, once=True, runner=runner, worktree=_fake_worktree, log=lambda _: None)
    conn = db.connect(dbdir)
    assert control.heartbeat_at(conn) is not None
    messages = [e.message for e in control.recent_events(conn)]
    assert any(m.startswith("start #1") for m in messages)
    assert any(m.startswith("finish #1") for m in messages)


def test_run_loop_cycles_while_session_running(dbdir: Path) -> None:
    """セッション実行中もサイクルが回り、空き枠に新しい Issue が補充される。"""
    import threading

    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")

    b_finished = threading.Event()

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        if "issues/12" in prompt:
            conn2 = db.connect(dbdir)
            registry.add(conn2, url="https://github.com/denzow/alir/issues/13", workdir="/tmp")
            assert b_finished.wait(timeout=5), "issue B did not start while A was running"
            return RunResult(exit_code=0, session_id=None, output="a")
        b_finished.set()
        return RunResult(exit_code=0, session_id=None, output="b")

    driver.run_loop(
        dbdir,
        once=True,
        parallel=2,
        interval=0.05,
        runner=runner,
        worktree=_fake_worktree,
        log=lambda _: None,
    )
    conn = db.connect(dbdir)
    statuses = {i.ref: i.status for i in registry.list_issues(conn)}
    assert statuses == {
        "denzow/alir#12": registry.STATUS_DONE,
        "denzow/alir#13": registry.STATUS_DONE,
    }
