"""Web UI: FastAPI + Jinja2 + htmx。

htmx からのリクエスト(HX-Request ヘッダ)には一覧の断片だけを返して部分更新し、
JavaScript なしの通常のフォーム送信ではリダイレクトで全画面を返す。
htmx はベンダリングした静的ファイルとして配信し、外部 CDN に依存しない。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from alir import control, db, progress, questions, registry, reports, settings
from alir.questions import Question, QuestionError
from alir.registry import RegistryError
from alir.settings import SettingsError

_BASE = Path(__file__).parent
templates = Jinja2Templates(directory=_BASE / "templates")


def _localtime(value: str | datetime) -> str:
    """UTC で保存した時刻をローカル時刻の表示用文字列にする。"""
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return dt.astimezone().strftime("%m-%d %H:%M")


templates.env.filters["localtime"] = _localtime

router = APIRouter()


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _list_questions(dbdir: Path, *, show_all: bool) -> list[Question]:
    status = None if show_all else questions.STATUS_OPEN
    return questions.list_questions(db.connect(dbdir), status=status)


def _render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    fragment: str | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    """全画面テンプレートを返す。htmx リクエストなら断片テンプレートを返す。"""
    name = fragment if fragment and _is_htmx(request) else template
    return templates.TemplateResponse(request, name, context, headers=headers)


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


def _reports_by_entry(conn: Any, issues: list[Any]) -> dict[int, Any]:
    """登録エントリごとに、その実行期間内の最新の報告を対応づける。

    報告は GitHub の ref でしか記録されないため、同じ Issue を複数回登録した
    場合に備えて「登録時刻以降(終了済みなら終了時刻まで)」の報告に絞る。
    """
    all_reports = reports.list_reports(conn, limit=1000)  # 新しい順
    finished = (registry.STATUS_DONE, registry.STATUS_FAILED)
    mapping: dict[int, Any] = {}
    for issue in issues:
        for report in all_reports:
            if report.issue != issue.ref or report.created_at < issue.created_at:
                continue
            if issue.status in finished and report.created_at > issue.updated_at:
                continue
            mapping[issue.id] = report
            break
    return mapping


def _issues_context(conn: Any) -> dict[str, Any]:
    items = registry.list_issues(conn)
    running = [i for i in items if i.status == registry.STATUS_RUNNING]
    running.sort(key=lambda i: i.updated_at, reverse=True)
    shown = [i for i in items if i.status in registry.REORDERABLE] + running
    return {
        "queue_items": [i for i in items if i.status in registry.REORDERABLE],
        "running_items": running,
        "reports": _reports_by_entry(conn, shown),
    }


def _history_context(conn: Any) -> dict[str, Any]:
    finished = [
        i
        for i in registry.list_issues(conn)
        if i.status in (registry.STATUS_DONE, registry.STATUS_FAILED)
    ]
    finished.sort(key=lambda i: i.updated_at, reverse=True)
    return {"items": finished, "reports": _reports_by_entry(conn, finished)}


@router.get("/issues")
async def issues_index(request: Request) -> Response:
    dbdir = request.app.state.dbdir

    def work() -> dict[str, Any]:
        conn = db.connect(dbdir)
        context = _issues_context(conn)
        context["workdirs"] = registry.list_workdirs(conn)
        return context

    context = await db.run_in_thread(work)
    context["error"] = request.query_params.get("error")
    return _render(request, "issues.html", context)


@router.post("/issues/add")
async def issues_add(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    url = str(form.get("url") or "").strip()
    workdir = str(form.get("workdir") or "").strip()
    mode = str(form.get("mode") or registry.MODE_AUTO).strip()
    note = str(form.get("note") or "").strip() or None

    error: str | None = None
    workdir_path = Path(workdir).expanduser()
    if not workdir_path.is_dir():
        error = f"workdir not found: {workdir}"
    elif mode not in registry.MODES:
        error = f"mode must be one of {registry.MODES}"
    if error is None:

        def work() -> None:
            title = registry.fetch_title(url)
            registry.add(
                db.connect(dbdir),
                url=url,
                workdir=str(workdir_path.resolve()),
                title=title,
                mode=mode,
                note=note,
            )

        try:
            await db.run_in_thread(work)
        except RegistryError as exc:
            error = str(exc)

    return await _issues_result(
        request, dbdir, error, trigger="issue-added" if error is None else None
    )


@router.post("/issues/reorder")
async def issues_reorder(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    raw = str(form.get("order") or "").strip()

    error: str | None = None
    ids: list[int] = []
    try:
        ids = [int(part) for part in raw.split(",") if part]
    except ValueError:
        error = "order must be a comma-separated list of ids"
    if error is None:
        try:
            await db.run_in_thread(lambda: registry.reorder(db.connect(dbdir), ids))
        except RegistryError as exc:
            error = str(exc)
    return await _issues_result(request, dbdir, error)


@router.get("/history")
async def history_index(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    context = await db.run_in_thread(lambda: _history_context(db.connect(dbdir)))
    context["error"] = request.query_params.get("error")
    return _render(request, "history.html", context, fragment="_history_list.html")


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

    if _is_htmx(request):
        context = await db.run_in_thread(lambda: _history_context(db.connect(dbdir)))
        context["error"] = error
        return _render(request, "_history_list.html", context, fragment="_history_list.html")
    if error:
        return RedirectResponse(f"/history?error={quote(error)}", 303)
    return RedirectResponse("/history", 303)


async def _issues_result(
    request: Request, dbdir: Path, error: str | None, *, trigger: str | None = None
) -> Response:
    """issues への POST の結果を返す。htmx なら一覧断片、通常はリダイレクト。

    trigger を指定すると HX-Trigger ヘッダでクライアント側イベントを発火させる
    (フォームのクリアなど、断片の外の後処理に使う)。
    """
    if _is_htmx(request):
        context = await db.run_in_thread(lambda: _issues_context(db.connect(dbdir)))
        context["error"] = error
        headers = {"HX-Trigger": trigger} if trigger else None
        return _render(
            request, "_issue_list.html", context, fragment="_issue_list.html", headers=headers
        )
    if error:
        return RedirectResponse(f"/issues?error={quote(error)}", 303)
    return RedirectResponse("/issues", 303)


@router.get("/sessions")
async def sessions_index(request: Request) -> Response:
    dbdir = request.app.state.dbdir

    def work() -> dict[str, Any]:
        conn = db.connect(dbdir)
        now = datetime.now(timezone.utc)
        sessions = []
        for issue in registry.list_issues(conn, status=registry.STATUS_RUNNING):
            started = datetime.fromisoformat(issue.updated_at)
            # 同じ Issue の過去セッションの進捗を混ぜない
            # (updated_at は running へ遷移した時刻 = このセッションの開始時刻)
            entries = [
                p
                for p in progress.list_progress(conn, issue=issue.ref)
                if p.created_at >= issue.updated_at
            ]
            sessions.append(
                {
                    "issue": issue,
                    "entries": entries,
                    "elapsed_min": int((now - started).total_seconds() // 60),
                }
            )
        return {"sessions": sessions, "recent_reports": reports.list_reports(conn, limit=5)}

    context = await db.run_in_thread(work)
    return _render(request, "sessions.html", context, fragment="_session_list.html")


@router.get("/settings")
async def settings_index(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    template = await db.run_in_thread(lambda: settings.branch_template(db.connect(dbdir)))
    context = {
        "branch_template": template,
        "saved": False,
        "error": request.query_params.get("error"),
    }
    return _render(request, "settings.html", context, fragment="_settings_form.html")


@router.post("/settings")
async def settings_save(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    template = str(form.get("branch_template") or "").strip()

    error: str | None = None
    try:
        await db.run_in_thread(lambda: settings.set_branch_template(db.connect(dbdir), template))
    except SettingsError as exc:
        error = str(exc)

    current = await db.run_in_thread(lambda: settings.branch_template(db.connect(dbdir)))
    if _is_htmx(request):
        context = {"branch_template": current, "saved": error is None, "error": error}
        return _render(request, "_settings_form.html", context, fragment="_settings_form.html")
    if error:
        return RedirectResponse(f"/settings?error={quote(error)}", 303)
    return RedirectResponse("/settings", 303)


async def _loop_context(dbdir: Path) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        conn = db.connect(dbdir)
        raw = control.get_value(conn, control.KEY_USAGE_STATUS)
        usage_windows = json.loads(raw) if raw else None
        return {
            "paused": control.is_paused(conn),
            "alive": control.driver_alive(conn),
            "heartbeat": control.heartbeat_at(conn),
            "events": control.recent_events(conn),
            "usage_windows": usage_windows,
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
