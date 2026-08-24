"""回答検知と timeout の処理。

ループドライバの各サイクルの先頭で呼ばれ、
回答が揃った parked の Issue を queued に戻し、期限切れの質問を処理する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import iceql

from alir import control, questions, registry
from alir.questions import Question
from alir.registry import Issue


def expire_timeouts(
    conn: iceql.Connection,
    *,
    timeout: timedelta,
    now: datetime | None = None,
) -> list[Question]:
    """timeout を過ぎた open の質問を期限切れにし、処理した質問を返す。

    期限切れにするのは timeout_action が proceed_with_recommended の質問だけで、
    推奨案が回答として記録される。keep_parked の質問は open のまま残す。
    """
    current = now or datetime.now(UTC)
    expired = []
    for q in questions.list_questions(conn, status=questions.STATUS_OPEN):
        if q.timeout_action != "proceed_with_recommended":
            continue
        created = datetime.fromisoformat(q.created_at)
        if current - created >= timeout:
            expired.append(questions.expire(conn, q.id))
    return expired


def requeue_answered(conn: iceql.Connection) -> list[Issue]:
    """回答が揃った parked の Issue を queued に戻し、戻した Issue を返す。

    「揃った」とは、その Issue の質問に open が残っておらず、
    回答(人間の回答または期限切れの推奨案)が 1 件以上あることを指す。
    park 時のセッション ID があれば再開用に記録し、
    ドライバが `claude -p --resume` で文脈ごと再開できるようにする。
    """
    all_questions = questions.list_questions(conn, status=None)
    requeued = []
    for issue in registry.list_issues(conn, status=registry.STATUS_PARKED):
        related = [q for q in all_questions if q.issue == issue.ref]
        has_open = any(q.status == questions.STATUS_OPEN for q in related)
        has_answer = any(q.answer is not None for q in related)
        if not has_open and has_answer:
            if issue.session_id is not None:
                control.mark_resume_session(conn, issue.ref, issue.session_id)
            requeued.append(registry.set_status(conn, issue.id, registry.STATUS_QUEUED))
    return requeued
