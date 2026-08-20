"""voice の意図解釈ツール(propose → execute の確認フロー)のテスト。

SDK の常駐セッションは使わず、プレーンなツール関数の振る舞いを確かめる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import db, questions, registry, voice_agent
from alir.voice_agent import AgentState


@pytest.fixture
def dbdir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _ask(dbdir: Path) -> questions.Question:
    return questions.ask(
        db.connect(dbdir),
        issue="denzow/alir#1",
        question="DB スキーマを変更してよいか",
        options=["変更する", "変更しない"],
        recommended="変更する",
        impact="high",
        timeout_action="keep_parked",
    )


def test_propose_and_execute_answer(dbdir: Path) -> None:
    q = _ask(dbdir)
    state = AgentState()
    conn = db.connect(dbdir)
    result = voice_agent.tool_propose_answer(
        conn, state, question_id=q.id, choice="1", note="API 層に限定"
    )
    # 番号は選択肢の本文に展開して復唱する
    assert "変更する" in result
    assert state.pending is not None

    result = voice_agent.tool_execute_pending(conn, state)
    assert f"質問 #{q.id}" in result
    assert state.pending is None
    answered = questions.get(db.connect(dbdir), q.id)
    assert answered.status == questions.STATUS_ANSWERED
    assert answered.answer == "変更する"
    assert answered.answer_note == "API 層に限定"


def test_propose_answer_rejects_missing_or_closed(dbdir: Path) -> None:
    state = AgentState()
    conn = db.connect(dbdir)
    result = voice_agent.tool_propose_answer(conn, state, question_id=99, choice="1")
    assert "見つからない" in result
    assert state.pending is None

    q = _ask(dbdir)
    questions.answer(conn, q.id, "1")
    result = voice_agent.tool_propose_answer(conn, state, question_id=q.id, choice="2")
    assert "回答できない" in result
    assert state.pending is None


def test_execute_without_pending(dbdir: Path) -> None:
    result = voice_agent.tool_execute_pending(db.connect(dbdir), AgentState())
    assert "確認待ちの操作はない" in result


def test_cancel_pending(dbdir: Path) -> None:
    q = _ask(dbdir)
    state = AgentState()
    conn = db.connect(dbdir)
    voice_agent.tool_propose_answer(conn, state, question_id=q.id, choice="1")
    result = voice_agent.tool_cancel_pending(state)
    assert "取り消した" in result
    assert state.pending is None
    # 取り消し後の execute は何もしない
    assert "確認待ちの操作はない" in voice_agent.tool_execute_pending(conn, state)
    assert questions.get(conn, q.id).status == questions.STATUS_OPEN


def test_propose_issue_resolves_number_from_known_repo(
    dbdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(dbdir)
    registry.add(conn, url="https://github.com/denzow/alir/issues/12", workdir="/tmp/a")
    state = AgentState()
    result = voice_agent.tool_propose_issue(conn, state, url_or_number="45", note="API 層に限定")
    assert "https://github.com/denzow/alir/issues/45" in result
    assert state.pending is not None
    assert state.pending.payload["workdir"] == "/tmp/a"

    monkeypatch.setattr(voice_agent.registry, "fetch_title", lambda url: "音声対応")
    result = voice_agent.tool_execute_pending(conn, state)
    assert "denzow/alir#45" in result
    added = [i for i in registry.list_issues(db.connect(dbdir)) if i.number == 45]
    assert len(added) == 1
    assert added[0].note == "API 層に限定"
    assert added[0].title == "音声対応"


def test_propose_issue_needs_repo_when_ambiguous(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    state = AgentState()
    # 既知のリポジトリがない
    result = voice_agent.tool_propose_issue(conn, state, url_or_number="45")
    assert state.pending is None
    assert "特定できない" in result
    # 複数のリポジトリがある
    registry.add(conn, url="https://github.com/denzow/alir/issues/1", workdir="/tmp/a")
    registry.add(conn, url="https://github.com/denzow/iceql/issues/2", workdir="/tmp/b")
    result = voice_agent.tool_propose_issue(conn, state, url_or_number="45")
    assert state.pending is None
    assert "特定できない" in result


def test_propose_issue_rejects_non_url_text(dbdir: Path) -> None:
    state = AgentState()
    result = voice_agent.tool_propose_issue(
        db.connect(dbdir), state, url_or_number="よんじゅうご"
    )
    assert state.pending is None
    assert "番号か GitHub の URL" in result


def test_list_questions_and_session_status(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    assert "未回答の質問はない" in voice_agent.tool_list_questions(conn)
    assert "登録された Issue はない" in voice_agent.tool_session_status(conn)

    q = _ask(dbdir)
    listed = voice_agent.tool_list_questions(conn)
    assert f"#{q.id}" in listed
    assert "変更する(推奨)" in listed

    registry.add(conn, url="https://github.com/denzow/alir/issues/12", workdir="/tmp/a")
    status = voice_agent.tool_session_status(conn)
    assert "queued が 1 件" in status
