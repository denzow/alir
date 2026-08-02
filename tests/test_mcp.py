"""ask_human MCP ツールと通知のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import db, mcp_server, notify, questions
from alir.questions import QuestionError


@pytest.fixture
def conn(tmp_path: Path):  # type: ignore[no-untyped-def]
    return db.connect(tmp_path / "data")


def _params():  # type: ignore[no-untyped-def]
    return {
        "issue": "denzow/alir#1",
        "question": "スキーマを変えてよいか",
        "options": ["変える", "変えない"],
        "recommended": "変える",
        "impact": "high",
        "timeout_action": "keep_parked",
    }


def test_ask_human_tool_registers_and_notifies(conn, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    notified = []
    monkeypatch.setattr(notify, "notify_question", notified.append)
    result = mcp_server.ask_human_tool(conn, **_params())
    assert result["question_id"] == 1
    assert result["status"] == questions.STATUS_OPEN
    assert "Do not wait" in result["message"]
    assert [q.id for q in notified] == [1]


def test_ask_human_tool_rejects_invalid_input(conn) -> None:  # type: ignore[no-untyped-def]
    params = _params()
    params["impact"] = "medium"
    with pytest.raises(QuestionError):
        mcp_server.ask_human_tool(conn, **params)


def test_enqueue_issue_tool_queues_with_inherited_workdir(conn, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from alir import control, registry

    monkeypatch.setattr(registry, "fetch_title", lambda url: "後続のタイトル")
    registry.add(conn, url="https://github.com/denzow/alir/issues/1", workdir="/tmp/repo")

    result = mcp_server.enqueue_issue_tool(
        conn,
        url="https://github.com/denzow/alir/issues/2",
        source_issue="denzow/alir#1",
        mode=registry.MODE_REFINE,
        note="仕様から詰める",
    )
    assert result["message"] == "Queued."
    issue = registry.get(conn, result["issue_id"])
    assert issue.status == registry.STATUS_QUEUED
    assert issue.workdir == "/tmp/repo"
    assert issue.mode == registry.MODE_REFINE
    assert issue.note == "仕様から詰める"
    assert issue.origin == "denzow/alir#1"
    assert issue.title == "後続のタイトル"
    assert any("enqueued" in e.message for e in control.recent_events(conn))


def test_enqueue_issue_tool_prefers_given_title(conn, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from alir import registry

    def no_gh(url):  # type: ignore[no-untyped-def]
        raise AssertionError("must not call gh when title is given")

    monkeypatch.setattr(registry, "fetch_title", no_gh)
    registry.add(conn, url="https://github.com/denzow/alir/issues/1", workdir="/tmp/repo")

    result = mcp_server.enqueue_issue_tool(
        conn,
        url="https://github.com/denzow/alir/issues/2",
        source_issue="denzow/alir#1",
        title="渡されたタイトル",
    )
    assert registry.get(conn, result["issue_id"]).title == "渡されたタイトル"


def test_enqueue_issue_tool_rejects_unknown_source(conn) -> None:  # type: ignore[no-untyped-def]
    from alir import registry

    with pytest.raises(registry.RegistryError):
        mcp_server.enqueue_issue_tool(
            conn,
            url="https://github.com/denzow/alir/issues/2",
            source_issue="denzow/alir#99",
        )


def test_enqueue_issue_tool_rejects_duplicate(conn, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from alir import registry

    monkeypatch.setattr(registry, "fetch_title", lambda url: None)
    registry.add(conn, url="https://github.com/denzow/alir/issues/1", workdir="/tmp/repo")

    with pytest.raises(registry.RegistryError):
        mcp_server.enqueue_issue_tool(
            conn,
            url="https://github.com/denzow/alir/issues/1",
            source_issue="denzow/alir#1",
        )


def test_notify_question_swallows_channel_errors(conn, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    q = questions.ask(conn, **_params())

    def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(notify, "send_desktop", broken)
    monkeypatch.setattr(notify, "send_pushover", broken)
    notify.notify_question(q)


def test_send_pushover_noop_without_credentials(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(notify.ENV_PUSHOVER_TOKEN, raising=False)
    monkeypatch.delenv(notify.ENV_PUSHOVER_USER, raising=False)

    def must_not_call(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("urlopen must not be called")

    monkeypatch.setattr("urllib.request.urlopen", must_not_call)
    notify.send_pushover("hello", url=None)


def test_build_message(conn) -> None:  # type: ignore[no-untyped-def]
    q = questions.ask(conn, **_params())
    assert notify.build_message(q) == "#1 denzow/alir#1: スキーマを変えてよいか"
