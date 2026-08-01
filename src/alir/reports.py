"""実施内容の記録。

セッションが report_result MCP ツールで報告した「何をしたか」を
reports テーブルに蓄積する。Issue ごとの最新の報告を Web UI に表示する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import iceql

from alir import db


class ReportError(Exception):
    """報告の登録に関する利用側の誤り。"""


@dataclass(frozen=True)
class Report:
    id: int
    issue: str
    summary: str
    pr_url: str | None
    session_id: str | None
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add(
    conn: iceql.Connection,
    *,
    issue: str,
    summary: str,
    pr_url: str | None = None,
    session_id: str | None = None,
) -> Report:
    """実施内容を 1 件記録する。"""
    if not summary.strip():
        raise ReportError("summary must not be empty")
    now = _now()
    with db.transaction(conn):
        cur = conn.execute("SELECT COALESCE(MAX(id), 0) FROM reports")
        row = cur.fetchone()
        assert row is not None
        rid = int(str(row[0])) + 1
        conn.execute(
            "INSERT INTO reports (id, issue, summary, pr_url, session_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rid, issue, summary.strip(), pr_url, session_id, now),
        )
    return Report(
        id=rid,
        issue=issue,
        summary=summary.strip(),
        pr_url=pr_url,
        session_id=session_id,
        created_at=now,
    )


def list_reports(
    conn: iceql.Connection, *, issue: str | None = None, limit: int = 100
) -> list[Report]:
    """報告を新しい順に返す。issue を指定するとその Issue の分だけ返す。"""
    # iceql は LIMIT のプレースホルダに対応していないため整数を直接埋め込む
    limit_sql = int(limit)
    if issue is None:
        cur = conn.execute(
            "SELECT id, issue, summary, pr_url, session_id, created_at FROM reports "
            f"ORDER BY id DESC LIMIT {limit_sql}"
        )
    else:
        cur = conn.execute(
            "SELECT id, issue, summary, pr_url, session_id, created_at FROM reports "
            f"WHERE issue = ? ORDER BY id DESC LIMIT {limit_sql}",
            (issue,),
        )
    return [
        Report(
            id=int(str(rid)),
            issue=str(ref),
            summary=str(summary),
            pr_url=None if pr_url is None else str(pr_url),
            session_id=None if session_id is None else str(session_id),
            created_at=str(created_at),
        )
        for rid, ref, summary, pr_url, session_id, created_at in cur.fetchall()
    ]


def latest_by_issue(conn: iceql.Connection) -> dict[str, Report]:
    """Issue ごとの最新の報告を返す。"""
    latest: dict[str, Report] = {}
    for report in list_reports(conn, limit=1000):
        latest.setdefault(report.issue, report)
    return latest
