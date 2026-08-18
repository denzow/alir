"""DB のトランザクションとスレッド逃がしのテスト。"""

from __future__ import annotations

import threading
from pathlib import Path

import anyio

from alir import db, questions


def _ask(dbdir: Path) -> None:
    questions.ask(
        db.connect(dbdir),
        issue="denzow/alir#1",
        question="q",
        options=["a", "b"],
        recommended="a",
        impact="high",
        timeout_action="keep_parked",
    )


def test_concurrent_asks_get_unique_ids(tmp_path: Path) -> None:
    """別接続からの同時登録でも採番が重複しない(iceql のトランザクションによる直列化)。"""
    dbdir = tmp_path / "data"
    db.connect(dbdir)  # スキーマ初期化を先に済ませる
    threads = [threading.Thread(target=_ask, args=(dbdir,)) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    items = questions.list_questions(db.connect(dbdir))
    assert [q.id for q in items] == list(range(1, 11))


def test_concurrent_connects_initialize_schema_once(tmp_path: Path) -> None:
    """未初期化の DB に同時に接続してもスキーマ作成が衝突しない。

    作成はトランザクションで囲んであり、write ロックで直列化される。
    """
    dbdir = tmp_path / "data"
    errors: list[BaseException] = []

    def work() -> None:
        try:
            db.connect(dbdir)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert questions.list_questions(db.connect(dbdir)) == []


def test_connect_does_not_wait_for_open_transaction(tmp_path: Path) -> None:
    """初期化済みの DB への接続は稼働中のトランザクションを待たない。"""
    dbdir = tmp_path / "data"
    holder = db.connect(dbdir)
    holder.execute("BEGIN")
    try:
        done = threading.Event()

        def work() -> None:
            db.connect(dbdir)
            done.set()

        thread = threading.Thread(target=work)
        thread.start()
        thread.join(timeout=5.0)
        assert done.is_set()
    finally:
        holder.rollback()


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO runs (id, issue, session_id, input_tokens, "
                "cache_creation_tokens, cache_read_tokens, output_tokens, created_at) "
                "VALUES (1, 'a#1', NULL, 1, 0, 0, 0, 't')"
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    cur = conn.execute("SELECT COUNT(*) FROM runs")
    row = cur.fetchone()
    assert row is not None
    assert int(str(row[0])) == 0


def test_transaction_joins_outer_transaction(tmp_path: Path) -> None:
    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    with db.transaction(conn):
        _ask_with_conn = questions.ask(
            conn,
            issue="denzow/alir#1",
            question="q",
            options=["a", "b"],
            recommended="a",
            impact="high",
            timeout_action="keep_parked",
        )
        assert conn.in_transaction
    assert _ask_with_conn.id == 1


def test_run_in_thread_returns_value(tmp_path: Path) -> None:
    dbdir = tmp_path / "data"

    async def main() -> int:
        def work() -> int:
            db.connect(dbdir)
            return 42

        return await db.run_in_thread(work)

    assert anyio.run(main) == 42


def test_migrate_adds_title_column(tmp_path: Path) -> None:
    """title 列のない既存 DB に接続すると列が追加される。"""
    import iceql

    dbdir = tmp_path / "data"
    dbdir.mkdir(parents=True)
    old = iceql.connect(dbdir)
    old.execute(
        "CREATE TABLE issues (id INTEGER PRIMARY KEY, url TEXT NOT NULL, repo TEXT NOT NULL, "
        "number INTEGER NOT NULL, workdir TEXT NOT NULL, priority INTEGER NOT NULL, "
        "status TEXT NOT NULL, session_id TEXT, branch TEXT, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    old.execute(
        "INSERT INTO issues (id, url, repo, number, workdir, priority, status, "
        "session_id, branch, created_at, updated_at) VALUES "
        "(1, 'u', 'o/r', 1, '/tmp', 0, 'queued', NULL, NULL, 't', 't')"
    )
    old.close()

    from alir import registry

    conn = db.connect(dbdir)
    issue = registry.get(conn, 1)
    assert issue.title is None


def test_migrate_adds_outcome_column(tmp_path: Path) -> None:
    """outcome 列のない既存 reports テーブルに接続すると列が追加される。"""
    import iceql

    dbdir = tmp_path / "data"
    dbdir.mkdir(parents=True)
    old = iceql.connect(dbdir)
    old.execute(
        "CREATE TABLE reports (id INTEGER PRIMARY KEY, issue TEXT NOT NULL, "
        "summary TEXT NOT NULL, pr_url TEXT, session_id TEXT, created_at TEXT NOT NULL)"
    )
    old.execute(
        "INSERT INTO reports (id, issue, summary, pr_url, session_id, created_at) "
        "VALUES (1, 'a#1', 'done', NULL, NULL, 't')"
    )
    old.close()

    from alir import reports

    conn = db.connect(dbdir)
    assert reports.list_reports(conn)[0].outcome == "implemented"


def test_migrate_adds_parent_id_column(tmp_path: Path) -> None:
    """parent_id 列のない既存 questions テーブルに接続すると列が追加される。"""
    import iceql

    dbdir = tmp_path / "data"
    dbdir.mkdir(parents=True)
    old = iceql.connect(dbdir)
    old.execute(
        "CREATE TABLE questions (id INTEGER PRIMARY KEY, issue TEXT NOT NULL, "
        "session_id TEXT, question TEXT NOT NULL, options TEXT NOT NULL, "
        "recommended TEXT NOT NULL, impact TEXT NOT NULL, timeout_action TEXT NOT NULL, "
        "status TEXT NOT NULL, answer TEXT, answer_note TEXT, created_at TEXT NOT NULL, "
        "answered_at TEXT)"
    )
    old.execute(
        "INSERT INTO questions (id, issue, session_id, question, options, recommended, "
        "impact, timeout_action, status, answer, answer_note, created_at, answered_at) "
        "VALUES (1, 'a#1', NULL, 'q', '[\"a\", \"b\"]', 'a', 'high', 'keep_parked', "
        "'open', NULL, NULL, 't', NULL)"
    )
    old.close()

    from alir import questions

    conn = db.connect(dbdir)
    assert questions.get(conn, 1).parent_id is None
