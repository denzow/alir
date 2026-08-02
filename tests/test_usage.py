"""稼働管理(トークン記録・予算判定・レート制限バックオフ)のテスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alir import db, driver, registry, usage
from alir.driver import RunResult

URL = "https://github.com/denzow/alir/issues/12"


@pytest.fixture
def dbdir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _usage(tokens: int) -> dict[str, int]:
    return {
        "input_tokens": tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 999_999,
        "output_tokens": 0,
    }


def test_tokens_in_window_excludes_cache_read_and_old_runs(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    now = datetime.now(timezone.utc)
    usage.record_run(conn, issue_ref="a#1", session_id=None, usage=_usage(100), now=now)
    usage.record_run(
        conn,
        issue_ref="a#2",
        session_id=None,
        usage=_usage(1000),
        now=now - timedelta(hours=6),
    )
    assert usage.tokens_in_window(conn, window=usage.WINDOW_SESSION, now=now) == 100
    assert usage.tokens_in_window(conn, window=usage.WINDOW_WEEKLY, now=now) == 1100


def test_pause_reason_by_session_budget(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    usage.record_run(conn, issue_ref="a#1", session_id=None, usage=_usage(900))
    budget = usage.Budget(session_tokens=1000, threshold=0.8)
    reason = usage.pause_reason(conn, budget)
    assert reason is not None
    assert "5h window" in reason


def test_pause_reason_none_under_threshold(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    usage.record_run(conn, issue_ref="a#1", session_id=None, usage=_usage(100))
    assert usage.pause_reason(conn, usage.Budget(session_tokens=1000)) is None


def _fake_worktree(issue: registry.Issue, branch: str, *, push: bool = False) -> Path:
    return Path("/tmp/wt")


def test_process_issue_records_usage(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp")

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        return RunResult(exit_code=0, session_id="sess-1", output="ok", usage=_usage(500))

    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    conn = db.connect(dbdir)
    assert usage.tokens_in_window(conn, window=usage.WINDOW_SESSION) == 500


def test_rate_limited_run_requeues_issue(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp")

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        return RunResult(
            exit_code=1, session_id=None, output="usage limit reached", rate_limited=True
        )

    with pytest.raises(driver.RateLimited):
        driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    conn = db.connect(dbdir)
    assert registry.get(conn, issue.id).status == registry.STATUS_QUEUED


def test_run_loop_backs_off_on_rate_limit(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        return RunResult(
            exit_code=1, session_id=None, output="usage limit reached", rate_limited=True
        )

    logs: list[str] = []
    driver.run_loop(dbdir, once=True, runner=runner, worktree=_fake_worktree, log=logs.append)
    conn = db.connect(dbdir)
    assert registry.list_issues(conn)[0].status == registry.STATUS_QUEUED
    assert any("rate limited" in line for line in logs)


def test_run_loop_pauses_over_budget(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")
    usage.record_run(conn, issue_ref="a#1", session_id=None, usage=_usage(900))

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        raise AssertionError("must not run while paused")

    logs: list[str] = []
    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=_fake_worktree,
        budget=usage.Budget(session_tokens=1000),
        log=logs.append,
    )
    conn = db.connect(dbdir)
    assert registry.list_issues(conn)[0].status == registry.STATUS_QUEUED
    assert any("pause: " in line for line in logs)
