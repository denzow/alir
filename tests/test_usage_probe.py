"""公式使用率(/usage)の取得と停止判定のテスト。"""

from __future__ import annotations

from pathlib import Path

from alir import control, db, driver, registry, usage
from alir.driver import RunResult

URL = "https://github.com/denzow/alir/issues/12"

SAMPLE = """You are currently using your subscription to power your Claude Code usage

Current session: 19% used · resets Aug 1, 9:30pm (Asia/Tokyo)
Current week (all models): 29% used · resets Aug 2, 11am (Asia/Tokyo)
Current week (Fable): 54% used · resets Aug 2, 11am (Asia/Tokyo)

What's contributing to your limits usage?
"""


def test_parse_usage_text() -> None:
    status = usage.parse_usage_text(SAMPLE)
    assert status is not None
    assert [(w.label, w.used_percentage) for w in status.windows] == [
        ("session", 19.0),
        ("week (all models)", 29.0),
        ("week (Fable)", 54.0),
    ]


def test_parse_usage_text_without_matches() -> None:
    assert usage.parse_usage_text("something else") is None


def test_status_pause_reason_threshold() -> None:
    status = usage.parse_usage_text(SAMPLE)
    assert status is not None
    reason = usage.status_pause_reason(status, threshold=0.5)
    assert reason is not None
    assert "week (Fable)" in reason
    assert usage.status_pause_reason(status, threshold=0.8) is None


def _fake_worktree(issue: registry.Issue, branch: str) -> Path:
    return Path("/tmp/wt")


def test_run_loop_pauses_when_usage_over_threshold(tmp_path: Path) -> None:
    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")

    def probe() -> usage.UsageStatus:
        return usage.UsageStatus(windows=(usage.UsageWindow("session", 92.0),))

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        raise AssertionError("must not run while over the official limit")

    logs: list[str] = []
    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=_fake_worktree,
        usage_probe=probe,
        log=logs.append,
    )
    conn = db.connect(dbdir)
    assert registry.list_issues(conn)[0].status == registry.STATUS_QUEUED
    assert any("official" in line for line in logs)


def test_run_loop_stores_usage_and_reprobes_before_next_start(tmp_path: Path) -> None:
    """セッション完了後、次の開始候補があれば使用率を取り直す。"""
    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")
    registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir="/tmp")

    calls: list[int] = []

    def probe() -> usage.UsageStatus:
        calls.append(1)
        return usage.UsageStatus(windows=(usage.UsageWindow("session", 19.0),))

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        return RunResult(exit_code=0, session_id=None, output="ok")

    driver.run_loop(
        dbdir,
        once=True,
        parallel=1,
        runner=runner,
        worktree=_fake_worktree,
        usage_probe=probe,
        log=lambda _: None,
    )
    conn = db.connect(dbdir)
    assert all(i.status == registry.STATUS_DONE for i in registry.list_issues(conn))
    # 初回 + 1 件目の完了後(2 件目の開始前)で 2 回以上呼ばれる
    assert len(calls) >= 2
    assert control.get_value(conn, control.KEY_USAGE_STATUS) is not None


def test_run_loop_skips_probe_when_disabled(tmp_path: Path) -> None:
    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        return RunResult(exit_code=0, session_id=None, output="ok")

    driver.run_loop(dbdir, once=True, runner=runner, worktree=_fake_worktree, log=lambda _: None)
    conn = db.connect(dbdir)
    assert registry.list_issues(conn)[0].status == registry.STATUS_DONE
    assert control.get_value(conn, control.KEY_USAGE_STATUS) is None


def test_run_loop_honors_usage_threshold_without_budget(tmp_path: Path) -> None:
    """トークン予算なしでも --budget-threshold 相当の閾値が使用率判定に効く。"""
    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")

    def probe() -> usage.UsageStatus:
        return usage.UsageStatus(windows=(usage.UsageWindow("week (Fable)", 55.0),))

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        return RunResult(exit_code=0, session_id=None, output="ok")

    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=_fake_worktree,
        usage_probe=probe,
        usage_threshold=0.7,
        log=lambda _: None,
    )
    conn = db.connect(dbdir)
    assert registry.list_issues(conn)[0].status == registry.STATUS_DONE


def test_run_loop_skips_probe_when_queue_empty(tmp_path: Path) -> None:
    """キューが空なら使用率を取りに行かない。"""
    dbdir = tmp_path / "data"
    db.connect(dbdir)

    calls: list[int] = []

    def probe() -> usage.UsageStatus:
        calls.append(1)
        return usage.UsageStatus(windows=(usage.UsageWindow("session", 10.0),))

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        raise AssertionError("nothing to run")

    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=_fake_worktree,
        usage_probe=probe,
        log=lambda _: None,
    )
    assert calls == []
