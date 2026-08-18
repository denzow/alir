"""ループドライバのテスト。claude の実行と worktree は差し替える。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import db, driver, questions, registry
from alir.driver import RunResult

URL = "https://github.com/denzow/alir/issues/12"


@pytest.fixture
def dbdir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _add_issue(dbdir: Path, workdir: str = "/tmp/alir") -> registry.Issue:
    conn = db.connect(dbdir)
    return registry.add(conn, url=URL, workdir=workdir)


def _fake_worktree(issue: registry.Issue, branch: str, *, push: bool = False) -> Path:
    return Path("/tmp/worktree")


def _runner(result: RunResult):  # type: ignore[no-untyped-def]
    def run(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        run.prompts.append(prompt)  # type: ignore[attr-defined]
        return result

    run.prompts = []  # type: ignore[attr-defined]
    return run


def _reporting_runner(result: RunResult, *, outcome: str = "implemented"):  # type: ignore[no-untyped-def]
    """report_result 相当の報告を残して終える runner。プロンプトから issue を読む。"""
    import re

    from alir import reports

    def run(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        match = re.search(r"denzow/alir#\d+", prompt)
        assert match is not None
        conn = db.connect(dbdir)
        reports.add(conn, issue=match.group(0), summary="実施内容", outcome=outcome)
        run.prompts.append(prompt)  # type: ignore[attr-defined]
        return result

    run.prompts = []  # type: ignore[attr-defined]
    return run


def test_process_issue_done(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    runner = _reporting_runner(RunResult(exit_code=0, session_id="sess-1", output="ok"))
    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert finished.status == registry.STATUS_DONE
    assert finished.session_id == "sess-1"
    assert finished.branch == "alir/issue-12"


def test_process_issue_failed_on_error_exit(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    runner = _runner(RunResult(exit_code=1, session_id=None, output="boom"))
    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert finished.status == registry.STATUS_FAILED


def test_process_issue_parked_when_question_open(dbdir: Path) -> None:
    issue = _add_issue(dbdir)

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        conn = db.connect(dbdir)
        questions.ask(
            conn,
            issue="denzow/alir#12",
            question="スキーマを変えてよいか",
            options=["変える", "変えない"],
            recommended="変える",
            impact="high",
            timeout_action="keep_parked",
        )
        return RunResult(exit_code=0, session_id="sess-1", output="asked")

    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert finished.status == registry.STATUS_PARKED


def test_process_issue_failed_when_worktree_setup_raises(dbdir: Path) -> None:
    issue = _add_issue(dbdir)

    def broken_worktree(issue: registry.Issue, branch: str, *, push: bool = False) -> Path:
        raise driver.DriverError("no such repo")

    runner = _runner(RunResult(exit_code=0, session_id=None, output=""))
    with pytest.raises(driver.DriverError):
        driver.process_issue(dbdir, issue.id, runner=runner, worktree=broken_worktree)
    conn = db.connect(dbdir)
    assert registry.get(conn, issue.id).status == registry.STATUS_FAILED


def test_prompt_includes_answers_on_resume(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    conn = db.connect(dbdir)
    questions.ask(
        conn,
        issue="denzow/alir#12",
        question="スキーマを変えてよいか",
        options=["変える", "変えない"],
        recommended="変える",
        impact="high",
        timeout_action="keep_parked",
    )
    questions.answer(conn, 1, "2", note="互換性を守る")

    runner = _runner(RunResult(exit_code=0, session_id="sess-2", output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "スキーマを変えてよいか" in prompt
    assert "変えない" in prompt
    assert "互換性を守る" in prompt


def _ask_q(conn, parent_id: int | None = None):  # type: ignore[no-untyped-def]
    return questions.ask(
        conn,
        issue="denzow/alir#12",
        question="スキーマを変えてよいか",
        options=["変える", "変えない"],
        recommended="変える",
        impact="high",
        timeout_action="keep_parked",
        parent_id=parent_id,
    )


def test_prompt_instructs_reask_when_last_question_returned(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    conn = db.connect(dbdir)
    _ask_q(conn)
    questions.return_question(conn, 1, "既存データも対象か?")

    runner = _runner(RunResult(exit_code=0, session_id="sess-2", output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "確認(回答ではない): 既存データも対象か?" in prompt
    assert "parent_question_id パラメータに 1 を渡し" in prompt


def test_prompt_omits_reask_when_last_question_answered(dbdir: Path) -> None:
    # 差し戻し → 再質問 → 回答済み。再質問の指示は入らず、履歴として確認は残る
    issue = _add_issue(dbdir)
    conn = db.connect(dbdir)
    _ask_q(conn)
    questions.return_question(conn, 1, "既存データも対象か?")
    _ask_q(conn, parent_id=1)
    questions.answer(conn, 2, "1")

    runner = _runner(RunResult(exit_code=0, session_id="sess-2", output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "確認(回答ではない): 既存データも対象か?" in prompt
    assert "parent_question_id" not in prompt


def test_run_loop_once_processes_all_queued(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp/a")
    registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir="/tmp/b")

    runner = _reporting_runner(RunResult(exit_code=0, session_id=None, output="ok"))
    logs: list[str] = []
    driver.run_loop(dbdir, once=True, runner=runner, worktree=_fake_worktree, log=logs.append)
    conn = db.connect(dbdir)
    statuses = [i.status for i in registry.list_issues(conn)]
    assert statuses == [registry.STATUS_DONE, registry.STATUS_DONE]


def test_log_with_timestamp_prefixes_local_time(capsys: pytest.CaptureFixture[str]) -> None:
    import re

    driver.log_with_timestamp("start #1 denzow/alir#12")
    out = capsys.readouterr().out
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} start #1 denzow/alir#12\n", out)


def test_run_loop_notifies_on_failed_session(dbdir: Path) -> None:
    issue = _add_issue(dbdir, workdir="/tmp/a")
    runner = _runner(RunResult(exit_code=1, session_id=None, output="boom"))
    calls: list[tuple[int, str | None]] = []
    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=_fake_worktree,
        log=lambda _: None,
        fail_notifier=lambda i, detail: calls.append((i.id, detail)),
    )
    assert calls == [(issue.id, None)]


def test_run_loop_notifies_on_worktree_error(dbdir: Path) -> None:
    issue = _add_issue(dbdir, workdir="/tmp/a")

    def broken_worktree(issue: registry.Issue, branch: str, *, push: bool = False) -> Path:
        raise driver.DriverError("no such repo")

    runner = _runner(RunResult(exit_code=0, session_id=None, output=""))
    calls: list[tuple[int, str | None]] = []
    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=broken_worktree,
        log=lambda _: None,
        fail_notifier=lambda i, detail: calls.append((i.id, detail)),
    )
    assert calls == [(issue.id, "no such repo")]


def test_next_import_at_runs_once_when_enabled() -> None:
    """無効から有効にしたときは待たずに 1 回取り込む。"""
    assert driver.next_import_at(0.0, 0.0, 300.0) == 0.0


def test_next_import_at_keeps_schedule_when_interval_changes() -> None:
    """間隔を変えただけなら、前回の実行時刻から新しい間隔で測り直す。"""
    # 100.0 に実行し、次回は 400.0 の予定。間隔を 600 に延ばすと次回は 700.0
    assert driver.next_import_at(400.0, 300.0, 600.0) == 700.0
    # 縮めたときも同じ起点から測る
    assert driver.next_import_at(400.0, 300.0, 60.0) == 160.0


def test_run_loop_does_not_import_without_interval(dbdir: Path, tmp_path: Path) -> None:
    """定期取り込みの間隔が未設定なら、対象があってもドライバは検索しない。"""
    from alir import importer

    workdir = tmp_path / "repo"
    workdir.mkdir()
    conn = db.connect(dbdir)
    importer.add_target(conn, workdir=str(workdir), label="alir")

    def fetch(wd: str, label: str) -> list[dict[str, str]]:
        raise AssertionError("fetch must not be called")

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    logs: list[str] = []
    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=_fake_worktree,
        import_fetch=fetch,
        log=logs.append,
    )
    assert registry.list_issues(db.connect(dbdir)) == []
    assert not any("import" in m for m in logs)


def test_run_loop_imports_labeled_issues_once_per_ttl(dbdir: Path, tmp_path: Path) -> None:
    """間隔を設定すればキューへ自動登録され、TTL 内は再検索しない。"""
    from alir import control, importer

    workdir = tmp_path / "repo"
    workdir.mkdir()
    conn = db.connect(dbdir)
    importer.add_target(conn, workdir=str(workdir), label="alir")
    importer.set_import_interval(conn, importer.DEFAULT_INTERVAL)

    calls: list[str] = []

    def fetch(wd: str, label: str) -> list[dict[str, str]]:
        calls.append(label)
        return [{"url": URL, "title": "自動取り込み"}]

    runner = _reporting_runner(RunResult(exit_code=0, session_id=None, output="ok"))
    logs: list[str] = []
    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=_fake_worktree,
        import_fetch=fetch,
        log=logs.append,
    )

    conn = db.connect(dbdir)
    items = registry.list_issues(conn)
    assert [(i.url, i.status) for i in items] == [(URL, registry.STATUS_DONE)]
    # 取り込み → 処理完了 → 空キュー確認、と複数サイクル回っても検索は 1 回
    assert calls == ["alir"]
    assert any(m.startswith("import #1") for m in logs)
    assert any("import #1" in e.message for e in control.recent_events(conn))
    # 稼働ログはコンソールにのみ出し、events には残さない
    assert any(m == "import check: 1 targets, found 1, imported 1" for m in logs)
    assert not any("import check" in e.message for e in control.recent_events(conn))


def test_run_loop_emits_import_errors(dbdir: Path, tmp_path: Path) -> None:
    from alir import importer

    workdir = tmp_path / "repo"
    workdir.mkdir()
    conn = db.connect(dbdir)
    importer.add_target(conn, workdir=str(workdir), label="alir")
    importer.set_import_interval(conn, importer.DEFAULT_INTERVAL)

    def fetch(wd: str, label: str) -> list[dict[str, str]]:
        raise importer.ImporterError("gh issue list failed: boom")

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    logs: list[str] = []
    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=_fake_worktree,
        import_fetch=fetch,
        log=logs.append,
    )
    assert any("import error" in m and "boom" in m for m in logs)


def test_prompt_instructs_report_result(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "report_result" in prompt


def test_prompt_instructs_report_progress(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "report_progress" in prompt


def _run_git(cwd: Path, *args: str) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _make_issue(workdir: Path) -> registry.Issue:
    return registry.Issue(
        id=1,
        url=URL,
        repo="denzow/alir",
        number=12,
        workdir=str(workdir),
        priority=0,
        status=registry.STATUS_QUEUED,
        session_id=None,
        branch=None,
        created_at="t",
        updated_at="t",
    )


def test_setup_worktree_bases_on_latest_default_branch(tmp_path: Path) -> None:
    """clone が古くても、fetch した origin のデフォルトブランチ先端から分岐する。"""
    seed = tmp_path / "seed"
    seed.mkdir()
    _run_git(seed, "init", "-b", "main")
    (seed / "a.txt").write_text("1")
    _run_git(seed, "add", ".")
    _run_git(seed, "commit", "-m", "c1")

    origin = tmp_path / "origin.git"
    _run_git(tmp_path, "clone", "--bare", str(seed), str(origin))

    work = tmp_path / "repo"
    _run_git(tmp_path, "clone", str(origin), str(work))

    # clone 後に origin 側だけ進める(work の origin/main は古いまま)
    (seed / "a.txt").write_text("2")
    _run_git(seed, "commit", "-am", "c2")
    _run_git(seed, "push", str(origin), "main")
    latest = _run_git(seed, "rev-parse", "HEAD")

    path = driver.setup_worktree(_make_issue(work), "alir/issue-12")
    assert _run_git(path, "rev-parse", "HEAD") == latest


def test_setup_worktree_without_remote_uses_local_default(tmp_path: Path) -> None:
    work = tmp_path / "repo"
    work.mkdir()
    _run_git(work, "init", "-b", "main")
    (work / "a.txt").write_text("1")
    _run_git(work, "add", ".")
    _run_git(work, "commit", "-m", "c1")
    # 別ブランチをチェックアウトした状態でも main から分岐する
    _run_git(work, "switch", "-c", "wip")
    (work / "b.txt").write_text("wip")
    _run_git(work, "add", ".")
    _run_git(work, "commit", "-m", "wip commit")
    main_rev = _run_git(work, "rev-parse", "main")

    path = driver.setup_worktree(_make_issue(work), "alir/issue-12")
    assert _run_git(path, "rev-parse", "HEAD") == main_rev


def test_process_issue_uses_branch_template(dbdir: Path) -> None:
    from alir import settings

    conn = db.connect(dbdir)
    settings.set_branch_template(conn, "feature/{repo}-{number}")
    issue = _add_issue(dbdir)

    captured: dict[str, str] = {}

    def worktree(issue: registry.Issue, branch: str, *, push: bool = False) -> Path:
        captured["branch"] = branch
        return Path("/tmp/wt")

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=worktree)
    assert captured["branch"] == "feature/alir-12"
    assert finished.branch == "feature/alir-12"


def test_process_issue_reuses_stored_branch(dbdir: Path) -> None:
    from alir import settings

    issue = _add_issue(dbdir)
    conn = db.connect(dbdir)
    registry.set_status(conn, issue.id, registry.STATUS_QUEUED, branch="old-branch")
    settings.set_branch_template(conn, "feature/{repo}-{number}")

    captured: dict[str, str] = {}

    def worktree(issue: registry.Issue, branch: str, *, push: bool = False) -> Path:
        captured["branch"] = branch
        return Path("/tmp/wt")

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=worktree)
    assert captured["branch"] == "old-branch"


def test_process_issue_requeues_when_refined(dbdir: Path) -> None:
    from alir import reports

    issue = _add_issue(dbdir)

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        conn = db.connect(dbdir)
        reports.add(
            conn, issue="denzow/alir#12", summary="仕様を整理して Issue に投稿", outcome="refined"
        )
        return RunResult(exit_code=0, session_id="sess-1", output="refined")

    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert finished.status == registry.STATUS_QUEUED


def test_process_issue_ignores_stale_refined_report(dbdir: Path) -> None:
    """今回の実行より前の refined 報告では requeue しない。"""
    from alir import reports

    issue = _add_issue(dbdir)
    conn = db.connect(dbdir)
    reports.add(conn, issue="denzow/alir#12", summary="前回のリファインメント", outcome="refined")

    import time

    time.sleep(1.1)  # created_at が秒精度のため、開始時刻を確実に後にする
    runner = _reporting_runner(RunResult(exit_code=0, session_id=None, output="ok"))
    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert finished.status == registry.STATUS_DONE


def test_prompt_includes_refinement_cycle(dbdir: Path) -> None:
    from alir import reports

    issue = _add_issue(dbdir)
    conn = db.connect(dbdir)
    reports.add(conn, issue="denzow/alir#12", summary="仕様を整理", outcome="refined")

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "リファインメント" in prompt
    assert 'outcome="refined"' in prompt
    assert "[refined] 仕様を整理" in prompt


def test_prompt_instructs_rename_for_session_placeholders(dbdir: Path) -> None:
    from alir import settings

    conn = db.connect(dbdir)
    settings.set_branch_template(conn, "{number}/{type}/{summary}")
    issue = _add_issue(dbdir)

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "git branch -m" in prompt
    assert "12/work/wip" in prompt


def test_prompt_omits_rename_without_session_placeholders(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "git branch -m" not in prompt


def test_process_issue_captures_renamed_branch(dbdir: Path, tmp_path: Path) -> None:
    """セッションが git branch -m で改名した名前を registry に取り込む。"""
    repo = tmp_path / "wt"
    repo.mkdir()
    _run_git(repo, "init", "-b", "12/work/wip")
    (repo / "a.txt").write_text("1")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "c1")

    issue = _add_issue(dbdir)

    def runner(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        _run_git(cwd, "branch", "-m", "12/bugfix/fix-login")
        return RunResult(exit_code=0, session_id=None, output="ok")

    def worktree(issue: registry.Issue, branch: str, *, push: bool = False) -> Path:
        return repo

    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=worktree)
    assert finished.branch == "12/bugfix/fix-login"


def test_process_issue_direct_push_uses_push_branch(dbdir: Path, tmp_path: Path) -> None:
    """直接 push 運用の workdir では push 先ブランチで作業し、PR を作らない手順にする。"""
    from alir import settings

    work = tmp_path / "repo"
    work.mkdir()
    issue = _add_issue(dbdir, workdir=str(work.resolve()))
    conn = db.connect(dbdir)
    settings.set_push_branch(conn, workdir=str(work), branch="develop")

    captured: dict[str, object] = {}

    def worktree(issue: registry.Issue, branch: str, *, push: bool = False) -> Path:
        captured["branch"] = branch
        captured["push"] = push
        return Path("/tmp/wt")

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=worktree)
    assert captured == {"branch": "develop", "push": True}
    assert finished.branch == "develop"
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "git push origin develop" in prompt
    assert "PR は作らない" in prompt
    assert "gh pr create" not in prompt


def test_process_issue_direct_push_overrides_stored_branch(dbdir: Path, tmp_path: Path) -> None:
    """保存済みブランチがあっても、直接 push 運用では現在の push 先を使う。"""
    from alir import settings

    work = tmp_path / "repo"
    work.mkdir()
    issue = _add_issue(dbdir, workdir=str(work.resolve()))
    conn = db.connect(dbdir)
    registry.set_status(conn, issue.id, registry.STATUS_QUEUED, branch="old-branch")
    settings.set_push_branch(conn, workdir=str(work), branch="develop")

    captured: dict[str, str] = {}

    def worktree(issue: registry.Issue, branch: str, *, push: bool = False) -> Path:
        captured["branch"] = branch
        return Path("/tmp/wt")

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=worktree)
    assert captured["branch"] == "develop"
    assert finished.branch == "develop"


def test_prompt_direct_push_instructs_issue_close(dbdir: Path, tmp_path: Path) -> None:
    """直接 push 運用では push 後に対象 Issue をクローズする手順を含める。"""
    from alir import settings

    work = tmp_path / "repo"
    work.mkdir()
    issue = _add_issue(dbdir, workdir=str(work.resolve()))
    conn = db.connect(dbdir)
    settings.set_push_branch(conn, workdir=str(work), branch="develop")

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert f"gh issue close {URL} --comment" in prompt
    assert "実施内容と対応コミットを記載する" in prompt
    assert "push した場合だけ" in prompt


def test_prompt_pr_mode_omits_issue_close(dbdir: Path) -> None:
    """PR 運用のプロンプトにはクローズ手順を含めない(Fixes に任せる)。"""
    issue = _add_issue(dbdir)
    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "gh issue close" not in prompt


def test_prompt_direct_push_refine_mode_omits_issue_close(dbdir: Path, tmp_path: Path) -> None:
    """直接 push 運用でも、リファインメント手順にはクローズ指示を含めない。"""
    from alir import settings

    work = tmp_path / "repo"
    work.mkdir()
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir=str(work.resolve()), mode=registry.MODE_REFINE)
    settings.set_push_branch(conn, workdir=str(work), branch="develop")

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, 1, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "gh issue close" not in prompt


def test_prompt_direct_push_omits_rename_instruction(dbdir: Path, tmp_path: Path) -> None:
    """直接 push 運用ではテンプレートに {type} 等があってもブランチを改名させない。"""
    from alir import settings

    work = tmp_path / "repo"
    work.mkdir()
    issue = _add_issue(dbdir, workdir=str(work.resolve()))
    conn = db.connect(dbdir)
    settings.set_branch_template(conn, "{number}/{type}/{summary}")
    settings.set_push_branch(conn, workdir=str(work), branch="develop")

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "git branch -m" not in prompt


def test_setup_worktree_direct_push_shares_branch_worktree(tmp_path: Path) -> None:
    """直接 push 運用は push 先ブランチの共有 worktree を使い、origin の先端に追従する。"""
    seed = tmp_path / "seed"
    seed.mkdir()
    _run_git(seed, "init", "-b", "main")
    (seed / "a.txt").write_text("1")
    _run_git(seed, "add", ".")
    _run_git(seed, "commit", "-m", "c1")
    _run_git(seed, "branch", "develop")

    origin = tmp_path / "origin.git"
    _run_git(tmp_path, "clone", "--bare", str(seed), str(origin))

    work = tmp_path / "repo"
    _run_git(tmp_path, "clone", str(origin), str(work))

    # clone 後に origin 側だけ develop を進める
    _run_git(seed, "switch", "develop")
    (seed / "a.txt").write_text("2")
    _run_git(seed, "commit", "-am", "c2")
    _run_git(seed, "push", str(origin), "develop")
    latest = _run_git(seed, "rev-parse", "develop")

    path = driver.setup_worktree(_make_issue(work), "develop", push=True)
    assert path == work.parent / "repo-alir" / "branches" / "develop"
    assert _run_git(path, "branch", "--show-current") == "develop"
    assert _run_git(path, "rev-parse", "HEAD") == latest

    # 別の Issue でも同じ worktree を再利用し、origin の進みに追従する
    (seed / "a.txt").write_text("3")
    _run_git(seed, "commit", "-am", "c3")
    _run_git(seed, "push", str(origin), "develop")
    newest = _run_git(seed, "rev-parse", "develop")

    again = driver.setup_worktree(_make_issue(work), "develop", push=True)
    assert again == path
    assert _run_git(path, "rev-parse", "HEAD") == newest


def test_setup_worktree_direct_push_creates_missing_branch(tmp_path: Path) -> None:
    """push 先ブランチがどこにもなければデフォルトブランチの先端から新設する。"""
    work = tmp_path / "repo"
    work.mkdir()
    _run_git(work, "init", "-b", "main")
    (work / "a.txt").write_text("1")
    _run_git(work, "add", ".")
    _run_git(work, "commit", "-m", "c1")
    main_rev = _run_git(work, "rev-parse", "main")

    path = driver.setup_worktree(_make_issue(work), "develop", push=True)
    assert _run_git(path, "branch", "--show-current") == "develop"
    assert _run_git(path, "rev-parse", "HEAD") == main_rev


def test_next_startable_serializes_direct_push_workdir(dbdir: Path, tmp_path: Path) -> None:
    """直接 push 運用の workdir は同時に 1 Issue しか開始しない。"""
    from alir import settings

    work = tmp_path / "repo"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    conn = db.connect(dbdir)
    settings.set_push_branch(conn, workdir=str(work), branch="develop")
    first = registry.add(
        conn, url="https://github.com/denzow/alir/issues/1", workdir=str(work.resolve())
    )
    second = registry.add(
        conn, url="https://github.com/denzow/alir/issues/2", workdir=str(work.resolve())
    )
    outside = registry.add(
        conn, url="https://github.com/denzow/alir/issues/3", workdir=str(other.resolve())
    )

    registry.set_status(conn, first.id, registry.STATUS_RUNNING)
    picked = driver.next_startable(conn)
    assert picked is not None and picked.id == outside.id

    registry.set_status(conn, first.id, registry.STATUS_DONE)
    picked = driver.next_startable(conn)
    assert picked is not None and picked.id == second.id


def test_prompt_requires_explicit_acceptance_criteria(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "明文化" in prompt
    assert "推測できそうでもリファインメントを行う" in prompt


def test_prompt_requires_report_even_when_nothing_to_do(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "終了前に必ず report_result を 1 回呼ぶ" in prompt
    assert "何もする必要がなかった場合" in prompt


def test_prompt_instructs_stop_on_usage(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert '"stop": true' in prompt
    assert 'outcome="aborted"' in prompt


def test_prompt_includes_note(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp/alir", note="対象は API 層だけにする")

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, 1, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "登録者からの補足" in prompt
    assert "対象は API 層だけにする" in prompt


def test_prompt_for_implement_mode_skips_refinement(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp/alir", mode=registry.MODE_IMPLEMENT)

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, 1, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "実装を指定して登録されている" in prompt
    assert "gh pr create" in prompt
    assert "不十分な場合(リファインメント)" not in prompt
    assert 'outcome="refined"' not in prompt


def test_prompt_for_refine_mode_skips_implementation(dbdir: Path) -> None:
    from alir import settings

    conn = db.connect(dbdir)
    settings.set_branch_template(conn, "{number}/{type}/{summary}")
    registry.add(conn, url=URL, workdir="/tmp/alir", mode=registry.MODE_REFINE)

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, 1, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "リファインメントを指定して登録されている" in prompt
    assert 'outcome="refined"' in prompt
    assert "gh pr create" not in prompt
    # 実装しないのでブランチ改名の指示も出さない
    assert "git branch -m" not in prompt


def test_refine_mode_completes_on_refined_report(dbdir: Path) -> None:
    """mode=refine の Issue は refined 報告で requeue せず done になる。"""
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp/alir", mode=registry.MODE_REFINE)

    runner = _reporting_runner(
        RunResult(exit_code=0, session_id=None, output="ok"), outcome="refined"
    )
    finished = driver.process_issue(dbdir, 1, runner=runner, worktree=_fake_worktree)
    assert finished.status == registry.STATUS_DONE


def test_prompt_instructs_enqueue_issue(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "enqueue_issue" in prompt
    assert "このセッションでは着手しない" in prompt


def test_prompt_warns_against_background_wait(dbdir: Path) -> None:
    """バックグラウンド完了待ちで応答を終えるとセッションごと死ぬことを明記する。"""
    issue = _add_issue(dbdir)
    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "run_in_background" in prompt
    assert "フォアグラウンドで実行して完了まで待つ" in prompt


def test_process_issue_requeues_once_without_report(dbdir: Path) -> None:
    """report_result なしの正常終了は 1 回だけ queued に戻し、連続したら failed。"""
    issue = _add_issue(dbdir)
    silent = _runner(RunResult(exit_code=0, session_id="sess-1", output="ok"))

    first = driver.process_issue(dbdir, issue.id, runner=silent, worktree=_fake_worktree)
    assert first.status == registry.STATUS_QUEUED

    second = driver.process_issue(dbdir, issue.id, runner=silent, worktree=_fake_worktree)
    assert second.status == registry.STATUS_FAILED

    # failed 時に印は消えるので、人間が queued に戻せば再試行の権利は復活する
    third = driver.process_issue(dbdir, issue.id, runner=silent, worktree=_fake_worktree)
    assert third.status == registry.STATUS_QUEUED


def test_report_clears_missing_report_marker(dbdir: Path) -> None:
    """報告のあるセッションを挟めば、報告なし requeue はまた 1 回許される。"""
    issue = _add_issue(dbdir)
    silent = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    reporting = _reporting_runner(RunResult(exit_code=0, session_id=None, output="ok"))

    assert (
        driver.process_issue(dbdir, issue.id, runner=silent, worktree=_fake_worktree).status
        == registry.STATUS_QUEUED
    )
    assert (
        driver.process_issue(dbdir, issue.id, runner=reporting, worktree=_fake_worktree).status
        == registry.STATUS_DONE
    )
    import time

    time.sleep(1.1)  # created_at が秒精度のため、直前の報告を実行前のものにする
    # 印が消えているので failed ではなく queued に戻る
    assert (
        driver.process_issue(dbdir, issue.id, runner=silent, worktree=_fake_worktree).status
        == registry.STATUS_QUEUED
    )


def test_run_claude_passes_resume_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout='{"session_id": "sess-9"}', stderr="")

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    dbdir = tmp_path / "data"
    dbdir.mkdir()
    result = driver.run_claude(prompt="p", cwd=tmp_path, dbdir=dbdir, resume_session_id="sess-9")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--resume") + 1] == "sess-9"
    assert result.session_id == "sess-9"


def test_run_claude_omits_resume_flag_without_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    dbdir = tmp_path / "data"
    dbdir.mkdir()
    driver.run_claude(prompt="p", cwd=tmp_path, dbdir=dbdir)
    assert "--resume" not in captured["cmd"]


def test_run_claude_passes_model_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    dbdir = tmp_path / "data"
    dbdir.mkdir()
    driver.run_claude(prompt="p", cwd=tmp_path, dbdir=dbdir, model="sonnet")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_run_claude_omits_model_flag_without_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    dbdir = tmp_path / "data"
    dbdir.mkdir()
    driver.run_claude(prompt="p", cwd=tmp_path, dbdir=dbdir)
    assert "--model" not in captured["cmd"]


def test_process_issue_passes_model_from_settings(dbdir: Path) -> None:
    """settings にモデルが設定されていれば runner に渡り、未設定なら渡らない。"""
    from alir import settings

    issue = _add_issue(dbdir)
    models: list[str | None] = []

    def run(
        *, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False, model=None
    ) -> RunResult:
        models.append(model)
        return RunResult(exit_code=0, session_id=None, output="ok")

    driver.process_issue(dbdir, issue.id, runner=run, worktree=_fake_worktree)
    settings.set_model(db.connect(dbdir), "sonnet")
    issue2 = registry.add(
        db.connect(dbdir), url="https://github.com/denzow/alir/issues/99", workdir="/tmp/alir"
    )
    driver.process_issue(dbdir, issue2.id, runner=run, worktree=_fake_worktree)
    assert models == [None, "sonnet"]


def test_process_issue_resumes_with_marked_session(dbdir: Path) -> None:
    """再開の印があれば --resume 用のセッション ID が runner に渡り、印は消費される。"""
    from alir import control, reports

    issue = _add_issue(dbdir)
    control.mark_resume_session(db.connect(dbdir), issue.ref, "sess-park")

    calls: list[str | None] = []

    def runner(
        *,
        prompt: str,
        cwd: Path,
        dbdir: Path,
        skip_permissions: bool = False,
        resume_session_id: str | None = None,
    ) -> RunResult:
        calls.append(resume_session_id)
        conn = db.connect(dbdir)
        reports.add(conn, issue="denzow/alir#12", summary="実施内容", outcome="implemented")
        return RunResult(exit_code=0, session_id="sess-park", output="ok")

    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert calls == ["sess-park"]
    assert finished.status == registry.STATUS_DONE
    assert control.resume_session(db.connect(dbdir), issue.ref) is None


def test_process_issue_resume_failure_falls_back_to_new_session(dbdir: Path) -> None:
    """resume が失敗しても新規セッションでやり直し、Issue は failed にならない。"""
    from alir import control, reports

    issue = _add_issue(dbdir)
    control.mark_resume_session(db.connect(dbdir), issue.ref, "sess-park")

    calls: list[str | None] = []

    def runner(
        *,
        prompt: str,
        cwd: Path,
        dbdir: Path,
        skip_permissions: bool = False,
        resume_session_id: str | None = None,
    ) -> RunResult:
        calls.append(resume_session_id)
        if resume_session_id is not None:
            return RunResult(exit_code=1, session_id=None, output="No conversation found")
        conn = db.connect(dbdir)
        reports.add(conn, issue="denzow/alir#12", summary="実施内容", outcome="implemented")
        return RunResult(exit_code=0, session_id="sess-new", output="ok")

    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert calls == ["sess-park", None]
    assert finished.status == registry.STATUS_DONE
    assert finished.session_id == "sess-new"


def test_process_issue_resume_disabled_by_setting(dbdir: Path) -> None:
    """settings で無効化すると印があっても常に新規セッションで再開する。"""
    from alir import control, settings

    issue = _add_issue(dbdir)
    conn = db.connect(dbdir)
    settings.set_resume_enabled(conn, False)
    control.mark_resume_session(conn, issue.ref, "sess-park")

    calls: list[str | None] = []

    def runner(
        *,
        prompt: str,
        cwd: Path,
        dbdir: Path,
        skip_permissions: bool = False,
        resume_session_id: str | None = None,
    ) -> RunResult:
        calls.append(resume_session_id)
        return RunResult(exit_code=0, session_id=None, output="ok")

    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert calls == [None]
    # 古い印を持ち越さないよう、無効化中でも実行の完了で消費する
    assert control.resume_session(db.connect(dbdir), issue.ref) is None


def test_process_issue_rate_limited_keeps_resume_marker(dbdir: Path) -> None:
    """rate limit での requeue は印を残し、明けの再試行でも --resume を使う。"""
    from alir import control

    issue = _add_issue(dbdir)
    control.mark_resume_session(db.connect(dbdir), issue.ref, "sess-park")

    def runner(
        *,
        prompt: str,
        cwd: Path,
        dbdir: Path,
        skip_permissions: bool = False,
        resume_session_id: str | None = None,
    ) -> RunResult:
        return RunResult(
            exit_code=1, session_id=None, output="usage limit reached", rate_limited=True
        )

    with pytest.raises(driver.RateLimited):
        driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    assert control.resume_session(db.connect(dbdir), issue.ref) == "sess-park"


def test_refined_requeue_does_not_mark_resume(dbdir: Path) -> None:
    """refined 経由の再キューには印が付かず、実装フェーズは新規セッションで始まる。"""
    from alir import control, reports

    issue = _add_issue(dbdir)

    def refiner(
        *, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False
    ) -> RunResult:
        conn = db.connect(dbdir)
        reports.add(conn, issue="denzow/alir#12", summary="仕様を整理", outcome="refined")
        return RunResult(exit_code=0, session_id="sess-1", output="refined")

    requeued = driver.process_issue(dbdir, issue.id, runner=refiner, worktree=_fake_worktree)
    assert requeued.status == registry.STATUS_QUEUED
    assert control.resume_session(db.connect(dbdir), issue.ref) is None


PR_URL = "https://github.com/denzow/alir/pull/34"


def test_prompt_includes_ci_failure_once(dbdir: Path) -> None:
    """CI 失敗の記録がプロンプトに載り、1 回で消費される。"""
    from alir import ci

    issue = _add_issue(dbdir)
    ci.mark_ci_failed(db.connect(dbdir), issue.ref, PR_URL)

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert PR_URL in prompt
    assert "CI が失敗している" in prompt
    assert "新しい PR は作らない" in prompt
    assert ci.take_ci_failure(db.connect(dbdir), issue.ref) is None


def test_prompt_omits_ci_failure_without_mark(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.process_issue(dbdir, issue.id, runner=runner, worktree=_fake_worktree)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert "CI が失敗している" not in prompt


def _add_done_issue_with_pr(dbdir: Path) -> registry.Issue:
    from alir import reports

    conn = db.connect(dbdir)
    issue = registry.add(conn, url=URL, workdir="/tmp/alir")
    reports.add(conn, issue=issue.ref, summary="実装して PR 作成", pr_url=PR_URL)
    return registry.set_status(conn, issue.id, registry.STATUS_DONE)


def test_run_loop_requeues_done_issue_with_failed_ci(dbdir: Path) -> None:
    """CI が失敗した PR を持つ done の Issue が、人手なしで再実行される。"""
    from alir import ci

    issue = _add_done_issue_with_pr(dbdir)

    runner = _reporting_runner(RunResult(exit_code=0, session_id=None, output="ok"))
    logs: list[str] = []
    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=_fake_worktree,
        pr_status_fetch=lambda url: ci.PrStatus(state="OPEN", ci_failed=True),
        log=logs.append,
    )
    assert any("PR CI failed" in line for line in logs)
    prompt = runner.prompts[0]  # type: ignore[attr-defined]
    assert PR_URL in prompt
    assert "CI が失敗している" in prompt
    conn = db.connect(dbdir)
    assert registry.get(conn, issue.id).status == registry.STATUS_DONE


def test_run_loop_does_not_requeue_merged_pr(dbdir: Path) -> None:
    from alir import ci

    issue = _add_done_issue_with_pr(dbdir)

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    driver.run_loop(
        dbdir,
        once=True,
        runner=runner,
        worktree=_fake_worktree,
        pr_status_fetch=lambda url: ci.PrStatus(state=ci.STATE_MERGED, ci_failed=True),
        log=lambda _: None,
    )
    assert runner.prompts == []  # type: ignore[attr-defined]
    conn = db.connect(dbdir)
    assert registry.get(conn, issue.id).status == registry.STATUS_DONE
