"""質問の登録・一覧・回答。

質問は questions テーブルの 1 行として保存する。
選択肢(options)は JSON 配列のテキストとして格納する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import iceql

from alir import db

IMPACTS = ("high", "low")
TIMEOUT_ACTIONS = ("proceed_with_recommended", "keep_parked")

STATUS_OPEN = "open"
STATUS_ANSWERED = "answered"
STATUS_EXPIRED = "expired"

_COLUMNS = (
    "id, issue, session_id, question, options, recommended, impact, "
    "timeout_action, status, answer, answer_note, created_at, answered_at"
)

# INSERT で並べる列。id は iceql が採番するので渡さない。
_INSERT_COLUMNS = _COLUMNS.removeprefix("id, ")


class QuestionError(Exception):
    """質問の登録・回答に関する利用側の誤り。"""


@dataclass(frozen=True)
class Question:
    id: int
    issue: str
    session_id: str | None
    question: str
    options: tuple[str, ...]
    recommended: str
    impact: str
    timeout_action: str
    status: str
    answer: str | None
    answer_note: str | None
    created_at: str
    answered_at: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_question(row: tuple[object, ...]) -> Question:
    (
        qid,
        issue,
        session_id,
        question,
        options,
        recommended,
        impact,
        timeout_action,
        status,
        answer,
        answer_note,
        created_at,
        answered_at,
    ) = row
    return Question(
        id=int(str(qid)),
        issue=str(issue),
        session_id=None if session_id is None else str(session_id),
        question=str(question),
        options=tuple(json.loads(str(options))),
        recommended=str(recommended),
        impact=str(impact),
        timeout_action=str(timeout_action),
        status=str(status),
        answer=None if answer is None else str(answer),
        answer_note=None if answer_note is None else str(answer_note),
        created_at=str(created_at),
        answered_at=None if answered_at is None else str(answered_at),
    )


def ask(
    conn: iceql.Connection,
    *,
    issue: str,
    question: str,
    options: list[str],
    recommended: str,
    impact: str,
    timeout_action: str,
    session_id: str | None = None,
) -> Question:
    """質問を登録し、登録した質問を返す。"""
    if not (2 <= len(options) <= 4):
        raise QuestionError("options must have 2 to 4 items")
    if recommended not in options:
        raise QuestionError("recommended must be one of options")
    if impact not in IMPACTS:
        raise QuestionError(f"impact must be one of {IMPACTS}")
    if timeout_action not in TIMEOUT_ACTIONS:
        raise QuestionError(f"timeout_action must be one of {TIMEOUT_ACTIONS}")

    cur = conn.execute(
        f"INSERT INTO questions ({_INSERT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        f"RETURNING {_COLUMNS}",
        (
            issue,
            session_id,
            question,
            json.dumps(options, ensure_ascii=False),
            recommended,
            impact,
            timeout_action,
            STATUS_OPEN,
            None,
            None,
            _now(),
            None,
        ),
    )
    row = cur.fetchone()
    assert row is not None
    return _row_to_question(row)


def get(conn: iceql.Connection, qid: int) -> Question:
    """質問を 1 件取得する。"""
    cur = conn.execute(f"SELECT {_COLUMNS} FROM questions WHERE id = ?", (qid,))
    row = cur.fetchone()
    if row is None:
        raise QuestionError(f"question {qid} not found")
    return _row_to_question(row)


def list_questions(conn: iceql.Connection, *, status: str | None = STATUS_OPEN) -> list[Question]:
    """質問を一覧する。status が None ならすべて返す。"""
    if status is None:
        cur = conn.execute(f"SELECT {_COLUMNS} FROM questions ORDER BY id")
    else:
        cur = conn.execute(
            f"SELECT {_COLUMNS} FROM questions WHERE status = ? ORDER BY id", (status,)
        )
    return [_row_to_question(row) for row in cur.fetchall()]


def resolve_choice(question: Question, choice: str) -> str:
    """回答の指定を選択肢のテキストに解決する。

    選択肢番号(1 始まり)ならその選択肢のテキストを返し、それ以外は自由記述として扱う。
    """
    if choice.isdigit():
        index = int(choice)
        if not (1 <= index <= len(question.options)):
            raise QuestionError(f"choice {index} is out of range (1-{len(question.options)})")
        return question.options[index - 1]
    return choice


def expire(conn: iceql.Connection, qid: int) -> Question:
    """質問を期限切れにする。

    timeout_action が proceed_with_recommended なら推奨案を回答として記録する。
    keep_parked の質問には使えない(open のまま人間の回答を待ち続ける)。
    """
    with db.transaction(conn):
        q = get(conn, qid)
        if q.status != STATUS_OPEN:
            raise QuestionError(f"question {qid} is already {q.status}")
        if q.timeout_action != "proceed_with_recommended":
            raise QuestionError(f"question {qid} must be kept parked until answered")
        conn.execute(
            "UPDATE questions SET status = ?, answer = ?, answer_note = ?, answered_at = ? "
            "WHERE id = ?",
            (STATUS_EXPIRED, q.recommended, "timeout: proceeded with recommended", _now(), qid),
        )
    return get(conn, qid)


def answer(conn: iceql.Connection, qid: int, choice: str, *, note: str | None = None) -> Question:
    """質問に回答する。choice は選択肢番号(1 始まり)または自由記述。"""
    with db.transaction(conn):
        q = get(conn, qid)
        if q.status != STATUS_OPEN:
            raise QuestionError(f"question {qid} is already {q.status}")
        resolved = resolve_choice(q, choice)
        conn.execute(
            "UPDATE questions SET status = ?, answer = ?, answer_note = ?, answered_at = ? "
            "WHERE id = ?",
            (STATUS_ANSWERED, resolved, note, _now(), qid),
        )
    return get(conn, qid)
