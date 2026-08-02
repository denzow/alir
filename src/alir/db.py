"""iceql データベースへの接続とスキーマ初期化、トランザクション補助。

書き込みの直列化は iceql 0.2.0 の 2 段ロック(BEGIN で DB 全体の write ロックを
取得し COMMIT まで保持)に委ねる。プロセス内の別接続もプロセス間も直列化される。
採番を含む read-modify-write はドメイン層が transaction() で囲んで原子的にする。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

import anyio.to_thread
import iceql

T = TypeVar("T")

_DDLS = {
    "questions": """
CREATE TABLE questions (
    id INTEGER PRIMARY KEY,
    issue TEXT NOT NULL,
    session_id TEXT,
    question TEXT NOT NULL,
    options TEXT NOT NULL,
    recommended TEXT NOT NULL,
    impact TEXT NOT NULL,
    timeout_action TEXT NOT NULL,
    status TEXT NOT NULL,
    answer TEXT,
    answer_note TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT
)
""",
    "runs": """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    issue TEXT NOT NULL,
    session_id TEXT,
    input_tokens INTEGER NOT NULL,
    cache_creation_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
)
""",
    "events": """
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    message TEXT NOT NULL
)
""",
    "control": """
CREATE TABLE control (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""",
    "progress": """
CREATE TABLE progress (
    id INTEGER PRIMARY KEY,
    issue TEXT NOT NULL,
    session_id TEXT,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
)
""",
    "reports": """
CREATE TABLE reports (
    id INTEGER PRIMARY KEY,
    issue TEXT NOT NULL,
    summary TEXT NOT NULL,
    pr_url TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL,
    outcome TEXT
)
""",
    "issues": """
CREATE TABLE issues (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    workdir TEXT NOT NULL,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL,
    session_id TEXT,
    branch TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title TEXT,
    mode TEXT,
    note TEXT
)
""",
}


def _migrate(conn: iceql.Connection) -> None:
    """既存 DB への後方互換の列追加。"""
    additions = (
        ("issues", "title", "ALTER TABLE issues ADD COLUMN title TEXT"),
        ("issues", "mode", "ALTER TABLE issues ADD COLUMN mode TEXT"),
        ("issues", "note", "ALTER TABLE issues ADD COLUMN note TEXT"),
        ("reports", "outcome", "ALTER TABLE reports ADD COLUMN outcome TEXT"),
    )
    for table, column, ddl in additions:
        try:
            conn.execute(f"SELECT {column} FROM {table} LIMIT 1")
        except iceql.Error:
            conn.execute(ddl)


def connect(dbdir: Path) -> iceql.Connection:
    """データディレクトリに接続する。未初期化のテーブルがあれば作る。

    テーブルの有無は iceql のストレージ規約(テーブルごとの schema.yaml)で判定する。
    timeout は他の書き手のロック解放を待つ秒数。
    """
    dbdir.mkdir(parents=True, exist_ok=True)
    conn = iceql.connect(dbdir, timeout=30.0)
    for table, ddl in _DDLS.items():
        if not (dbdir / f"{table}.schema.yaml").exists():
            conn.execute(ddl)
    _migrate(conn)
    return conn


@contextmanager
def transaction(conn: iceql.Connection) -> Iterator[None]:
    """read-modify-write を原子的に行うトランザクション。

    すでにトランザクション中ならそれに参加する(ネストしない)。
    例外時はロールバックする。
    """
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


async def run_in_thread(fn: Callable[[], T]) -> T:
    """同期の DB 操作をワーカースレッドで実行する。async ハンドラから使う。"""
    return await anyio.to_thread.run_sync(fn)
