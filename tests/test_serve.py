"""ワンプロセス起動(Web UI + MCP 同居)のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from alir import db, driver, questions
from alir.serve import create_combined_app


@pytest.fixture
def dbdir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def test_combined_app_serves_web_ui(dbdir: Path) -> None:
    conn = db.connect(dbdir)
    questions.ask(
        conn,
        issue="denzow/alir#1",
        question="スキーマを変えてよいか",
        options=["変える", "変えない"],
        recommended="変える",
        impact="high",
        timeout_action="keep_parked",
    )
    with TestClient(create_combined_app(dbdir)) as client:
        res = client.get("/")
        assert res.status_code == 200
        assert "スキーマを変えてよいか" in res.text

        res = client.post("/questions/1/answer", data={"choice": "1"}, follow_redirects=True)
        assert res.status_code == 200
    conn = db.connect(dbdir)
    assert questions.get(conn, 1).status == questions.STATUS_ANSWERED


def test_combined_app_mounts_mcp(dbdir: Path) -> None:
    with TestClient(create_combined_app(dbdir)) as client:
        res = client.get("/mcp")
        assert res.status_code != 404


def test_write_mcp_config_http(dbdir: Path) -> None:
    dbdir.mkdir(parents=True)
    path = driver.write_mcp_config(dbdir, mcp_url="http://127.0.0.1:8710/mcp")
    text = path.read_text(encoding="utf-8")
    assert '"type": "http"' in text
    assert "http://127.0.0.1:8710/mcp" in text
    assert "command" not in text


def test_write_mcp_config_stdio_default(dbdir: Path) -> None:
    dbdir.mkdir(parents=True)
    path = driver.write_mcp_config(dbdir)
    text = path.read_text(encoding="utf-8")
    assert '"command": "alir"' in text
