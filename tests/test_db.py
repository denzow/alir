"""DB 直列化(locked / run_locked)のテスト。"""

from __future__ import annotations

import threading
from pathlib import Path

import anyio

from alir import db, questions


def _ask(dbdir: Path) -> None:
    with db.locked():
        questions.ask(
            db.connect(dbdir),
            issue="denzow/alir#1",
            question="q",
            options=["a", "b"],
            recommended="a",
            impact="high",
            timeout_action="keep_parked",
        )


def test_locked_serializes_concurrent_asks(tmp_path: Path) -> None:
    dbdir = tmp_path / "data"
    threads = [threading.Thread(target=_ask, args=(dbdir,)) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    items = questions.list_questions(db.connect(dbdir))
    assert [q.id for q in items] == list(range(1, 11))


def test_run_locked_returns_value(tmp_path: Path) -> None:
    dbdir = tmp_path / "data"

    async def main() -> int:
        def work() -> int:
            db.connect(dbdir)
            return 42

        return await db.run_locked(work)

    assert anyio.run(main) == 42
