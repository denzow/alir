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


def test_issues_reorder(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry

    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir=str(tmp_path))
    registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir=str(tmp_path))
    res = client.post("/issues/reorder", data={"order": "2,1"}, follow_redirects=True)
    assert res.status_code == 200

    conn = db.connect(dbdir)
    items = [i.id for i in registry.list_issues(conn)]
    assert items == [2, 1]


def test_issues_reorder_rejects_mismatch(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry

    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir=str(tmp_path))
    registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir=str(tmp_path))
    res = client.post("/issues/reorder", data={"order": "1"}, follow_redirects=True)
    assert "order must contain exactly" in res.text


def test_queue_cards_are_draggable(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry

    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir=str(tmp_path))
    done = registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir=str(tmp_path))
    registry.set_status(conn, done.id, registry.STATUS_DONE)
    res = client.get("/issues")
    assert 'data-issue-id="1"' in res.text
    assert 'data-issue-id="2"' not in res.text
    assert "drag-handle" in res.text


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
    assert res.status_code == 200
    assert 'card st-failed' not in res.text  # requeue されて history から消える

    conn = db.connect(dbdir)
    assert registry.get(conn, issue.id).status == registry.STATUS_QUEUED


def test_history_page_shows_finished_newest_first(
    client: TestClient, dbdir: Path, tmp_path: Path
) -> None:
    from alir import registry

    conn = db.connect(dbdir)
    a = registry.add(conn, url=URL, workdir=str(tmp_path))
    b = registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir=str(tmp_path))
    registry.set_status(conn, a.id, registry.STATUS_DONE)
    import time as time_mod

    time_mod.sleep(1.1)  # updated_at が秒精度のため
    registry.set_status(conn, b.id, registry.STATUS_FAILED)

    res = client.get("/history")
    assert res.status_code == 200
    assert res.text.index("denzow/alir#13") < res.text.index("denzow/alir#12")
    assert "キューに戻して再実行" in res.text


def test_issues_page_excludes_finished(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry

    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir=str(tmp_path))
    registry.set_status(conn, issue.id, registry.STATUS_DONE)
    res = client.get("/issues")
    assert 'card st-done' not in res.text
    assert "登録された Issue はありません" in res.text


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


def test_localtime_filter_converts_utc_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_mod
    from datetime import datetime, timezone

    from alir.web import _localtime

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time_mod.tzset()
    try:
        assert _localtime("2026-08-01T00:30:00+00:00") == "08-01 09:30"
        assert _localtime(datetime(2026, 8, 1, 0, 30, tzinfo=timezone.utc)) == "08-01 09:30"
    finally:
        monkeypatch.undo()
        time_mod.tzset()


def test_issues_add_success_sets_trigger_header(client: TestClient, tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    res = client.post(
        "/issues/add",
        data={"url": URL, "workdir": str(workdir)},
        headers={"HX-Request": "true"},
    )
    assert res.headers.get("HX-Trigger") == "issue-added"


def test_issues_add_error_has_no_trigger_header(client: TestClient) -> None:
    res = client.post(
        "/issues/add",
        data={"url": URL, "workdir": "/no/such/dir"},
        headers={"HX-Request": "true"},
    )
    assert "HX-Trigger" not in res.headers


def test_sessions_page_empty(client: TestClient, dbdir: Path) -> None:
    res = client.get("/sessions")
    assert res.status_code == 200
    assert "実行中のセッションはありません" in res.text


def test_sessions_page_shows_running_with_progress(
    client: TestClient, dbdir: Path, tmp_path: Path
) -> None:
    from alir import progress, registry

    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir=str(tmp_path), title="設定の TOML 移行")
    registry.set_status(conn, issue.id, registry.STATUS_RUNNING)
    progress.add(conn, issue="denzow/alir#12", message="調査完了、実装に着手")

    res = client.get("/sessions")
    assert "設定の TOML 移行" in res.text
    assert "調査完了、実装に着手" in res.text
    assert "分経過" in res.text

    res = client.get("/sessions", headers={"HX-Request": "true"})
    assert "<html" not in res.text
    assert "調査完了、実装に着手" in res.text


def test_settings_page_shows_current_template(client: TestClient, dbdir: Path) -> None:
    res = client.get("/settings")
    assert res.status_code == 200
    assert 'value="alir/issue-{number}"' in res.text


def test_settings_save_valid_template(client: TestClient, dbdir: Path) -> None:
    from alir import settings

    res = client.post(
        "/settings",
        data={"branch_template": "feature/{repo}-{number}"},
        headers={"HX-Request": "true"},
    )
    assert "保存しました" in res.text
    conn = db.connect(dbdir)
    assert settings.branch_template(conn) == "feature/{repo}-{number}"


def test_settings_save_invalid_template_keeps_current(client: TestClient, dbdir: Path) -> None:
    from alir import settings

    res = client.post(
        "/settings",
        data={"branch_template": "static-name"},
        headers={"HX-Request": "true"},
    )
    assert "must contain" in res.text
    conn = db.connect(dbdir)
    assert settings.branch_template(conn) == "alir/issue-{number}"


def test_loop_page_shows_usage_windows(client: TestClient, dbdir: Path) -> None:
    from alir import control

    conn = db.connect(dbdir)
    control.set_value(conn, control.KEY_USAGE_STATUS, '[["session", 19.0], ["week (Fable)", 54.0]]')
    res = client.get("/loop")
    assert "week (Fable)" in res.text
    assert "54%" in res.text


def test_sessions_page_shows_recent_reports(client: TestClient, dbdir: Path) -> None:
    from alir import reports

    conn = db.connect(dbdir)
    for i in range(6):
        reports.add(conn, issue=f"denzow/alir#{i}", summary=f"報告{i}")
    reports.add(
        conn,
        issue="denzow/alir#9",
        summary="リファインメント実施",
        outcome="refined",
        pr_url=None,
    )
    res = client.get("/sessions")
    assert "直近の報告" in res.text
    assert "リファインメント実施" in res.text
    assert "報告5" in res.text
    assert "報告1" not in res.text  # 直近 5 件に含まれない
    assert "st-open" in res.text  # refined はアンバー表示
