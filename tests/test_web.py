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


def test_issues_add_with_mode_and_note(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry

    workdir = tmp_path / "repo"
    workdir.mkdir()
    res = client.post(
        "/issues/add",
        data={
            "url": URL,
            "workdir": str(workdir),
            "mode": "refine",
            "note": "仕様だけ詰めてほしい",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "リファインメントのみ" in res.text
    assert "仕様だけ詰めてほしい" in res.text

    conn = db.connect(dbdir)
    issue = registry.get(conn, 1)
    assert issue.mode == registry.MODE_REFINE
    assert issue.note == "仕様だけ詰めてほしい"


def test_issues_add_rejects_unknown_mode(client: TestClient, tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    res = client.post(
        "/issues/add",
        data={"url": URL, "workdir": str(workdir), "mode": "review"},
        follow_redirects=True,
    )
    assert "mode must be one of" in res.text


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
    assert "card st-failed" not in res.text  # requeue されて history から消える

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
    assert "card st-done" not in res.text
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


def test_settings_page_shows_import_targets(
    client: TestClient, dbdir: Path, tmp_path: Path
) -> None:
    from alir import importer

    workdir = tmp_path / "repo"
    workdir.mkdir()
    importer.add_target(db.connect(dbdir), workdir=str(workdir), label="alir")
    res = client.get("/settings")
    assert "Issue の取り込み" in res.text
    assert "alir" in res.text
    assert str(workdir.resolve()) in res.text


def test_settings_targets_add(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import importer

    workdir = tmp_path / "repo"
    workdir.mkdir()
    res = client.post(
        "/settings/targets/add",
        data={"workdir": str(workdir), "label": "alir"},
        headers={"HX-Request": "true"},
    )
    assert res.status_code == 200
    targets = importer.list_targets(db.connect(dbdir))
    assert [(t.workdir, t.label) for t in targets] == [(str(workdir.resolve()), "alir")]


def test_settings_targets_add_missing_workdir_shows_error(
    client: TestClient, dbdir: Path, tmp_path: Path
) -> None:
    from alir import importer

    res = client.post(
        "/settings/targets/add",
        data={"workdir": str(tmp_path / "missing"), "label": "alir"},
        headers={"HX-Request": "true"},
    )
    assert "workdir not found" in res.text
    assert importer.list_targets(db.connect(dbdir)) == []


def test_settings_targets_remove(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import importer

    workdir = tmp_path / "repo"
    workdir.mkdir()
    importer.add_target(db.connect(dbdir), workdir=str(workdir), label="alir")
    res = client.post(
        "/settings/targets/remove",
        data={"workdir": str(workdir.resolve()), "label": "alir"},
        headers={"HX-Request": "true"},
    )
    assert res.status_code == 200
    assert importer.list_targets(db.connect(dbdir)) == []


def _fake_fetch(monkeypatch: pytest.MonkeyPatch, items: list[dict[str, str]]) -> list[str]:
    """gh の検索を差し替え、検索した workdir を記録するリストを返す。"""
    from alir import importer

    searched: list[str] = []

    def fetch(workdir: str, label: str) -> list[dict[str, str]]:
        searched.append(workdir)
        return items

    monkeypatch.setattr(importer, "fetch_labeled_issues", fetch)
    return searched


def test_settings_targets_import(
    client: TestClient, dbdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alir import control, importer, registry

    workdir = tmp_path / "repo"
    workdir.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    conn = db.connect(dbdir)
    importer.add_target(conn, workdir=str(workdir), label="alir")
    importer.add_target(conn, workdir=str(other), label="alir")
    url = "https://github.com/denzow/alir/issues/12"
    searched = _fake_fetch(monkeypatch, [{"url": url, "title": "取り込む Issue"}])

    res = client.post(
        "/settings/targets/import",
        data={"workdir": str(workdir.resolve()), "label": "alir"},
        headers={"HX-Request": "true"},
    )
    assert res.status_code == 200
    assert "1 件を取り込みました" in res.text
    # ボタンを押した対象だけを検索する
    assert searched == [str(workdir.resolve())]
    conn = db.connect(dbdir)
    items = registry.list_issues(conn)
    assert [(i.url, i.status) for i in items] == [(url, registry.STATUS_QUEUED)]
    assert any("import #1" in e.message for e in control.recent_events(conn))


def test_settings_targets_import_unknown_target_shows_error(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    searched = _fake_fetch(monkeypatch, [])
    res = client.post(
        "/settings/targets/import",
        data={"workdir": str(workdir), "label": "alir"},
        headers={"HX-Request": "true"},
    )
    assert "target not registered" in res.text
    assert searched == []


def test_settings_targets_import_all(
    client: TestClient, dbdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alir import importer, registry

    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    conn = db.connect(dbdir)
    importer.add_target(conn, workdir=str(first), label="alir")
    importer.add_target(conn, workdir=str(second), label="alir")
    searched = _fake_fetch(monkeypatch, [{"url": "https://github.com/denzow/alir/issues/12"}])

    res = client.post("/settings/targets/import-all", headers={"HX-Request": "true"})
    assert res.status_code == 200
    assert searched == [str(first.resolve()), str(second.resolve())]
    # 同じ URL は 2 対象目でスキップされる
    assert "2 件が該当し、うち 1 件を取り込みました" in res.text
    assert len(registry.list_issues(db.connect(dbdir))) == 1


def test_settings_targets_import_shows_errors_with_count(
    client: TestClient, dbdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一部の対象が失敗しても、取り込めた件数はエラーと一緒に表示する。"""
    from alir import importer, registry

    broken = tmp_path / "broken"
    broken.mkdir()
    ok = tmp_path / "ok"
    ok.mkdir()
    conn = db.connect(dbdir)
    importer.add_target(conn, workdir=str(broken), label="alir")
    importer.add_target(conn, workdir=str(ok), label="alir")

    def fetch(wd: str, label: str) -> list[dict[str, str]]:
        if wd == str(broken.resolve()):
            raise importer.ImporterError("gh issue list failed: boom")
        return [{"url": "https://github.com/denzow/alir/issues/12"}]

    monkeypatch.setattr(importer, "fetch_labeled_issues", fetch)
    res = client.post("/settings/targets/import-all", headers={"HX-Request": "true"})
    assert "boom" in res.text
    assert "うち 1 件を取り込みました" in res.text
    assert len(registry.list_issues(db.connect(dbdir))) == 1


def test_settings_targets_interval_set_and_disable(client: TestClient, dbdir: Path) -> None:
    from alir import importer

    res = client.post(
        "/settings/targets/interval", data={"seconds": "300"}, headers={"HX-Request": "true"}
    )
    assert res.status_code == 200
    assert importer.import_interval(db.connect(dbdir)) == 300.0

    res = client.post(
        "/settings/targets/interval", data={"seconds": "0"}, headers={"HX-Request": "true"}
    )
    assert importer.import_interval(db.connect(dbdir)) == 0.0
    assert "無効にしました" in res.text


def test_settings_targets_interval_rejects_too_short(client: TestClient, dbdir: Path) -> None:
    from alir import importer

    res = client.post(
        "/settings/targets/interval", data={"seconds": "5"}, headers={"HX-Request": "true"}
    )
    assert "interval must be" in res.text
    assert importer.import_interval(db.connect(dbdir)) == 0.0


def test_settings_page_shows_push_branches(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import settings

    workdir = tmp_path / "repo"
    workdir.mkdir()
    settings.set_push_branch(db.connect(dbdir), workdir=str(workdir), branch="develop")
    res = client.get("/settings")
    assert "直接 push" in res.text
    assert "develop" in res.text
    assert str(workdir.resolve()) in res.text


def test_settings_push_branches_set(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import settings

    workdir = tmp_path / "repo"
    workdir.mkdir()
    res = client.post(
        "/settings/push-branches/set",
        data={"workdir": str(workdir), "branch": "develop"},
        headers={"HX-Request": "true"},
    )
    assert res.status_code == 200
    assert settings.push_branches(db.connect(dbdir)) == {str(workdir.resolve()): "develop"}


def test_settings_push_branches_set_invalid_branch_shows_error(
    client: TestClient, dbdir: Path, tmp_path: Path
) -> None:
    from alir import settings

    workdir = tmp_path / "repo"
    workdir.mkdir()
    res = client.post(
        "/settings/push-branches/set",
        data={"workdir": str(workdir), "branch": "bad..name"},
        headers={"HX-Request": "true"},
    )
    assert "invalid branch name" in res.text
    assert settings.push_branches(db.connect(dbdir)) == {}


def test_settings_push_branches_unset(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import settings

    workdir = tmp_path / "repo"
    workdir.mkdir()
    settings.set_push_branch(db.connect(dbdir), workdir=str(workdir), branch="develop")
    res = client.post(
        "/settings/push-branches/unset",
        data={"workdir": str(workdir.resolve())},
        headers={"HX-Request": "true"},
    )
    assert res.status_code == 200
    assert settings.push_branches(db.connect(dbdir)) == {}


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


def test_sessions_page_excludes_previous_session_progress(
    client: TestClient, dbdir: Path, tmp_path: Path
) -> None:
    """同じ Issue の過去セッションの進捗は表示しない。"""
    import time as time_mod

    from alir import progress, registry

    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir=str(tmp_path))
    progress.add(conn, issue="denzow/alir#12", message="前回セッションの進捗")
    time_mod.sleep(1.1)  # created_at / updated_at が秒精度のため
    registry.set_status(conn, issue.id, registry.STATUS_RUNNING)
    progress.add(conn, issue="denzow/alir#12", message="今回セッションの進捗")

    res = client.get("/sessions")
    assert "今回セッションの進捗" in res.text
    assert "前回セッションの進捗" not in res.text


def test_history_reports_scoped_to_each_registration(
    client: TestClient, dbdir: Path, tmp_path: Path
) -> None:
    """同じ Issue を複数回登録しても、報告は各登録の実行期間内のものだけ紐づく。"""
    import time as time_mod

    from alir import registry, reports

    conn = db.connect(dbdir)
    first = registry.add(conn, url=URL, workdir=str(tmp_path))
    reports.add(conn, issue="denzow/alir#12", summary="1回目の実行報告")
    registry.set_status(conn, first.id, registry.STATUS_DONE)
    time_mod.sleep(1.1)  # created_at / updated_at が秒精度のため

    second = registry.add(conn, url=URL, workdir=str(tmp_path))
    registry.set_status(conn, second.id, registry.STATUS_DONE)

    res = client.get("/history")
    # 1 回目の報告は 1 回目のカードにだけ表示される
    assert res.text.count("1回目の実行報告") == 1


def test_issues_requeue_resets_retry_state(client: TestClient, dbdir: Path, tmp_path: Path) -> None:
    from alir import registry, retry
    from alir.control import get_value

    conn = db.connect(dbdir)
    issue = registry.add(conn, url="https://github.com/denzow/alir/issues/9", workdir=str(tmp_path))
    registry.set_status(conn, issue.id, registry.STATUS_FAILED, retries=2)
    retry.process_failed(conn, limit=2, notifier=lambda i, n: None)  # 通知済みの印を付ける

    res = client.post(f"/issues/{issue.id}/requeue", follow_redirects=True)
    assert res.status_code == 200

    conn = db.connect(dbdir)
    requeued = registry.get(conn, issue.id)
    assert requeued.status == registry.STATUS_QUEUED
    assert requeued.retries == 0
    assert get_value(conn, f"retry_exhausted_notified:{requeued.ref}") is None


@pytest.fixture
def _no_pushover_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from alir import notify

    monkeypatch.delenv(notify.ENV_PUSHOVER_TOKEN, raising=False)
    monkeypatch.delenv(notify.ENV_PUSHOVER_USER, raising=False)


@pytest.mark.usefixtures("_no_pushover_env")
def test_settings_page_shows_pushover_not_set(client: TestClient, dbdir: Path) -> None:
    res = client.get("/settings")
    assert "Pushover はまだ設定されていません" in res.text


@pytest.mark.usefixtures("_no_pushover_env")
def test_settings_pushover_set_and_clear(client: TestClient, dbdir: Path) -> None:
    from alir import settings

    res = client.post(
        "/settings/pushover/set",
        data={"token": "app-token", "user": "user-key"},
        headers={"HX-Request": "true"},
    )
    assert "設定済み(この画面で保存)" in res.text
    assert "app-token" not in res.text  # トークンの値は画面に出さない
    assert settings.pushover(db.connect(dbdir)) == ("app-token", "user-key")

    res = client.post("/settings/pushover/clear", headers={"HX-Request": "true"})
    assert "Pushover はまだ設定されていません" in res.text
    assert settings.pushover(db.connect(dbdir)) is None


@pytest.mark.usefixtures("_no_pushover_env")
def test_settings_pushover_set_empty_shows_error(client: TestClient, dbdir: Path) -> None:
    from alir import settings

    res = client.post(
        "/settings/pushover/set",
        data={"token": "", "user": ""},
        headers={"HX-Request": "true"},
    )
    assert "token and user are required" in res.text
    assert settings.pushover(db.connect(dbdir)) is None


def test_settings_page_shows_pushover_environment(
    client: TestClient, dbdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alir import notify

    monkeypatch.setenv(notify.ENV_PUSHOVER_TOKEN, "t")
    monkeypatch.setenv(notify.ENV_PUSHOVER_USER, "u")
    res = client.get("/settings")
    assert "設定済み(環境変数)" in res.text


@pytest.mark.usefixtures("_no_pushover_env")
def test_settings_pushover_test_sends(
    client: TestClient, dbdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alir import notify, settings

    settings.set_pushover(db.connect(dbdir), token="t", user="u")
    monkeypatch.setattr(notify, "send_pushover", lambda message, *, url: True)
    res = client.post("/settings/pushover/test", headers={"HX-Request": "true"})
    assert "テスト通知を送信しました" in res.text


@pytest.mark.usefixtures("_no_pushover_env")
def test_settings_pushover_test_reports_unconfigured(
    client: TestClient, dbdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alir import notify

    monkeypatch.setattr(notify, "send_pushover", lambda message, *, url: False)
    res = client.post("/settings/pushover/test", headers={"HX-Request": "true"})
    assert "未設定のため送信できません" in res.text


def test_settings_web_url_set_and_clear(
    client: TestClient, dbdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alir import notify, settings

    monkeypatch.delenv(notify.ENV_WEB_URL, raising=False)
    res = client.post(
        "/settings/web-url/set",
        data={"url": "http://192.168.1.10:8710"},
        headers={"HX-Request": "true"},
    )
    assert 'value="http://192.168.1.10:8710"' in res.text
    assert settings.web_url(db.connect(dbdir)) == "http://192.168.1.10:8710"

    res = client.post("/settings/web-url/clear", headers={"HX-Request": "true"})
    assert settings.web_url(db.connect(dbdir)) is None


def test_settings_web_url_invalid_shows_error(client: TestClient, dbdir: Path) -> None:
    from alir import settings

    res = client.post(
        "/settings/web-url/set",
        data={"url": "192.168.1.10:8710"},
        headers={"HX-Request": "true"},
    )
    assert "must start with http" in res.text
    assert settings.web_url(db.connect(dbdir)) is None


def test_settings_pushover_test_uses_saved_web_url(
    client: TestClient, dbdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alir import notify, settings

    conn = db.connect(dbdir)
    settings.set_pushover(conn, token="t", user="u")
    settings.set_web_url(conn, "http://192.168.1.10:8710")
    urls: list[str | None] = []

    def fake_send(message: str, *, url: str | None) -> bool:
        urls.append(url)
        return True

    monkeypatch.setattr(notify, "send_pushover", fake_send)
    res = client.post("/settings/pushover/test", headers={"HX-Request": "true"})
    assert "テスト通知を送信しました" in res.text
    assert urls == ["http://192.168.1.10:8710"]


def test_record_auto_web_url_uses_specific_host(dbdir: Path) -> None:
    from alir import control
    from alir.web import record_auto_web_url

    url = record_auto_web_url(dbdir, "192.168.1.20", 8710)
    assert url == "http://192.168.1.20:8710"
    assert control.get_value(db.connect(dbdir), control.KEY_WEB_URL_AUTO) == url


def test_record_auto_web_url_detects_for_wildcard(dbdir: Path) -> None:
    from alir import control
    from alir.web import record_auto_web_url

    url = record_auto_web_url(dbdir, "0.0.0.0", 8710, detect=lambda: "192.168.1.30")
    assert url == "http://192.168.1.30:8710"
    assert control.get_value(db.connect(dbdir), control.KEY_WEB_URL_AUTO) == url


def test_record_auto_web_url_clears_when_undetectable(dbdir: Path) -> None:
    from alir import control
    from alir.web import record_auto_web_url

    conn = db.connect(dbdir)
    control.set_value(conn, control.KEY_WEB_URL_AUTO, "http://old:8710")
    url = record_auto_web_url(dbdir, "0.0.0.0", 8710, detect=lambda: None)
    assert url is None
    assert control.get_value(db.connect(dbdir), control.KEY_WEB_URL_AUTO) in (None, "")


def test_settings_page_shows_auto_detected_web_url(
    client: TestClient, dbdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alir import control, notify

    monkeypatch.delenv(notify.ENV_WEB_URL, raising=False)
    control.set_value(db.connect(dbdir), control.KEY_WEB_URL_AUTO, "http://192.168.1.30:8710")
    res = client.get("/settings")
    assert "自動検出した" in res.text
    assert "http://192.168.1.30:8710" in res.text


def test_record_auto_web_url_brackets_ipv6_host(dbdir: Path) -> None:
    from alir.web import record_auto_web_url

    url = record_auto_web_url(dbdir, "2001:db8::5", 8710)
    assert url == "http://[2001:db8::5]:8710"


def test_record_auto_web_url_detects_for_ipv6_wildcard(dbdir: Path) -> None:
    from alir.web import record_auto_web_url

    url = record_auto_web_url(dbdir, "::", 8710, detect=lambda: "192.168.1.30")
    assert url == "http://192.168.1.30:8710"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_record_auto_web_url_skips_loopback_bind(dbdir: Path, host: str) -> None:
    from alir import control
    from alir.web import record_auto_web_url

    conn = db.connect(dbdir)
    control.set_value(conn, control.KEY_WEB_URL_AUTO, "http://old:8710")
    url = record_auto_web_url(dbdir, host, 8710, detect=lambda: "192.168.1.30")
    assert url is None
    assert control.get_value(db.connect(dbdir), control.KEY_WEB_URL_AUTO) in (None, "")
