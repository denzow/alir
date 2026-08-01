"""Web UI のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from alir import db, questions
from alir.web import create_app


@pytest.fixture(autouse=True)
def _no_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("alir.registry.fetch_title", lambda url: "タイトル取得テスト")


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
    assert "未回答の質問はありません" in res.text


def test_index_shows_open_question(client: TestClient, dbdir: Path) -> None:
    _ask(dbdir)
    res = client.get("/")
    assert "DB スキーマを変更してよいか" in res.text
    assert "変更する" in res.text
    assert "推奨" in res.text


def test_answer_by_option_button(client: TestClient, dbdir: Path) -> None:
    _ask(dbdir)
    res = client.post(
        "/questions/1/answer", data={"choice": "2", "note": "今回は見送り"}, follow_redirects=True
    )
    assert res.status_code == 200
    assert "未回答の質問はありません" in res.text

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
    assert "st-answered" in res.text
    assert "変更する" in res.text


URL = "https://github.com/denzow/alir/issues/12"


def test_issues_page_empty(client: TestClient) -> None:
    res = client.get("/issues")
    assert res.status_code == 200
    assert "登録された Issue はありません" in res.text


def test_issues_add_and_list(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    res = client.post(
        "/issues/add",
        data={"url": URL, "workdir": str(workdir)},
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "denzow/alir#12" in res.text
    assert "st-queued" in res.text
    assert "タイトル取得テスト" in res.text

    from alir import registry

    conn = db.connect(dbdir)
    issue = registry.get(conn, 1)
    assert issue.title == "タイトル取得テスト"


def test_workdir_datalist(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry

    workdir = tmp_path / "repo"
    workdir.mkdir()
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir=str(workdir))
    res = client.get("/issues")
    assert f'<option value="{workdir}">' in res.text


def test_issues_move(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry

    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir=str(tmp_path))
    registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir=str(tmp_path))
    res = client.post("/issues/2/move/up", follow_redirects=True)
    assert res.status_code == 200

    conn = db.connect(dbdir)
    items = [i.id for i in registry.list_issues(conn)]
    assert items == [2, 1]


def test_issues_move_rejects_done(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry

    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir=str(tmp_path))
    registry.set_status(conn, issue.id, registry.STATUS_DONE)
    res = client.post(f"/issues/{issue.id}/move/up", follow_redirects=True)
    assert "can be moved" in res.text


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
    assert "st-queued" in res.text

    conn = db.connect(dbdir)
    assert registry.get(conn, issue.id).status == registry.STATUS_QUEUED


def test_issues_requeue_rejects_non_failed(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry

    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir=str(tmp_path))

    res = client.post(f"/issues/{issue.id}/requeue", follow_redirects=True)
    assert "not failed" in res.text


def test_htmx_answer_returns_fragment(client: TestClient, dbdir: Path) -> None:
    _ask(dbdir)
    res = client.post(
        "/questions/1/answer",
        data={"choice": "1", "show_all": ""},
        headers={"HX-Request": "true"},
    )
    assert res.status_code == 200
    assert "<html" not in res.text
    assert "未回答の質問はありません" in res.text


def test_htmx_answer_error_shown_in_fragment(client: TestClient, dbdir: Path) -> None:
    _ask(dbdir)
    res = client.post(
        "/questions/1/answer",
        data={"choice": "9"},
        headers={"HX-Request": "true"},
    )
    assert "<html" not in res.text
    assert "out of range" in res.text
    assert "DB スキーマを変更してよいか" in res.text


def test_htmx_issues_add_returns_fragment(client: TestClient, tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    res = client.post(
        "/issues/add",
        data={"url": URL, "workdir": str(workdir)},
        headers={"HX-Request": "true"},
    )
    assert "<html" not in res.text
    assert "denzow/alir#12" in res.text


def test_static_htmx_served(client: TestClient) -> None:
    res = client.get("/static/htmx.min.js")
    assert res.status_code == 200
    assert "htmx" in res.text[:200]


def test_loop_page_shows_stopped(client: TestClient, dbdir: Path) -> None:
    res = client.get("/loop")
    assert res.status_code == 200
    assert "stopped" in res.text
    assert "一時停止" in res.text


def test_loop_pause_and_resume(client: TestClient, dbdir: Path) -> None:
    from alir import control

    res = client.post("/loop/pause", headers={"HX-Request": "true"})
    assert "<html" not in res.text
    assert "paused" in res.text
    conn = db.connect(dbdir)
    assert control.is_paused(conn) is True

    res = client.post("/loop/resume", follow_redirects=True)
    assert res.status_code == 200
    conn = db.connect(dbdir)
    assert control.is_paused(conn) is False


def test_loop_page_shows_events(client: TestClient, dbdir: Path) -> None:
    from alir import control

    conn = db.connect(dbdir)
    control.log_event(conn, "start #1 denzow/alir#12")
    res = client.get("/loop")
    assert "start #1 denzow/alir#12" in res.text


def test_issue_card_shows_latest_report(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry, reports

    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir=str(tmp_path))
    reports.add(conn, issue="denzow/alir#12", summary="実装して PR を作成")
    reports.add(
        conn,
        issue="denzow/alir#12",
        summary="レビュー指摘を反映",
        pr_url="https://github.com/denzow/alir/pull/5",
    )
    res = client.get("/issues")
    assert "レビュー指摘を反映" in res.text
    assert "実装して PR を作成" not in res.text
    assert 'href="https://github.com/denzow/alir/pull/5"' in res.text
