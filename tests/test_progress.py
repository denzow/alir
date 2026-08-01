"""セッションの進捗報告(progress)のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import db, mcp_server, progress
from alir.progress import ProgressError


@pytest.fixture
def conn(tmp_path: Path):  # type: ignore[no-untyped-def]
    return db.connect(tmp_path / "data")


def test_add_and_list_newest_first(conn) -> None:  # type: ignore[no-untyped-def]
    progress.add(conn, issue="denzow/alir#1", message="調査完了")
    progress.add(conn, issue="denzow/alir#1", message="実装に着手", session_id="sess-1")
    items = progress.list_progress(conn)
    assert [p.message for p in items] == ["実装に着手", "調査完了"]
    assert items[0].session_id == "sess-1"


def test_list_filters_by_issue(conn) -> None:  # type: ignore[no-untyped-def]
    progress.add(conn, issue="denzow/alir#1", message="a")
    progress.add(conn, issue="denzow/alir#2", message="b")
    assert [p.message for p in progress.list_progress(conn, issue="denzow/alir#2")] == ["b"]


def test_add_rejects_empty_message(conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ProgressError):
        progress.add(conn, issue="denzow/alir#1", message="  ")


def test_rotation(conn, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(progress, "MAX_PROGRESS", 2)
    for i in range(4):
        progress.add(conn, issue="denzow/alir#1", message=f"p{i}")
    assert [p.message for p in progress.list_progress(conn)] == ["p3", "p2"]


def test_report_progress_tool(conn) -> None:  # type: ignore[no-untyped-def]
    result = mcp_server.report_progress_tool(
        conn, issue="denzow/alir#1", message="テスト実行中", session_id="sess-1"
    )
    assert result["progress_id"] == 1
    assert progress.list_progress(conn)[0].message == "テスト実行中"
