"""CLI のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from alir import db, questions
from alir.cli import main
from alir.config import ENV_DATA_DIR


@pytest.fixture
def dbdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    monkeypatch.setenv(ENV_DATA_DIR, str(path))
    return path


def _ask(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    questions.ask(
        conn,
        issue="denzow/alir#1",
        question="DB スキーマを変更してよいか",
        options=["変更する", "変更しない"],
        recommended="変更する",
        impact="high",
        timeout_action="keep_parked",
    )


def test_questions_empty(dbdir: Path) -> None:
    result = CliRunner().invoke(main, ["questions"])
    assert result.exit_code == 0
    assert "no questions" in result.output


def test_questions_lists_open(dbdir: Path) -> None:
    _ask(dbdir)
    result = CliRunner().invoke(main, ["questions"])
    assert result.exit_code == 0
    assert "#1 [open] denzow/alir#1 (impact: high)" in result.output
    assert "*1) 変更する" in result.output


def test_answer_and_hidden_from_default_list(dbdir: Path) -> None:
    _ask(dbdir)
    result = CliRunner().invoke(main, ["answer", "1", "2", "今回は見送り"])
    assert result.exit_code == 0
    assert "answered #1: 変更しない" in result.output

    result = CliRunner().invoke(main, ["questions"])
    assert "no questions" in result.output

    result = CliRunner().invoke(main, ["questions", "--all"])
    assert "[answered]" in result.output
    assert "A: 変更しない (今回は見送り)" in result.output


def test_answer_unknown_id_fails(dbdir: Path) -> None:
    result = CliRunner().invoke(main, ["answer", "99", "1"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_issues_targets_add_list_remove(dbdir: Path, tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    runner = CliRunner()

    result = runner.invoke(main, ["issues", "targets"])
    assert result.exit_code == 0
    assert "no targets" in result.output

    result = runner.invoke(
        main, ["issues", "targets", "add", "--label", "alir", "--workdir", str(workdir)]
    )
    assert result.exit_code == 0
    assert "added: alir" in result.output

    result = runner.invoke(main, ["issues", "targets"])
    assert f"alir ({workdir.resolve()})" in result.output

    result = runner.invoke(
        main, ["issues", "targets", "remove", "--label", "alir", "--workdir", str(workdir)]
    )
    assert result.exit_code == 0

    result = runner.invoke(main, ["issues", "targets"])
    assert "no targets" in result.output


def test_issues_targets_interval(dbdir: Path) -> None:
    from alir import db, importer

    runner = CliRunner()
    result = runner.invoke(main, ["issues", "targets", "interval"])
    assert result.exit_code == 0
    assert "disabled" in result.output

    result = runner.invoke(main, ["issues", "targets", "interval", "300"])
    assert result.exit_code == 0
    assert importer.import_interval(db.connect(dbdir)) == 300.0

    result = runner.invoke(main, ["issues", "targets", "interval"])
    assert "every 300s" in result.output

    result = runner.invoke(main, ["issues", "targets", "interval", "5"])
    assert result.exit_code != 0
    assert "interval must be" in result.output


def test_issues_targets_add_duplicate_fails(dbdir: Path, tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    runner = CliRunner()
    args = ["issues", "targets", "add", "--label", "alir", "--workdir", str(workdir)]
    runner.invoke(main, args)
    result = runner.invoke(main, args)
    assert result.exit_code != 0
    assert "already registered" in result.output


def test_push_branch_set_list_unset(dbdir: Path, tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    runner = CliRunner()

    result = runner.invoke(main, ["push-branch"])
    assert result.exit_code == 0
    assert "no push branches" in result.output

    result = runner.invoke(
        main, ["push-branch", "set", "--workdir", str(workdir), "--branch", "develop"]
    )
    assert result.exit_code == 0
    assert "set: develop" in result.output

    result = runner.invoke(main, ["push-branch"])
    assert f"develop ({workdir.resolve()})" in result.output

    result = runner.invoke(main, ["push-branch", "unset", "--workdir", str(workdir)])
    assert result.exit_code == 0

    result = runner.invoke(main, ["push-branch"])
    assert "no push branches" in result.output


def test_push_branch_set_invalid_branch_fails(dbdir: Path, tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = CliRunner().invoke(
        main, ["push-branch", "set", "--workdir", str(workdir), "--branch", "bad..name"]
    )
    assert result.exit_code != 0
    assert "invalid branch name" in result.output


def test_push_branch_unset_unknown_fails(dbdir: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["push-branch", "unset", "--workdir", str(tmp_path)])
    assert result.exit_code != 0
    assert "not set" in result.output


def test_resume_show_enable_disable(dbdir: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["resume"])
    assert result.exit_code == 0
    assert result.output.strip() == "enabled"

    result = runner.invoke(main, ["resume", "disable"])
    assert result.exit_code == 0
    result = runner.invoke(main, ["resume"])
    assert result.output.strip() == "disabled"

    result = runner.invoke(main, ["resume", "enable"])
    assert result.exit_code == 0
    result = runner.invoke(main, ["resume"])
    assert result.output.strip() == "enabled"


def test_model_show_set_clear(dbdir: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["model"])
    assert result.exit_code == 0
    assert result.output.strip() == "(default)"

    result = runner.invoke(main, ["model", "set", "sonnet"])
    assert result.exit_code == 0
    result = runner.invoke(main, ["model"])
    assert result.output.strip() == "sonnet"

    result = runner.invoke(main, ["model", "clear"])
    assert result.exit_code == 0
    result = runner.invoke(main, ["model"])
    assert result.output.strip() == "(default)"


def test_model_set_empty_fails(dbdir: Path) -> None:
    result = CliRunner().invoke(main, ["model", "set", "  "])
    assert result.exit_code != 0


def test_uvicorn_log_config_adds_timestamp() -> None:
    import logging.config

    from alir.cli import _uvicorn_log_config

    config = _uvicorn_log_config()
    formatters = config["formatters"]
    assert isinstance(formatters, dict)
    for formatter in formatters.values():
        assert formatter["fmt"].startswith("%(asctime)s ")
        assert formatter["datefmt"] == "%Y-%m-%d %H:%M:%S"
    # dictConfig として妥当な形のまま(uvicorn.run に渡して起動できる)
    logging.config.dictConfig(config)


def test_pushover_show_set_clear(dbdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alir import notify

    monkeypatch.delenv(notify.ENV_PUSHOVER_TOKEN, raising=False)
    monkeypatch.delenv(notify.ENV_PUSHOVER_USER, raising=False)
    runner = CliRunner()

    result = runner.invoke(main, ["pushover"])
    assert result.exit_code == 0
    assert result.output.strip() == "not set"

    result = runner.invoke(main, ["pushover", "set", "--token", "t", "--user", "u"])
    assert result.exit_code == 0
    result = runner.invoke(main, ["pushover"])
    assert result.output.strip() == "configured (settings)"

    result = runner.invoke(main, ["pushover", "clear"])
    assert result.exit_code == 0
    result = runner.invoke(main, ["pushover"])
    assert result.output.strip() == "not set"


def test_pushover_show_reports_environment(dbdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alir import notify

    monkeypatch.setenv(notify.ENV_PUSHOVER_TOKEN, "t")
    monkeypatch.setenv(notify.ENV_PUSHOVER_USER, "u")
    result = CliRunner().invoke(main, ["pushover"])
    assert result.output.strip() == "configured (environment)"


def test_pushover_test_reports_unconfigured(dbdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alir import notify

    monkeypatch.delenv(notify.ENV_PUSHOVER_TOKEN, raising=False)
    monkeypatch.delenv(notify.ENV_PUSHOVER_USER, raising=False)
    result = CliRunner().invoke(main, ["pushover", "test"])
    assert result.exit_code != 0
    assert "not configured" in result.output


def test_pushover_test_sends(dbdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alir import notify

    sent: list[str] = []

    def fake_send(message: str, *, url: str | None) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr(notify, "send_pushover", fake_send)
    result = CliRunner().invoke(main, ["pushover", "test"])
    assert result.exit_code == 0
    assert result.output.strip() == "sent"
    assert sent


def test_web_url_show_set_clear(dbdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alir import notify

    monkeypatch.delenv(notify.ENV_WEB_URL, raising=False)
    runner = CliRunner()

    result = runner.invoke(main, ["web-url"])
    assert result.exit_code == 0
    assert result.output.strip() == "not set"

    result = runner.invoke(main, ["web-url", "set", "http://192.168.1.10:8710"])
    assert result.exit_code == 0
    result = runner.invoke(main, ["web-url"])
    assert result.output.strip() == "http://192.168.1.10:8710"

    result = runner.invoke(main, ["web-url", "clear"])
    assert result.exit_code == 0
    result = runner.invoke(main, ["web-url"])
    assert result.output.strip() == "not set"


def test_web_url_set_requires_scheme(dbdir: Path) -> None:
    result = CliRunner().invoke(main, ["web-url", "set", "192.168.1.10:8710"])
    assert result.exit_code != 0
    assert "http://" in result.output
