"""alir CLI: 質問の一覧・回答と MCP サーバーの起動。"""

from __future__ import annotations

import click

import alir
from alir import db, questions
from alir.config import data_dir
from alir.questions import Question, QuestionError


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
