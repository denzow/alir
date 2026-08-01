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


def _add_issue(dbdir: Path) -> registry.Issue:
    conn = db.connect(dbdir)
    return registry.add(conn, url=URL, workdir="/tmp/alir")


def _fake_worktree(issue: registry.Issue, branch: str) -> Path:
    return Path("/tmp/worktree")


def _runner(result: RunResult):  # type: ignore[no-untyped-def]
    def run(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
        run.prompts.append(prompt)  # type: ignore[attr-defined]
        return result

    run.prompts = []  # type: ignore[attr-defined]
    return run


def test_process_issue_done(dbdir: Path) -> None:
    issue = _add_issue(dbdir)
    runner = _runner(RunResult(exit_code=0, session_id="sess-1", output="ok"))
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

    def broken_worktree(issue: registry.Issue, branch: str) -> Path:
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


def test_run_loop_once_processes_all_queued(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    registry.add(conn, url=URL, workdir="/tmp/a")
    registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir="/tmp/b")

    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
    logs: list[str] = []
    driver.run_loop(dbdir, once=True, runner=runner, worktree=_fake_worktree, log=logs.append)
    conn = db.connect(dbdir)
    statuses = [i.status for i in registry.list_issues(conn)]
    assert statuses == [registry.STATUS_DONE, registry.STATUS_DONE]


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

    def worktree(issue: registry.Issue, branch: str) -> Path:
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

    def worktree(issue: registry.Issue, branch: str) -> Path:
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
    runner = _runner(RunResult(exit_code=0, session_id=None, output="ok"))
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

    def worktree(issue: registry.Issue, branch: str) -> Path:
        return repo

    finished = driver.process_issue(dbdir, issue.id, runner=runner, worktree=worktree)
    assert finished.branch == "12/bugfix/fix-login"


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
