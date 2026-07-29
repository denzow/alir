"""questions ドメインのテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import db, questions
from alir.questions import QuestionError


@pytest.fixture
def conn(tmp_path: Path):  # type: ignore[no-untyped-def]
    return db.connect(tmp_path / "data")


def _ask(conn, **overrides):  # type: ignore[no-untyped-def]
    params = {
        "issue": "denzow/alir#1",
        "question": "DB スキーマを変更してよいか",
        "options": ["変更する", "変更しない"],
        "recommended": "変更する",
        "impact": "high",
        "timeout_action": "keep_parked",
    }
    params.update(overrides)
    return questions.ask(conn, **params)


def test_ask_registers_open_question(conn) -> None:  # type: ignore[no-untyped-def]
    q = _ask(conn, session_id="sess-1")
    assert q.id == 1
    assert q.status == questions.STATUS_OPEN
    assert q.options == ("変更する", "変更しない")
    assert q.session_id == "sess-1"
    assert q.answer is None
    assert q.created_at


def test_ask_assigns_sequential_ids(conn) -> None:  # type: ignore[no-untyped-def]
    assert _ask(conn).id == 1
    assert _ask(conn).id == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"options": ["only-one"]},
        {"options": ["a", "b", "c", "d", "e"]},
        {"recommended": "not in options"},
        {"impact": "medium"},
        {"timeout_action": "explode"},
    ],
)
def test_ask_rejects_invalid_input(conn, overrides) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(QuestionError):
        _ask(conn, **overrides)


def test_list_questions_filters_by_status(conn) -> None:  # type: ignore[no-untyped-def]
    _ask(conn)
    _ask(conn)
    questions.answer(conn, 1, "1")
    open_items = questions.list_questions(conn)
    assert [q.id for q in open_items] == [2]
    all_items = questions.list_questions(conn, status=None)
    assert [q.id for q in all_items] == [1, 2]


def test_answer_by_option_number(conn) -> None:  # type: ignore[no-untyped-def]
    _ask(conn)
    q = questions.answer(conn, 1, "2", note="今回は見送り")
    assert q.status == questions.STATUS_ANSWERED
    assert q.answer == "変更しない"
    assert q.answer_note == "今回は見送り"
    assert q.answered_at is not None


def test_answer_by_free_text(conn) -> None:  # type: ignore[no-untyped-def]
    _ask(conn)
    q = questions.answer(conn, 1, "別リポジトリに切り出す")
    assert q.answer == "別リポジトリに切り出す"


def test_answer_rejects_out_of_range_number(conn) -> None:  # type: ignore[no-untyped-def]
    _ask(conn)
    with pytest.raises(QuestionError):
        questions.answer(conn, 1, "3")


def test_answer_rejects_double_answer(conn) -> None:  # type: ignore[no-untyped-def]
    _ask(conn)
    questions.answer(conn, 1, "1")
    with pytest.raises(QuestionError):
        questions.answer(conn, 1, "1")


def test_answer_rejects_unknown_id(conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(QuestionError):
        questions.answer(conn, 99, "1")


def test_storage_is_plaintext(tmp_path: Path) -> None:
    dbdir = tmp_path / "data"
    conn = db.connect(dbdir)
    _ask(conn)
    csv_text = (dbdir / "questions.csv").read_text(encoding="utf-8")
    assert "DB スキーマを変更してよいか" in csv_text
