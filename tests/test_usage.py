"""稼働管理(トークン記録・レート制限バックオフ)のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import db, driver, registry
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


def _fake_worktree(issue: registry.Issue, branch: str, *, push: bool = False) -> Path:
    return Path("/tmp/wt")


def test_process_issue_records_usage(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp")

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        return RunResult(exit_code=0, session_id="sess-1", output="ok", usage=_usage(500))

    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    conn = db.connect(dbdir)
    rows = conn.execute("SELECT issue, input_tokens, cache_read_tokens FROM runs").fetchall()
    assert len(rows) == 1
    assert str(rows[0][0]) == "denzow/alir#12"
    assert int(str(rows[0][1])) == 500


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
