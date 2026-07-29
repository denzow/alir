"""iceql データベースへの接続とスキーマ初期化。"""

from __future__ import annotations

from pathlib import Path

import iceql

_QUESTIONS_DDL = """
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
"""


def connect(dbdir: Path) -> iceql.Connection:
    """データディレクトリに接続する。未初期化ならスキーマを作る。

    テーブルの有無は iceql のストレージ規約(テーブルごとの schema.yaml)で判定する。
    """
    dbdir.mkdir(parents=True, exist_ok=True)
    conn = iceql.connect(dbdir)
    if not (dbdir / "questions.schema.yaml").exists():
        conn.execute(_QUESTIONS_DDL)
    return conn
