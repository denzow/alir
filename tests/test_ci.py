"""PR の CI 監視と再キューのテスト。gh の呼び出しは差し替える。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import ci, db, registry, reports
from alir.ci import PrStatus

URL = "https://github.com/denzow/alir/issues/12"
REF = "denzow/alir#12"
PR_URL = "https://github.com/denzow/alir/pull/34"


@pytest.fixture
def dbdir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _add_done_issue(dbdir: Path, *, pr_url: str | None = PR_URL) -> registry.Issue:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp/alir")
    if pr_url is not None:
        reports.add(conn, issue=issue.ref, summary="実装して PR 作成", pr_url=pr_url)
    return registry.set_status(conn, issue.id, registry.STATUS_DONE)


def test_pr_status_from_json_detects_check_run_failure() -> None:
    status = ci.pr_status_from_json(
        {
            "state": "OPEN",
            "mergedAt": None,
            "statusCheckRollup": [
                {"conclusion": "SUCCESS"},
                {"conclusion": "FAILURE"},
            ],
        }
    )
    assert status == PrStatus(state="OPEN", ci_failed=True)


def test_pr_status_from_json_detects_status_context_error() -> None:
    status = ci.pr_status_from_json({"state": "OPEN", "statusCheckRollup": [{"state": "ERROR"}]})
    assert status.ci_failed


def test_pr_status_from_json_pending_checks_are_not_failure() -> None:
    status = ci.pr_status_from_json(
        {
            "state": "OPEN",
            "statusCheckRollup": [{"conclusion": None, "status": "IN_PROGRESS"}],
        }
    )
    assert not status.ci_failed


def test_pr_status_from_json_merged_at_wins_over_state() -> None:
    status = ci.pr_status_from_json(
        {"state": "OPEN", "mergedAt": "2026-08-01T00:00:00Z", "statusCheckRollup": []}
    )
    assert status.state == ci.STATE_MERGED


def test_latest_pr_url_returns_newest_report_with_pr(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    reports.add(conn, issue=REF, summary="古い PR", pr_url="https://github.com/denzow/alir/pull/1")
    reports.add(conn, issue=REF, summary="新しい PR", pr_url=PR_URL)
    reports.add(conn, issue=REF, summary="PR なしの報告", pr_url=None)
    assert ci.latest_pr_url(conn, REF) == PR_URL


def test_latest_pr_url_none_without_reports(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    assert ci.latest_pr_url(conn, REF) is None


def test_take_ci_failure_consumes_mark(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    ci.mark_ci_failed(conn, REF, PR_URL)
    assert ci.take_ci_failure(conn, REF) == PR_URL
    assert ci.take_ci_failure(conn, REF) is None


def test_requeue_ci_failures_requeues_done_issue_with_failed_ci(dbdir: Path) -> None:
    issue = _add_done_issue(dbdir)
    conn = db.connect(dbdir)

    requeued = ci.requeue_ci_failures(
        conn, fetch=lambda url: PrStatus(state="OPEN", ci_failed=True)
    )
    assert [(i.id, url) for i, url in requeued] == [(issue.id, PR_URL)]
    assert registry.get(conn, issue.id).status == registry.STATUS_QUEUED
    assert ci.take_ci_failure(conn, issue.ref) == PR_URL


def test_requeue_ci_failures_keeps_done_when_ci_passing(dbdir: Path) -> None:
    issue = _add_done_issue(dbdir)
    conn = db.connect(dbdir)

    requeued = ci.requeue_ci_failures(
        conn, fetch=lambda url: PrStatus(state="OPEN", ci_failed=False)
    )
    assert requeued == []
    assert registry.get(conn, issue.id).status == registry.STATUS_DONE
    assert ci.take_ci_failure(conn, issue.ref) is None


@pytest.mark.parametrize("state", [ci.STATE_MERGED, ci.STATE_CLOSED])
def test_requeue_ci_failures_excludes_merged_and_closed(dbdir: Path, state: str) -> None:
    """マージ済み・クローズ済みの PR は CI が失敗していても再キューしない。"""
    issue = _add_done_issue(dbdir)
    conn = db.connect(dbdir)
    resolved: set[str] = set()

    requeued = ci.requeue_ci_failures(
        conn, fetch=lambda url: PrStatus(state=state, ci_failed=True), resolved=resolved
    )
    assert requeued == []
    assert registry.get(conn, issue.id).status == registry.STATUS_DONE
    assert resolved == {PR_URL}


def test_requeue_ci_failures_skips_resolved_without_fetch(dbdir: Path) -> None:
    _add_done_issue(dbdir)
    conn = db.connect(dbdir)
    calls: list[str] = []

    def fetch(url: str) -> PrStatus:
        calls.append(url)
        return PrStatus(state="OPEN", ci_failed=True)

    ci.requeue_ci_failures(conn, fetch=fetch, resolved={PR_URL})
    assert calls == []


def test_requeue_ci_failures_skips_issue_without_pr_url(dbdir: Path) -> None:
    _add_done_issue(dbdir, pr_url=None)
    conn = db.connect(dbdir)
    calls: list[str] = []

    def fetch(url: str) -> PrStatus:
        calls.append(url)
        return PrStatus(state="OPEN", ci_failed=True)

    assert ci.requeue_ci_failures(conn, fetch=fetch) == []
    assert calls == []


def test_requeue_ci_failures_keeps_done_when_fetch_fails(dbdir: Path) -> None:
    issue = _add_done_issue(dbdir)
    conn = db.connect(dbdir)

    requeued = ci.requeue_ci_failures(conn, fetch=lambda url: None)
    assert requeued == []
    assert registry.get(conn, issue.id).status == registry.STATUS_DONE


def test_requeue_ci_failures_ignores_non_done_issues(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp/alir")
    reports.add(conn, issue=issue.ref, summary="実装して PR 作成", pr_url=PR_URL)
    calls: list[str] = []

    def fetch(url: str) -> PrStatus:
        calls.append(url)
        return PrStatus(state="OPEN", ci_failed=True)

    assert ci.requeue_ci_failures(conn, fetch=fetch) == []
    assert calls == []
    assert registry.get(conn, issue.id).status == registry.STATUS_QUEUED
