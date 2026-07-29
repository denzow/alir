"""通知: 質問の登録を Pushover とデスクトップに知らせる。

通知は補助機能なので、失敗しても質問の登録は成功させる(best-effort)。
Pushover は環境変数 ALIR_PUSHOVER_TOKEN / ALIR_PUSHOVER_USER があるときだけ送る。
通知に含める Web UI の URL は ALIR_WEB_URL(例: http://192.168.1.10:8710)で指定する。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import urllib.parse
import urllib.request

from alir.questions import Question

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


def send_pushover(message: str, *, url: str | None) -> None:
    """Pushover に通知を送る。認証情報がなければ何もしない。"""
    token = os.environ.get(ENV_PUSHOVER_TOKEN)
    user = os.environ.get(ENV_PUSHOVER_USER)
    if not token or not user:
        return
    fields = {"token": token, "user": user, "title": "alir", "message": message}
    if url:
        fields["url"] = url
    data = urllib.parse.urlencode(fields).encode("utf-8")
    with urllib.request.urlopen(_PUSHOVER_API, data=data, timeout=10):
        pass


def notify_question(question: Question) -> None:
    """質問の登録を各チャネルへ通知する。失敗は握りつぶす。"""
    message = build_message(question)
    web_url = os.environ.get(ENV_WEB_URL)
    with contextlib.suppress(Exception):
        send_desktop(message)
    with contextlib.suppress(Exception):
        send_pushover(message, url=web_url)
