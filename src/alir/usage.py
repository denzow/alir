"""稼働管理: トークン消費の記録とウィンドウ予算の判定。

セッションごとの消費を runs テーブルに記録し、
5 時間ウィンドウと週次ウィンドウの消費が予算の閾値を超えたら新規実行を止める。
予算はサーバー側の残枠を取れないため設定値であり、limit 到達時の実測で見直す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import iceql

WINDOW_SESSION = timedelta(hours=5)
WINDOW_WEEKLY = timedelta(days=7)


@dataclass(frozen=True)
class Budget:
    """ウィンドウごとのトークン予算。None のウィンドウは判定しない。"""

    session_tokens: int | None = None
    weekly_tokens: int | None = None
    threshold: float = 0.8


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_run(
    conn: iceql.Connection,
    *,
    issue_ref: str,
    session_id: str | None,
    usage: dict[str, Any],
    now: datetime | None = None,
) -> None:
    """claude -p の結果 JSON の usage を 1 行として記録する。"""
    cur = conn.execute("SELECT COALESCE(MAX(id), 0) FROM runs")
    row = cur.fetchone()
    assert row is not None
    rid = int(str(row[0])) + 1
    conn.execute(
        "INSERT INTO runs (id, issue, session_id, input_tokens, cache_creation_tokens, "
        "cache_read_tokens, output_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rid,
            issue_ref,
            session_id,
            int(usage.get("input_tokens", 0)),
            int(usage.get("cache_creation_input_tokens", 0)),
            int(usage.get("cache_read_input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
            (now or _now()).isoformat(timespec="seconds"),
        ),
    )


def tokens_in_window(
    conn: iceql.Connection, *, window: timedelta, now: datetime | None = None
) -> int:
    """ウィンドウ内の消費トークン数を返す。

    数えるのは input + cache_creation + output。cache_read は含めない。
    """
    cutoff = (now or _now()) - window
    total = 0
    cur = conn.execute(
        "SELECT input_tokens, cache_creation_tokens, output_tokens, created_at FROM runs"
    )
    for input_tokens, cache_creation, output_tokens, created_at in cur.fetchall():
        if datetime.fromisoformat(str(created_at)) >= cutoff:
            total += int(str(input_tokens)) + int(str(cache_creation)) + int(str(output_tokens))
    return total


def pause_reason(
    conn: iceql.Connection, budget: Budget, *, now: datetime | None = None
) -> str | None:
    """予算の閾値を超えているウィンドウがあれば理由を返す。なければ None。"""
    checks = (
        ("5h window", WINDOW_SESSION, budget.session_tokens),
        ("weekly window", WINDOW_WEEKLY, budget.weekly_tokens),
    )
    for label, window, limit in checks:
        if limit is None:
            continue
        used = tokens_in_window(conn, window=window, now=now)
        if used >= limit * budget.threshold:
            return f"{label}: {used} tokens used (threshold {int(limit * budget.threshold)})"
    return None
