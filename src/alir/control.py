"""ループの制御と稼働ログ。

ドライバの一時停止フラグとハートビートを control テーブルで、
ドライバが発したイベントを events テーブルで管理する。
Web UI とドライバは別プロセスでも DB 経由で連携できる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import iceql

from alir import db

KEY_PAUSED = "paused"
KEY_HEARTBEAT = "heartbeat_at"

# events テーブルに残す直近件数。超えた分は登録時に削除する。
MAX_EVENTS = 500


@dataclass(frozen=True)
class Event:
    id: int
    at: str
    message: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _set(conn: iceql.Connection, key: str, value: str) -> None:
    with db.transaction(conn):
        cur = conn.execute("SELECT COUNT(*) FROM control WHERE key = ?", (key,))
        row = cur.fetchone()
        assert row is not None
        if int(str(row[0])) > 0:
            conn.execute("UPDATE control SET value = ? WHERE key = ?", (value, key))
        else:
            conn.execute("INSERT INTO control (key, value) VALUES (?, ?)", (key, value))


def _get(conn: iceql.Connection, key: str) -> str | None:
    cur = conn.execute("SELECT value FROM control WHERE key = ?", (key,))
    row = cur.fetchone()
    return None if row is None else str(row[0])


def set_paused(conn: iceql.Connection, paused: bool) -> None:
    """新規実行の一時停止フラグを設定する。"""
    _set(conn, KEY_PAUSED, "1" if paused else "0")


def is_paused(conn: iceql.Connection) -> bool:
    return _get(conn, KEY_PAUSED) == "1"


def heartbeat(conn: iceql.Connection) -> None:
    """ドライバの生存時刻を記録する。各サイクルの先頭で呼ぶ。"""
    _set(conn, KEY_HEARTBEAT, _now().isoformat(timespec="seconds"))


def heartbeat_at(conn: iceql.Connection) -> datetime | None:
    value = _get(conn, KEY_HEARTBEAT)
    return None if value is None else datetime.fromisoformat(value)


def driver_alive(conn: iceql.Connection, *, stale_after_seconds: float = 120.0) -> bool:
    """ハートビートが新しければ True。ドライバの稼働判定に使う。"""
    at = heartbeat_at(conn)
    if at is None:
        return False
    return (_now() - at).total_seconds() < stale_after_seconds


def log_event(conn: iceql.Connection, message: str) -> Event:
    """イベントを 1 件記録し、古いものを MAX_EVENTS 件に切り詰める。"""
    with db.transaction(conn):
        cur = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events")
        row = cur.fetchone()
        assert row is not None
        eid = int(str(row[0])) + 1
        at = _now().isoformat(timespec="seconds")
        conn.execute("INSERT INTO events (id, at, message) VALUES (?, ?, ?)", (eid, at, message))
        conn.execute("DELETE FROM events WHERE id <= ?", (eid - MAX_EVENTS,))
    return Event(id=eid, at=at, message=message)


def recent_events(conn: iceql.Connection, *, limit: int = 100) -> list[Event]:
    """イベントを新しい順に返す。"""
    # iceql は LIMIT のプレースホルダに対応していないため整数を直接埋め込む
    cur = conn.execute(f"SELECT id, at, message FROM events ORDER BY id DESC LIMIT {int(limit)}")
    return [Event(id=int(str(i)), at=str(a), message=str(m)) for i, a, m in cur.fetchall()]
