"""イベントバス: 通知ポイントで起きた出来事をプロセス内の購読者へ配る。

質問の登録や処理の失敗を、Pushover 通知と voice 読み上げ(後続フェーズ)のように
複数のチャネルへ届けるための pub/sub。通知の発行元(driver・MCP ハンドラ)は
届け先を知らずに publish だけを行い、チャネル側が購読者として登録する。
driver はバックグラウンドスレッドで動くため、publish はどのスレッドから
呼ばれてもよいようにロックで守る。購読者は publish 元のスレッドで同期実行される
ので、asyncio 側の購読者(voice の WS 配送など)はコールバック内で
loop.call_soon_threadsafe などを使ってイベントループへ橋渡しする。
配信は best-effort で、購読者の失敗は他の購読者と発行元の処理に影響させない。
既定の購読者(Pushover・デスクトップ)は notify の import 時に登録されるため、
バスへ publish する側は notify 経由の通知関数を使うか、notify を import しておく。
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

KIND_QUESTION = "question"
KIND_ISSUE_FAILED = "issue_failed"
KIND_RETRY_EXHAUSTED = "retry_exhausted"


@dataclass(frozen=True)
class Event:
    """通知ポイントで起きた出来事。

    message は人間向けの一文で、Pushover などテキスト通知はこれをそのまま使う。
    data には voice の読み上げ生成などで使う構造化した値(question_id など)を入れる。
    """

    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


Subscriber = Callable[[Event], None]


class EventBus:
    """スレッドセーフな in-process pub/sub。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        """購読者を登録し、解除用の関数を返す。"""
        with self._lock:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock, contextlib.suppress(ValueError):
                self._subscribers.remove(subscriber)

        return unsubscribe

    def publish(self, event: Event) -> None:
        """すべての購読者へイベントを配る。購読者の例外は握りつぶす。"""
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            with contextlib.suppress(Exception):
                subscriber(event)


# プロセス全体で共有する既定のバス。notify が既定の購読者(Pushover・デスクトップ)を
# 登録し、serve が voice の購読者を足す
bus = EventBus()
