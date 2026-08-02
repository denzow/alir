"""alir CLI: 質問の一覧・回答と MCP サーバーの起動。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import click

import alir
from alir import db, importer, questions, registry, usage
from alir.config import data_dir
from alir.importer import ImporterError
from alir.questions import Question, QuestionError
from alir.registry import RegistryError


@click.group()
@click.version_option(alir.__version__)
def main() -> None:
    """Claude Code の自律ループを支える非同期判断サービス。"""


def _format_question(q: Question) -> str:
    lines = [f"#{q.id} [{q.status}] {q.issue} (impact: {q.impact})"]
    lines.append(f"  Q: {q.question}")
    for i, option in enumerate(q.options, start=1):
        mark = "*" if option == q.recommended else " "
        lines.append(f"  {mark}{i}) {option}")
    lines.append(f"  timeout: {q.timeout_action}")
    if q.answer is not None:
        note = f" ({q.answer_note})" if q.answer_note else ""
        lines.append(f"  A: {q.answer}{note}")
    return "\n".join(lines)


@main.command("questions")
@click.option("--all", "show_all", is_flag=True, help="回答済み・期限切れも含めて表示する")
def questions_cmd(show_all: bool) -> None:
    """質問を一覧する(既定は未回答のみ)。"""
    conn = db.connect(data_dir())
    status = None if show_all else questions.STATUS_OPEN
    items = questions.list_questions(conn, status=status)
    if not items:
        click.echo("no questions")
        return
    click.echo("\n\n".join(_format_question(q) for q in items))


@main.command("answer")
@click.argument("question_id", type=int)
@click.argument("choice")
@click.argument("note", required=False)
def answer_cmd(question_id: int, choice: str, note: str | None) -> None:
    """質問に回答する。

    CHOICE は選択肢番号(1 始まり)または自由記述。NOTE は任意の補足。
    """
    conn = db.connect(data_dir())
    try:
        q = questions.answer(conn, question_id, choice, note=note)
    except QuestionError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"answered #{q.id}: {q.answer}")


@main.command("mcp")
def mcp_cmd() -> None:
    """MCP サーバーとして起動する(stdio)。"""
    from alir.mcp_server import create_server

    create_server(data_dir()).run()


@main.group("issues", invoke_without_command=True)
@click.pass_context
def issues_group(ctx: click.Context) -> None:
    """Issue レジストリを操作する。サブコマンドなしなら一覧する。"""
    if ctx.invoked_subcommand is None:
        conn = db.connect(data_dir())
        items = registry.list_issues(conn)
        if not items:
            click.echo("no issues")
            return
        for issue in items:
            title = f" {issue.title}" if issue.title else ""
            click.echo(f"#{issue.id} [{issue.status}] {issue.ref}{title} ({issue.workdir})")


@issues_group.command("add")
@click.argument("url")
@click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="対象リポジトリのローカルパス",
)
@click.option(
    "--mode",
    type=click.Choice(registry.MODES),
    default=registry.MODE_AUTO,
    help="セッションに求める作業種別。auto はセッションが判断する",
)
@click.option("--note", default=None, help="セッションのプロンプトに含める補足コメント")
def issues_add(url: str, workdir: Path, mode: str, note: str | None) -> None:
    """GitHub Issue の URL を queued として登録する。タイトルは gh で取得する。"""
    conn = db.connect(data_dir())
    title = registry.fetch_title(url)
    try:
        issue = registry.add(
            conn, url=url, workdir=str(workdir.resolve()), title=title, mode=mode, note=note
        )
    except RegistryError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"added #{issue.id}: {issue.ref} {issue.title or ''}".rstrip())


@issues_group.command("import")
@click.option("--label", required=True, help="取り込む Issue のラベル")
@click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="対象リポジトリのローカルパス",
)
def issues_import(label: str, workdir: Path) -> None:
    """gh CLI でラベル付き Issue を検索して一括登録する(補助コマンド)。"""
    try:
        items = importer.fetch_labeled_issues(str(workdir), label)
    except ImporterError as exc:
        raise click.ClickException(str(exc)) from exc
    conn = db.connect(data_dir())
    for item in items:
        try:
            issue = registry.add(
                conn, url=item["url"], workdir=str(workdir.resolve()), title=item.get("title")
            )
            click.echo(f"added #{issue.id}: {issue.ref}")
        except RegistryError as exc:
            click.echo(f"skipped: {exc}")


@issues_group.group("targets", invoke_without_command=True)
@click.pass_context
def issues_targets(ctx: click.Context) -> None:
    """自動取り込みの対象を操作する。サブコマンドなしなら一覧する。"""
    if ctx.invoked_subcommand is None:
        conn = db.connect(data_dir())
        items = importer.list_targets(conn)
        if not items:
            click.echo("no targets")
            return
        for target in items:
            click.echo(f"{target.label} ({target.workdir})")


@issues_targets.command("add")
@click.option("--label", required=True, help="取り込む Issue のラベル")
@click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="対象リポジトリのローカルパス",
)
def targets_add(label: str, workdir: Path) -> None:
    """自動取り込みの対象(workdir + ラベル)を追加する。"""
    conn = db.connect(data_dir())
    try:
        target = importer.add_target(conn, workdir=str(workdir), label=label)
    except ImporterError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"added: {target.label} ({target.workdir})")


@issues_targets.command("remove")
@click.option("--label", required=True, help="取り込む Issue のラベル")
@click.option(
    "--workdir",
    type=click.Path(file_okay=False, path_type=Path),
    default=".",
    help="対象リポジトリのローカルパス",
)
def targets_remove(label: str, workdir: Path) -> None:
    """自動取り込みの対象を削除する。"""
    conn = db.connect(data_dir())
    try:
        importer.remove_target(conn, workdir=str(workdir), label=label)
    except ImporterError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"removed: {label} ({workdir})")


def _run_options(f: Callable[..., None]) -> Callable[..., None]:
    """run と serve で共通のループドライバ用オプション。"""
    options = [
        click.option("--parallel", default=1, show_default=True, type=int, help="並列実行数"),
        click.option(
            "--interval",
            default=30.0,
            show_default=True,
            type=float,
            help="キューが空のときの待機秒数",
        ),
        click.option(
            "--dangerously-skip-permissions",
            "skip_permissions",
            is_flag=True,
            help="claude に --dangerously-skip-permissions を渡す",
        ),
        click.option(
            "--question-timeout-hours",
            default=12.0,
            show_default=True,
            type=float,
            help="proceed_with_recommended の質問を期限切れにするまでの時間",
        ),
        click.option(
            "--session-budget",
            type=int,
            default=None,
            help="5 時間ウィンドウのトークン予算(未指定なら判定しない)",
        ),
        click.option(
            "--weekly-budget",
            type=int,
            default=None,
            help="週次ウィンドウのトークン予算(未指定なら判定しない)",
        ),
        click.option(
            "--budget-threshold",
            default=usage.DEFAULT_THRESHOLD,
            show_default=True,
            type=float,
            help="使用率や予算がこの割合を超えたら新規実行を止める",
        ),
        click.option(
            "--no-usage-check",
            is_flag=True,
            help="claude -p /usage による公式使用率の確認を無効にする",
        ),
        click.option(
            "--import-interval",
            default=importer.DEFAULT_INTERVAL,
            show_default=True,
            type=float,
            help="ラベル付き Issue の自動取り込みの実行間隔秒数",
        ),
        click.option(
            "--no-ci-check",
            is_flag=True,
            help="done の Issue の PR の CI 確認(失敗時の再キュー)を無効にする",
        ),
    ]
    for option in reversed(options):
        f = option(f)
    return f


def _build_budget(
    session_budget: int | None, weekly_budget: int | None, threshold: float
) -> usage.Budget | None:
    if session_budget is None and weekly_budget is None:
        return None
    return usage.Budget(
        session_tokens=session_budget, weekly_tokens=weekly_budget, threshold=threshold
    )


@main.command("run")
@click.option("--once", is_flag=True, help="キューを 1 巡したら終了する")
@_run_options
def run_cmd(
    once: bool,
    parallel: int,
    interval: float,
    skip_permissions: bool,
    question_timeout_hours: float,
    session_budget: int | None,
    weekly_budget: int | None,
    budget_threshold: float,
    no_usage_check: bool,
    import_interval: float,
    no_ci_check: bool,
) -> None:
    """ループドライバを起動し、queued の Issue を処理し続ける。"""
    from datetime import timedelta

    from alir import ci, driver

    driver.run_loop(
        data_dir(),
        once=once,
        parallel=parallel,
        interval=interval,
        skip_permissions=skip_permissions,
        question_timeout=timedelta(hours=question_timeout_hours),
        budget=_build_budget(session_budget, weekly_budget, budget_threshold),
        usage_probe=None if no_usage_check else usage.fetch_usage_status,
        usage_threshold=budget_threshold,
        import_interval=import_interval,
        pr_status_fetch=None if no_ci_check else ci.fetch_pr_status,
    )


@main.command("serve")
@click.option("--host", default="0.0.0.0", show_default=True, help="バインドするホスト")
@click.option("--port", default=8710, show_default=True, type=int, help="ポート")
@_run_options
def serve_cmd(
    host: str,
    port: int,
    parallel: int,
    interval: float,
    skip_permissions: bool,
    question_timeout_hours: float,
    session_budget: int | None,
    weekly_budget: int | None,
    budget_threshold: float,
    no_usage_check: bool,
    import_interval: float,
    no_ci_check: bool,
) -> None:
    """Web UI・MCP(HTTP)・ループドライバをワンプロセスで起動する。

    MCP は http://127.0.0.1:PORT/mcp で提供され、ドライバが起動する
    claude セッションはサブプロセスを立てずにここへ接続する。
    """
    import functools
    import threading
    from datetime import timedelta

    import uvicorn

    from alir import ci, driver
    from alir.serve import create_combined_app

    dbdir = data_dir()
    app = create_combined_app(dbdir)
    runner = functools.partial(driver.run_claude, mcp_url=f"http://127.0.0.1:{port}/mcp")
    thread = threading.Thread(
        target=driver.run_loop,
        args=(dbdir,),
        kwargs={
            "parallel": parallel,
            "interval": interval,
            "skip_permissions": skip_permissions,
            "question_timeout": timedelta(hours=question_timeout_hours),
            "budget": _build_budget(session_budget, weekly_budget, budget_threshold),
            "usage_probe": None if no_usage_check else usage.fetch_usage_status,
            "usage_threshold": budget_threshold,
            "import_interval": import_interval,
            "pr_status_fetch": None if no_ci_check else ci.fetch_pr_status,
            "runner": runner,
        },
        daemon=True,
    )
    thread.start()
    uvicorn.run(app, host=host, port=port)


@main.command("web")
@click.option("--host", default="0.0.0.0", show_default=True, help="バインドするホスト")
@click.option("--port", default=8710, show_default=True, type=int, help="ポート")
def web_cmd(host: str, port: int) -> None:
    """回答用の Web UI を起動する(LAN 内のスマホからのアクセスを想定)。"""
    import uvicorn

    from alir.web import create_app

    uvicorn.run(create_app(data_dir()), host=host, port=port)
