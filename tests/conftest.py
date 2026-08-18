"""テスト共通の分離設定。

通知(notify)は Pushover の認証情報を実環境のデータディレクトリ
(config.data_dir() が返す settings)から読むため、テストが通知経路に
入ると運用者の実クレデンシャルで実送信してしまう。全テストで
データディレクトリを一時領域に、通知系の環境変数を未設定に切り替え、
デスクトップ通知(notify-send)も外部プロセスを起こさないよう無効化する。
個別のテストが自分の monkeypatch で上書きするのは自由
(この autouse フィクスチャより後に適用されるため、そちらが勝つ)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import notify
from alir.config import ENV_DATA_DIR


@pytest.fixture(autouse=True)
def _isolate_notifications(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "isolated-data"))
    monkeypatch.delenv(notify.ENV_PUSHOVER_TOKEN, raising=False)
    monkeypatch.delenv(notify.ENV_PUSHOVER_USER, raising=False)
    monkeypatch.delenv(notify.ENV_WEB_URL, raising=False)
    monkeypatch.setattr(notify, "send_desktop", lambda message: None)
