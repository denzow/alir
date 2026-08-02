"""通知: 質問の登録やリトライ上限の到達を Pushover とデスクトップに知らせる。

通知は補助機能なので、失敗しても元の処理は成功させる(best-effort)。
Pushover の認証情報は settings(alir pushover set)を優先し、
未設定なら環境変数 ALIR_PUSHOVER_TOKEN / ALIR_PUSHOVER_USER を使う。
どちらもなければ送らない。
通知に含める Web UI の URL は ALIR_WEB_URL(例: http://192.168.1.10:8710)で指定する。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import urllib.parse
import urllib.request

from alir import config, db, settings
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
    web_url = os.environ.get(ENV_WEB_URL)
    with contextlib.suppress(Exception):
        send_desktop(message)
    with contextlib.suppress(Exception):
        send_pushover(message, url=web_url)


def notify_question(question: Question) -> None:
    """質問の登録を各チャネルへ通知する。"""
    notify_message(build_message(question))


def notify_retry_exhausted(issue: Issue, limit: int) -> None:
    """自動リトライの上限到達を各チャネルへ通知する。"""
    notify_message(
        f"#{issue.id} {issue.ref}: 自動リトライが上限({limit}回)に達し failed のまま停止"
    )
