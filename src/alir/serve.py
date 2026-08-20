"""ワンプロセス起動: Web UI と MCP(streamable HTTP)を 1 つの ASGI アプリにまとめる。

MCP を stdio ではなく HTTP で提供することで、セッションごとのサブプロセスが不要になり、
質問 DB への書き込みもこのプロセスに集約される。
ループドライバは CLI 側でバックグラウンドスレッドとして同居させる。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from alir import mcp_server, voice, voice_engines, web


def create_combined_app(dbdir: Path) -> FastAPI:
    """Web UI・voice(/voice, /voice/ws)・MCP(/mcp)を同居させた ASGI アプリを返す。"""
    server = mcp_server.create_server(dbdir)
    mcp_app = server.streamable_http_app()  # 内部で /mcp を提供する
    app = web.create_app(dbdir, lifespan=lambda app: server.session_manager.run())
    app.include_router(voice.router)  # mount("/") より先に登録した経路が優先される
    # voice extra が未導入なら None になり、WS は制御フレームにだけ応答する
    app.state.voice_engines = voice_engines.build_engines(dbdir)
    app.mount("/", mcp_app)
    return app
