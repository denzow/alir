"""Web UI のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from alir import db, questions
from alir.web import create_app


@pytest.fixture
def dbdir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(dbdir: Path) -> TestClient:
    return TestClient(create_app(dbdir))


def _ask(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    questions.ask(
        conn,
        issue="denzow/alir#1",
        question="DB スキーマを変更してよいか",
        options=["変更する", "変更しない"],
        recommended="変更する",
        impact="high",
        timeout_action="keep_parked",
    )


def test_index_empty(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "no questions" in res.text


def test_index_shows_open_question(client: TestClient, dbdir: Path) -> None:
    _ask(dbdir)
    res = client.get("/")
    assert "DB スキーマを変更してよいか" in res.text
    assert "変更する (推奨)" in res.text


def test_answer_by_option_button(client: TestClient, dbdir: Path) -> None:
    _ask(dbdir)
    res = client.post(
        "/questions/1/answer", data={"choice": "2", "note": "今回は見送り"}, follow_redirects=True
    )
    assert res.status_code == 200
    assert "no questions" in res.text

    conn = db.connect(dbdir)
    q = questions.get(conn, 1)
    assert q.answer == "変更しない"
    assert q.answer_note == "今回は見送り"


def test_answer_by_free_text(client: TestClient, dbdir: Path) -> None:
    _ask(dbdir)
    client.post("/questions/1/answer", data={"choice": "free", "free": "別案で"})
    conn = db.connect(dbdir)
    assert questions.get(conn, 1).answer == "別案で"


def test_answer_empty_choice_shows_error(client: TestClient, dbdir: Path) -> None:
    _ask(dbdir)
    res = client.post(
        "/questions/1/answer", data={"choice": "free", "free": ""}, follow_redirects=True
    )
    assert "choice is empty" in res.text


def test_answered_question_visible_with_all(client: TestClient, dbdir: Path) -> None:
    _ask(dbdir)
    client.post("/questions/1/answer", data={"choice": "1"})
    res = client.get("/?all=1")
    assert "[answered]" in res.text
    assert "A: 変更する" in res.text


URL = "https://github.com/denzow/alir/issues/12"


def test_issues_page_empty(client: TestClient) -> None:
    res = client.get("/issues")
    assert res.status_code == 200
    assert "no issues" in res.text


def test_issues_add_and_list(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    res = client.post(
        "/issues/add",
        data={"url": URL, "workdir": str(workdir), "priority": "5"},
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "denzow/alir#12" in res.text
    assert "[queued]" in res.text

    from alir import registry

    conn = db.connect(dbdir)
    issue = registry.get(conn, 1)
    assert issue.priority == 5


def test_issues_add_rejects_bad_workdir(client: TestClient) -> None:
    res = client.post(
        "/issues/add",
        data={"url": URL, "workdir": "/no/such/dir"},
        follow_redirects=True,
    )
    assert "workdir not found" in res.text


def test_issues_add_rejects_bad_url(client: TestClient, tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    res = client.post(
        "/issues/add",
        data={"url": "https://example.com/x", "workdir": str(workdir)},
        follow_redirects=True,
    )
    assert "not a GitHub issue URL" in res.text


def test_issues_requeue_failed(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry

    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir=str(tmp_path))
    registry.set_status(conn, issue.id, registry.STATUS_FAILED)

    res = client.post(f"/issues/{issue.id}/requeue", follow_redirects=True)
    assert "[queued]" in res.text

    conn = db.connect(dbdir)
    assert registry.get(conn, issue.id).status == registry.STATUS_QUEUED


def test_issues_requeue_rejects_non_failed(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry

    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir=str(tmp_path))

    res = client.post(f"/issues/{issue.id}/requeue", follow_redirects=True)
    assert "not failed" in res.text
