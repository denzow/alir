"""実施内容の記録(reports)のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import db, mcp_server, reports
from alir.reports import ReportError


@pytest.fixture
def conn(tmp_path: Path):  # type: ignore[no-untyped-def]
    return db.connect(tmp_path / "data")


def test_add_and_list(conn) -> None:  # type: ignore[no-untyped-def]
    reports.add(conn, issue="denzow/alir#1", summary="実装して PR を作成")
    reports.add(
        conn,
        issue="denzow/alir#1",
        summary="レビュー指摘を反映",
        pr_url="https://github.com/denzow/alir/pull/5",
        session_id="sess-2",
    )
    items = reports.list_reports(conn)
    assert [r.summary for r in items] == ["レビュー指摘を反映", "実装して PR を作成"]
    assert items[0].pr_url == "https://github.com/denzow/alir/pull/5"


def test_list_filters_by_issue(conn) -> None:  # type: ignore[no-untyped-def]
    reports.add(conn, issue="denzow/alir#1", summary="a")
    reports.add(conn, issue="denzow/alir#2", summary="b")
    items = reports.list_reports(conn, issue="denzow/alir#2")
    assert [r.summary for r in items] == ["b"]


def test_latest_by_issue(conn) -> None:  # type: ignore[no-untyped-def]
    reports.add(conn, issue="denzow/alir#1", summary="old")
    reports.add(conn, issue="denzow/alir#1", summary="new")
    reports.add(conn, issue="denzow/alir#2", summary="only")
    latest = reports.latest_by_issue(conn)
    assert latest["denzow/alir#1"].summary == "new"
    assert latest["denzow/alir#2"].summary == "only"


def test_add_rejects_empty_summary(conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ReportError):
        reports.add(conn, issue="denzow/alir#1", summary="   ")


def test_report_result_tool(conn) -> None:  # type: ignore[no-untyped-def]
    result = mcp_server.report_result_tool(
        conn,
        issue="denzow/alir#1",
        summary="実装して PR を作成",
        pr_url="https://github.com/denzow/alir/pull/5",
    )
    assert result["report_id"] == 1
    assert reports.list_reports(conn)[0].pr_url == "https://github.com/denzow/alir/pull/5"


def test_add_with_refined_outcome(conn) -> None:  # type: ignore[no-untyped-def]
    r = reports.add(conn, issue="denzow/alir#1", summary="仕様を整理", outcome="refined")
    assert r.outcome == "refined"
    assert reports.list_reports(conn)[0].outcome == "refined"


def test_default_outcome_is_implemented(conn) -> None:  # type: ignore[no-untyped-def]
    reports.add(conn, issue="denzow/alir#1", summary="実装した")
    assert reports.list_reports(conn)[0].outcome == "implemented"


def test_add_rejects_unknown_outcome(conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ReportError):
        reports.add(conn, issue="denzow/alir#1", summary="x", outcome="paused")
