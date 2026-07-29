"""Issue レジストリ: 消化対象 Issue の登録と状態管理。

ループドライバは GitHub を直接検索せず、このレジストリからキューを取得する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import iceql

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PARKED = "parked"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_PARKED, STATUS_DONE, STATUS_FAILED)

_ISSUE_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/issues/(\d+)$")

_COLUMNS = (
    "id, url, repo, number, workdir, priority, status, session_id, branch, created_at, updated_at"
)


class RegistryError(Exception):
    """Issue の登録・状態遷移に関する利用側の誤り。"""


@dataclass(frozen=True)
class Issue:
    id: int
    url: str
    repo: str
    number: int
    workdir: str
    priority: int
    status: str
    session_id: str | None
    branch: str | None
    created_at: str
    updated_at: str

    @property
    def ref(self) -> str:
        """ask_human の issue パラメータと同じ表記(owner/repo#number)。"""
        return f"{self.repo}#{self.number}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_issue(row: tuple[object, ...]) -> Issue:
    (iid, url, repo, number, workdir, priority, status, session_id, branch, created, updated) = row
    return Issue(
        id=int(str(iid)),
        url=str(url),
        repo=str(repo),
        number=int(str(number)),
        workdir=str(workdir),
        priority=int(str(priority)),
        status=str(status),
        session_id=None if session_id is None else str(session_id),
        branch=None if branch is None else str(branch),
        created_at=str(created),
        updated_at=str(updated),
    )


def parse_issue_url(url: str) -> tuple[str, int]:
    """GitHub Issue の URL を (owner/repo, number) に分解する。"""
    m = _ISSUE_URL.match(url)
    if m is None:
        raise RegistryError(f"not a GitHub issue URL: {url}")
    return m.group(1), int(m.group(2))


def add(
    conn: iceql.Connection,
    *,
    url: str,
    workdir: str,
    priority: int = 0,
) -> Issue:
    """Issue を queued として登録する。同じ URL の未完了 Issue があれば拒否する。"""
    repo, number = parse_issue_url(url)
    cur = conn.execute(
        "SELECT COUNT(*) FROM issues WHERE url = ? AND status IN (?, ?, ?)",
        (url, STATUS_QUEUED, STATUS_RUNNING, STATUS_PARKED),
    )
    row = cur.fetchone()
    assert row is not None
    if int(str(row[0])) > 0:
        raise RegistryError(f"issue already registered and not finished: {url}")

    cur = conn.execute("SELECT COALESCE(MAX(id), 0) FROM issues")
    row = cur.fetchone()
    assert row is not None
    iid = int(str(row[0])) + 1
    now = _now()
    conn.execute(
        f"INSERT INTO issues ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (iid, url, repo, number, workdir, priority, STATUS_QUEUED, None, None, now, now),
    )
    return get(conn, iid)


def get(conn: iceql.Connection, iid: int) -> Issue:
    """Issue を 1 件取得する。"""
    cur = conn.execute(f"SELECT {_COLUMNS} FROM issues WHERE id = ?", (iid,))
    row = cur.fetchone()
    if row is None:
        raise RegistryError(f"issue {iid} not found")
    return _row_to_issue(row)


def list_issues(conn: iceql.Connection, *, status: str | None = None) -> list[Issue]:
    """Issue を優先度順(priority 降順、同順位は登録順)に一覧する。"""
    if status is None:
        cur = conn.execute(f"SELECT {_COLUMNS} FROM issues ORDER BY priority DESC, id")
    else:
        cur = conn.execute(
            f"SELECT {_COLUMNS} FROM issues WHERE status = ? ORDER BY priority DESC, id",
            (status,),
        )
    return [_row_to_issue(row) for row in cur.fetchall()]


def next_queued(conn: iceql.Connection) -> Issue | None:
    """次に実行すべき queued の Issue を返す。なければ None。"""
    items = list_issues(conn, status=STATUS_QUEUED)
    return items[0] if items else None


def set_status(
    conn: iceql.Connection,
    iid: int,
    status: str,
    *,
    session_id: str | None = None,
    branch: str | None = None,
) -> Issue:
    """Issue の状態を更新する。session_id と branch は指定されたときだけ上書きする。"""
    if status not in STATUSES:
        raise RegistryError(f"status must be one of {STATUSES}")
    issue = get(conn, iid)
    conn.execute(
        "UPDATE issues SET status = ?, session_id = ?, branch = ?, updated_at = ? WHERE id = ?",
        (
            status,
            session_id if session_id is not None else issue.session_id,
            branch if branch is not None else issue.branch,
            _now(),
            iid,
        ),
    )
    return get(conn, iid)
