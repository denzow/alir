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
KEY_USAGE_STATUS = "usage_status"
# serve / web が起動時に自動検出して記録する Web UI の URL(運用者の設定ではない)
KEY_WEB_URL_AUTO = "web_url_auto"

# events テーブルに残す直近件数。超えた分は登録時に削除する。
MAX_EVENTS = 500


@dataclass(frozen=True)
class Event:
    id: int
    at: str
    message: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def set_value(conn: iceql.Connection, key: str, value: str) -> None:
    """control テーブルの KV を設定する。設定値の保存にも使う。

    主キーの衝突を DO UPDATE で解決するので、有無を調べてから
    INSERT / UPDATE を分ける read-modify-write は要らない。
    """
    conn.execute(
        "INSERT INTO control (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_value(conn: iceql.Connection, key: str) -> str | None:
    """control テーブルの KV を取得する。未設定なら None。"""
    cur = conn.execute("SELECT value FROM control WHERE key = ?", (key,))
    row = cur.fetchone()
    return None if row is None else str(row[0])


def clear_value(conn: iceql.Connection, key: str) -> None:
    """control テーブルの KV を削除する。未設定なら何もしない。"""
    with db.transaction(conn):
        conn.execute("DELETE FROM control WHERE key = ?", (key,))


def set_paused(conn: iceql.Connection, paused: bool) -> None:
    """新規実行の一時停止フラグを設定する。"""
    set_value(conn, KEY_PAUSED, "1" if paused else "0")


def is_paused(conn: iceql.Connection) -> bool:
    return get_value(conn, KEY_PAUSED) == "1"


def mark_stop_instructed(conn: iceql.Connection, issue_ref: str) -> None:
    """使用率超過でセッションに停止を指示したことを記録する。

    指示に従わず報告なしで終了した場合に、ドライバが requeue するための保険。
    """
    set_value(
        conn,
        f"stop_instructed:{issue_ref}",
        _now().isoformat(timespec="seconds"),
    )


def stop_instructed_at(conn: iceql.Connection, issue_ref: str) -> str | None:
    """停止指示を出した時刻(ISO 文字列)。出していなければ None。"""
    return get_value(conn, f"stop_instructed:{issue_ref}")


def mark_resume_session(conn: iceql.Connection, issue_ref: str, session_id: str) -> None:
    """park からの再開で `claude -p --resume` に使うセッション ID を記録する。

    回答検知(resume.requeue_answered)が queued に戻すときに付ける印で、
    refined 経由の再キューには付かない。ドライバは実行の完了時に消費する。
    """
    set_value(conn, f"resume_session:{issue_ref}", session_id)


def resume_session(conn: iceql.Connection, issue_ref: str) -> str | None:
    """記録済みの再開用セッション ID。なければ None。"""
    return get_value(conn, f"resume_session:{issue_ref}") or None


def clear_resume_session(conn: iceql.Connection, issue_ref: str) -> None:
    """再開用セッション ID を消す。セッションの実行が完了したら呼ぶ。"""
    clear_value(conn, f"resume_session:{issue_ref}")


def mark_missing_report_requeued(conn: iceql.Connection, issue_ref: str) -> None:
    """報告なしで終了したセッションを queued に戻したことを記録する。

    連続して報告なしで終了した場合に failed へ落とすための印。
    """
    set_value(
        conn,
        f"missing_report_requeued:{issue_ref}",
        _now().isoformat(timespec="seconds"),
    )


def missing_report_requeued_at(conn: iceql.Connection, issue_ref: str) -> str | None:
    """報告なし requeue を記録した時刻(ISO 文字列)。なければ None。"""
    return get_value(conn, f"missing_report_requeued:{issue_ref}") or None


def clear_missing_report_requeued(conn: iceql.Connection, issue_ref: str) -> None:
    """報告なし requeue の印を消す。報告のあるセッションが終わったら呼ぶ。"""
    if get_value(conn, f"missing_report_requeued:{issue_ref}"):
        set_value(conn, f"missing_report_requeued:{issue_ref}", "")


def heartbeat(conn: iceql.Connection) -> None:
    """ドライバの生存時刻を記録する。各サイクルの先頭で呼ぶ。"""
    set_value(conn, KEY_HEARTBEAT, _now().isoformat(timespec="seconds"))


def heartbeat_at(conn: iceql.Connection) -> datetime | None:
    value = get_value(conn, KEY_HEARTBEAT)
    return None if value is None else datetime.fromisoformat(value)


def driver_alive(conn: iceql.Connection, *, stale_after_seconds: float = 120.0) -> bool:
    """ハートビートが新しければ True。ドライバの稼働判定に使う。"""
    at = heartbeat_at(conn)
    if at is None:
        return False
    return (_now() - at).total_seconds() < stale_after_seconds


def log_event(conn: iceql.Connection, message: str) -> Event:
    """イベントを 1 件記録し、古いものを MAX_EVENTS 件に切り詰める。"""
    at = _now().isoformat(timespec="seconds")
    with db.transaction(conn):
        cur = conn.execute("INSERT INTO events (at, message) VALUES (?, ?)", (at, message))
        eid = cur.lastrowid
        assert eid is not None
        conn.execute("DELETE FROM events WHERE id <= ?", (eid - MAX_EVENTS,))
    return Event(id=eid, at=at, message=message)


def recent_events(conn: iceql.Connection, *, limit: int = 100) -> list[Event]:
    """イベントを新しい順に返す。"""
    # iceql は LIMIT のプレースホルダに対応していないため整数を直接埋め込む
    cur = conn.execute(f"SELECT id, at, message FROM events ORDER BY id DESC LIMIT {int(limit)}")
    return [Event(id=int(str(i)), at=str(a), message=str(m)) for i, a, m in cur.fetchall()]
