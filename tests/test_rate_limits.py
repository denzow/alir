"""公式レート制限使用率の読み取りと停止判定のテスト。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from alir import db, driver, registry, usage
from alir.driver import RunResult

URL = "https://github.com/denzow/alir/issues/12"


def _write(
    path: Path, five_pct: float | None, seven_pct: float | None, *, resets_in: int = 3600
) -> None:
    resets_at = int(time.time()) + resets_in
    limits: dict = {}
    if five_pct is not None:
        limits["five_hour"] = {"used_percentage": five_pct, "resets_at": resets_at}
    if seven_pct is not None:
        limits["seven_day"] = {"used_percentage": seven_pct, "resets_at": resets_at}
    path.write_text(json.dumps({"rate_limits": limits}), encoding="utf-8")


def test_read_rate_limits(tmp_path: Path) -> None:
    path = tmp_path / "latest.json"
    _write(path, 18, 28)
    status = usage.read_rate_limits(path)
    assert status is not None
    assert status.five_hour is not None
    assert status.five_hour.used_percentage == 18
    assert status.seven_day is not None
    assert status.seven_day.used_percentage == 28


def test_read_rate_limits_drops_expired_window(tmp_path: Path) -> None:
    path = tmp_path / "latest.json"
    _write(path, 95, None, resets_in=-60)
    assert usage.read_rate_limits(path) is None


def test_read_rate_limits_missing_or_broken_file(tmp_path: Path) -> None:
    assert usage.read_rate_limits(tmp_path / "none.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    assert usage.read_rate_limits(broken) is None


def test_rate_limit_pause_reason_threshold(tmp_path: Path) -> None:
    path = tmp_path / "latest.json"
    _write(path, 85, 30)
    status = usage.read_rate_limits(path)
    assert status is not None
    reason = usage.rate_limit_pause_reason(status, threshold=0.8)
    assert reason is not None
    assert "5h window" in reason
    assert usage.rate_limit_pause_reason(status, threshold=0.9) is None


def _fake_worktree(issue: registry.Issue, branch: str) -> Path:
    return Path("/tmp/wt")


def test_run_loop_pauses_on_official_rate_limit(tmp_path: Path) -> None:
    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")
    limits_path = tmp_path / "latest.json"
    _write(limits_path, 90, 30)

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        raise AssertionError("must not run while over the official limit")

    logs: list[str] = []
    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=_fake_worktree,
        rate_limits_path=limits_path,
        log=logs.append,
    )
    conn = db.connect(dbdir)
    assert registry.list_issues(conn)[0].status == registry.STATUS_QUEUED
    assert any("official rate limit" in line for line in logs)


def test_run_loop_proceeds_under_official_rate_limit(tmp_path: Path) -> None:
    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")
    limits_path = tmp_path / "latest.json"
    _write(limits_path, 18, 28)

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        return RunResult(exit_code=0, session_id=None, output="ok")

    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=_fake_worktree,
        rate_limits_path=limits_path,
        log=lambda _: None,
    )
    conn = db.connect(dbdir)
    assert registry.list_issues(conn)[0].status == registry.STATUS_DONE
