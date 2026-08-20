"""voice WebSocket エンドポイントと音声パイプのテスト。

実エンジン(silero / whisper / VOICEVOX)は使わず、フェイクの Engines を注入して
「PCM → 区間確定 → STT → TTS 返送」の流れとプロトコルを確かめる。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from alir import voice
from alir.serve import create_combined_app
from alir.voice import WS_PATH, Engines, Segmenter, SegmenterConfig, SpeechSegment, SpeechStart

# テスト用の小さいチャンク(1 チャンク 32 バイト)
CHUNK = 32
CONFIG = SegmenterConfig(
    prefix_chunks=2, silence_chunks=3, min_speech_chunks=2, max_chunks=100
)


def fake_probe(chunk: bytes) -> float:
    """非ゼロのバイトを含むチャンクを発話とみなす決定的な VAD。"""
    return 1.0 if any(chunk) else 0.0


def make_engines(transcribed: list[bytes] | None = None) -> Engines:
    def transcriber(pcm: bytes) -> str:
        if transcribed is not None:
            transcribed.append(pcm)
        return "こんにちは"

    return Engines(
        probe_factory=lambda: fake_probe,
        transcriber=transcriber,
        synthesizer=lambda text: b"WAV:" + text.encode(),
        chunk_bytes=CHUNK,
        segmenter_config=CONFIG,
    )


@pytest.fixture
def dbdir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def make_app(dbdir: Path, engines: Engines | None) -> FastAPI:
    app = create_combined_app(dbdir)
    app.state.voice_engines = engines
    return app


# --- 制御フレーム(Phase 0 から変わらない部分) ---


def test_ping_returns_pong(dbdir: Path) -> None:
    with (
        TestClient(make_app(dbdir, None)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_start_and_stop_are_acknowledged(dbdir: Path) -> None:
    with (
        TestClient(make_app(dbdir, None)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_json({"type": "start", "sample_rate": 16000})
        assert ws.receive_json() == {"type": "started", "sample_rate": 16000}
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "stopped"}


def test_unsupported_sample_rate_returns_error(dbdir: Path) -> None:
    with (
        TestClient(make_app(dbdir, None)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_json({"type": "start", "sample_rate": 44100})
        res = ws.receive_json()
        assert res["type"] == "error"
        assert "44100" in res["message"]


def test_binary_frames_are_discarded_without_engines(dbdir: Path) -> None:
    # voice extra 未導入(Engines なし)では音声フレームを捨て、接続は生きたままになる
    with (
        TestClient(make_app(dbdir, None)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_bytes(b"\x00\x01" * 160)
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_invalid_json_returns_error(dbdir: Path) -> None:
    with (
        TestClient(make_app(dbdir, None)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_text("not json")
        assert ws.receive_json() == {"type": "error", "message": "invalid json"}


def test_non_object_json_returns_error(dbdir: Path) -> None:
    # "123" は有効な JSON だが object ではない。接続を落とさず error を返す
    with (
        TestClient(make_app(dbdir, None)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_text("123")
        assert ws.receive_json() == {"type": "error", "message": "invalid frame"}
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_unknown_type_returns_error(dbdir: Path) -> None:
    with (
        TestClient(make_app(dbdir, None)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_json({"type": "dance"})
        res = ws.receive_json()
        assert res["type"] == "error"
        assert "dance" in res["message"]


# --- 音声パイプ(Phase 1) ---


def test_speech_roundtrip_echoes_synthesized_audio(dbdir: Path) -> None:
    """発話 → interrupt → stt_final → 合成音声、のオウム返し一往復。"""
    transcribed: list[bytes] = []
    with (
        TestClient(make_app(dbdir, make_engines(transcribed))) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_json({"type": "start", "sample_rate": 16000})
        assert ws.receive_json() == {"type": "started", "sample_rate": 16000}
        ws.send_bytes(b"\x01" * (CHUNK * 4))  # 発話 4 チャンク
        assert ws.receive_json() == {"type": "interrupt"}
        ws.send_bytes(b"\x00" * (CHUNK * 3))  # 末尾無音 3 チャンクで区間確定
        assert ws.receive_json() == {"type": "stt_final", "text": "こんにちは"}
        assert ws.receive_json() == {"type": "audio_start"}
        assert ws.receive_bytes() == b"WAV:" + "こんにちは".encode()
        assert ws.receive_json() == {"type": "audio_end"}
    # 区間には発話と末尾無音の全チャンクが含まれる
    assert [len(pcm) for pcm in transcribed] == [CHUNK * 7]


def test_empty_transcription_sends_nothing(dbdir: Path) -> None:
    """認識結果が空白だけなら stt_final も音声も送らない。"""
    engines = Engines(
        probe_factory=lambda: fake_probe,
        transcriber=lambda pcm: "  ",
        synthesizer=lambda text: b"WAV",
        chunk_bytes=CHUNK,
        segmenter_config=CONFIG,
    )
    with (
        TestClient(make_app(dbdir, engines)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_bytes(b"\x01" * (CHUNK * 4))
        assert ws.receive_json() == {"type": "interrupt"}
        ws.send_bytes(b"\x00" * (CHUNK * 3))
        # 何も来ないことを ping/pong の順序で確かめる
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_synthesizer_failure_keeps_connection(dbdir: Path) -> None:
    """TTS が失敗しても stt_final は届き、接続は生きたままになる。"""

    def broken_synthesizer(text: str) -> bytes:
        raise RuntimeError("voicevox down")

    engines = Engines(
        probe_factory=lambda: fake_probe,
        transcriber=lambda pcm: "こんにちは",
        synthesizer=broken_synthesizer,
        chunk_bytes=CHUNK,
        segmenter_config=CONFIG,
    )
    with (
        TestClient(make_app(dbdir, engines)) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_bytes(b"\x01" * (CHUNK * 4))
        assert ws.receive_json() == {"type": "interrupt"}
        ws.send_bytes(b"\x00" * (CHUNK * 3))
        assert ws.receive_json() == {"type": "stt_final", "text": "こんにちは"}
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_oversized_input_frame_is_ignored(dbdir: Path) -> None:
    """上限を超える上りフレームは VAD にかけず無視する(受信ループの保護)。"""
    with (
        TestClient(make_app(dbdir, make_engines())) as client,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.send_bytes(b"\x01" * (64 * 1024 + 1))  # 発話相当のバイト列だが無視される
        ws.send_json({"type": "ping"})
        # interrupt が来ない(= VAD に届いていない)ことを応答順で確かめる
        assert ws.receive_json() == {"type": "pong"}


def test_voice_page_is_served(dbdir: Path) -> None:
    with TestClient(make_app(dbdir, None)) as client:
        res = client.get(voice.PAGE_PATH)
        assert res.status_code == 200
        assert "alir voice" in res.text


# --- Segmenter 単体 ---


def test_segmenter_buffers_partial_chunks() -> None:
    probs: list[bytes] = []

    def probe(chunk: bytes) -> float:
        probs.append(chunk)
        return 0.0

    seg = Segmenter(probe, chunk_bytes=CHUNK, config=CONFIG)
    seg.feed(b"\x00" * (CHUNK - 1))
    assert probs == []
    seg.feed(b"\x00" * 1)
    assert [len(c) for c in probs] == [CHUNK]


def test_segmenter_includes_prefix_before_speech_start() -> None:
    seg = Segmenter(fake_probe, chunk_bytes=CHUNK, config=CONFIG)
    seg.feed(b"\x00" * (CHUNK * 5))  # 無音 5 チャンク(prefix には直近 2 つだけ残る)
    events = seg.feed(b"\x01" * (CHUNK * 4))  # 発話開始
    assert events == [SpeechStart()]
    events = seg.feed(b"\x00" * (CHUNK * 3))  # 区間確定
    assert len(events) == 1
    segment = events[0]
    assert isinstance(segment, SpeechSegment)
    # prefix 2 + 発話 4 + 末尾無音 3 = 9 チャンク(語頭の欠け防止で prefix が入る)
    assert len(segment.audio) == CHUNK * 9


def test_segmenter_discards_short_blip() -> None:
    config = SegmenterConfig(
        prefix_chunks=2, silence_chunks=3, min_speech_chunks=3, max_chunks=100
    )
    seg = Segmenter(fake_probe, chunk_bytes=CHUNK, config=config)
    assert seg.feed(b"\x01" * CHUNK) == [SpeechStart()]  # 発話 1 チャンクだけ
    assert seg.feed(b"\x00" * (CHUNK * 3)) == []  # min_speech_chunks 未満なので捨てる


def test_segmenter_forces_cut_at_max_chunks() -> None:
    config = SegmenterConfig(
        prefix_chunks=0, silence_chunks=100, min_speech_chunks=1, max_chunks=5
    )
    seg = Segmenter(fake_probe, chunk_bytes=CHUNK, config=config)
    events = seg.feed(b"\x01" * (CHUNK * 10))
    starts = [e for e in events if isinstance(e, SpeechStart)]
    segments = [e for e in events if isinstance(e, SpeechSegment)]
    assert len(starts) >= 1
    assert segments and len(segments[0].audio) == CHUNK * 5


def test_segmenter_reset_clears_state() -> None:
    seg = Segmenter(fake_probe, chunk_bytes=CHUNK, config=CONFIG)
    seg.feed(b"\x01" * (CHUNK * 4))  # 発話中の状態にする
    seg.reset()
    assert seg.feed(b"\x00" * (CHUNK * 3)) == []  # 区間は確定しない(捨てられている)


# --- serve 起動時の Engines 組み立て ---


def test_serve_builds_real_engines_when_extra_installed(dbdir: Path) -> None:
    pytest.importorskip("pysilero_vad")
    pytest.importorskip("faster_whisper")
    app = create_combined_app(dbdir)
    engines = app.state.voice_engines
    assert engines is not None
    assert engines.chunk_bytes == 1024  # silero は 512 サンプル(16bit)単位
