"""failed の自動リトライ: 上限とバックオフ付きで queued に戻す。

セッションの失敗の多く(ネットワーク断、gh の一時エラー、異常終了)は
再実行で解消するため、ループドライバのサイクルから failed の Issue を
自動で queued に戻す。同じ原因で失敗し続ける Issue を回し続けないよう、
回数に上限(settings.retry_limit)を設け、上限に達したら failed のまま
通知(notify)で人間に知らせる。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import iceql

from alir import control, notify, registry
from alir.registry import Issue

# 自動リトライのバックオフ。n 回目のリトライは失敗から INITIAL * FACTOR^n 秒後
# (即時再実行で同じ一時的な失敗を繰り返さないための間隔)。
BACKOFF_INITIAL = 300.0
BACKOFF_FACTOR = 2.0

Notify = Callable[[Issue, int], None]


@dataclass(frozen=True)
class RetryOutcome:
    """1 回のスキャンで再キューした Issue と、上限到達を通知した Issue。"""

    requeued: list[Issue]
    exhausted: list[Issue]


def backoff_delay(retries: int) -> float:
    """retries 回リトライ済みの Issue を次に再実行するまでの待ち秒数。"""
    return BACKOFF_INITIAL * BACKOFF_FACTOR**retries


def _notified_key(issue_ref: str) -> str:
    return f"retry_exhausted_notified:{issue_ref}"


def clear_notified(conn: iceql.Connection, issue_ref: str) -> None:
    """上限到達の通知済みの印を消す。手動再キューで自動リトライを再開するときに呼ぶ。"""
    control.clear_value(conn, _notified_key(issue_ref))


def process_failed(
    conn: iceql.Connection,
    *,
    limit: int,
    now: datetime | None = None,
    notifier: Notify = notify.notify_retry_exhausted,
) -> RetryOutcome:
    """failed の Issue を確認し、上限内ならバックオフ経過後に queued へ戻す。

    上限に達した Issue は failed のまま残し、1 回だけ notifier で通知する
    (通知済みの印は control テーブルに置き、手動再キューで消える)。
    failed になった時刻には updated_at を使う。
    """
    current = now or datetime.now(UTC)
    requeued: list[Issue] = []
    exhausted: list[Issue] = []
    for issue in registry.list_issues(conn, status=registry.STATUS_FAILED):
        if issue.retries >= limit:
            if control.get_value(conn, _notified_key(issue.ref)) is None:
                control.set_value(conn, _notified_key(issue.ref), issue.updated_at)
                notifier(issue, limit)
                exhausted.append(issue)
            continue
        failed_at = datetime.fromisoformat(issue.updated_at)
        if (current - failed_at).total_seconds() < backoff_delay(issue.retries):
            continue
        requeued.append(
            registry.set_status(conn, issue.id, registry.STATUS_QUEUED, retries=issue.retries + 1)
        )
    return RetryOutcome(requeued=requeued, exhausted=exhausted)
