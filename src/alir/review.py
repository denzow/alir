"""PR のレビュー監視: done になった Issue の PR にレビュー指摘が付いたら再キューする。

ci モジュールと同じく reports の pr_url を使い、ループドライバのサイクルから
定期的に PR のレビューとコメントを確認する。前回確認した位置(watermark)より
新しい活動があれば Issue を queued に戻し、次のセッションのプロンプトに含める
PR URL を control テーブルに記録する。watermark を検知のたびに進めることで、
同じ指摘で繰り返し再キューされることを防ぐ。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import iceql

from alir import ci, control, registry
from alir.registry import Issue


@dataclass(frozen=True)
class ReviewStatus:
    """PR の状態と、検知対象になるレビュー・コメントの時刻一覧。"""

    state: str  # OPEN / MERGED / CLOSED
    events: tuple[str, ...]  # ISO 8601 のタイムスタンプ


ReviewStatusFetch = Callable[[str], "ReviewStatus | None"]


def _parse_ts(value: str) -> datetime | None:
    """ISO 8601 文字列を datetime にする。GitHub の "Z" 形式も受ける。

    Python 3.10 の fromisoformat は "Z" を解釈できないため置換する。
    """
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _review_event_at(entry: dict[str, Any]) -> str | None:
    """レビュー 1 件から検知対象の時刻を取り出す。対象外なら None。

    approve は指摘ではないので、本文が空なら対象外とする。
    本文付きの approve は「直してほしい点のコメント」でありうるので対象に含める。
    対応が必要かどうかの判断はセッション側に任せる。
    """
    state = str(entry.get("state") or "").upper()
    body = str(entry.get("body") or "").strip()
    if state == "APPROVED" and not body:
        return None
    at = entry.get("submittedAt")
    return str(at) if at else None


def review_status_from_json(data: dict[str, Any]) -> ReviewStatus:
    """gh pr view --json state,mergedAt,reviews,comments の結果を解釈する。"""
    state = str(data.get("state") or "").upper()
    if data.get("mergedAt"):
        state = ci.STATE_MERGED
    events: list[str] = []
    for entry in data.get("reviews") or []:
        if isinstance(entry, dict):
            at = _review_event_at(entry)
            if at is not None:
                events.append(at)
    for entry in data.get("comments") or []:
        if isinstance(entry, dict) and entry.get("createdAt"):
            events.append(str(entry["createdAt"]))
    return ReviewStatus(state=state, events=tuple(events))


def fetch_review_status(pr_url: str, *, timeout: float = 60.0) -> ReviewStatus | None:
    """gh CLI で PR のレビュー状況を取得する。取得できなければ None。"""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state,mergedAt,reviews,comments"],
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
    return review_status_from_json(data)


def _seen_key(issue_ref: str) -> str:
    return f"review_seen:{issue_ref}"


def _request_key(issue_ref: str) -> str:
    return f"review_requested:{issue_ref}"


def last_seen(conn: iceql.Connection, issue_ref: str) -> str | None:
    """検知済みの位置(最後に確認したレビュー・コメントの時刻)。なければ None。"""
    return control.get_value(conn, _seen_key(issue_ref))


def set_last_seen(conn: iceql.Connection, issue_ref: str, at: str) -> None:
    control.set_value(conn, _seen_key(issue_ref), at)


def record_session_end(conn: iceql.Connection, issue_ref: str) -> None:
    """セッション終了までの活動を確認済みとして watermark を進める。

    セッション自身が PR に付けた返信コメントを次の監視で拾って
    再キューし続けることを防ぐ。以後は人間がこの時刻より後に付けた
    レビュー・コメントだけを検知する。
    """
    set_last_seen(conn, issue_ref, datetime.now(timezone.utc).isoformat(timespec="seconds"))


def mark_review_requested(conn: iceql.Connection, issue_ref: str, pr_url: str) -> None:
    """レビュー指摘で再キューしたことを記録する。次のセッションのプロンプトに使う。"""
    control.set_value(conn, _request_key(issue_ref), pr_url)


def take_review_request(conn: iceql.Connection, issue_ref: str) -> str | None:
    """記録されたレビュー指摘の PR URL を取り出し、記録を消す。なければ None。

    プロンプトへの反映は 1 回きりでよい。新しい指摘が付けば監視が改めて記録する。
    """
    pr_url = control.get_value(conn, _request_key(issue_ref))
    if pr_url is not None:
        control.clear_value(conn, _request_key(issue_ref))
    return pr_url


def _fresh_events(events: tuple[str, ...], seen: str | None) -> list[tuple[datetime, str]]:
    """watermark より後のイベントを (時刻, 元の文字列) で返す。"""
    seen_at = None if seen is None else _parse_ts(seen)
    fresh: list[tuple[datetime, str]] = []
    for at in events:
        parsed = _parse_ts(at)
        if parsed is None:
            continue
        if seen_at is None or parsed > seen_at:
            fresh.append((parsed, at))
    return fresh


def requeue_review_requests(
    conn: iceql.Connection,
    *,
    fetch: ReviewStatusFetch = fetch_review_status,
    resolved: set[str] | None = None,
) -> list[tuple[Issue, str]]:
    """done の Issue の PR を確認し、新しいレビュー・コメントがあれば queued に戻す。

    戻した (Issue, pr_url) の一覧を返す。resolved の扱いは
    ci.requeue_ci_failures と同じで、マージ済み・クローズ済みの PR を
    以後の確認から外すために使う。
    """
    requeued: list[tuple[Issue, str]] = []
    for issue in registry.list_issues(conn, status=registry.STATUS_DONE):
        pr_url = ci.latest_pr_url(conn, issue.ref)
        if pr_url is None or (resolved is not None and pr_url in resolved):
            continue
        status = fetch(pr_url)
        if status is None:
            continue
        if status.state in (ci.STATE_MERGED, ci.STATE_CLOSED):
            if resolved is not None:
                resolved.add(pr_url)
            continue
        fresh = _fresh_events(status.events, last_seen(conn, issue.ref))
        if not fresh:
            continue
        set_last_seen(conn, issue.ref, max(fresh)[1])
        mark_review_requested(conn, issue.ref, pr_url)
        requeued.append((registry.set_status(conn, issue.id, registry.STATUS_QUEUED), pr_url))
    return requeued
