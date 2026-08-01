"""設定値(ブランチ名テンプレート)のテスト。"""

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
