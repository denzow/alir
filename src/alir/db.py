"""iceql データベースへの接続とスキーマ初期化、トランザクション補助。

書き込みの直列化は iceql の 2 段ロック(BEGIN で DB 全体の write ロックを取得し
COMMIT まで保持)に委ねる。プロセス内の別接続もプロセス間も直列化される。
read-modify-write はドメイン層が transaction() で囲んで原子的にする。

id の採番は iceql に任せる。単一の integer 主キーを持つテーブルでは INSERT が
id を省略すると最大値 + 1 が割り当たるので、ドメイン層は SELECT MAX(id) を
投げずに Cursor.lastrowid か RETURNING で採番結果を受け取る。
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
    answered_at TEXT,
    parent_id INTEGER
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
    note TEXT,
    origin TEXT,
    retries INTEGER
)
""",
}


# 既存 DB への後方互換の列追加。(テーブル, 列, ALTER 文)
_ADDITIONS = (
    ("issues", "title", "ALTER TABLE issues ADD COLUMN title TEXT"),
    ("issues", "mode", "ALTER TABLE issues ADD COLUMN mode TEXT"),
    ("issues", "note", "ALTER TABLE issues ADD COLUMN note TEXT"),
    ("issues", "origin", "ALTER TABLE issues ADD COLUMN origin TEXT"),
    ("issues", "retries", "ALTER TABLE issues ADD COLUMN retries INTEGER"),
    ("reports", "outcome", "ALTER TABLE reports ADD COLUMN outcome TEXT"),
    ("questions", "parent_id", "ALTER TABLE questions ADD COLUMN parent_id INTEGER"),
)


def _missing_tables(dbdir: Path) -> list[str]:
    """未作成のテーブル名。iceql のストレージ規約(schema.yaml)で判定する。"""
    return [t for t in _DDLS if not (dbdir / f"{t}.schema.yaml").exists()]


def _missing_columns(conn: iceql.Connection, skip: set[str]) -> list[str]:
    """既存テーブルに足りない列の ALTER 文。skip のテーブルは作りたてなので見ない。"""
    pending = []
    for table, column, ddl in _ADDITIONS:
        if table in skip:
            continue
        try:
            conn.execute(f"SELECT {column} FROM {table} LIMIT 1")
        except iceql.Error:
            pending.append(ddl)
    return pending


def connect(dbdir: Path) -> iceql.Connection:
    """データディレクトリに接続する。未初期化のテーブルがあれば作る。

    スキーマの作成と列追加はトランザクションで囲む。iceql 0.3.0 から DDL を
    トランザクション内で使えるようになったので、初期化は全か無かになり、
    同時に起動した別プロセスの初期化とも write ロックで直列化される。
    既に初期化済みなら BEGIN しない。稼働中のドライバのトランザクションを
    起動のたびに待つのを避けるため。
    timeout は他の書き手のロック解放を待つ秒数。
    """
    dbdir.mkdir(parents=True, exist_ok=True)
    conn = iceql.connect(dbdir, timeout=30.0)
    if not _missing_tables(dbdir) and not _missing_columns(conn, skip=set()):
        return conn
    with transaction(conn):
        # ロックを取ってから判定し直す。待っている間に別プロセスが作り終えている
        # ことがあり、その状態で CREATE / ALTER を投げると失敗する。
        created = _missing_tables(dbdir)
        for table in created:
            conn.execute(_DDLS[table])
        for ddl in _missing_columns(conn, skip=set(created)):
            conn.execute(ddl)
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
