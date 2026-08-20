"""Web UI: FastAPI + Jinja2 + htmx。

htmx からのリクエスト(HX-Request ヘッダ)には一覧の断片だけを返して部分更新し、
JavaScript なしの通常のフォーム送信ではリダイレクトで全画面を返す。
htmx はベンダリングした静的ファイルとして配信し、外部 CDN に依存しない。
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import socket
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from alir import (
    control,
    db,
    driver,
    importer,
    notify,
    progress,
    questions,
    registry,
    reports,
    retry,
    settings,
    usage,
)
from alir.importer import ImporterError
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


def lan_ip() -> str | None:
    """LAN 内から到達できそうなこのホストの IPv4 アドレスを推定する。

    外向きの UDP ソケットに選ばれる送信元アドレスを使う(パケットは送らない)。
    経路がない・loopback しか得られないときは None。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))  # TEST-NET-1。経路の選択にだけ使う
            ip = str(sock.getsockname()[0])
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def _notify_address(host: str, detect: Callable[[], str | None]) -> str | None:
    """通知に使うアドレスを bind 先から決める。

    ワイルドカード(0.0.0.0 / :: / 空文字)なら LAN 内 IP を推定する。
    loopback は LAN 内から到達できないので None(記録しない)。
    IP でも loopback でもない値(ホスト名)はそのまま使う。
    """
    if host == "":
        return detect()
    if host == "localhost":
        return None
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return host
    if parsed.is_unspecified:
        return detect()
    if parsed.is_loopback:
        return None
    return host


def record_auto_web_url(
    dbdir: Path,
    host: str,
    port: int,
    *,
    detect: Callable[[], str | None] = lan_ip,
    scheme: str = "http",
) -> str | None:
    """自動検出した Web UI の URL を control に記録し、その URL を返す。

    serve / web の起動時に呼ぶ。通知(notify.web_url)は settings と環境変数が
    どちらも未設定のときこの記録を使う。bind 先が特定のアドレスならそれを、
    ワイルドカードなら LAN 内 IP の推定値を使う。検出できないときと
    loopback バインドのときは記録を消し、届かないアドレスを通知に載せない。
    scheme は TLS 証明書つきで起動したとき(serve --ssl-certfile)に https になる。
    """
    address = _notify_address(host, detect)
    conn = db.connect(dbdir)
    if address is None:
        control.set_value(conn, control.KEY_WEB_URL_AUTO, "")
        return None
    if ":" in address:
        # IPv6 リテラルは URL ではブラケットで囲む
        address = f"[{address}]"
    url = f"{scheme}://{address}:{port}"
    control.set_value(conn, control.KEY_WEB_URL_AUTO, url)
    return url


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _list_threads(dbdir: Path, *, show_all: bool) -> list[list[Question]]:
    return questions.list_threads(db.connect(dbdir), only_active=not show_all)


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
    threads = await db.run_in_thread(lambda: _list_threads(dbdir, show_all=show_all))
    context = {"threads": threads, "show_all": show_all, "error": request.query_params.get("error")}
    return _render(request, "questions.html", context)


async def _question_list_response(
    request: Request, dbdir: Path, *, show_all: bool, error: str | None
) -> Response:
    """回答・差し戻し後の共通応答。htmx なら一覧の断片、通常 POST ならリダイレクト。"""
    if _is_htmx(request):
        threads = await db.run_in_thread(lambda: _list_threads(dbdir, show_all=show_all))
        context = {"threads": threads, "show_all": show_all, "error": error}
        return _render(request, "_question_list.html", context, fragment="_question_list.html")
    if error:
        return RedirectResponse(f"/?error={quote(error)}", 303)
    return RedirectResponse("/", 303)


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
    return await _question_list_response(request, dbdir, show_all=show_all, error=error)


@router.post("/questions/{qid}/return")
async def return_question(request: Request, qid: int) -> Response:
    """回答の代わりに確認(逆質問)を返し、質問を差し戻す。"""
    dbdir = request.app.state.dbdir
    form = await request.form()
    text = str(form.get("text") or "").strip()
    show_all = str(form.get("show_all") or "") == "1"

    error: str | None = None
    try:
        await db.run_in_thread(lambda: questions.return_question(db.connect(dbdir), qid, text))
    except QuestionError as exc:
        error = str(exc)
    return await _question_list_response(request, dbdir, show_all=show_all, error=error)


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


