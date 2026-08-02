"""PR の CI 監視: done になった Issue の PR を確認し、CI 失敗なら再キューする。

reports テーブルに記録された pr_url を使い、ループドライバのサイクルから
定期的に PR の状態を確認する。CI が失敗していれば Issue を queued に戻し、
次のセッションのプロンプトに含める失敗情報を control テーブルに記録する。
マージ済み・クローズ済みの PR は監視対象から外す。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import iceql

from alir import control, registry, reports
from alir.registry import Issue

STATE_MERGED = "MERGED"
STATE_CLOSED = "CLOSED"

# statusCheckRollup の conclusion / state のうち、CI の失敗とみなす値。
# CANCELLED / TIMED_OUT も「通っていない」ので失敗に含める。
_FAILED_VALUES = frozenset({"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE"})


@dataclass(frozen=True)
class PrStatus:
    """PR の状態と CI の成否。"""

    state: str  # OPEN / MERGED / CLOSED
    ci_failed: bool


PrStatusFetch = Callable[[str], "PrStatus | None"]


def _check_failed(entry: dict[str, Any]) -> bool:
    """statusCheckRollup の 1 エントリが失敗を表すか。

    CheckRun は conclusion、StatusContext は state に結果が入る。
    実行中(conclusion が空)は失敗とみなさない。
    """
    value = str(entry.get("conclusion") or entry.get("state") or "").upper()
    return value in _FAILED_VALUES


def pr_status_from_json(data: dict[str, Any]) -> PrStatus:
    """gh pr view --json state,mergedAt,statusCheckRollup の結果を解釈する。"""
    state = str(data.get("state") or "").upper()
    if data.get("mergedAt"):
        state = STATE_MERGED
    rollup = data.get("statusCheckRollup") or []
    ci_failed = any(_check_failed(entry) for entry in rollup if isinstance(entry, dict))
    return PrStatus(state=state, ci_failed=ci_failed)


def fetch_pr_status(pr_url: str, *, timeout: float = 60.0) -> PrStatus | None:
    """gh CLI で PR の状態を取得する。取得できなければ None。"""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state,mergedAt,statusCheckRollup"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return pr_status_from_json(data)


def latest_pr_url(conn: iceql.Connection, issue_ref: str) -> str | None:
    """Issue の報告のうち、最新の pr_url を返す。なければ None。"""
    for report in reports.list_reports(conn, issue=issue_ref):
        if report.pr_url is not None:
            return report.pr_url
    return None


def _failure_key(issue_ref: str) -> str:
    return f"ci_failed:{issue_ref}"


def mark_ci_failed(conn: iceql.Connection, issue_ref: str, pr_url: str) -> None:
    """CI 失敗で再キューしたことを記録する。次のセッションのプロンプトに使う。"""
    control.set_value(conn, _failure_key(issue_ref), pr_url)


def take_ci_failure(conn: iceql.Connection, issue_ref: str) -> str | None:
    """記録された CI 失敗の PR URL を取り出し、記録を消す。なければ None。

    プロンプトへの反映は 1 回きりでよい。再び失敗すれば監視が改めて記録する。
    """
    pr_url = control.get_value(conn, _failure_key(issue_ref))
    if pr_url is not None:
        control.clear_value(conn, _failure_key(issue_ref))
    return pr_url


def requeue_ci_failures(
    conn: iceql.Connection,
    *,
    fetch: PrStatusFetch = fetch_pr_status,
    resolved: set[str] | None = None,
) -> list[tuple[Issue, str]]:
    """done の Issue の PR を確認し、CI 失敗なら queued に戻す。

    戻した (Issue, pr_url) の一覧を返す。resolved にはマージ済み・
    クローズ済みで監視不要になった PR URL を溜める。呼び出し側が
    サイクルをまたいで保持すれば、以後の API 呼び出しを省ける。
    """
    requeued: list[tuple[Issue, str]] = []
    for issue in registry.list_issues(conn, status=registry.STATUS_DONE):
        pr_url = latest_pr_url(conn, issue.ref)
        if pr_url is None or (resolved is not None and pr_url in resolved):
            continue
        status = fetch(pr_url)
        if status is None:
            continue
        if status.state in (STATE_MERGED, STATE_CLOSED):
            if resolved is not None:
                resolved.add(pr_url)
            continue
        if status.ci_failed:
            mark_ci_failed(conn, issue.ref, pr_url)
            requeued.append((registry.set_status(conn, issue.id, registry.STATUS_QUEUED), pr_url))
    return requeued
