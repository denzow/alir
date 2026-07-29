"""alir CLI: 質問の一覧・回答と MCP サーバーの起動。"""

from __future__ import annotations

from pathlib import Path

import click

import alir
from alir import db, questions, registry
from alir.config import data_dir
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
            click.echo(
                f"#{issue.id} [{issue.status}] {issue.ref} "
                f"(priority: {issue.priority}) {issue.workdir}"
            )


@issues_group.command("add")
@click.argument("url")
@click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="対象リポジトリのローカルパス",
)
@click.option("--priority", default=0, show_default=True, type=int, help="大きいほど先に実行する")
def issues_add(url: str, workdir: Path, priority: int) -> None:
    """GitHub Issue の URL を queued として登録する。"""
    conn = db.connect(data_dir())
    try:
        issue = registry.add(conn, url=url, workdir=str(workdir.resolve()), priority=priority)
    except RegistryError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"added #{issue.id}: {issue.ref} (priority: {issue.priority})")


@issues_group.command("import")
@click.option("--label", required=True, help="取り込む Issue のラベル")
@click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="対象リポジトリのローカルパス",
)
@click.option("--priority", default=0, show_default=True, type=int)
def issues_import(label: str, workdir: Path, priority: int) -> None:
    """gh CLI でラベル付き Issue を検索して一括登録する(補助コマンド)。"""
    import subprocess

    proc = subprocess.run(
        ["gh", "issue", "list", "--label", label, "--json", "url", "--limit", "100"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise click.ClickException(f"gh issue list failed: {proc.stderr.strip()}")
    import json

    conn = db.connect(data_dir())
    for item in json.loads(proc.stdout):
        try:
            issue = registry.add(
                conn, url=item["url"], workdir=str(workdir.resolve()), priority=priority
            )
            click.echo(f"added #{issue.id}: {issue.ref}")
        except RegistryError as exc:
            click.echo(f"skipped: {exc}")


@main.command("run")
@click.option("--once", is_flag=True, help="キューを 1 巡したら終了する")
@click.option("--parallel", default=1, show_default=True, type=int, help="並列実行数")
@click.option(
    "--interval", default=30.0, show_default=True, type=float, help="キューが空のときの待機秒数"
)
@click.option(
    "--dangerously-skip-permissions",
    "skip_permissions",
    is_flag=True,
    help="claude に --dangerously-skip-permissions を渡す",
)
def run_cmd(once: bool, parallel: int, interval: float, skip_permissions: bool) -> None:
    """ループドライバを起動し、queued の Issue を処理し続ける。"""
    from alir import driver

    driver.run_loop(
        data_dir(),
        once=once,
        parallel=parallel,
        interval=interval,
        skip_permissions=skip_permissions,
    )


@main.command("web")
@click.option("--host", default="0.0.0.0", show_default=True, help="バインドするホスト")
@click.option("--port", default=8710, show_default=True, type=int, help="ポート")
def web_cmd(host: str, port: int) -> None:
    """回答用の Web UI を起動する(LAN 内のスマホからのアクセスを想定)。"""
    import uvicorn

    from alir.web import create_app

    uvicorn.run(create_app(data_dir()), host=host, port=port)
