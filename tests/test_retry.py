"""failed の自動リトライ(回数管理・上限判定・バックオフ・通知)のテスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alir import db, registry, retry
from alir.registry import Issue

URL = "https://github.com/denzow/alir/issues/12"


@pytest.fixture
def dbdir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _add_failed_issue(dbdir: Path, *, retries: int = 0) -> Issue:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp/alir")
    return registry.set_status(conn, issue.id, registry.STATUS_FAILED, retries=retries)


def _notifier():  # type: ignore[no-untyped-def]
    def notify(issue: Issue, limit: int) -> None:
        notify.calls.append((issue.id, limit))  # type: ignore[attr-defined]

    notify.calls = []  # type: ignore[attr-defined]
    return notify


def _after(issue: Issue, seconds: float) -> datetime:
    return datetime.fromisoformat(issue.updated_at) + timedelta(seconds=seconds)


def test_backoff_delay_grows_exponentially() -> None:
    assert retry.backoff_delay(0) == 300.0
    assert retry.backoff_delay(1) == 600.0
    assert retry.backoff_delay(2) == 1200.0


def test_failed_issue_not_requeued_before_backoff(dbdir: Path) -> None:
    issue = _add_failed_issue(dbdir)
    conn = db.connect(dbdir)
    outcome = retry.process_failed(conn, limit=2, now=_after(issue, 1), notifier=_notifier())
    assert outcome.requeued == []
    assert registry.get(conn, issue.id).status == registry.STATUS_FAILED


def test_failed_issue_requeued_after_backoff(dbdir: Path) -> None:
    issue = _add_failed_issue(dbdir)
    conn = db.connect(dbdir)
    outcome = retry.process_failed(conn, limit=2, now=_after(issue, 301), notifier=_notifier())
    assert [i.id for i in outcome.requeued] == [issue.id]
    requeued = registry.get(conn, issue.id)
    assert requeued.status == registry.STATUS_QUEUED
    assert requeued.retries == 1


def test_second_retry_waits_longer(dbdir: Path) -> None:
    issue = _add_failed_issue(dbdir, retries=1)
    conn = db.connect(dbdir)
    outcome = retry.process_failed(conn, limit=2, now=_after(issue, 301), notifier=_notifier())
    assert outcome.requeued == []
    outcome = retry.process_failed(conn, limit=2, now=_after(issue, 601), notifier=_notifier())
    assert [i.id for i in outcome.requeued] == [issue.id]
    assert registry.get(conn, issue.id).retries == 2


def test_limit_reached_stays_failed_and_notifies_once(dbdir: Path) -> None:
    issue = _add_failed_issue(dbdir, retries=2)
    conn = db.connect(dbdir)
    notifier = _notifier()
    outcome = retry.process_failed(conn, limit=2, now=_after(issue, 3600), notifier=notifier)
    assert outcome.requeued == []
    assert [i.id for i in outcome.exhausted] == [issue.id]
    assert notifier.calls == [(issue.id, 2)]  # type: ignore[attr-defined]
    assert registry.get(conn, issue.id).status == registry.STATUS_FAILED

    # 2 回目のスキャンでは通知しない
    outcome = retry.process_failed(conn, limit=2, now=_after(issue, 7200), notifier=notifier)
    assert outcome.exhausted == []
    assert len(notifier.calls) == 1  # type: ignore[attr-defined]


def test_limit_zero_disables_auto_retry(dbdir: Path) -> None:
    issue = _add_failed_issue(dbdir)
    conn = db.connect(dbdir)
    notifier = _notifier()
    outcome = retry.process_failed(conn, limit=0, now=_after(issue, 3600), notifier=notifier)
    assert outcome.requeued == []
    assert [i.id for i in outcome.exhausted] == [issue.id]
    assert registry.get(conn, issue.id).status == registry.STATUS_FAILED


def test_clear_notified_allows_notification_again(dbdir: Path) -> None:
    issue = _add_failed_issue(dbdir, retries=2)
    conn = db.connect(dbdir)
    notifier = _notifier()
    retry.process_failed(conn, limit=2, now=_after(issue, 3600), notifier=notifier)
    assert len(notifier.calls) == 1  # type: ignore[attr-defined]

    # 手動再キュー相当の初期化のあと、再び上限に達したら改めて通知する
    retry.clear_notified(conn, issue.ref)
    registry.set_status(conn, issue.id, registry.STATUS_FAILED, retries=2)
    outcome = retry.process_failed(conn, limit=2, now=_after(issue, 3600), notifier=notifier)
    assert [i.id for i in outcome.exhausted] == [issue.id]
    assert len(notifier.calls) == 2  # type: ignore[attr-defined]


def test_done_resets_retries(dbdir: Path) -> None:
    issue = _add_failed_issue(dbdir, retries=2)
    conn = db.connect(dbdir)
    finished = registry.set_status(conn, issue.id, registry.STATUS_DONE)
    assert finished.retries == 0


def test_non_failed_issues_untouched(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp/alir")
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    outcome = retry.process_failed(conn, limit=2, now=now, notifier=_notifier())
    assert outcome.requeued == []
    assert outcome.exhausted == []
    assert registry.get(conn, issue.id).status == registry.STATUS_QUEUED
