"""iceql データベースへの接続とスキーマ初期化。"""

from __future__ import annotations

from pathlib import Path

import iceql

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
    updated_at TEXT NOT NULL
)
""",
}


def connect(dbdir: Path) -> iceql.Connection:
    """データディレクトリに接続する。未初期化のテーブルがあれば作る。

    テーブルの有無は iceql のストレージ規約(テーブルごとの schema.yaml)で判定する。
    """
    dbdir.mkdir(parents=True, exist_ok=True)
    conn = iceql.connect(dbdir)
    for table, ddl in _DDLS.items():
        if not (dbdir / f"{table}.schema.yaml").exists():
            conn.execute(ddl)
    return conn
