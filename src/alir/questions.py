"""質問の登録・一覧・回答・差し戻し。

質問は questions テーブルの 1 行として保存する。
選択肢(options)は JSON 配列のテキストとして格納する。

回答者は回答の代わりに確認(逆質問)を返せる(return_question)。差し戻された質問は
returned になり、再開したセッションが parent_id で元の質問に紐づけて質問を登録し直す。
この親子の連なりをスレッドとして一覧できる(list_threads)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import iceql

from alir import db

IMPACTS = ("high", "low")
TIMEOUT_ACTIONS = ("proceed_with_recommended", "keep_parked")

STATUS_OPEN = "open"
STATUS_ANSWERED = "answered"
STATUS_EXPIRED = "expired"
STATUS_RETURNED = "returned"

_COLUMNS = (
    "id, issue, session_id, question, options, recommended, impact, "
    "timeout_action, status, answer, answer_note, created_at, answered_at, parent_id"
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
    parent_id: int | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
        parent_id,
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
        parent_id=None if parent_id is None else int(str(parent_id)),
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
    parent_id: int | None = None,
) -> Question:
    """質問を登録し、登録した質問を返す。

    parent_id は差し戻された質問への再質問で使い、元の質問に紐づける。
    親は同じ Issue の質問でなければならない。
    """
    if not (2 <= len(options) <= 4):
        raise QuestionError("options must have 2 to 4 items")
    if recommended not in options:
        raise QuestionError("recommended must be one of options")
    if impact not in IMPACTS:
        raise QuestionError(f"impact must be one of {IMPACTS}")
    if timeout_action not in TIMEOUT_ACTIONS:
        raise QuestionError(f"timeout_action must be one of {TIMEOUT_ACTIONS}")
    if parent_id is not None:
        parent = get(conn, parent_id)
        if parent.issue != issue:
            raise QuestionError(f"parent question {parent_id} belongs to another issue")

    cur = conn.execute(
        f"INSERT INTO questions ({_INSERT_COLUMNS}) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
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
            parent_id,
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


def return_question(conn: iceql.Connection, qid: int, text: str) -> Question:
    """質問に回答せず、確認(逆質問)を返して差し戻す。

    確認テキストは answer 列に記録する。差し戻しも「回答が付いた」とみなされ、
    parked の Issue は既存の回答検知(resume.requeue_answered)で queued に戻る。
    再開したセッションは確認に答える内容を含めて質問を登録し直す。
    """
    text = text.strip()
    if not text:
        raise QuestionError("clarification text is empty")
    with db.transaction(conn):
        q = get(conn, qid)
        if q.status != STATUS_OPEN:
            raise QuestionError(f"question {qid} is already {q.status}")
        conn.execute(
            "UPDATE questions SET status = ?, answer = ?, answered_at = ? WHERE id = ?",
            (STATUS_RETURNED, text, _now(), qid),
        )
    return get(conn, qid)


def list_threads(conn: iceql.Connection, *, only_active: bool = True) -> list[list[Question]]:
    """質問を parent_id の連なりでまとめたスレッドの一覧を返す。

    ルートは parent_id が無い(または親が見当たらない)質問で、各スレッドは
    ルートから id 昇順に子孫を辿った列。only_active なら末尾が open の
    スレッドだけを返す(未回答の一覧に相当する)。
    """
    items = list_questions(conn, status=None)
    ids = {q.id for q in items}
    children: dict[int, list[Question]] = {}
    roots = []
    for q in items:
        if q.parent_id is None or q.parent_id not in ids:
            roots.append(q)
        else:
            children.setdefault(q.parent_id, []).append(q)

    def collect(q: Question) -> list[Question]:
        thread = [q]
        for child in sorted(children.get(q.id, []), key=lambda c: c.id):
            thread.extend(collect(child))
        return thread

    threads = [collect(root) for root in roots]
    if only_active:
        threads = [t for t in threads if t[-1].status == STATUS_OPEN]
    return threads
