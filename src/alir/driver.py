"""ループドライバ: レジストリの Issue を worktree 上で Claude Code に処理させる。

1 Issue の処理は「worktree を用意し、claude -p を実行し、結果で状態を遷移させる」の
3 段からなる。質問(ask_human)が登録されていれば parked、失敗なら failed、それ以外は done。
claude の実行と worktree の用意は差し替え可能にしてテストする。
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import iceql

from alir import (
    ci,
    control,
    db,
    importer,
    questions,
    registry,
    reports,
    resume,
    retry,
    settings,
    usage,
)
from alir.registry import Issue

BACKOFF_INITIAL = 60.0
BACKOFF_FACTOR = 2.0
BACKOFF_MAX = 3600.0

_RATE_LIMIT_RE = re.compile(r"rate.?limit|usage limit|limit reached", re.IGNORECASE)


class DriverError(Exception):
    """worktree の用意や claude の起動に関する失敗。"""


class RateLimited(DriverError):
    """レート制限に当たった。Issue は queued に戻されている。"""


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    session_id: str | None
    output: str
    usage: dict[str, Any] = field(default_factory=dict)
    rate_limited: bool = False


Runner = Callable[..., RunResult]
WorktreeSetup = Callable[..., Path]


def _git(workdir: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(workdir), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise DriverError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _default_branch_ref(workdir: Path) -> str | None:
    """新しいブランチの基点となるデフォルトブランチの ref を返す。

    origin/HEAD の指す先 → origin/main → origin/master → main → master の順で
    探し、見つからなければ None(現在の HEAD から分岐する)。
    """
    proc = subprocess.run(
        ["git", "-C", str(workdir), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    for ref in ("origin/main", "origin/master", "main", "master"):
        verify = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            check=False,
        )
        if verify.returncode == 0:
            return ref
    return None


def _current_branch(path: Path) -> str | None:
    """worktree の現在のブランチ名を返す。読めなければ None。"""
    proc = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _setup_push_worktree(workdir: Path, branch: str, root: Path) -> Path:
    """push 先ブランチをチェックアウトした共有 worktree を用意する。

    直接 push 運用では同じ workdir の全 Issue がこの worktree
    (<repo>-alir/branches/<ブランチ名>)で順番に作業する。再利用時は origin の
    進みに追従する(fast-forward のみ。追従できなくても続行する)。
    push 先ブランチが workdir 側でチェックアウト中だと git が worktree の
    作成を拒否するので、専用のブランチを指定する運用を想定する。
    """
    # "/" を含むブランチ名はそのまま入れ子のディレクトリにする
    # (feat/x と feat-x が同じパスに写像されて別ブランチの worktree を
    # 誤って再利用することを避ける)
    path = root / "branches" / branch
    if path.exists():
        with contextlib.suppress(DriverError):
            _git(path, "pull", "--ff-only", "origin", branch)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    # fetch の失敗(オフライン・リモート未作成など)は致命的でないので続行する
    with contextlib.suppress(DriverError):
        _git(workdir, "fetch", "origin", branch)
    if _git(workdir, "branch", "--list", branch).strip():
        _git(workdir, "worktree", "add", str(path), branch)
        with contextlib.suppress(DriverError):
            _git(path, "pull", "--ff-only", "origin", branch)
        return path
    remote = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "--verify", "--quiet", f"origin/{branch}"],
        capture_output=True,
        check=False,
    )
    if remote.returncode == 0:
        _git(workdir, "worktree", "add", "-b", branch, str(path), f"origin/{branch}")
        return path
    # リモートにもまだないブランチはデフォルトブランチの先端から新設する
    args = ["worktree", "add", "-b", branch, str(path)]
    base = _default_branch_ref(workdir)
    if base is not None:
        args.append(base)
    _git(workdir, *args)
    return path


def setup_worktree(issue: Issue, branch: str, *, push: bool = False) -> Path:
    """Issue 用の worktree と指定のブランチを用意し、worktree のパスを返す。

    worktree は対象リポジトリの隣(<repo>-alir/issue-<n>)に置く。
    すでに存在すればそのまま再利用する(park からの再開)。
    新しくブランチを切るときはデフォルトブランチ(main / master)を fetch で
    最新化し、その先端を基点にする。workdir のチェックアウトは変更しない。
    push が真(直接 push 運用)のときは Issue ごとではなく push 先ブランチの
    共有 worktree を使う。
    """
    workdir = Path(issue.workdir)
    root = workdir.parent / f"{workdir.name}-alir"
    if push:
        return _setup_push_worktree(workdir, branch, root)
    path = root / f"issue-{issue.number}"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if _git(workdir, "branch", "--list", branch).strip():
        _git(workdir, "worktree", "add", str(path), branch)
        return path

    base = _default_branch_ref(workdir)
    if base is not None and base.startswith("origin/"):
        # fetch の失敗(オフラインなど)は致命的でないので手元の ref から続行する
        with contextlib.suppress(DriverError):
            _git(workdir, "fetch", "origin", base.removeprefix("origin/"))
    args = ["worktree", "add", "-b", branch, str(path)]
    if base is not None:
        args.append(base)
    _git(workdir, *args)
    return path


def build_prompt(
    issue: Issue,
    branch: str,
    *,
    answers: list[questions.Question],
    past_reports: list[reports.Report] | None = None,
    branch_template: str | None = None,
    ci_failed_pr: str | None = None,
    direct_push: bool = False,
) -> str:
    """1 Issue を処理するセッションのプロンプトを組み立てる。

    issue.mode に応じて手順を変える。auto はセッションが実装かリファインメントかを
    判断し、implement / refine は指定された作業だけを行う。
    issue.note(登録者の補足)があればプロンプトに含める。
    branch_template にセッションが決める変数({type} / {summary})が含まれる場合は、
    改名の指示を加える。ci_failed_pr があれば、その PR の CI が失敗している旨と
    修正の指示を加える。direct_push が真なら PR を作らず作業ブランチへ直接
    push し、push 後に対象 Issue をクローズする手順にする。
    """
    if direct_push:
        implement_steps = [
            "3. このリポジトリで実装し、作業ブランチにコミットする。",
            "4. テストとリンタが通ることを確認する。",
            f"5. `git push origin {branch}` で作業ブランチへ直接 push する。PR は作らない。",
            "   低影響の判断とその理由はコミットメッセージに記載する。",
            f"6. `gh issue close {issue.url} --comment` で対象 Issue をクローズする。",
            "   コメントには実施内容と対応コミットを記載する。クローズするのは実装を",
            "   push した場合だけで、リファインメントで終えた場合・park した場合・",
            "   何もしなかった場合はクローズしない。",
            '7. report_result MCP ツール(outcome="implemented")で実施内容を 1 行で報告する。',
            f'   issue パラメータには "{issue.ref}" を渡す。',
        ]
    else:
        implement_steps = [
            "3. このリポジトリで実装し、作業ブランチにコミットする。",
            "4. テストとリンタが通ることを確認する。",
            f"5. `git push -u origin {branch}` のうえ `gh pr create` で PR を作る。",
            "   PR 本文には「判断ポイント」節を設け、低影響の判断とその理由を記載する。",
            '6. report_result MCP ツール(outcome="implemented")で実施内容を 1 行で報告する。',
            f'   issue パラメータには "{issue.ref}"、PR を作成した場合は pr_url も渡す。',
        ]
    refine_steps = [
        "3. コードベースを調査し、仕様案・実装方針・判断ポイントを整理する。",
        f"4. 整理した内容を `gh issue comment {issue.url}` で Issue に投稿する。",
        "5. 人間の判断が必要な点は ask_human MCP ツールで質問する。",
        '6. 実装は行わず、report_result MCP ツール(outcome="refined")で報告して終了する。',
    ]
    lines = [
        "あなたは GitHub Issue を自律的に処理するエージェントである。",
        "",
        f"対象 Issue: {issue.url}",
        f"作業ブランチ: {branch}(すでにチェックアウト済み)",
    ]
    if direct_push:
        lines += [
            "",
            f"このリポジトリは PR を作らず、{branch} ブランチに直接 push して",
            "開発を続ける運用である。新しいブランチや PR は作らない。",
        ]
    if ci_failed_pr is not None:
        lines += [
            "",
            "この Issue で過去のセッションが作成した PR の CI が失敗している。",
            f"対象 PR: {ci_failed_pr}",
            f"`gh pr checks {ci_failed_pr}` などで失敗内容を確認し、修正をコミットして",
            "同じブランチに push することで既存の PR を更新する。新しい PR は作らない。",
        ]
    if issue.note:
        lines += ["", "登録者からの補足(要件の一部として扱う):"]
        lines += [f"  {line}" for line in issue.note.splitlines()]
    lines += [
        "",
        "手順:",
        f"1. `gh issue view {issue.url} --comments` で Issue とコメントを読み、要件を把握する。",
    ]
    if issue.mode == registry.MODE_IMPLEMENT:
        lines += [
            "2. この Issue は実装を指定して登録されている。リファインメントは行わず、",
            "   Issue・コメント・上記の補足から要件を確定して実装に進む。",
            *implement_steps,
        ]
    elif issue.mode == registry.MODE_REFINE:
        lines += [
            "2. この Issue はリファインメントを指定して登録されている。実装は行わない。",
            *refine_steps,
        ]
    else:
        lines += [
            "2. Issue が実装に着手できる程度に具体化されているか判断する。",
            "   実装に進んでよいのは次のいずれかの場合だけである。",
            "   - Issue 本文に受け入れ条件(完了条件)と対象範囲が明文化されている",
            "   - 過去のリファインメント(下記の実施記録や Issue コメント)で仕様が整理済みである",
            "   どちらにも当てはまらなければ、内容が推測できそうでもリファインメントを行う。",
            "",
            "十分な場合(実装):",
            *implement_steps,
            "",
            "不十分な場合(リファインメント):",
            *refine_steps,
            "   次のセッションがリファインメント結果を前提に実装する。",
        ]
    lines += [
        "",
        "セッションの終了前に必ず report_result を 1 回呼ぶ。",
        "何もする必要がなかった場合(実装済みの PR がすでにある、など)も、",
        'その旨を summary にして report_result(outcome="implemented")で報告する。',
        "",
        "実行環境の注意:",
        "このセッションは応答を終えた時点で終了し、再開されない。",
        "バックグラウンド実行(run_in_background)や Monitor の完了通知を待つ形で",
        "応答を終えると、通知は届かず、そこまでの作業がすべて失われる。",
        "テストなどの長いコマンドはフォアグラウンドで実行して完了まで待つ。",
        "",
        "作業の節目(調査を終えた、実装に入った、テストを回した、PR を作った、など)ごとに、",
        "report_progress MCP ツールで進捗を 1 行報告する。"
        f'issue パラメータには "{issue.ref}" を渡す。',
        'report_progress の応答に "stop": true が含まれていたら使用量の上限が近い。',
        "作業を中断して自己完結する範囲でコミットし、どこまで進んだかを",
        'report_result(outcome="aborted")で報告して直ちに終了する。',
        "",
        "人間の判断が必要になったら ask_human MCP ツールで質問を登録する。",
        f'issue パラメータには "{issue.ref}" を渡す。',
        "質問は不可逆または高影響の判断に限る。低影響の判断は推奨案で進めて"
        + ("コミットメッセージに記載する。" if direct_push else " PR に記載する。"),
        "質問を登録したら回答を待たず、そこまでの実施内容を report_result で報告して"
        "即座に終了する。",
        "",
        "この Issue の範囲外の後続作業(切り出すべき修正、作業中に見つけた別の問題など)は、",
        "このセッションでは着手しない。後続セッションに任せる価値があるものだけ、",
        "`gh issue create` で Issue を作成したうえで enqueue_issue MCP ツールで登録する。",
        f'source_issue パラメータには "{issue.ref}" を渡す。',
    ]
    if (
        branch_template is not None
        and settings.has_session_placeholders(branch_template)
        and issue.mode != registry.MODE_REFINE  # リファインメントのみなら push しない
    ):
        lines += [
            "",
            "ブランチ名について:",
            f'現在のブランチ名 "{branch}" は仮名である。実装に入る前に、',
            f'テンプレート "{branch_template}" の {{type}} に作業種別(bugfix / feature など)、',
            "{summary} に Issue 内容の短い英語スラッグ(小文字とハイフン)を当てはめた名前を決め、",
            "`git branch -m <新しい名前>` で改名してから作業する。push もその名前で行う。",
        ]
    if past_reports:
        lines += ["", "これまでの実施記録(新しい順):"]
        for report in past_reports:
            lines.append(f"- [{report.outcome}] {report.summary}")
    if answers:
        lines += ["", "過去の質問への回答(これを前提に続行する):"]
        for q in answers:
            note = f"(補足: {q.answer_note})" if q.answer_note else ""
            lines.append(f"- Q: {q.question}")
            lines.append(f"  A: {q.answer} {note}".rstrip())
    return "\n".join(lines)


def write_mcp_config(dbdir: Path, *, mcp_url: str | None = None) -> Path:
    """ask_human を提供する MCP 設定ファイルを書き、パスを返す。

    mcp_url があれば HTTP(alir serve への接続)、なければ stdio(alir mcp の起動)。
    """
    server_config: dict[str, Any]
    if mcp_url:
        server_config = {"type": "http", "url": mcp_url}
    else:
        server_config = {
            "command": "alir",
            "args": ["mcp"],
            "env": {"ALIR_DATA_DIR": str(dbdir)},
        }
    config = {"mcpServers": {"alir": server_config}}
    path = dbdir / "mcp.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_claude(
    *,
    prompt: str,
    cwd: Path,
    dbdir: Path,
    skip_permissions: bool = False,
    mcp_url: str | None = None,
    resume_session_id: str | None = None,
) -> RunResult:
    """claude -p を 1 回実行し、結果 JSON から session_id と成否を取り出す。

    resume_session_id があれば --resume を付け、park 時のセッションを文脈ごと再開する。
    """
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--mcp-config",
        str(write_mcp_config(dbdir, mcp_url=mcp_url)),
        "--allowedTools",
        "mcp__alir__ask_human,mcp__alir__report_result,mcp__alir__report_progress,"
        "mcp__alir__enqueue_issue",
    ]
    if resume_session_id is not None:
        cmd += ["--resume", resume_session_id]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    env = dict(os.environ, ALIR_DATA_DIR=str(dbdir))
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, check=False)
    session_id: str | None = None
    run_usage: dict[str, Any] = {}
    is_error = proc.returncode != 0
    try:
        data = json.loads(proc.stdout)
        session_id = data.get("session_id")
        run_usage = data.get("usage") or {}
        is_error = is_error or bool(data.get("is_error", False))
    except ValueError:
        pass
    output = proc.stdout or proc.stderr
    exit_code = proc.returncode if proc.returncode != 0 else (1 if is_error else 0)
    rate_limited = is_error and _RATE_LIMIT_RE.search(output) is not None
    return RunResult(
        exit_code=exit_code,
        session_id=session_id,
        output=output,
        usage=run_usage,
        rate_limited=rate_limited,
    )


def process_issue(
    dbdir: Path,
    iid: int,
    *,
    runner: Runner = run_claude,
    worktree: WorktreeSetup = setup_worktree,
    skip_permissions: bool = False,
) -> Issue:
    """Issue を 1 件処理し、最終状態の Issue を返す。

    ブランチ名は初回はテンプレート(settings)から作り、
    再開時は前回のブランチをそのまま使う。workdir が直接 push 運用
    (settings.push_branch)なら再開時も含め常に現在の push 先ブランチを使い、
    PR を作らない手順のプロンプトにする。
    park からの再開(回答検知が resume_session を記録済み)は、settings で
    無効化されていなければ --resume で前回セッションを文脈ごと再開する。
    resume が失敗した場合(セッションの期限切れなど)は新規セッションに
    フォールバックする。記録は rate limit 以外の完了で消費する。
    セッションがリファインメントだけで終えた(outcome="refined"の報告)場合は
    queued に戻し、次のセッションが実装する。ただし mode=refine の Issue は
    リファインメントが最終成果なので done にする。
    report_result を呼ばずに終了した場合は途中死とみなして 1 回だけ queued に戻し、
    それでも連続したら failed にする。
    """
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = db.connect(dbdir)
    issue = registry.get(conn, iid)
    registry.set_status(conn, iid, registry.STATUS_RUNNING)
    answered = [
        q
        for q in questions.list_questions(conn, status=None)
        if q.issue == issue.ref and q.answer is not None
    ]
    past_reports = reports.list_reports(conn, issue=issue.ref, limit=3)
    template = settings.branch_template(conn)
    push_branch = settings.push_branch(conn, issue.workdir)
    if push_branch is not None:
        # 直接 push 運用は保存済みブランチより現在の設定を優先する。
        # 通常運用から切り替えた Issue や、push 先を変更した後の再開でも
        # 常に設定された push 先ブランチで作業する。
        branch = push_branch
    else:
        branch = issue.branch or settings.render_branch(template, issue)
    ci_failed_pr = ci.take_ci_failure(conn, issue.ref)
    resume_sid = control.resume_session(conn, issue.ref) if settings.resume_enabled(conn) else None
    try:
        path = worktree(issue, branch, push=push_branch is not None)
        prompt = build_prompt(
            issue,
            branch,
            answers=answered,
            past_reports=past_reports,
            branch_template=None if (issue.branch or push_branch) else template,
            ci_failed_pr=ci_failed_pr,
            direct_push=push_branch is not None,
        )
        if resume_sid is not None:
            result = runner(
                prompt=prompt,
                cwd=path,
                dbdir=dbdir,
                skip_permissions=skip_permissions,
                resume_session_id=resume_sid,
            )
            if result.exit_code != 0 and not result.rate_limited:
                # セッションの期限切れなどで resume できなかったとみなし、
                # 新規セッションでやり直す(回答はプロンプトに含まれている)
                control.log_event(
                    db.connect(dbdir),
                    f"issue #{iid} の resume に失敗したため新規セッションで再実行する",
                )
                result = runner(
                    prompt=prompt, cwd=path, dbdir=dbdir, skip_permissions=skip_permissions
                )
        else:
            result = runner(prompt=prompt, cwd=path, dbdir=dbdir, skip_permissions=skip_permissions)
    except Exception:
        registry.set_status(db.connect(dbdir), iid, registry.STATUS_FAILED)
        raise

    # セッションが git branch -m で改名した場合に備え、実際のブランチ名を取り込む
    # (直接 push 運用は push 先ブランチが固定なので取り込まない)
    if push_branch is None:
        actual = _current_branch(path)
        if actual:
            branch = actual

    conn = db.connect(dbdir)
    if result.usage:
        usage.record_run(
            conn, issue_ref=issue.ref, session_id=result.session_id, usage=result.usage
        )
    if result.rate_limited:
        # 再開の印は残し、rate limit 明けの再試行でも --resume を使えるようにする
        registry.set_status(
            conn, iid, registry.STATUS_QUEUED, session_id=result.session_id, branch=branch
        )
        raise RateLimited(f"issue #{iid} hit a rate limit; requeued")
    # 再開の印は 1 回の実行(フォールバック含む)で消費する
    control.clear_resume_session(conn, issue.ref)

    has_open_question = any(
        q.issue == issue.ref for q in questions.list_questions(conn, status=questions.STATUS_OPEN)
    )
    run_reports = [
        r for r in reports.list_reports(conn, issue=issue.ref) if r.created_at >= started_at
    ]
    if run_reports:
        control.clear_missing_report_requeued(conn, issue.ref)
    # refine 指定の Issue はリファインメント完了が最終成果なので requeue しない
    requeue_outcomes = (
        (reports.OUTCOME_ABORTED,)
        if issue.mode == registry.MODE_REFINE
        else (reports.OUTCOME_REFINED, reports.OUTCOME_ABORTED)
    )
    requeue_reported = any(r.outcome in requeue_outcomes for r in run_reports)
    # stop 指示を受けたのに報告なしで終わった場合は中断とみなして queued に戻す
    # (報告があるならその outcome を信頼する)
    stop_at = control.stop_instructed_at(conn, issue.ref)
    stopped_without_report = stop_at is not None and stop_at >= started_at and not run_reports
    if has_open_question:
        status = registry.STATUS_PARKED
    elif result.exit_code != 0:
        status = registry.STATUS_FAILED
    elif requeue_reported or stopped_without_report:
        status = registry.STATUS_QUEUED
    elif not run_reports:
        # report_result なしの正常終了は途中死とみなす(プロンプトは報告を義務づけている)。
        # 1 回だけ queued に戻して再試行し、連続したら failed で人間に返す。
        if control.missing_report_requeued_at(conn, issue.ref) is not None:
            control.clear_missing_report_requeued(conn, issue.ref)
            control.log_event(
                conn, f"issue #{iid} が連続して report_result なしで終了したため failed にした"
            )
            status = registry.STATUS_FAILED
        else:
            control.mark_missing_report_requeued(conn, issue.ref)
            control.log_event(
                conn, f"issue #{iid} が report_result なしで終了したため queued に戻した"
            )
            status = registry.STATUS_QUEUED
    else:
        status = registry.STATUS_DONE
    return registry.set_status(conn, iid, status, session_id=result.session_id, branch=branch)


def next_startable(conn: iceql.Connection) -> Issue | None:
    """次に開始できる queued の Issue を返す。なければ None。

    直接 push 運用の workdir は push 先ブランチの worktree を全 Issue で
    共有するため、同じ workdir の Issue が実行中の間は開始せず、
    後続の別 workdir の Issue を先に開始する。
    """
    running = {i.workdir for i in registry.list_issues(conn, status=registry.STATUS_RUNNING)}
    for issue in registry.list_issues(conn, status=registry.STATUS_QUEUED):
        if issue.workdir in running and settings.push_branch(conn, issue.workdir) is not None:
            continue
        return issue
    return None


def run_loop(
    dbdir: Path,
    *,
    once: bool = False,
    parallel: int = 1,
    interval: float = 30.0,
    runner: Runner = run_claude,
    worktree: WorktreeSetup = setup_worktree,
    skip_permissions: bool = False,
    question_timeout: timedelta = timedelta(hours=12),
    budget: usage.Budget | None = None,
    usage_probe: Callable[[], usage.UsageStatus | None] | None = None,
    usage_check_ttl: float = 300.0,
    usage_threshold: float | None = None,
    import_interval: float = importer.DEFAULT_INTERVAL,
    import_fetch: importer.Fetch = importer.fetch_labeled_issues,
    pr_status_fetch: ci.PrStatusFetch | None = None,
    ci_check_ttl: float = 300.0,
    log: Callable[[str], None] = print,
) -> None:
    """queued の Issue を優先度順に処理し続ける。once ならキューを使い切ったら終える。

    各サイクルの先頭で timeout を過ぎた質問を処理し、回答が揃った parked の
    Issue を queued に戻す。failed の Issue は上限(settings.retry_limit)まで
    バックオフ付きで自動的に queued に戻し、上限に達したら通知する。
    手動の一時停止フラグ、公式レート制限使用率の
    閾値超過、トークン予算の閾値超過、レート制限時のバックオフ中は
    新規 Issue を開始しない(実行中のものは完了まで走らせる)。

    usage_probe(通常は usage.fetch_usage_status)は初回サイクルで呼ばれ、
    以後 usage_check_ttl 秒キャッシュされる。セッションが完了するたびに
    キャッシュを破棄し、次の開始前に必ず新しい使用率で判定する。
    イベントは events テーブルにも記録し、Web UI から参照できるようにする。

    取り込み対象(importer)が設定されていれば、import_interval 秒ごとに
    ラベル付き Issue を検索してキュー末尾へ登録する。一時停止中も取り込みは
    続ける(セッションを開始しないだけで、キューへの登録は無害なため)。
    実行のたびに確認結果(対象数・発見数・登録数)をログへ 1 行出し、
    新規ゼロでも稼働していることを確認できるようにする。

    pr_status_fetch(通常は ci.fetch_pr_status)を渡すと、done の Issue の
    PR を ci_check_ttl 秒間隔で確認し、CI が失敗していれば queued に戻す。
    マージ済み・クローズ済みの PR は以後の確認から除外する。
    """

    def emit(message: str) -> None:
        log(message)
        # ログの永続化に失敗してもループは止めない
        with contextlib.suppress(Exception):
            control.log_event(db.connect(dbdir), message)

    # 前回のクラッシュや強制終了で running のまま残った Issue を回収する。
    # このドライバはまだ何も実行していないので、起動時点の running は遺骸とみなせる
    # (ドライバの多重起動は想定しない)。
    conn = db.connect(dbdir)
    for orphan in registry.list_issues(conn, status=registry.STATUS_RUNNING):
        registry.set_status(conn, orphan.id, registry.STATUS_QUEUED)
        emit(f"recover #{orphan.id} {orphan.ref}: running -> queued")

    backoff = BACKOFF_INITIAL
    backoff_until = 0.0
    last_pause_reason: str | None = None
    probe_status: usage.UsageStatus | None = None
    next_probe = 0.0  # monotonic 時刻。0 なので初回サイクルで必ず取得する
    next_import = 0.0  # 自動取り込みの次回実行時刻(monotonic)
    next_ci_check = 0.0
    resolved_prs: set[str] = set()  # マージ済み・クローズ済みで監視を終えた PR
    if usage_threshold is not None:
        threshold = usage_threshold
    elif budget is not None:
        threshold = budget.threshold
    else:
        threshold = usage.DEFAULT_THRESHOLD
    # MCP 側(report_progress の中断判定)が同じ閾値を参照できるようにする
    control.set_value(db.connect(dbdir), control.KEY_USAGE_THRESHOLD, str(threshold))
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        active: dict[concurrent.futures.Future[Issue], int] = {}
        while True:
            conn = db.connect(dbdir)
            control.heartbeat(conn)
            for q in resume.expire_timeouts(conn, timeout=question_timeout):
                emit(f"question #{q.id} expired: proceeding with recommended")
            for requeued in resume.requeue_answered(conn):
                emit(f"requeue #{requeued.id} {requeued.ref}")

            retry_limit = settings.retry_limit(conn)
            retried = retry.process_failed(conn, limit=retry_limit)
            for requeued in retried.requeued:
                emit(
                    f"retry #{requeued.id} {requeued.ref}: failed -> queued "
                    f"({requeued.retries}/{retry_limit})"
                )
            for stopped in retried.exhausted:
                emit(f"retry limit reached #{stopped.id} {stopped.ref}: kept failed, notified")

            if time.monotonic() >= next_import:
                next_import = time.monotonic() + import_interval
                outcome = importer.run_import(conn, fetch=import_fetch)
                if outcome.checked:
                    # 稼働確認用。新規ゼロの周回も 1 行残す。events には
                    # 残さない(5 分ごとの定期実行で埋まるのを避ける)。
                    log(
                        f"import check: {outcome.checked} targets, "
                        f"found {outcome.found}, imported {len(outcome.imported)}"
                    )
                for imported in outcome.imported:
                    emit(f"import #{imported.id} {imported.ref}")
                for error in outcome.errors:
                    emit(f"import error: {error}")

            if pr_status_fetch is not None and time.monotonic() >= next_ci_check:
                next_ci_check = time.monotonic() + ci_check_ttl
                for requeued, pr_url in ci.requeue_ci_failures(
                    conn, fetch=pr_status_fetch, resolved=resolved_prs
                ):
                    emit(f"requeue #{requeued.id} {requeued.ref}: PR CI failed ({pr_url})")

            # 開始候補がないときは使用率を取りに行かない(アイドル時の無駄な実行を避ける)
            has_queued = next_startable(conn) is not None
            if usage_probe is not None and has_queued and time.monotonic() >= next_probe:
                probe_status = usage_probe()
                next_probe = time.monotonic() + usage_check_ttl
                if probe_status is not None:
                    control.set_value(
                        conn,
                        control.KEY_USAGE_STATUS,
                        json.dumps(
                            [[w.label, w.used_percentage] for w in probe_status.windows],
                            ensure_ascii=False,
                        ),
                    )

            paused: str | None = None
            if control.is_paused(conn):
                paused = "paused manually"
            if paused is None and probe_status is not None:
                paused = usage.status_pause_reason(probe_status, threshold=threshold)
            if paused is None and budget is not None:
                paused = usage.pause_reason(conn, budget)
            if paused != last_pause_reason:
                emit(f"pause: {paused}" if paused else "resume")
                last_pause_reason = paused
            in_backoff = time.monotonic() < backoff_until

            while not paused and not in_backoff and len(active) < parallel:
                conn = db.connect(dbdir)
                # 取得と running への遷移を原子的に行い、
                # 別プロセスのドライバとの二重取得を防ぐ
                with db.transaction(conn):
                    issue = next_startable(conn)
                    if issue is not None:
                        registry.set_status(conn, issue.id, registry.STATUS_RUNNING)
                if issue is None:
                    break
                emit(f"start #{issue.id} {issue.ref}")
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

            # timeout 付きで待ち、セッション実行中もサイクルを回す。
            # これによりハートビートの更新、空き枠への補充、
            # 一時停止フラグの反映がセッション完了を待たずに行われる。
            done, _ = concurrent.futures.wait(
                active, timeout=interval, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                iid = active.pop(future)
                try:
                    finished = future.result()
                    emit(f"finish #{finished.id} {finished.ref}: {finished.status}")
                    backoff = BACKOFF_INITIAL
                except RateLimited as exc:
                    emit(f"rate limited #{iid}: {exc}; backoff {int(backoff)}s")
                    backoff_until = time.monotonic() + backoff
                    backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)
                except Exception as exc:  # noqa: BLE001
                    emit(f"error #{iid}: {exc}")
                # セッションが終わるたびに使用率を取り直す(次の開始前の確認)
                next_probe = 0.0
