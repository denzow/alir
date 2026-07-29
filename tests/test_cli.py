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
