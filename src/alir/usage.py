"""稼働管理: レート制限使用率の参照とトークン予算の判定。

判定の主軸は、Claude Code が statusline に渡す公式のレート制限使用率
(statusline スクリプトが JSON ファイルとして保存したもの)を読む方式。
補助として、セッションごとの消費を runs テーブルに記録し、
設定したトークン予算の閾値超過でも新規実行を止められる。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import iceql

from alir import db

WINDOW_SESSION = timedelta(hours=5)
WINDOW_WEEKLY = timedelta(days=7)

DEFAULT_THRESHOLD = 0.8

# Claude Code の statusline スクリプトが保存するレート制限 JSON の既定パス
DEFAULT_RATE_LIMITS_PATH = Path("~/.claude/usage-monitor/latest.json")


@dataclass(frozen=True)
class Budget:
    """ウィンドウごとのトークン予算。None のウィンドウは判定しない。"""

    session_tokens: int | None = None
    weekly_tokens: int | None = None
    threshold: float = DEFAULT_THRESHOLD


@dataclass(frozen=True)
class RateLimitWindow:
    used_percentage: float
    resets_at: datetime | None


@dataclass(frozen=True)
class RateLimitStatus:
    five_hour: RateLimitWindow | None
    seven_day: RateLimitWindow | None


def read_rate_limits(path: Path, *, now: datetime | None = None) -> RateLimitStatus | None:
    """statusline が保存した公式のレート制限使用率を読む。

    ファイルは対話セッションの応答時にしか更新されないため、
    リセット時刻を過ぎたウィンドウの読み値は捨てる(古い値で止めない)。
    読めない・値がないときは None。
    """
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    limits = data.get("rate_limits") or {}
    current = now or datetime.now(timezone.utc)

    def window(raw: Any) -> RateLimitWindow | None:
        if not isinstance(raw, dict) or raw.get("used_percentage") is None:
            return None
        resets_raw = raw.get("resets_at")
        resets = (
            datetime.fromtimestamp(resets_raw, tz=timezone.utc)
            if isinstance(resets_raw, int | float)
            else None
        )
        if resets is not None and current >= resets:
            return None
        return RateLimitWindow(used_percentage=float(raw["used_percentage"]), resets_at=resets)

    five_hour = window(limits.get("five_hour"))
    seven_day = window(limits.get("seven_day"))
    if five_hour is None and seven_day is None:
        return None
    return RateLimitStatus(five_hour=five_hour, seven_day=seven_day)


def rate_limit_pause_reason(status: RateLimitStatus, *, threshold: float) -> str | None:
    """公式の使用率が閾値を超えているウィンドウがあれば理由を返す。"""
    checks = (("5h window", status.five_hour), ("weekly window", status.seven_day))
    for label, window in checks:
        if window is not None and window.used_percentage >= threshold * 100:
            return f"{label}: {window.used_percentage:.0f}% used (official rate limit)"
    return None


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
    with db.transaction(conn):
        _insert_run(conn, issue_ref=issue_ref, session_id=session_id, usage=usage, now=now)


def _insert_run(
    conn: iceql.Connection,
    *,
    issue_ref: str,
    session_id: str | None,
    usage: dict[str, Any],
    now: datetime | None,
) -> None:
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
