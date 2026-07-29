"""ループドライバ: レジストリの Issue を worktree 上で Claude Code に処理させる。

1 Issue の処理は「worktree を用意し、claude -p を実行し、結果で状態を遷移させる」の
3 段からなる。質問(ask_human)が登録されていれば parked、失敗なら failed、それ以外は done。
claude の実行と worktree の用意は差し替え可能にしてテストする。
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from alir import db, questions, registry
from alir.registry import Issue


class DriverError(Exception):
    """worktree の用意や claude の起動に関する失敗。"""


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    session_id: str | None
    output: str


Runner = Callable[..., RunResult]
WorktreeSetup = Callable[[Issue], tuple[Path, str]]


def _git(workdir: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(workdir), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise DriverError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def setup_worktree(issue: Issue) -> tuple[Path, str]:
    """Issue 用の worktree とブランチを用意し、(パス, ブランチ名) を返す。

    worktree は対象リポジトリの隣(<repo>-alir/issue-<n>)に置く。
    すでに存在すればそのまま再利用する(park からの再開)。
    """
    workdir = Path(issue.workdir)
    branch = f"alir/issue-{issue.number}"
    path = workdir.parent / f"{workdir.name}-alir" / f"issue-{issue.number}"
    if path.exists():
        return path, branch
    path.parent.mkdir(parents=True, exist_ok=True)
    if _git(workdir, "branch", "--list", branch).strip():
        _git(workdir, "worktree", "add", str(path), branch)
    else:
        _git(workdir, "worktree", "add", "-b", branch, str(path))
    return path, branch


def build_prompt(issue: Issue, branch: str, *, answers: list[questions.Question]) -> str:
    """1 Issue を処理するセッションのプロンプトを組み立てる。"""
    lines = [
        "あなたは GitHub Issue を自律的に処理するエージェントである。",
        "",
        f"対象 Issue: {issue.url}",
        f"作業ブランチ: {branch}(すでにチェックアウト済み)",
        "",
        "手順:",
        f"1. `gh issue view {issue.url}` で Issue を読み、要件を把握する。",
        "2. このリポジトリで実装し、作業ブランチにコミットする。",
        "3. テストとリンタが通ることを確認する。",
        f"4. `git push -u origin {branch}` のうえ `gh pr create` で PR を作る。",
        "   PR 本文には「判断ポイント」節を設け、低影響の判断とその理由を記載する。",
        "",
        "人間の判断が必要になったら ask_human MCP ツールで質問を登録する。",
        f'issue パラメータには "{issue.ref}" を渡す。',
        "質問は不可逆または高影響の判断に限る。低影響の判断は推奨案で進めて PR に記載する。",
        "質問を登録したら回答を待たず、現状を要約して即座に終了する。",
    ]
    if answers:
        lines += ["", "過去の質問への回答(これを前提に続行する):"]
        for q in answers:
            note = f"(補足: {q.answer_note})" if q.answer_note else ""
            lines.append(f"- Q: {q.question}")
            lines.append(f"  A: {q.answer} {note}".rstrip())
    return "\n".join(lines)


def write_mcp_config(dbdir: Path) -> Path:
    """ask_human を提供する MCP 設定ファイルを書き、パスを返す。"""
    config = {
        "mcpServers": {
            "alir": {
                "command": "alir",
                "args": ["mcp"],
                "env": {"ALIR_DATA_DIR": str(dbdir)},
            }
        }
    }
    path = dbdir / "mcp.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_claude(*, prompt: str, cwd: Path, dbdir: Path, skip_permissions: bool = False) -> RunResult:
    """claude -p を 1 回実行し、結果 JSON から session_id と成否を取り出す。"""
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--mcp-config",
        str(write_mcp_config(dbdir)),
        "--allowedTools",
        "mcp__alir__ask_human",
    ]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    env = dict(os.environ, ALIR_DATA_DIR=str(dbdir))
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, check=False)
    session_id: str | None = None
    is_error = proc.returncode != 0
    try:
        data = json.loads(proc.stdout)
        session_id = data.get("session_id")
        is_error = is_error or bool(data.get("is_error", False))
    except ValueError:
        pass
    exit_code = proc.returncode if proc.returncode != 0 else (1 if is_error else 0)
    return RunResult(exit_code=exit_code, session_id=session_id, output=proc.stdout or proc.stderr)


def process_issue(
    dbdir: Path,
    iid: int,
    *,
    runner: Runner = run_claude,
    worktree: WorktreeSetup = setup_worktree,
    skip_permissions: bool = False,
) -> Issue:
    """Issue を 1 件処理し、最終状態の Issue を返す。"""
    conn = db.connect(dbdir)
    issue = registry.get(conn, iid)
    registry.set_status(conn, iid, registry.STATUS_RUNNING)
    try:
        path, branch = worktree(issue)
        answered = [
            q
            for q in questions.list_questions(conn, status=questions.STATUS_ANSWERED)
            if q.issue == issue.ref
        ]
        prompt = build_prompt(issue, branch, answers=answered)
        result = runner(prompt=prompt, cwd=path, dbdir=dbdir, skip_permissions=skip_permissions)
    except Exception:
        registry.set_status(conn, iid, registry.STATUS_FAILED)
        raise

    conn = db.connect(dbdir)
    has_open_question = any(
        q.issue == issue.ref for q in questions.list_questions(conn, status=questions.STATUS_OPEN)
    )
    if has_open_question:
        status = registry.STATUS_PARKED
    elif result.exit_code != 0:
        status = registry.STATUS_FAILED
    else:
        status = registry.STATUS_DONE
    return registry.set_status(conn, iid, status, session_id=result.session_id, branch=branch)


def run_loop(
    dbdir: Path,
    *,
    once: bool = False,
    parallel: int = 1,
    interval: float = 30.0,
    runner: Runner = run_claude,
    worktree: WorktreeSetup = setup_worktree,
    skip_permissions: bool = False,
    log: Callable[[str], None] = print,
) -> None:
    """queued の Issue を優先度順に処理し続ける。once ならキューを使い切ったら終える。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        active: dict[concurrent.futures.Future[Issue], int] = {}
        while True:
            while len(active) < parallel:
                conn = db.connect(dbdir)
                issue = registry.next_queued(conn)
                if issue is None:
                    break
                log(f"start #{issue.id} {issue.ref}")
                registry.set_status(conn, issue.id, registry.STATUS_RUNNING)
                future = pool.submit(
                    process_issue,
                    dbdir,
                    issue.id,
                    runner=runner,
                    worktree=worktree,
                    skip_permissions=skip_permissions,
                )
                active[future] = issue.id

            if not active:
                if once:
                    return
                time.sleep(interval)
                continue

            done, _ = concurrent.futures.wait(
                active, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                iid = active.pop(future)
                try:
                    finished = future.result()
                    log(f"finish #{finished.id} {finished.ref}: {finished.status}")
                except Exception as exc:  # noqa: BLE001
                    log(f"error #{iid}: {exc}")
