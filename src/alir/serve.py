"""ワンプロセス起動: Web UI と MCP(streamable HTTP)を 1 つの ASGI アプリにまとめる。

MCP を stdio ではなく HTTP で提供することで、セッションごとのサブプロセスが不要になり、
質問 DB への書き込みもこのプロセスに集約される。
ループドライバは CLI 側でバックグラウンドスレッドとして同居させる。
"""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Mount

from alir import mcp_server, web


def create_combined_app(dbdir: Path) -> Starlette:
    """Web UI のルートと MCP(/mcp)を同居させた ASGI アプリを返す。"""
    server = mcp_server.create_server(dbdir)
    mcp_app = server.streamable_http_app()  # 内部で /mcp を提供する
    app = Starlette(
        routes=[*web.app_routes(), Mount("/", app=mcp_app)],
        lifespan=lambda app: server.session_manager.run(),
    )
    app.state.dbdir = dbdir
    return app
