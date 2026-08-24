"""稼働管理: 公式レート制限使用率の取得と停止判定。

判定は `claude -p /usage` で取得する公式の使用率
(5 時間セッション、週次、モデル別週次)で行う。ドライバが自分の
タイミングで取得するため鮮度の問題がない。停止閾値は
settings.usage_threshold(Web UI から変更・永続化)を参照する。
セッションごとの消費は補助記録として runs テーブルに残す(判定には使わない)。
"""

from __future__ import annotations

import json
import re
import subprocess
import zoneinfo
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import iceql

from alir import db

DEFAULT_THRESHOLD = 0.5


@dataclass(frozen=True)
class UsageWindow:
    label: str
    used_percentage: float
    # /usage が示すリセット時刻の原文(例: "Aug 1, 9:30pm (Asia/Tokyo)")。
    # 取れなかった場合は None。残り時間の計算は parse_reset_at で行う
    resets: str | None = None


@dataclass(frozen=True)
class UsageStatus:
    windows: tuple[UsageWindow, ...]


_USAGE_LINE = re.compile(
    r"^Current (?P<label>session|week \([^)]+\)): (?P<pct>\d+(?:\.\d+)?)% used"
    r"(?:\s*·\s*resets\s+(?P<resets>.+))?",
    re.MULTILINE,
)


def parse_usage_text(text: str) -> UsageStatus | None:
    """/usage の出力テキストからウィンドウごとの使用率とリセット時刻を取り出す。"""
    windows = tuple(
        UsageWindow(
            label=m["label"],
            used_percentage=float(m["pct"]),
            resets=(m["resets"] or "").strip() or None,
        )
        for m in _USAGE_LINE.finditer(text)
    )
    return UsageStatus(windows=windows) if windows else None


_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}  # fmt: skip

_RESET_AT = re.compile(
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (?P<day>\d{1,2}), "
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<ampm>am|pm) \((?P<tz>[^)]+)\)"
)


def parse_reset_at(resets: str, *, now: datetime | None = None) -> datetime | None:
    """リセット時刻の表記(例: "Aug 1, 9:30pm (Asia/Tokyo)")を datetime にする。

    表記に年がないので現在時刻から補う。リセットは最長でも 7 日先なので、
    8 日以上過去を指すのは年末に翌年の日付を見ている場合だけとみなして
    繰り上げる。それより浅い過去は、保存された使用率が古い(リセットを
    またいだ)だけなのでそのまま返し、過去かどうかの扱いは呼び出し側に任せる。
    解釈できない表記やタイムゾーンなら None を返す(表示側は原文だけ出せばよい)。
    """
    m = _RESET_AT.search(resets)
    if m is None:
        return None
    try:
        tz = zoneinfo.ZoneInfo(m["tz"])
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None
    hour = int(m["hour"]) % 12 + (12 if m["ampm"] == "pm" else 0)
    current = (now or _now()).astimezone(tz)
    try:
        candidate = datetime(
            current.year, _MONTHS[m["month"]], int(m["day"]), hour, int(m["minute"] or 0), tzinfo=tz
        )
    except ValueError:
        return None
    if candidate < current - timedelta(days=8):
        candidate = candidate.replace(year=current.year + 1)
    return candidate


def pause_until(
    status: UsageStatus, *, threshold: float, now: datetime | None = None
) -> datetime | None:
    """閾値超過による停止が(閾値の設定が変わらない限り)解けない時刻を返す。

    使用率はウィンドウのリセットまで下がらないため、超過中のウィンドウのうち
    最も早いリセット時刻までは使用率を取り直しても停止は解けない
    (その時点で再確認すれば、他のウィンドウが超過したままなら次のリセットまで
    改めて先送りされる)。超過しているウィンドウがない、またはリセット時刻を
    解釈できないウィンドウが超過している場合は None(通常の間隔で確認する)。
    """
    resets = []
    for window in status.windows:
        if window.used_percentage < threshold * 100:
            continue
        reset_at = parse_reset_at(window.resets, now=now) if window.resets else None
        if reset_at is None:
            return None
        resets.append(reset_at)
    return min(resets) if resets else None


def status_to_json(status: UsageStatus) -> str:
    """KEY_USAGE_STATUS に保存する JSON 表現([[label, used, resets], ...])。"""
    return json.dumps(
        [[w.label, w.used_percentage, w.resets] for w in status.windows], ensure_ascii=False
    )


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
            reason = f"{window.label}: {window.used_percentage:.0f}% used (official)"
            if window.resets:
                reason += f", resets {window.resets}"
            return reason
    return None


def _now() -> datetime:
    return datetime.now(UTC)


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
    conn.execute(
        "INSERT INTO runs (issue, session_id, input_tokens, cache_creation_tokens, "
        "cache_read_tokens, output_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            issue_ref,
            session_id,
            int(usage.get("input_tokens", 0)),
            int(usage.get("cache_creation_input_tokens", 0)),
            int(usage.get("cache_read_input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
            (now or _now()).isoformat(timespec="seconds"),
        ),
    )


