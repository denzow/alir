"""MCP サーバー: Claude Code から質問(ask_human)を登録する。

ツール本体はプレーンな関数に分離し、FastMCP への登録は薄く保つ。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import iceql

from alir import db, notify, progress, questions, reports


def ask_human_tool(
    conn: iceql.Connection,
    *,
    issue: str,
    question: str,
    options: list[str],
    recommended: str,
    impact: str,
    timeout_action: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """質問を登録し、登録結果を返す。"""
    q = questions.ask(
        conn,
        issue=issue,
        question=question,
        options=options,
        recommended=recommended,
        impact=impact,
        timeout_action=timeout_action,
        session_id=session_id,
    )
    notify.notify_question(q)
    return {
        "question_id": q.id,
        "status": q.status,
        "message": (
            "Question registered. A human will answer asynchronously. "
            "Do not wait for the answer: summarize the current state and finish this session."
        ),
    }


def report_progress_tool(
    conn: iceql.Connection,
    *,
    issue: str,
    message: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """進捗を記録し、登録結果を返す。"""
    entry = progress.add(conn, issue=issue, message=message, session_id=session_id)
    return {"progress_id": entry.id, "message": "Recorded."}


def report_result_tool(
    conn: iceql.Connection,
    *,
    issue: str,
    summary: str,
    pr_url: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """実施内容を記録し、登録結果を返す。"""
    report = reports.add(conn, issue=issue, summary=summary, pr_url=pr_url, session_id=session_id)
    return {"report_id": report.id, "message": "Recorded."}


def create_server(dbdir: str | Path) -> Any:
    try:
        from mcp.server import MCPServer as _ServerClass
    except ImportError:  # 旧バージョンの SDK
        from mcp.server.fastmcp import FastMCP as _ServerClass  # type: ignore[no-redef]

    conn = db.connect(Path(dbdir))
    server = _ServerClass("alir")

    @server.tool()
    def ask_human(
        issue: str,
        question: str,
        options: list[str],
        recommended: str,
        impact: str,
        timeout_action: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Register a question for a human to answer asynchronously.

        Use this only for irreversible or high-impact decisions (schema changes,
        external API contracts, deletions). For low-impact decisions, proceed with
        your recommended option and note the decision in the PR body instead.

        Args:
            issue: Target issue as "owner/repo#number".
            question: What you need decided, with enough background to judge.
            options: 2-4 candidate choices. Each item should be self-contained.
            recommended: The option you recommend. Must be one of options.
            impact: "high" or "low".
            timeout_action: What to do if unanswered for the configured period:
                "proceed_with_recommended" or "keep_parked".
            session_id: Claude Code session id, if known (enables resume).

        Returns immediately; do not wait for the answer.
        """
        return ask_human_tool(
            conn,
            issue=issue,
            question=question,
            options=options,
            recommended=recommended,
            impact=impact,
            timeout_action=timeout_action,
            session_id=session_id,
        )

    @server.tool()
    def report_progress(
        issue: str,
        message: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Report a one-line progress update while working on an issue.

        Call this at each meaningful checkpoint: finished investigating,
        started implementing, running tests, opening a PR, and so on.
        A human watches these updates live, so keep each message short.

        Args:
            issue: Target issue as "owner/repo#number".
            message: One-line progress update, e.g. "調査完了、実装に着手".
            session_id: Claude Code session id, if known.
        """
        return report_progress_tool(conn, issue=issue, message=message, session_id=session_id)

    @server.tool()
    def report_result(
        issue: str,
        summary: str,
        pr_url: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Record what you did for an issue. Call this once before finishing a session.

        Args:
            issue: Target issue as "owner/repo#number".
            summary: One-line summary of what was done (what changed, or how far
                you got before parking). Write it for a human scanning a list.
            pr_url: URL of the pull request, if you opened one.
            session_id: Claude Code session id, if known.
        """
        return report_result_tool(
            conn, issue=issue, summary=summary, pr_url=pr_url, session_id=session_id
        )

    return server
