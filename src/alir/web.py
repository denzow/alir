"""Web UI: 未回答質問の一覧と回答登録。

テンプレートエンジンは使わず、HTML は文字列で組み立てる。
スマホから選択肢のタップだけで回答できることを優先する。
"""

from __future__ import annotations

import html
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from alir import db, questions
from alir.questions import Question, QuestionError

_STYLE = """
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; max-width: 40rem; margin: 0 auto; padding: 1rem; }
h1 { font-size: 1.2rem; }
.card { border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
        border-radius: 8px; padding: 1rem; margin-bottom: 1rem; }
.meta { font-size: 0.85rem; opacity: 0.7; }
.question { margin: 0.5rem 0; white-space: pre-wrap; }
.options button { display: block; width: 100%; margin: 0.4rem 0; padding: 0.7rem;
                  border-radius: 6px; border: 1px solid currentColor;
                  background: none; color: inherit; font-size: 1rem; text-align: left; }
.options button.recommended { border-width: 2px; font-weight: bold; }
input[type=text] { width: 100%; box-sizing: border-box; padding: 0.6rem; margin: 0.4rem 0;
                   border-radius: 6px; border: 1px solid currentColor;
                   background: none; color: inherit; font-size: 1rem; }
.free button { padding: 0.7rem 1rem; border-radius: 6px; border: 1px solid currentColor;
               background: none; color: inherit; font-size: 1rem; }
.answered { opacity: 0.6; }
.error { color: #c00; }
"""


def _render_question(q: Question) -> str:
    """質問 1 件を回答フォーム付きのカードとして描画する。"""
    esc = html.escape
    parts = [
        '<div class="card">',
        f'<div class="meta">#{q.id} [{esc(q.status)}] {esc(q.issue)} '
        f"(impact: {esc(q.impact)}, timeout: {esc(q.timeout_action)})</div>",
        f'<p class="question">{esc(q.question)}</p>',
    ]
    if q.status == questions.STATUS_OPEN:
        parts.append(f'<form method="post" action="/questions/{q.id}/answer">')
        parts.append('<input type="text" name="note" placeholder="補足(任意)">')
        parts.append('<div class="options">')
        for i, option in enumerate(q.options, start=1):
            cls = ' class="recommended"' if option == q.recommended else ""
            label = esc(option) + (" (推奨)" if option == q.recommended else "")
            parts.append(f'<button name="choice" value="{i}"{cls}>{label}</button>')
        parts.append("</div>")
        parts.append('<div class="free">')
        parts.append('<input type="text" name="free" placeholder="自由記述で回答">')
        parts.append('<button name="choice" value="free">自由記述で回答</button>')
        parts.append("</div>")
        parts.append("</form>")
    else:
        note = f" ({esc(q.answer_note)})" if q.answer_note else ""
        parts.append(f'<p class="answered">A: {esc(q.answer or "")}{note}</p>')
    parts.append("</div>")
    return "\n".join(parts)


def _render_page(items: list[Question], *, show_all: bool, error: str | None) -> str:
    esc = html.escape
    toggle = '<a href="/">未回答のみ</a>' if show_all else '<a href="/?all=1">すべて表示</a>'
    body = [
        "<!doctype html>",
        '<html lang="ja"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>alir</title><style>{_STYLE}</style></head><body>",
        f"<h1>alir questions</h1><p>{toggle}</p>",
    ]
    if error:
        body.append(f'<p class="error">{esc(error)}</p>')
    if items:
        body.extend(_render_question(q) for q in items)
    else:
        body.append("<p>no questions</p>")
    body.append("</body></html>")
    return "\n".join(body)


async def index(request: Request) -> HTMLResponse:
    conn = db.connect(request.app.state.dbdir)
    show_all = request.query_params.get("all") == "1"
    status = None if show_all else questions.STATUS_OPEN
    items = questions.list_questions(conn, status=status)
    error = request.query_params.get("error")
    return HTMLResponse(_render_page(items, show_all=show_all, error=error))


async def answer_question(request: Request) -> RedirectResponse:
    conn = db.connect(request.app.state.dbdir)
    qid = int(request.path_params["qid"])
    form = await request.form()
    choice = str(form.get("choice") or "").strip()
    if choice == "free":
        choice = str(form.get("free") or "").strip()
    note = str(form.get("note") or "").strip() or None
    if not choice:
        return RedirectResponse("/?error=choice+is+empty", status_code=303)
    try:
        questions.answer(conn, qid, choice, note=note)
    except QuestionError as exc:
        from urllib.parse import quote

        return RedirectResponse(f"/?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/", status_code=303)


def app_routes() -> list[Route]:
    """Web UI のルート定義を返す。単体起動でも MCP との同居でも同じものを使う。"""
    return [
        Route("/", index, methods=["GET"]),
        Route("/questions/{qid:int}/answer", answer_question, methods=["POST"]),
    ]


def create_app(dbdir: Path) -> Starlette:
    """回答用 Web UI の ASGI アプリケーションを返す。"""
    app = Starlette(routes=app_routes())
    app.state.dbdir = dbdir
    return app
