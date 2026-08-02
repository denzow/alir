"""回答検知と timeout 処理のテスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alir import db, driver, questions, registry, resume
from alir.driver import RunResult

URL = "https://github.com/denzow/alir/issues/12"
REF = "denzow/alir#12"


@pytest.fixture
def dbdir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _ask(conn, timeout_action: str = "keep_parked"):  # type: ignore[no-untyped-def]
    return questions.ask(
        conn,
        issue=REF,
        question="スキーマを変えてよいか",
        options=["変える", "変えない"],
        recommended="変える",
        impact="high",
        timeout_action=timeout_action,
    )


def test_expire_timeouts_proceeds_with_recommended(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    q = _ask(conn, timeout_action="proceed_with_recommended")
    future = datetime.now(timezone.utc) + timedelta(hours=13)
    expired = resume.expire_timeouts(conn, timeout=timedelta(hours=12), now=future)
    assert [e.id for e in expired] == [q.id]
    got = questions.get(conn, q.id)
    assert got.status == questions.STATUS_EXPIRED
    assert got.answer == "変える"


def test_expire_timeouts_keeps_parked_questions_open(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    q = _ask(conn, timeout_action="keep_parked")
    future = datetime.now(timezone.utc) + timedelta(hours=100)
    assert resume.expire_timeouts(conn, timeout=timedelta(hours=12), now=future) == []
    assert questions.get(conn, q.id).status == questions.STATUS_OPEN


def test_expire_timeouts_skips_recent_questions(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    q = _ask(conn, timeout_action="proceed_with_recommended")
    assert resume.expire_timeouts(conn, timeout=timedelta(hours=12)) == []
    assert questions.get(conn, q.id).status == questions.STATUS_OPEN


def test_requeue_answered_moves_parked_to_queued(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp")
    registry.set_status(conn, issue.id, registry.STATUS_PARKED)
    q = _ask(conn)
    questions.answer(conn, q.id, "1")
    requeued = resume.requeue_answered(conn)
    assert [i.id for i in requeued] == [issue.id]
    assert registry.get(conn, issue.id).status == registry.STATUS_QUEUED


def test_requeue_skips_while_question_open(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp")
    registry.set_status(conn, issue.id, registry.STATUS_PARKED)
    _ask(conn)
    assert resume.requeue_answered(conn) == []
    assert registry.get(conn, issue.id).status == registry.STATUS_PARKED


def test_requeue_skips_parked_without_answers(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp")
    registry.set_status(conn, issue.id, registry.STATUS_PARKED)
    assert resume.requeue_answered(conn) == []


def test_requeue_after_expire(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp")
    registry.set_status(conn, issue.id, registry.STATUS_PARKED)
    _ask(conn, timeout_action="proceed_with_recommended")
    future = datetime.now(timezone.utc) + timedelta(hours=13)
    resume.expire_timeouts(conn, timeout=timedelta(hours=12), now=future)
    requeued = resume.requeue_answered(conn)
    assert [i.id for i in requeued] == [issue.id]


def test_run_loop_requeues_and_processes_answered_parked(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp")
    registry.set_status(conn, issue.id, registry.STATUS_PARKED)
    q = _ask(conn)
    questions.answer(conn, q.id, "2", note="互換性を守る")

    prompts: list[str] = []

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        from alir import reports

        prompts.append(prompt)
        conn = db.connect(dbdir)
        reports.add(conn, issue="denzow/alir#12", summary="実装済み", outcome="implemented")
        return RunResult(exit_code=0, session_id="sess-2", output="ok")

    def worktree(issue: registry.Issue, branch: str) -> Path:
        return Path("/tmp/wt")

    driver.run_loop(dbdir, once=True, runner=runner, worktree=worktree, log=lambda _: None)
    conn = db.connect(dbdir)
    assert registry.get(conn, issue.id).status == registry.STATUS_DONE
    assert "変えない" in prompts[0]