def _resume_command(conn: Any, dbdir: Path, issue: Any) -> str | None:
    """セッションを別ターミナルで手動 resume するコマンドを組み立てる。

    終了したセッションを人間が開いて中を見たり続きを指示したりするための
    補助表示。MCP ツールも使えるよう、ドライバが書いた mcp.json を渡す。
    worktree が既に消えていることもあるので目安に留める。
    """
    if issue.session_id is None:
        return None
    push = settings.push_branch(conn, issue.workdir)
    branch = issue.branch or push or ""
    path = driver.worktree_path(issue, branch, push=push is not None)
    return f"cd {path} && claude --resume {issue.session_id} --mcp-config {dbdir / 'mcp.json'}"


def _history_context(conn: Any, dbdir: Path) -> dict[str, Any]:
    finished = [
        i
        for i in registry.list_issues(conn)
        if i.status in (registry.STATUS_DONE, registry.STATUS_FAILED)
    ]
    finished.sort(key=lambda i: i.updated_at, reverse=True)
    commands = {i.id: _resume_command(conn, dbdir, i) for i in finished}
    return {
        "items": finished,
        "reports": _reports_by_entry(conn, finished),
        "resume_commands": {iid: cmd for iid, cmd in commands.items() if cmd is not None},
    }


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
    context = await db.run_in_thread(lambda: _history_context(db.connect(dbdir), dbdir))
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
            # 人間の再キューはリトライ状態を初期化し、自動リトライを再び使えるようにする
            registry.set_status(conn, iid, registry.STATUS_QUEUED, retries=0)
            retry.clear_notified(conn, issue.ref)

    error: str | None = None
    try:
        await db.run_in_thread(work)
    except RegistryError as exc:
        error = str(exc)

    if _is_htmx(request):
        context = await db.run_in_thread(lambda: _history_context(db.connect(dbdir), dbdir))
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


def _settings_context(dbdir: Path) -> dict[str, Any]:
    conn = db.connect(dbdir)
    return {
        "branch_template": settings.branch_template(conn),
        "usage_threshold_percent": round(settings.usage_threshold(conn) * 100),
        "model": settings.model(conn),
        "push_branches": settings.push_branches(conn),
        "workdirs": registry.list_workdirs(conn),
        "pushover_status": notify.pushover_source(conn),
        "web_url": settings.web_url(conn),
        "web_url_env": os.environ.get(notify.ENV_WEB_URL),
        "web_url_auto": control.get_value(conn, control.KEY_WEB_URL_AUTO) or None,
    }


async def _settings_result(
    request: Request,
    dbdir: Path,
    error: str | None,
    *,
    saved: bool = False,
    notice: str | None = None,
) -> Response:
    """settings への POST の結果を返す。htmx ならフォーム断片、通常はリダイレクト。"""
    if _is_htmx(request):
        context = await db.run_in_thread(lambda: _settings_context(dbdir))
        context["saved"] = saved and error is None
        context["error"] = error
        context["notice"] = notice if error is None else None
        return _render(request, "_settings_form.html", context, fragment="_settings_form.html")
    if error:
        return RedirectResponse(f"/settings?error={quote(error)}", 303)
    if notice:
        return RedirectResponse(f"/settings?notice={quote(notice)}", 303)
    return RedirectResponse("/settings", 303)


