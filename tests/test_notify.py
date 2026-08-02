"""通知(Pushover の認証情報の解決と送信)のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import db, notify, settings
from alir.config import ENV_DATA_DIR


@pytest.fixture
def dbdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    monkeypatch.setenv(ENV_DATA_DIR, str(path))
    monkeypatch.delenv(notify.ENV_PUSHOVER_TOKEN, raising=False)
    monkeypatch.delenv(notify.ENV_PUSHOVER_USER, raising=False)
    return path


def test_credentials_none_without_config(dbdir: Path) -> None:
    assert notify.pushover_credentials() is None


def test_credentials_from_env(dbdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(notify.ENV_PUSHOVER_TOKEN, "env-token")
    monkeypatch.setenv(notify.ENV_PUSHOVER_USER, "env-user")
    assert notify.pushover_credentials() == ("env-token", "env-user")


def test_credentials_settings_take_precedence(
    dbdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(notify.ENV_PUSHOVER_TOKEN, "env-token")
    monkeypatch.setenv(notify.ENV_PUSHOVER_USER, "env-user")
    settings.set_pushover(db.connect(dbdir), token="db-token", user="db-user")
    assert notify.pushover_credentials() == ("db-token", "db-user")


def test_credentials_cleared_settings_fall_back_to_env(
    dbdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(notify.ENV_PUSHOVER_TOKEN, "env-token")
    monkeypatch.setenv(notify.ENV_PUSHOVER_USER, "env-user")
    conn = db.connect(dbdir)
    settings.set_pushover(conn, token="db-token", user="db-user")
    settings.clear_pushover(conn)
    assert notify.pushover_credentials() == ("env-token", "env-user")


def test_send_pushover_without_credentials_returns_false(
    dbdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_urlopen(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("must not send")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fail_urlopen)
    assert notify.send_pushover("hello", url=None) is False


def test_send_pushover_posts_credentials_from_settings(
    dbdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib
    import urllib.parse

    settings.set_pushover(db.connect(dbdir), token="db-token", user="db-user")
    captured: dict[str, bytes] = {}

    def fake_urlopen(url, data=None, timeout=None):  # type: ignore[no-untyped-def]
        captured["data"] = data
        return contextlib.nullcontext()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert notify.send_pushover("hello", url="http://example.com") is True
    fields = urllib.parse.parse_qs(captured["data"].decode("utf-8"))
    assert fields["token"] == ["db-token"]
    assert fields["user"] == ["db-user"]
    assert fields["message"] == ["hello"]
    assert fields["url"] == ["http://example.com"]
