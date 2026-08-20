"""voice: スマホ通話クライアント向けの WebSocket エンドポイント。

机に置いたスマホ(PWA 通話画面)を薄いマイク/スピーカー端末として、
VAD・STT・意図解釈・TTS をすべてサーバ側に集約する構成(voice-plan.md)。
プロトコルは JSON テキストフレーム(制御)とバイナリフレーム(音声 PCM)の混在方式。
Phase 0 の現状は骨組みで、制御フレーム(ping / start / stop)への応答だけを実装し、
バイナリフレームは受け取って捨てる。音声処理は後続フェーズで載せる。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

WS_PATH = "/voice/ws"

router = APIRouter()


async def _handle_control(ws: WebSocket, text: str) -> None:
    """JSON テキストフレーム(制御)に応答する。"""
    try:
        frame: Any = json.loads(text)
    except json.JSONDecodeError:
        await ws.send_json({"type": "error", "message": "invalid json"})
        return
    # "123" のような object でない有効な JSON でも接続を落とさず error を返す
    if not isinstance(frame, dict):
        await ws.send_json({"type": "error", "message": "invalid frame"})
        return
    kind = frame.get("type")
    if kind == "ping":
        await ws.send_json({"type": "pong"})
    elif kind == "start":
        await ws.send_json({"type": "started", "sample_rate": frame.get("sample_rate", 16000)})
    elif kind == "stop":
        await ws.send_json({"type": "stopped"})
    else:
        await ws.send_json({"type": "error", "message": f"unknown type: {kind}"})


@router.websocket(WS_PATH)
async def voice_ws(ws: WebSocket) -> None:
    """通話クライアントとの WebSocket 接続。

    テキストとバイナリの両方を受けるため、receive_text ではなく receive で
    生のメッセージを読む。
    """
    await ws.accept()
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is not None:
                await _handle_control(ws, text)
            # バイナリ(音声フレーム)は Phase 1 で処理する。今は捨てる
    except WebSocketDisconnect:
        pass
