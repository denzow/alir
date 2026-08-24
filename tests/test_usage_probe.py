"""公式使用率(/usage)の取得と停止判定のテスト。"""

from __future__ import annotations

from datetime import UTC
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


def test_parse_usage_text_captures_resets() -> None:
    status = usage.parse_usage_text(SAMPLE)
    assert status is not None
    assert [w.resets for w in status.windows] == [
        "Aug 1, 9:30pm (Asia/Tokyo)",
        "Aug 2, 11am (Asia/Tokyo)",
        "Aug 2, 11am (Asia/Tokyo)",
    ]


def test_parse_usage_text_without_resets_keeps_percentage() -> None:
    status = usage.parse_usage_text("Current session: 19% used")
    assert status is not None
    assert status.windows[0].resets is None


def test_parse_reset_at() -> None:
    import zoneinfo
    from datetime import datetime

    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    tokyo = zoneinfo.ZoneInfo("Asia/Tokyo")
    assert usage.parse_reset_at("Aug 1, 9:30pm (Asia/Tokyo)", now=now) == datetime(
        2026, 8, 1, 21, 30, tzinfo=tokyo
    )
    assert usage.parse_reset_at("Aug 2, 11am (Asia/Tokyo)", now=now) == datetime(
        2026, 8, 2, 11, 0, tzinfo=tokyo
    )


def test_parse_reset_at_keeps_stale_reset_in_current_year() -> None:
    """リセットを数時間過ぎた古いデータは過去のまま返す(翌年に繰り上げない)。"""
    import zoneinfo
    from datetime import datetime

    now = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)  # JST では 8/16 9:00
    at = usage.parse_reset_at("Aug 16, 5am (Asia/Tokyo)", now=now)
    assert at == datetime(2026, 8, 16, 5, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Tokyo"))
    assert at < now


def test_parse_reset_at_rolls_over_year() -> None:
    from datetime import datetime

    now = datetime(2026, 12, 31, 12, 0, tzinfo=UTC)
    at = usage.parse_reset_at("Jan 2, 12am (Asia/Tokyo)", now=now)
    assert at is not None
    assert at.year == 2027


def test_parse_reset_at_unparseable_returns_none() -> None:
    assert usage.parse_reset_at("soon") is None
    assert usage.parse_reset_at("Aug 1, 9:30pm (Not/AZone)") is None


def test_pause_until_returns_earliest_reset_of_exceeded_windows() -> None:
    import zoneinfo
    from datetime import datetime

    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    status = usage.UsageStatus(
        windows=(
            usage.UsageWindow("session", 92.0, resets="Jul 30, 11pm (Asia/Tokyo)"),
            usage.UsageWindow("week (Fable)", 80.0, resets="Aug 2, 11am (Asia/Tokyo)"),
            usage.UsageWindow("week (all models)", 10.0, resets="Aug 2, 11am (Asia/Tokyo)"),
        )
    )
    assert usage.pause_until(status, threshold=0.5, now=now) == datetime(
        2026, 7, 30, 23, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Tokyo")
    )


def test_pause_until_none_when_under_threshold() -> None:
    status = usage.UsageStatus(
        windows=(usage.UsageWindow("session", 30.0, resets="Jul 30, 11pm (Asia/Tokyo)"),)
    )
    assert usage.pause_until(status, threshold=0.5) is None


def test_pause_until_none_when_exceeded_window_lacks_reset() -> None:
    status = usage.UsageStatus(
        windows=(
            usage.UsageWindow("session", 92.0),
            usage.UsageWindow("week (Fable)", 80.0, resets="Aug 2, 11am (Asia/Tokyo)"),
        )
    )
    assert usage.pause_until(status, threshold=0.5) is None


def test_run_loop_defers_usage_check_until_reset(tmp_path: Path) -> None:
    """閾値超過中はリセット時刻まで使用率の確認を先送りする。"""
    import zoneinfo
    from datetime import datetime, timedelta

    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")

    local = (datetime.now(UTC) + timedelta(hours=3)).astimezone(
        zoneinfo.ZoneInfo("Asia/Tokyo")
    )
    month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][
        local.month - 1
    ]
    ampm = "am" if local.hour < 12 else "pm"
    resets = f"{month} {local.day}, {local.hour % 12 or 12}:{local.minute:02d}{ampm} (Asia/Tokyo)"

    def probe() -> usage.UsageStatus:
        return usage.UsageStatus(windows=(usage.UsageWindow("session", 92.0, resets=resets),))

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
    assert any("usage check deferred until reset" in line for line in logs)
    assert registry.list_issues(db.connect(dbdir))[0].status == registry.STATUS_QUEUED


