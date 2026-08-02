"""PR のレビュー監視と再キューのテスト。gh の呼び出しは差し替える。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import db, registry, reports, review
from alir.review import ReviewStatus

URL = "https://github.com/denzow/alir/issues/12"
REF = "denzow/alir#12"
PR_URL = "https://github.com/denzow/alir/pull/34"

T1 = "2026-08-01T00:00:00Z"
T2 = "2026-08-01T01:00:00Z"
T3 = "2026-08-01T02:00:00Z"


@pytest.fixture
def dbdir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _add_done_issue(dbdir: Path, *, pr_url: str | None = PR_URL) -> registry.Issue:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp/alir")
    if pr_url is not None:
        reports.add(conn, issue=issue.ref, summary="実装して PR 作成", pr_url=pr_url)
    return registry.set_status(conn, issue.id, registry.STATUS_DONE)


def _status(*events: str, state: str = "OPEN") -> ReviewStatus:
    return ReviewStatus(state=state, events=tuple(events))


def test_review_status_from_json_collects_changes_requested() -> None:
    status = review.review_status_from_json(
        {
            "state": "OPEN",
            "mergedAt": None,
            "reviews": [{"state": "CHANGES_REQUESTED", "submittedAt": T1, "body": "直して"}],
            "comments": [],
        }
    )
    assert status == ReviewStatus(state="OPEN", events=(T1,))


def test_review_status_from_json_collects_comments_and_commented_reviews() -> None:
    status = review.review_status_from_json(
        {
            "state": "OPEN",
            "reviews": [{"state": "COMMENTED", "submittedAt": T1, "body": ""}],
            "comments": [{"createdAt": T2, "body": "この行はなぜ?"}],
        }
    )
    assert status.events == (T1, T2)


def test_review_status_from_json_ignores_plain_approve() -> None:
    """本文のない approve は指摘ではないので検知しない。"""
    status = review.review_status_from_json(
        {
            "state": "OPEN",
            "reviews": [{"state": "APPROVED", "submittedAt": T1, "body": ""}],
        }
    )
    assert status.events == ()


def test_review_status_from_json_keeps_approve_with_body() -> None:
    status = review.review_status_from_json(
        {
            "state": "OPEN",
            "reviews": [{"state": "APPROVED", "submittedAt": T1, "body": "typo だけ直して"}],
        }
    )
    assert status.events == (T1,)


def test_review_status_from_json_merged_at_wins_over_state() -> None:
    status = review.review_status_from_json(
        {"state": "OPEN", "mergedAt": "2026-08-01T00:00:00Z", "reviews": [], "comments": []}
    )
    assert status.state == "MERGED"


def test_take_review_request_consumes_mark(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    review.mark_review_requested(conn, REF, PR_URL)
    assert review.take_review_request(conn, REF) == PR_URL
    assert review.take_review_request(conn, REF) is None


def test_requeue_requeues_done_issue_with_new_review(dbdir: Path) -> None:
    """changes requested が付いた PR の Issue が、人手なしで queued に戻る。"""
    issue = _add_done_issue(dbdir)
    conn = db.connect(dbdir)

    requeued = review.requeue_review_requests(conn, fetch=lambda url: _status(T1))
    assert [(i.id, url) for i, url in requeued] == [(issue.id, PR_URL)]
    assert registry.get(conn, issue.id).status == registry.STATUS_QUEUED
    assert review.take_review_request(conn, issue.ref) == PR_URL


def test_requeue_does_not_repeat_for_same_events(dbdir: Path) -> None:
    """同じ指摘で繰り返し再キューされない(検知済みの位置を記録する)。"""
    issue = _add_done_issue(dbdir)
    conn = db.connect(dbdir)

    assert review.requeue_review_requests(conn, fetch=lambda url: _status(T1)) != []
    registry.set_status(conn, issue.id, registry.STATUS_DONE)

    assert review.requeue_review_requests(conn, fetch=lambda url: _status(T1)) == []
    assert registry.get(conn, issue.id).status == registry.STATUS_DONE


def test_requeue_detects_events_after_watermark(dbdir: Path) -> None:
    issue = _add_done_issue(dbdir)
    conn = db.connect(dbdir)

    review.requeue_review_requests(conn, fetch=lambda url: _status(T1))
    registry.set_status(conn, issue.id, registry.STATUS_DONE)
    review.take_review_request(conn, issue.ref)

    requeued = review.requeue_review_requests(conn, fetch=lambda url: _status(T1, T2))
    assert [(i.id, url) for i, url in requeued] == [(issue.id, PR_URL)]
    assert review.last_seen(conn, issue.ref) == T2


def test_requeue_keeps_done_without_events(dbdir: Path) -> None:
    issue = _add_done_issue(dbdir)
    conn = db.connect(dbdir)

    assert review.requeue_review_requests(conn, fetch=lambda url: _status()) == []
    assert registry.get(conn, issue.id).status == registry.STATUS_DONE
    assert review.take_review_request(conn, issue.ref) is None


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_requeue_excludes_merged_and_closed(dbdir: Path, state: str) -> None:
    """マージ済み・クローズ済みの PR は新しいコメントがあっても再キューしない。"""
    issue = _add_done_issue(dbdir)
    conn = db.connect(dbdir)
    resolved: set[str] = set()

    requeued = review.requeue_review_requests(
        conn, fetch=lambda url: _status(T1, state=state), resolved=resolved
    )
    assert requeued == []
    assert registry.get(conn, issue.id).status == registry.STATUS_DONE
    assert resolved == {PR_URL}


def test_requeue_skips_resolved_without_fetch(dbdir: Path) -> None:
    _add_done_issue(dbdir)
    conn = db.connect(dbdir)
    calls: list[str] = []

    def fetch(url: str) -> ReviewStatus:
        calls.append(url)
        return _status(T1)

    review.requeue_review_requests(conn, fetch=fetch, resolved={PR_URL})
    assert calls == []


def test_requeue_skips_issue_without_pr_url(dbdir: Path) -> None:
    _add_done_issue(dbdir, pr_url=None)
    conn = db.connect(dbdir)
    calls: list[str] = []

    def fetch(url: str) -> ReviewStatus:
        calls.append(url)
        return _status(T1)

    assert review.requeue_review_requests(conn, fetch=fetch) == []
    assert calls == []


def test_requeue_keeps_done_when_fetch_fails(dbdir: Path) -> None:
    issue = _add_done_issue(dbdir)
    conn = db.connect(dbdir)

    assert review.requeue_review_requests(conn, fetch=lambda url: None) == []
    assert registry.get(conn, issue.id).status == registry.STATUS_DONE


def test_requeue_ignores_events_before_session_end(dbdir: Path) -> None:
    """record_session_end 以前のイベント(セッション自身のコメントなど)は拾わない。"""
    issue = _add_done_issue(dbdir)
    conn = db.connect(dbdir)
    review.record_session_end(conn, issue.ref)

    # T1〜T3 は record_session_end(現在時刻)より過去
    assert review.requeue_review_requests(conn, fetch=lambda url: _status(T1, T3)) == []
    assert registry.get(conn, issue.id).status == registry.STATUS_DONE


def test_watermark_comparison_handles_mixed_timestamp_formats(dbdir: Path) -> None:
    """GitHub の "Z" 形式と record_session_end の "+00:00" 形式を正しく比較する。"""
    conn = db.connect(dbdir)
    issue = _add_done_issue(dbdir)
    # watermark を "+00:00" 形式で設定し、それより後の "Z" 形式イベントを検知できる
    review.set_last_seen(conn, issue.ref, "2026-08-01T00:30:00+00:00")

    requeued = review.requeue_review_requests(conn, fetch=lambda url: _status(T1, T2))
    assert [(i.id, url) for i, url in requeued] == [(issue.id, PR_URL)]
    assert review.last_seen(conn, issue.ref) == T2
