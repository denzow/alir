"""設定値(ブランチ名テンプレート・直接 push 運用)のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import db, registry, settings
from alir.settings import SettingsError


@pytest.fixture
def conn(tmp_path: Path):  # type: ignore[no-untyped-def]
    return db.connect(tmp_path / "data")


def test_default_template(conn) -> None:  # type: ignore[no-untyped-def]
    assert settings.branch_template(conn) == "alir/issue-{number}"


def test_set_and_get_roundtrip(conn) -> None:  # type: ignore[no-untyped-def]
    settings.set_branch_template(conn, "feature/{repo}-{number}")
    assert settings.branch_template(conn) == "feature/{repo}-{number}"


@pytest.mark.parametrize(
    "template",
    [
        "alir/issue-{unknown}",
        "alir/static-name",
        "-starts-with-dash-{number}",
        "has space {number}",
        "ends-with-slash-{number}/",
        "double..dot-{number}",
    ],
)
def test_invalid_templates_rejected(conn, template) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SettingsError):
        settings.set_branch_template(conn, template)


def test_render_branch(conn) -> None:  # type: ignore[no-untyped-def]
    issue = registry.add(conn, url="https://github.com/denzow/alir/issues/12", workdir="/tmp")
    assert settings.render_branch("feature/{repo}-{number}", issue) == "feature/alir-12"
    assert settings.render_branch("work/{id}", issue) == "work/1"


def test_session_placeholder_template_accepted(conn) -> None:  # type: ignore[no-untyped-def]
    settings.set_branch_template(conn, "{number}/{type}/{summary}")
    assert settings.branch_template(conn) == "{number}/{type}/{summary}"


def test_render_branch_fills_session_placeholders_with_provisional(conn) -> None:  # type: ignore[no-untyped-def]
    issue = registry.add(conn, url="https://github.com/denzow/alir/issues/12", workdir="/tmp")
    assert settings.render_branch("{number}/{type}/{summary}", issue) == "12/work/wip"


def test_has_session_placeholders() -> None:
    assert settings.has_session_placeholders("{number}/{type}/{summary}") is True
    assert settings.has_session_placeholders("alir/issue-{number}") is False


def test_resume_enabled_default_true(conn) -> None:  # type: ignore[no-untyped-def]
    assert settings.resume_enabled(conn) is True


def test_set_resume_enabled_roundtrip(conn) -> None:  # type: ignore[no-untyped-def]
    settings.set_resume_enabled(conn, False)
    assert settings.resume_enabled(conn) is False
    settings.set_resume_enabled(conn, True)
    assert settings.resume_enabled(conn) is True


def test_model_default_none(conn) -> None:  # type: ignore[no-untyped-def]
    assert settings.model(conn) is None


def test_set_and_clear_model(conn) -> None:  # type: ignore[no-untyped-def]
    settings.set_model(conn, " sonnet ")
    assert settings.model(conn) == "sonnet"
    settings.clear_model(conn)
    assert settings.model(conn) is None


def test_set_empty_model_rejected(conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SettingsError):
        settings.set_model(conn, "  ")


def test_pushover_default_none(conn) -> None:  # type: ignore[no-untyped-def]
    assert settings.pushover(conn) is None


def test_set_and_clear_pushover(conn) -> None:  # type: ignore[no-untyped-def]
    settings.set_pushover(conn, token=" app-token ", user=" user-key ")
    assert settings.pushover(conn) == ("app-token", "user-key")
    settings.clear_pushover(conn)
    assert settings.pushover(conn) is None


@pytest.mark.parametrize("token,user", [("", "u"), ("t", ""), ("  ", "  ")])
def test_set_pushover_requires_both(conn, token, user) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SettingsError):
        settings.set_pushover(conn, token=token, user=user)


def test_web_url_default_none(conn) -> None:  # type: ignore[no-untyped-def]
    assert settings.web_url(conn) is None


def test_set_and_clear_web_url(conn) -> None:  # type: ignore[no-untyped-def]
    settings.set_web_url(conn, " http://192.168.1.10:8710 ")
    assert settings.web_url(conn) == "http://192.168.1.10:8710"
    settings.clear_web_url(conn)
    assert settings.web_url(conn) is None


@pytest.mark.parametrize("url", ["", "  ", "192.168.1.10:8710", "ftp://x"])
def test_set_web_url_requires_http_scheme(conn, url) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SettingsError):
        settings.set_web_url(conn, url)


def test_push_branches_default_empty(conn) -> None:  # type: ignore[no-untyped-def]
    assert settings.push_branches(conn) == {}
    assert settings.push_branch(conn, "/tmp/none") is None


def test_set_and_clear_push_branch(conn, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    workdir = tmp_path / "repo"
    workdir.mkdir()
    settings.set_push_branch(conn, workdir=str(workdir), branch="develop")
    resolved = str(workdir.resolve())
    assert settings.push_branches(conn) == {resolved: "develop"}
    assert settings.push_branch(conn, resolved) == "develop"

    settings.clear_push_branch(conn, workdir=str(workdir))
    assert settings.push_branches(conn) == {}


def test_set_push_branch_overwrites_existing(conn, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    workdir = tmp_path / "repo"
    workdir.mkdir()
    settings.set_push_branch(conn, workdir=str(workdir), branch="develop")
    settings.set_push_branch(conn, workdir=str(workdir), branch="main")
    assert settings.push_branch(conn, str(workdir.resolve())) == "main"


@pytest.mark.parametrize("branch", ["", "-bad", "has space", "double..dot", "ends/", "a.lock"])
def test_set_push_branch_rejects_invalid_branch(conn, tmp_path: Path, branch: str) -> None:  # type: ignore[no-untyped-def]
    workdir = tmp_path / "repo"
    workdir.mkdir()
    with pytest.raises(SettingsError):
        settings.set_push_branch(conn, workdir=str(workdir), branch=branch)


def test_set_push_branch_rejects_missing_workdir(conn, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SettingsError):
        settings.set_push_branch(conn, workdir=str(tmp_path / "missing"), branch="develop")


def test_clear_push_branch_unknown_raises(conn, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SettingsError):
        settings.clear_push_branch(conn, workdir=str(tmp_path))


def test_usage_threshold_default(conn) -> None:  # type: ignore[no-untyped-def]
    assert settings.usage_threshold(conn) == 0.5


def test_set_usage_threshold_roundtrip(conn) -> None:  # type: ignore[no-untyped-def]
    settings.set_usage_threshold(conn, 0.9)
    assert settings.usage_threshold(conn) == 0.9
    settings.set_usage_threshold(conn, 1.0)
    assert settings.usage_threshold(conn) == 1.0


def test_set_usage_threshold_rejects_out_of_range(conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SettingsError):
        settings.set_usage_threshold(conn, 0)
    with pytest.raises(SettingsError):
        settings.set_usage_threshold(conn, 1.5)


def test_retry_limit_default(conn) -> None:  # type: ignore[no-untyped-def]
    assert settings.retry_limit(conn) == 2


def test_set_retry_limit_roundtrip(conn) -> None:  # type: ignore[no-untyped-def]
    settings.set_retry_limit(conn, 5)
    assert settings.retry_limit(conn) == 5
    settings.set_retry_limit(conn, 0)
    assert settings.retry_limit(conn) == 0


def test_set_retry_limit_rejects_negative(conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SettingsError):
        settings.set_retry_limit(conn, -1)


def test_voice_notify_defaults_and_partial_update(conn) -> None:  # type: ignore[no-untyped-def]
    assert settings.voice_notify(conn) == settings.DEFAULT_VOICE_NOTIFY
    settings.set_voice_notify(conn, {"question": "chime"})
    policies = settings.voice_notify(conn)
    assert policies["question"] == "chime"
    # 指定しなかった種別は既定のまま
    assert policies["session_done"] == settings.DEFAULT_VOICE_NOTIFY["session_done"]


def test_voice_notify_rejects_unknown_kind_and_policy(conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SettingsError):
        settings.set_voice_notify(conn, {"unknown_kind": "speak"})
    with pytest.raises(SettingsError):
        settings.set_voice_notify(conn, {"question": "loud"})


def test_voice_notify_ignores_corrupted_stored_value(conn) -> None:  # type: ignore[no-untyped-def]
    from alir import control

    control.set_value(conn, settings.KEY_VOICE_NOTIFY, "not json")
    assert settings.voice_notify(conn) == settings.DEFAULT_VOICE_NOTIFY
    # 未知のポリシー値も無視して既定に落ちる
    control.set_value(conn, settings.KEY_VOICE_NOTIFY, '{"question": "loud"}')
    assert settings.voice_notify(conn)["question"] == "speak"