@router.get("/settings")
async def settings_index(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    context = await db.run_in_thread(lambda: _settings_context(dbdir))
    context["saved"] = False
    context["error"] = request.query_params.get("error")
    context["notice"] = request.query_params.get("notice")
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
    return await _settings_result(request, dbdir, error, saved=True)


@router.post("/settings/usage-threshold")
async def settings_usage_threshold_save(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    raw = str(form.get("threshold") or "").strip()

    # フォームはパーセント(1〜100)で受け、保存は割合(0〜1)で行う
    error: str | None = None
    try:
        percent = float(raw)
    except ValueError:
        error = f"usage threshold must be a number (percent): {raw or '(empty)'}"
    else:
        try:
            await db.run_in_thread(
                lambda: settings.set_usage_threshold(db.connect(dbdir), percent / 100)
            )
        except SettingsError:
            error = f"usage threshold must be greater than 0 and at most 100 (percent): {raw}"
    return await _settings_result(request, dbdir, error, saved=True)


@router.post("/settings/model/set")
async def settings_model_set(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    model = str(form.get("model") or "").strip()

    error: str | None = None
    try:
        await db.run_in_thread(lambda: settings.set_model(db.connect(dbdir), model))
    except SettingsError as exc:
        error = str(exc)
    return await _settings_result(request, dbdir, error, saved=True)


@router.post("/settings/model/clear")
async def settings_model_clear(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    await db.run_in_thread(lambda: settings.clear_model(db.connect(dbdir)))
    return await _settings_result(request, dbdir, None)


@router.post("/settings/push-branches/set")
async def settings_push_branches_set(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    workdir = str(form.get("workdir") or "").strip()
    branch = str(form.get("branch") or "").strip()

    error: str | None = None
    try:
        await db.run_in_thread(
            lambda: settings.set_push_branch(db.connect(dbdir), workdir=workdir, branch=branch)
        )
    except SettingsError as exc:
        error = str(exc)
    return await _settings_result(request, dbdir, error)


@router.post("/settings/push-branches/unset")
async def settings_push_branches_unset(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    workdir = str(form.get("workdir") or "").strip()

    error: str | None = None
    try:
        await db.run_in_thread(
            lambda: settings.clear_push_branch(db.connect(dbdir), workdir=workdir)
        )
    except SettingsError as exc:
        error = str(exc)
    return await _settings_result(request, dbdir, error)


@router.post("/settings/pushover/set")
async def settings_pushover_set(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    token = str(form.get("token") or "").strip()
    user = str(form.get("user") or "").strip()

    error: str | None = None
    try:
        await db.run_in_thread(
            lambda: settings.set_pushover(db.connect(dbdir), token=token, user=user)
        )
    except SettingsError as exc:
        error = str(exc)
    return await _settings_result(request, dbdir, error)


@router.post("/settings/pushover/clear")
async def settings_pushover_clear(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    await db.run_in_thread(lambda: settings.clear_pushover(db.connect(dbdir)))
    return await _settings_result(request, dbdir, None)


@router.post("/settings/pushover/test")
async def settings_pushover_test(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    error: str | None = None
    notice: str | None = None
    try:
        sent = await db.run_in_thread(
            lambda: notify.send_pushover(
                "alir からのテスト通知", url=notify.web_url(db.connect(dbdir))
            )
        )
        if sent:
            notice = "テスト通知を送信しました。"
        else:
            error = "Pushover が未設定のため送信できません。"
    except Exception as exc:  # noqa: BLE001
        error = f"送信に失敗しました: {exc}"
    return await _settings_result(request, dbdir, error, notice=notice)


@router.post("/settings/web-url/set")
async def settings_web_url_set(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    url = str(form.get("url") or "").strip()

    error: str | None = None
    try:
        await db.run_in_thread(lambda: settings.set_web_url(db.connect(dbdir), url))
    except SettingsError as exc:
        error = str(exc)
    return await _settings_result(request, dbdir, error)


@router.post("/settings/web-url/clear")
async def settings_web_url_clear(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    await db.run_in_thread(lambda: settings.clear_web_url(db.connect(dbdir)))
    return await _settings_result(request, dbdir, None)


def _import_context(dbdir: Path) -> dict[str, Any]:
    conn = db.connect(dbdir)
    return {
        "import_targets": importer.list_targets(conn),
        "import_interval": importer.import_interval(conn),
        "import_interval_hint": importer.DEFAULT_INTERVAL,
        "import_interval_min": importer.MIN_INTERVAL,
        "workdirs": registry.list_workdirs(conn),
    }


async def _import_result(
    request: Request,
    dbdir: Path,
    error: str | None,
    *,
    notice: str | None = None,
) -> Response:
    """import への POST の結果を返す。htmx ならフォーム断片、通常はリダイレクト。"""
    if _is_htmx(request):
        context = await db.run_in_thread(lambda: _import_context(dbdir))
        context["error"] = error
        context["notice"] = notice if error is None else None
        return _render(request, "_import_form.html", context, fragment="_import_form.html")
    if error:
        return RedirectResponse(f"/import?error={quote(error)}", 303)
    if notice:
        return RedirectResponse(f"/import?notice={quote(notice)}", 303)
    return RedirectResponse("/import", 303)


@router.get("/import")
async def import_index(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    context = await db.run_in_thread(lambda: _import_context(dbdir))
    context["error"] = request.query_params.get("error")
    context["notice"] = request.query_params.get("notice")
    return _render(request, "import.html", context, fragment="_import_form.html")


@router.post("/import/targets/add")
async def import_targets_add(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    workdir = str(form.get("workdir") or "").strip()
    label = str(form.get("label") or "").strip()

    error: str | None = None
    try:
        await db.run_in_thread(
            lambda: importer.add_target(db.connect(dbdir), workdir=workdir, label=label)
        )
    except ImporterError as exc:
        error = str(exc)
    return await _import_result(request, dbdir, error)


@router.post("/import/targets/remove")
async def import_targets_remove(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    workdir = str(form.get("workdir") or "").strip()
    label = str(form.get("label") or "").strip()

    error: str | None = None
    try:
        await db.run_in_thread(
            lambda: importer.remove_target(db.connect(dbdir), workdir=workdir, label=label)
        )
    except ImporterError as exc:
        error = str(exc)
    return await _import_result(request, dbdir, error)


def _import_now(conn: Any, targets: list[importer.ImportTarget] | None) -> importer.ImportOutcome:
    """取り込みを実行し、登録できた Issue を稼働ログに残す。

    fetch を明示的に渡すのは、テストから gh の呼び出しを差し替えられるようにするため。
    """
    outcome = importer.run_import(conn, fetch=importer.fetch_labeled_issues, targets=targets)
    for issue in outcome.imported:
        # ログの永続化に失敗しても、取り込んだ結果は画面に返す
        with contextlib.suppress(Exception):
            control.log_event(conn, f"import #{issue.id} {issue.ref}")
    return outcome


def _import_message(outcome: importer.ImportOutcome) -> tuple[str | None, str | None]:
    """取り込み結果を (エラー, 通知) の文言にする。

    一部の対象だけが失敗しても取り込めた件数は伝わるように、
    エラーがあるときは件数もエラー側の文言に含める(通知は表示されないため)。
    """
    summary = f"{outcome.found} 件が該当し、うち {len(outcome.imported)} 件を取り込みました。"
    if outcome.errors:
        return f"{summary} 失敗: {'; '.join(outcome.errors)}", None
    return None, summary


@router.post("/import/run")
async def import_run(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    workdir = str(form.get("workdir") or "").strip()
    label = str(form.get("label") or "").strip()

    def work() -> importer.ImportOutcome:
        conn = db.connect(dbdir)
        target = importer.find_target(conn, workdir=workdir, label=label)
        return _import_now(conn, [target])

    error: str | None = None
    notice: str | None = None
    try:
        outcome = await db.run_in_thread(work)
    except ImporterError as exc:
        error = str(exc)
    else:
        error, notice = _import_message(outcome)
    return await _import_result(request, dbdir, error, notice=notice)


@router.post("/import/run-all")
async def import_run_all(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    outcome = await db.run_in_thread(lambda: _import_now(db.connect(dbdir), None))
    error, notice = _import_message(outcome)
    return await _import_result(request, dbdir, error, notice=notice)


@router.post("/import/interval")
async def import_interval_save(request: Request) -> Response:
    dbdir = request.app.state.dbdir
    form = await request.form()
    raw = str(form.get("seconds") or "").strip()

    error: str | None = None
    notice: str | None = None
    try:
        seconds = float(raw or 0)
    except ValueError:
        error = f"interval must be a number: {raw}"
    else:
        try:
            await db.run_in_thread(lambda: importer.set_import_interval(db.connect(dbdir), seconds))
        except ImporterError as exc:
            error = str(exc)
        else:
            notice = (
                "定期取り込みを無効にしました。"
                if seconds == 0
                else f"{seconds:g} 秒ごとに定期取り込みします。"
            )
    return await _import_result(request, dbdir, error, notice=notice)


def _format_remaining(delta: timedelta) -> str | None:
    """リセットまでの残り時間を短い日本語にする。過ぎていれば None。"""
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return None
    days, rest = divmod(seconds, 86400)
    hours, minutes = divmod(rest // 60, 60)
    if days:
        return f"残り{days}日{hours}時間"
    if hours:
        return f"残り{hours}時間{minutes}分"
    return f"残り{minutes}分"


def _usage_windows(raw: str | None) -> list[dict[str, Any]]:
    """KEY_USAGE_STATUS の JSON を表示用に整形する。

    resets はドライバの更新前に保存された古い形式([label, used] の 2 要素)では
    欠けるので、その場合は残り時間なしで使用率だけ出す。
    """
    if not raw:
        return []
    now = datetime.now(timezone.utc)
    windows = []
    for row in json.loads(raw):
        resets = str(row[2]) if len(row) > 2 and row[2] else None
        remaining = None
        if resets:
            reset_at = usage.parse_reset_at(resets, now=now)
            if reset_at is not None:
                # リセット時刻を過ぎている = 保存された使用率が古い。
                # 誤った残り時間を出すより、リセット済みであることを示す
                remaining = _format_remaining(reset_at - now) or "リセット済み"
        windows.append({"label": str(row[0]), "used": float(row[1]), "remaining": remaining})
    return windows


async def _loop_context(dbdir: Path) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        conn = db.connect(dbdir)
        usage_windows = _usage_windows(control.get_value(conn, control.KEY_USAGE_STATUS))
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
