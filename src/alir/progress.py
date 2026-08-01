"""セッションの進捗報告。

実行中のセッションが report_progress MCP ツールで送る短い進捗を
progress テーブルに蓄積し、sessions 画面で表示する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import iceql

from alir import db

# progress テーブルに残す直近件数。超えた分は登録時に削除する。
MAX_PROGRESS = 1000


class ProgressError(Exception):
    """進捗報告の登録に関する利用側の誤り。"""


@dataclass(frozen=True)
class Progress:
    id: int
    issue: str
    session_id: str | None
    message: str
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add(
    conn: iceql.Connection,
    *,
    issue: str,
    message: str,
    session_id: str | None = None,
) -> Progress:
    """進捗を 1 件記録し、古いものを MAX_PROGRESS 件に切り詰める。"""
    if not message.strip():
        raise ProgressError("message must not be empty")
    now = _now()
    with db.transaction(conn):
        cur = conn.execute("SELECT COALESCE(MAX(id), 0) FROM progress")
        row = cur.fetchone()
        assert row is not None
        pid = int(str(row[0])) + 1
        conn.execute(
            "INSERT INTO progress (id, issue, session_id, message, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (pid, issue, session_id, message.strip(), now),
        )
        conn.execute("DELETE FROM progress WHERE id <= ?", (pid - MAX_PROGRESS,))
    return Progress(
        id=pid, issue=issue, session_id=session_id, message=message.strip(), created_at=now
    )


def list_progress(
    conn: iceql.Connection, *, issue: str | None = None, limit: int = 30
) -> list[Progress]:
    """進捗を新しい順に返す。issue を指定するとその Issue の分だけ返す。"""
    # iceql は LIMIT のプレースホルダに対応していないため整数を直接埋め込む
    limit_sql = int(limit)
    if issue is None:
        cur = conn.execute(
            "SELECT id, issue, session_id, message, created_at FROM progress "
            f"ORDER BY id DESC LIMIT {limit_sql}"
        )
    else:
        cur = conn.execute(
            "SELECT id, issue, session_id, message, created_at FROM progress "
            f"WHERE issue = ? ORDER BY id DESC LIMIT {limit_sql}",
            (issue,),
        )
    return [
        Progress(
            id=int(str(pid)),
            issue=str(ref),
            session_id=None if session_id is None else str(session_id),
            message=str(message),
            created_at=str(created_at),
        )
        for pid, ref, session_id, message, created_at in cur.fetchall()
    ]
