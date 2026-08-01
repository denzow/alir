"""稼働管理: 公式レート制限使用率の取得とトークン予算の判定。

判定の主軸は `claude -p /usage` で取得する公式の使用率
(5 時間セッション、週次、モデル別週次)。ドライバが自分のタイミングで
取得するため鮮度の問題がない。
補助として、セッションごとの消費を runs テーブルに記録し、
設定したトークン予算の閾値超過でも新規実行を止められる。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import iceql

from alir import db

WINDOW_SESSION = timedelta(hours=5)
WINDOW_WEEKLY = timedelta(days=7)

DEFAULT_THRESHOLD = 0.8


@dataclass(frozen=True)
class Budget:
    """ウィンドウごとのトークン予算。None のウィンドウは判定しない。"""

    session_tokens: int | None = None
    weekly_tokens: int | None = None
    threshold: float = DEFAULT_THRESHOLD


@dataclass(frozen=True)
class UsageWindow:
    label: str
    used_percentage: float


@dataclass(frozen=True)
class UsageStatus:
    windows: tuple[UsageWindow, ...]


_USAGE_LINE = re.compile(
    r"^Current (?P<label>session|week \([^)]+\)): (?P<pct>\d+(?:\.\d+)?)% used",
    re.MULTILINE,
)


def parse_usage_text(text: str) -> UsageStatus | None:
    """/usage の出力テキストからウィンドウごとの使用率を取り出す。"""
    windows = tuple(
        UsageWindow(label=m["label"], used_percentage=float(m["pct"]))
        for m in _USAGE_LINE.finditer(text)
    )
    return UsageStatus(windows=windows) if windows else None


def fetch_usage_status(*, timeout: float = 120.0) -> UsageStatus | None:
    """`claude -p /usage` で公式のレート制限使用率を取得する。失敗したら None。"""
    try:
        proc = subprocess.run(
            ["claude", "-p", "/usage", "--output-format", "json"],
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
    return parse_usage_text(str(data.get("result") or ""))


def status_pause_reason(status: UsageStatus, *, threshold: float) -> str | None:
    """公式の使用率が閾値を超えているウィンドウがあれば理由を返す。"""
    for window in status.windows:
        if window.used_percentage >= threshold * 100:
            return f"{window.label}: {window.used_percentage:.0f}% used (official)"
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
