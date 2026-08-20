"""voice WebSocket エンドポイント(Phase 0 の骨組み)のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from alir.serve import create_combined_app
from alir.voice import WS_PATH


@pytest.fixture
def dbdir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def test_ping_returns_pong(dbdir: Path) -> None:
    with (
        TestClient(create_combined_app(dbdir)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_start_and_stop_are_acknowledged(dbdir: Path) -> None:
    with (
        TestClient(create_combined_app(dbdir)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_json({"type": "start", "sample_rate": 16000})
        assert ws.receive_json() == {"type": "started", "sample_rate": 16000}
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "stopped"}


def test_binary_frames_are_discarded(dbdir: Path) -> None:
    # Phase 0 では音声フレームを捨てるだけで、接続は生きたままになる
    with (
        TestClient(create_combined_app(dbdir)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_bytes(b"\x00\x01" * 160)
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_invalid_json_returns_error(dbdir: Path) -> None:
    with (
        TestClient(create_combined_app(dbdir)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_text("not json")
        assert ws.receive_json() == {"type": "error", "message": "invalid json"}


def test_non_object_json_returns_error(dbdir: Path) -> None:
    # "123" は有効な JSON だが object ではない。接続を落とさず error を返す
    with (
        TestClient(create_combined_app(dbdir)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_text("123")
        assert ws.receive_json() == {"type": "error", "message": "invalid frame"}
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_unknown_type_returns_error(dbdir: Path) -> None:
    with (
        TestClient(create_combined_app(dbdir)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_json({"type": "dance"})
        res = ws.receive_json()
        assert res["type"] == "error"
        assert "dance" in res["message"]
