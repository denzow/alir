"""Web UI: FastAPI + Jinja2 + htmx。

htmx からのリクエスト(HX-Request ヘッダ)には一覧の断片だけを返して部分更新し、
JavaScript なしの通常のフォーム送信ではリダイレクトで全画面を返す。
htmx はベンダリングした静的ファイルとして配信し、外部 CDN に依存しない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from alir import control, db, questions, registry
from alir.questions import Question, QuestionError
from alir.registry import Issue, RegistryError

_BASE = Path(__file__).parent
templates = Jinja2Templates(directory=_BASE / "templates")

router = APIRouter()


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _list_questions(dbdir: Path, *, show_all: bool) -> list[Question]:
    status = None if show_all else questions.STATUS_OPEN
    return questions.list_questions(db.connect(dbdir), status=status)


def _list_issues(dbdir: Path) -> list[Issue]:
    return registry.list_issues(db.connect(dbdir))


def _render(
    request: Request, template: str, context: dict[str, Any], *, fragment: str | None = None
) -> Response:
    """全画面テンプレートを返す。htmx リクエストなら断片テンプレートを返す。"""
    name = fragment if fragment and _is_htmx(request) else template
    return templates.TemplateResponse(request, name, context)


@router.get("/")
async def index(request: Request) -> Response:
    show_all = request.query_params.get("all") == "1"
    dbdir = request.app.state.dbdir
    items = await db.run_in_thread(lambda: _list_questions(dbdir, show_all=show_all))
    context = {"items": items, "show_all": show_all, "error": request.query_params.get("error")}
    return _render(request, "questions.html", context)


@router.post("/questions/{qid}/answer")
async def answer_question(request: Request, qid: int) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    choice = str(form.get("choice") or "").strip()
    if choice == "free":
        choice = str(form.get("free") or "").strip()
    note = str(form.get("note") or "").strip() or None
    show_all = str(form.get("show_all") or "") == "1"

    error: str | None = None
    if not choice:
        error = "choice is empty"
    else:
        try:
            await db.run_in_thread(
                lambda: questions.answer(db.connect(dbdir), qid, choice, note=note)
            )
        except QuestionError as exc:
            error = str(exc)

    if _is_htmx(request):
        items = await db.run_in_thread(lambda: _list_questions(dbdir, show_all=show_all))
        context = {"items": items, "show_all": show_all, "error": error}
        return _render(request, "_question_list.html", context, fragment="_question_list.html")
    if error:
        return RedirectResponse(f"/?error={quote(error)}", 303)
    return RedirectResponse("/", 303)


@router.get("/issues")
async def issues_index(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    items = await db.run_in_thread(lambda: _list_issues(dbdir))
    workdirs = await db.run_in_thread(lambda: registry.list_workdirs(db.connect(dbdir)))
    context = {
        "items": items,
        "workdirs": workdirs,
        "error": request.query_params.get("error"),
    }
    return _render(request, "issues.html", context)


@router.post("/issues/add")
async def issues_add(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    url = str(form.get("url") or "").strip()
    workdir = str(form.get("workdir") or "").strip()

    error: str | None = None
    workdir_path = Path(workdir).expanduser()
    if not workdir_path.is_dir():
        error = f"workdir not found: {workdir}"
    if error is None:

        def work() -> None:
            title = registry.fetch_title(url)
            registry.add(
                db.connect(dbdir), url=url, workdir=str(workdir_path.resolve()), title=title
            )

        try:
            await db.run_in_thread(work)
        except RegistryError as exc:
            error = str(exc)

    return await _issues_result(request, dbdir, error)


@router.post("/issues/{iid}/move/{direction}")
async def issues_move(request: Request, iid: int, direction: str) -> Response:
    dbdir = request.app.state.dbdir

    error: str | None = None
    try:
        await db.run_in_thread(lambda: registry.move(db.connect(dbdir), iid, direction))
    except RegistryError as exc:
        error = str(exc)
    return await _issues_result(request, dbdir, error)


@router.post("/issues/{iid}/requeue")
async def issues_requeue(request: Request, iid: int) -> Response:
    dbdir = request.app.state.dbdir

    def work() -> None:
        conn = db.connect(dbdir)
        with db.transaction(conn):
            issue = registry.get(conn, iid)
            if issue.status != registry.STATUS_FAILED:
                raise RegistryError(f"issue {iid} is {issue.status}, not failed")
            registry.set_status(conn, iid, registry.STATUS_QUEUED)

    error: str | None = None
    try:
        await db.run_in_thread(work)
    except RegistryError as exc:
        error = str(exc)
    return await _issues_result(request, dbdir, error)


async def _issues_result(request: Request, dbdir: Path, error: str | None) -> Response:
    """issues への POST の結果を返す。htmx なら一覧断片、通常はリダイレクト。"""
    if _is_htmx(request):
        items = await db.run_in_thread(lambda: _list_issues(dbdir))
        context = {"items": items, "error": error}
        return _render(request, "_issue_list.html", context, fragment="_issue_list.html")
    if error:
        return RedirectResponse(f"/issues?error={quote(error)}", 303)
    return RedirectResponse("/issues", 303)


async def _loop_context(dbdir: Path) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        conn = db.connect(dbdir)
        return {
            "paused": control.is_paused(conn),
            "alive": control.driver_alive(conn),
            "heartbeat": control.heartbeat_at(conn),
            "events": control.recent_events(conn),
        }

    return await db.run_in_thread(work)


@router.get("/loop")
async def loop_index(request: Request) -> Response:
    context = await _loop_context(request.app.state.dbdir)
    return _render(request, "loop.html", context, fragment="_loop_panel.html")


@router.post("/loop/pause")
async def loop_pause(request: Request) -> Response:
    return await _loop_toggle(request, paused=True)


@router.post("/loop/resume")
async def loop_resume(request: Request) -> Response:
    return await _loop_toggle(request, paused=False)


async def _loop_toggle(request: Request, *, paused: bool) -> Response:
    dbdir = request.app.state.dbdir

    def work() -> None:
        conn = db.connect(dbdir)
        control.set_paused(conn, paused)
        control.log_event(conn, "pause requested" if paused else "resume requested")

    await db.run_in_thread(work)
    if _is_htmx(request):
        context = await _loop_context(dbdir)
        return _render(request, "_loop_panel.html", context, fragment="_loop_panel.html")
    return RedirectResponse("/loop", 303)


def create_app(dbdir: Path, *, lifespan: Any = None) -> FastAPI:
    """回答用 Web UI の FastAPI アプリケーションを返す。"""
    app = FastAPI(lifespan=lifespan)
    app.state.dbdir = dbdir
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=_BASE / "static"), name="static")
    return app
