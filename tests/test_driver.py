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


def _fake_worktree(issue: registry.Issue) -> tuple[Path, str]:
    return Path("/tmp/worktree"), f"alir/issue-{issue.number}"


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

    def broken_worktree(issue: registry.Issue) -> tuple[Path, str]:
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
