"""Issue レジストリ: 消化対象 Issue の登録と状態管理。

ループドライバは GitHub を直接検索せず、このレジストリからキューを取得する。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

import iceql

from alir import db

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PARKED = "parked"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_PARKED, STATUS_DONE, STATUS_FAILED)

# セッションに求める作業種別。auto はセッションが判断する(従来の挙動)。
MODE_AUTO = "auto"
MODE_IMPLEMENT = "implement"
MODE_REFINE = "refine"
MODES = (MODE_AUTO, MODE_IMPLEMENT, MODE_REFINE)

# 並び替えの対象。実行前(queued)と再開待ち(parked)は順序に意味がある。
REORDERABLE = (STATUS_QUEUED, STATUS_PARKED)

_ISSUE_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/issues/(\d+)$")

_COLUMNS = (
    "id, url, repo, number, workdir, priority, status, session_id, branch, "
    "created_at, updated_at, title, mode, note"
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
    title: str | None = None
    mode: str = MODE_AUTO
    note: str | None = None

    @property
    def ref(self) -> str:
        """ask_human の issue パラメータと同じ表記(owner/repo#number)。"""
        return f"{self.repo}#{self.number}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_issue(row: tuple[object, ...]) -> Issue:
    (
        iid,
        url,
        repo,
        number,
        workdir,
        priority,
        status,
        session_id,
        branch,
        created,
        updated,
        title,
        mode,
        note,
    ) = row
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
        title=None if title is None else str(title),
        mode=MODE_AUTO if mode is None else str(mode),
        note=None if note is None else str(note),
    )


def fetch_title(url: str) -> str | None:
    """gh CLI で Issue のタイトルを取得する。取得できなければ None。"""
    proc = subprocess.run(
        ["gh", "issue", "view", url, "--json", "title", "-q", ".title"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


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
    title: str | None = None,
    mode: str = MODE_AUTO,
    note: str | None = None,
) -> Issue:
    """Issue を queued として登録する。同じ URL の未完了 Issue があれば拒否する。

    mode はセッションに求める作業種別(auto / implement / refine)。
    note は登録者の補足コメントで、セッションのプロンプトに含める。
    """
    repo, number = parse_issue_url(url)
    if mode not in MODES:
        raise RegistryError(f"mode must be one of {MODES}")
    if note is not None and not note.strip():
        note = None
    with db.transaction(conn):
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
            f"INSERT INTO issues ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                iid,
                url,
                repo,
                number,
                workdir,
                priority,
                STATUS_QUEUED,
                None,
                None,
                now,
                now,
                title,
                mode,
                note,
            ),
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


def list_workdirs(conn: iceql.Connection) -> list[str]:
    """登録済みの workdir を新しい順に重複なく返す(入力補完用)。"""
    cur = conn.execute("SELECT workdir FROM issues ORDER BY id DESC")
    seen: list[str] = []
    for (workdir,) in cur.fetchall():
        text = str(workdir)
        if text not in seen:
            seen.append(text)
    return seen


def reorder(conn: iceql.Connection, ids: list[int]) -> list[Issue]:
    """queued / parked の並び順を ids の順に設定する。

    ids は並び替え対象(queued / parked)の全 Issue を過不足なく含む必要がある。
    対象全体に priority を降順で振り直すので、
    それ以外の状態(done / failed など)の priority には影響しない。
    """
    with db.transaction(conn):
        current = {i.id: i for i in list_issues(conn) if i.status in REORDERABLE}
        if sorted(ids) != sorted(current):
            raise RegistryError("order must contain exactly the queued/parked issue ids")
        now = _now()
        for position, iid in enumerate(ids):
            priority = len(ids) - position
            if current[iid].priority != priority:
                conn.execute(
                    "UPDATE issues SET priority = ?, updated_at = ? WHERE id = ?",
                    (priority, now, iid),
                )
    return [get(conn, iid) for iid in ids]


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
    with db.transaction(conn):
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
