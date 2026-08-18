"""通知: 質問の登録やリトライ上限の到達を Pushover とデスクトップに知らせる。

通知は補助機能なので、失敗しても元の処理は成功させる(best-effort)。
Pushover の認証情報は settings(alir pushover set)を優先し、
未設定なら環境変数 ALIR_PUSHOVER_TOKEN / ALIR_PUSHOVER_USER を使う。
どちらもなければ送らない。
通知に含める Web UI の URL(例: http://192.168.1.10:8710)も同様に
settings(alir web-url set)を優先し、未設定なら環境変数 ALIR_WEB_URL を使う。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import urllib.parse
import urllib.request

import iceql

from alir import config, control, db, settings
from alir.questions import Question
from alir.registry import Issue

ENV_PUSHOVER_TOKEN = "ALIR_PUSHOVER_TOKEN"
ENV_PUSHOVER_USER = "ALIR_PUSHOVER_USER"
ENV_WEB_URL = "ALIR_WEB_URL"

_PUSHOVER_API = "https://api.pushover.net/1/messages.json"


def build_message(question: Question) -> str:
    return f"#{question.id} {question.issue}: {question.question}"


def send_desktop(message: str) -> None:
    """notify-send があればデスクトップ通知を送る。"""
    if shutil.which("notify-send") is None:
        return
    subprocess.run(["notify-send", "alir", message], check=False, capture_output=True)


def pushover_credentials() -> tuple[str, str] | None:
    """Pushover の認証情報を settings、なければ環境変数から読む。"""
    # settings の読み取り失敗(データディレクトリ未作成など)は環境変数で継続する
    with contextlib.suppress(Exception):
        creds = settings.pushover(db.connect(config.data_dir()))
        if creds is not None:
            return creds
    token = os.environ.get(ENV_PUSHOVER_TOKEN)
    user = os.environ.get(ENV_PUSHOVER_USER)
    if token and user:
        return token, user
    return None


def pushover_source(conn: iceql.Connection | None = None) -> str | None:
    """認証情報の出所を返す。"settings" / "environment" / None(未設定)。

    CLI と Web UI が設定状態の表示に使う。判定は pushover_credentials と
    同じで、settings の読み取り失敗は環境変数の判定に落ちる。
    conn を渡すとその接続の settings で判定する(Web UI は自分の dbdir を使う)。
    """
    with contextlib.suppress(Exception):
        if conn is None:
            conn = db.connect(config.data_dir())
        if settings.pushover(conn) is not None:
            return "settings"
    if os.environ.get(ENV_PUSHOVER_TOKEN) and os.environ.get(ENV_PUSHOVER_USER):
        return "environment"
    return None


def web_url(conn: iceql.Connection | None = None) -> str | None:
    """通知に含める Web UI の URL を返す。

    settings(alir web-url set)、環境変数(ALIR_WEB_URL)、serve が起動時に
    自動検出して記録した URL の順で読む。どれもなければ None。
    """
    # settings の読み取り失敗(データディレクトリ未作成など)は次の段に落ちる
    with contextlib.suppress(Exception):
        if conn is None:
            conn = db.connect(config.data_dir())
    if conn is not None:
        with contextlib.suppress(Exception):
            url = settings.web_url(conn)
            if url is not None:
                return url
    env = os.environ.get(ENV_WEB_URL)
    if env:
        return env
    if conn is not None:
        with contextlib.suppress(Exception):
            return control.get_value(conn, control.KEY_WEB_URL_AUTO) or None
    return None


def send_pushover(message: str, *, url: str | None) -> bool:
    """Pushover に通知を送る。認証情報がなければ何もせず False を返す。"""
    creds = pushover_credentials()
    if creds is None:
        return False
    token, user = creds
    fields = {"token": token, "user": user, "title": "alir", "message": message}
    if url:
        fields["url"] = url
    data = urllib.parse.urlencode(fields).encode("utf-8")
    with urllib.request.urlopen(_PUSHOVER_API, data=data, timeout=10):
        pass
    return True


def notify_message(message: str) -> None:
    """メッセージを各チャネルへ通知する。失敗は握りつぶす。"""
    with contextlib.suppress(Exception):
        send_desktop(message)
    with contextlib.suppress(Exception):
        send_pushover(message, url=web_url())


def notify_question(question: Question) -> None:
    """質問の登録を各チャネルへ通知する。"""
    notify_message(build_message(question))


def notify_issue_failed(issue: Issue, detail: str | None = None) -> None:
    """Issue の処理失敗を各チャネルへ通知する。

    detail には失敗原因(例外メッセージなど)を渡す。複数行の場合は
    通知が長くなりすぎないよう先頭行だけを使う。
    """
    message = f"#{issue.id} {issue.ref}: 処理が失敗した"
    if detail:
        first_line = detail.strip().splitlines()[0]
        message = f"{message}: {first_line}"
    notify_message(message)


def notify_retry_exhausted(issue: Issue, limit: int) -> None:
    """自動リトライの上限到達を各チャネルへ通知する。"""
    notify_message(
        f"#{issue.id} {issue.ref}: 自動リトライが上限({limit}回)に達し failed のまま停止"
    )