def test_status_pause_reason_includes_resets() -> None:
    status = usage.parse_usage_text(SAMPLE)
    assert status is not None
    reason = usage.status_pause_reason(status, threshold=0.5)
    assert reason is not None
    assert "resets Aug 2, 11am (Asia/Tokyo)" in reason


def test_status_pause_reason_threshold() -> None:
    status = usage.parse_usage_text(SAMPLE)
    assert status is not None
    reason = usage.status_pause_reason(status, threshold=0.5)
    assert reason is not None
    assert "week (Fable)" in reason
    assert usage.status_pause_reason(status, threshold=0.8) is None


def _fake_worktree(issue: registry.Issue, branch: str, *, push: bool = False) -> Path:
    return Path("/tmp/wt")


def _report_and_ok(
    *, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False
) -> RunResult:
    """implemented の報告を残して正常終了する runner。プロンプトから issue を読む。"""
    import re

    from alir import reports

    match = re.search(r"denzow/alir#\d+", prompt)
    assert match is not None
    reports.add(db.connect(dbdir), issue=match.group(0), summary="実装済み")
    return RunResult(exit_code=0, session_id=None, output="ok")


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

    driver.run_loop(
        dbdir,
        once=True,
        parallel=1,
        runner=_report_and_ok,
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

    driver.run_loop(
        dbdir, once=True, runner=_report_and_ok, worktree=_fake_worktree, log=lambda _: None
    )
    conn = db.connect(dbdir)
    assert registry.list_issues(conn)[0].status == registry.STATUS_DONE
    assert control.get_value(conn, control.KEY_USAGE_STATUS) is None


def test_run_loop_honors_usage_threshold_setting(tmp_path: Path) -> None:
    """settings に保存した閾値が使用率判定に効く(既定の 50% では止まる使用率)。"""
    from alir import settings

    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp")
    settings.set_usage_threshold(conn, 0.7)

    def probe() -> usage.UsageStatus:
        return usage.UsageStatus(windows=(usage.UsageWindow("week (Fable)", 55.0),))

    driver.run_loop(
        dbdir,
        once=True,
        runner=_report_and_ok,
        worktree=_fake_worktree,
        usage_probe=probe,
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


def test_report_progress_instructs_stop_over_threshold(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from alir import mcp_server, settings

    conn = db.connect(tmp_path / "data")
    settings.set_usage_threshold(conn, 0.7)
    monkeypatch.setattr(mcp_server, "_usage_cache", None)
    monkeypatch.setattr(
        mcp_server,
        "usage_probe",
        lambda: usage.UsageStatus(windows=(usage.UsageWindow("week (Fable)", 85.0),)),
    )
    result = mcp_server.report_progress_tool(conn, issue="denzow/alir#1", message="実装中")
    assert result.get("stop") is True
    assert "aborted" in result["message"]
    # 進捗自体は記録されている
    from alir import progress as progress_mod

    assert progress_mod.list_progress(conn)[0].message == "実装中"


def test_report_progress_no_stop_under_threshold(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from alir import mcp_server, settings

    conn = db.connect(tmp_path / "data")
    settings.set_usage_threshold(conn, 0.7)
    monkeypatch.setattr(mcp_server, "_usage_cache", None)
    monkeypatch.setattr(
        mcp_server,
        "usage_probe",
        lambda: usage.UsageStatus(windows=(usage.UsageWindow("week (Fable)", 55.0),)),
    )
    result = mcp_server.report_progress_tool(conn, issue="denzow/alir#1", message="実装中")
    assert "stop" not in result


def test_report_progress_survives_probe_failure(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from alir import mcp_server

    conn = db.connect(tmp_path / "data")
    monkeypatch.setattr(mcp_server, "_usage_cache", None)

    def broken() -> usage.UsageStatus:
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_server, "usage_probe", broken)
    result = mcp_server.report_progress_tool(conn, issue="denzow/alir#1", message="実装中")
    assert result["message"] == "Recorded."


def test_process_issue_requeues_when_aborted(tmp_path: Path) -> None:
    from alir import registry as registry_mod
    from alir import reports

    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    issue = registry_mod.add(conn, url=URL, workdir="/tmp")

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        conn2 = db.connect(dbdir)
        reports.add(conn2, issue="denzow/alir#12", summary="使用量上限で中断", outcome="aborted")
        return RunResult(exit_code=0, session_id=None, output="aborted")

    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert finished.status == registry.STATUS_QUEUED


def test_stop_instruction_is_recorded(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from alir import control, mcp_server

    conn = db.connect(tmp_path / "data")
    monkeypatch.setattr(mcp_server, "_usage_cache", None)
    monkeypatch.setattr(
        mcp_server,
        "usage_probe",
        lambda: usage.UsageStatus(windows=(usage.UsageWindow("session", 95.0),)),
    )
    mcp_server.report_progress_tool(conn, issue="denzow/alir#12", message="実装中")
    assert control.stop_instructed_at(conn, "denzow/alir#12") is not None


def test_stop_without_report_requeues(tmp_path: Path) -> None:
    """stop 指示に従わず報告なしで終わったセッションは queued に戻す。"""
    from alir import control

    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp")

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        # セッション中に stop 指示が出たが、報告せずに終了した状況
        conn2 = db.connect(dbdir)
        control.mark_stop_instructed(conn2, "denzow/alir#12")
        return RunResult(exit_code=0, session_id=None, output="quit silently")

    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert finished.status == registry.STATUS_QUEUED


def test_stop_with_implemented_report_is_done(tmp_path: Path) -> None:
    """stop 指示後でも implemented の報告があれば完了として扱う。"""
    from alir import control, reports

    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp")

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        conn2 = db.connect(dbdir)
        control.mark_stop_instructed(conn2, "denzow/alir#12")
        reports.add(conn2, issue="denzow/alir#12", summary="完了直前だったので仕上げた")
        return RunResult(exit_code=0, session_id=None, output="ok")

    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert finished.status == registry.STATUS_DONE


def test_stale_stop_instruction_is_ignored(tmp_path: Path) -> None:
    """前回の実行で出た stop 指示は今回の判定に影響しない。"""
    import time as time_mod

    from alir import control, reports

    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp")
    control.mark_stop_instructed(conn, "denzow/alir#12")
    time_mod.sleep(1.1)  # 記録が秒精度のため

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        conn2 = db.connect(dbdir)
        reports.add(conn2, issue="denzow/alir#12", summary="実装済み")
        return RunResult(exit_code=0, session_id=None, output="ok")

    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert finished.status == registry.STATUS_DONE


def test_stale_stop_with_missing_report_uses_retry_marker(tmp_path: Path) -> None:
    """stale な stop 指示 + 報告なし終了は、stop の保険ではなく途中死として扱う。"""
    import time as time_mod

    from alir import control

    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp")
    control.mark_stop_instructed(conn, "denzow/alir#12")
    time_mod.sleep(1.1)  # 記録が秒精度のため

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        return RunResult(exit_code=0, session_id=None, output="quit silently")

    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert finished.status == registry.STATUS_QUEUED
    conn = db.connect(dbdir)
    assert control.missing_report_requeued_at(conn, "denzow/alir#12") is not None
