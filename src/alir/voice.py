"""voice: スマホ通話クライアント向けの WebSocket エンドポイントと音声パイプ。

机に置いたスマホ(PWA 通話画面)を薄いマイク/スピーカー端末として、
VAD・STT・TTS をすべてサーバ側に集約する構成(voice-plan.md)。
プロトコルは JSON テキストフレーム(制御)とバイナリフレーム(音声 PCM)の混在方式。

Phase 1 の現状は「話した内容をテキスト化し、そのまま合成音声で返す」オウム返し。
上りの PCM を Segmenter(VAD)で発話区間に切り出し、区間ごとに STT → TTS して
音声フレームで返送する。VAD・STT・TTS の実体は Engines として注入可能にしてあり、
実装は voice_engines(silero-VAD / faster-whisper / VOICEVOX)、テストはフェイクを使う。
STT・TTS は重いので接続ごとのワーカタスクに逃がし、受信ループ(VAD)を止めない。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from alir.driver import log_with_timestamp as _log

WS_PATH = "/voice/ws"
PAGE_PATH = "/voice"
SAMPLE_RATE = 16000

router = APIRouter()


@dataclass(frozen=True)
class SegmenterConfig:
    """発話区間の切り出しパラメータ。時間はチャンク数で数える(1 チャンク = 32ms)。"""

    # この確率以上で発話開始とみなす
    start_threshold: float = 0.5
    # 発話中にこの確率未満が続いたら無音とみなす(開始よりゆるくしてバタつきを防ぐ)
    end_threshold: float = 0.35
    # 発話開始判定より前に遡って区間へ含めるチャンク数(語頭の欠けを防ぐ)
    prefix_chunks: int = 5
    # 区間確定に必要な末尾無音のチャンク数(約 380ms。レイテンシとのトレードオフ)
    silence_chunks: int = 12
    # 発話とみなすチャンクがこれ未満の区間は雑音として捨てる
    min_speech_chunks: int = 6
    # 区間の最大長(約 30 秒)。超えたら強制確定する
    max_chunks: int = 940


@dataclass(frozen=True)
class SpeechStart:
    """発話の開始。クライアントへの interrupt(再生破棄)に使う。"""


@dataclass(frozen=True)
class SpeechSegment:
    """確定した発話区間の PCM。"""

    audio: bytes


SegmentEvent = SpeechStart | SpeechSegment


class Segmenter:
    """PCM ストリームを発話区間に切り出す。

    VAD 本体(チャンクの発話確率を返す関数)は注入で受け取り、
    このクラスは確率の系列から区間を決める状態機械だけを持つ。
    """

    def __init__(
        self,
        probe: Callable[[bytes], float],
        *,
        chunk_bytes: int,
        config: SegmenterConfig | None = None,
    ) -> None:
        self._probe = probe
        self._chunk_bytes = chunk_bytes
        self._config = config or SegmenterConfig()
        self._pending = bytearray()
        self._prefix: deque[bytes] = deque(maxlen=self._config.prefix_chunks)
        self._chunks: list[bytes] = []
        self._in_speech = False
        self._silence_run = 0
        self._speech_hits = 0

    def reset(self) -> None:
        """区間の途中状態を捨てて待機状態に戻す(start / stop 時)。"""
        self._pending.clear()
        self._prefix.clear()
        self._chunks = []
        self._in_speech = False
        self._silence_run = 0
        self._speech_hits = 0

    def feed(self, data: bytes) -> list[SegmentEvent]:
        """PCM を受け取り、発生したイベントを返す。チャンク未満の端数は持ち越す。"""
        events: list[SegmentEvent] = []
        self._pending.extend(data)
        while len(self._pending) >= self._chunk_bytes:
            chunk = bytes(self._pending[: self._chunk_bytes])
            del self._pending[: self._chunk_bytes]
            events.extend(self._process(chunk))
        return events

    def _process(self, chunk: bytes) -> list[SegmentEvent]:
        prob = self._probe(chunk)
        config = self._config
        if not self._in_speech:
            if prob >= config.start_threshold:
                self._in_speech = True
                self._chunks = [*self._prefix, chunk]
                self._prefix.clear()
                self._silence_run = 0
                self._speech_hits = 1
                return [SpeechStart()]
            self._prefix.append(chunk)
            return []
        self._chunks.append(chunk)
        if prob >= config.start_threshold:
            self._speech_hits += 1
        if prob < config.end_threshold:
            self._silence_run += 1
        else:
            self._silence_run = 0
        if self._silence_run >= config.silence_chunks or len(self._chunks) >= config.max_chunks:
            return self._finalize()
        return []

    def _finalize(self) -> list[SegmentEvent]:
        chunks, hits = self._chunks, self._speech_hits
        self._in_speech = False
        self._chunks = []
        self._silence_run = 0
        self._speech_hits = 0
        if hits < self._config.min_speech_chunks:
            return []
        return [SpeechSegment(audio=b"".join(chunks))]


@dataclass(frozen=True)
class Engines:
    """音声パイプの構成要素。実体は voice_engines、テストはフェイクを注入する。

    VAD はチャンク単位の内部状態(RNN)を持つため、接続ごとに probe_factory で作る。
    transcriber と synthesizer は接続をまたいで共有される(必要なら実装側で直列化する)。
    """

    probe_factory: Callable[[], Callable[[bytes], float]]
    transcriber: Callable[[bytes], str]
    synthesizer: Callable[[str], bytes]
    chunk_bytes: int = 1024
    segmenter_config: SegmenterConfig = field(default_factory=SegmenterConfig)


# TTS 音声(WAV)を送るときの 1 フレームの大きさ
_AUDIO_FRAME_BYTES = 32768

# 上り(マイク PCM)フレームの上限。クライアントは 2KB(1024 サンプル)ずつ送るので
# 十分大きく、巨大フレームで VAD が受信ループを長時間止めるのを防ぐ
_MAX_INPUT_FRAME_BYTES = 65536

# 未処理の確定区間を溜める上限。STT が追いつかないときは新しい区間を捨て、
# メモリを際限なく消費しないようにする
_SEGMENT_QUEUE_SIZE = 8


class _Connection:
    """1 接続分の状態。送信は複数タスク(受信ループとワーカ)から行うためロックで直列化する。"""

    def __init__(self, ws: WebSocket, engines: Engines | None) -> None:
        self.ws = ws
        self.engines = engines
        self.segments: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_SEGMENT_QUEUE_SIZE)
        self.segmenter: Segmenter | None = None
        self._send_lock = asyncio.Lock()
        if engines is not None:
            self.segmenter = Segmenter(
                engines.probe_factory(),
                chunk_bytes=engines.chunk_bytes,
                config=engines.segmenter_config,
            )

    async def send_json(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_json(payload)

    async def send_bytes(self, data: bytes) -> None:
        async with self._send_lock:
            await self.ws.send_bytes(data)


async def _handle_control(conn: _Connection, text: str) -> None:
    """JSON テキストフレーム(制御)に応答する。"""
    try:
        frame: Any = json.loads(text)
    except json.JSONDecodeError:
        await conn.send_json({"type": "error", "message": "invalid json"})
        return
    # "123" のような object でない有効な JSON でも接続を落とさず error を返す
    if not isinstance(frame, dict):
        await conn.send_json({"type": "error", "message": "invalid frame"})
        return
    kind = frame.get("type")
    if kind == "ping":
        await conn.send_json({"type": "pong"})
    elif kind == "start":
        rate = frame.get("sample_rate", SAMPLE_RATE)
        if rate != SAMPLE_RATE:
            await conn.send_json(
                {"type": "error", "message": f"unsupported sample_rate: {rate}"}
            )
            return
        if conn.segmenter is not None:
            conn.segmenter.reset()
        await conn.send_json({"type": "started", "sample_rate": SAMPLE_RATE})
    elif kind == "stop":
        if conn.segmenter is not None:
            conn.segmenter.reset()
        # マイク停止後に古い区間の返答が届かないよう、未処理の区間も捨てる
        while not conn.segments.empty():
            conn.segments.get_nowait()
        await conn.send_json({"type": "stopped"})
    else:
        await conn.send_json({"type": "error", "message": f"unknown type: {kind}"})


async def _segment_worker(conn: _Connection) -> None:
    """確定した発話区間を STT → TTS して返送する。受信ループとは独立に動く。

    送信の失敗(切断との競合)はここで捕まえてワーカを終える。放置すると
    「回収されない例外を持つタスク」になり、以後の区間がキューに溜まるだけになる。
    """
    while True:
        pcm = await conn.segments.get()
        try:
            await _reply_segment(conn, pcm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 送信失敗は切断とみなす
            _log(f"voice: 返送に失敗したためワーカを止める: {exc}")
            return


async def _reply_segment(conn: _Connection, pcm: bytes) -> None:
    """1 区間を STT → TTS して返送する。STT・TTS の失敗はログだけ残して区間を捨てる。"""
    engines = conn.engines
    assert engines is not None
    started = time.monotonic()
    try:
        text = (await asyncio.to_thread(engines.transcriber, pcm)).strip()
    except Exception as exc:  # noqa: BLE001 - 認識失敗で接続は落とさない
        _log(f"voice: STT に失敗した: {exc}")
        return
    stt_ms = int((time.monotonic() - started) * 1000)
    if not text:
        return
    await conn.send_json({"type": "stt_final", "text": text})
    tts_started = time.monotonic()
    try:
        wav = await asyncio.to_thread(engines.synthesizer, text)
    except Exception as exc:  # noqa: BLE001 - 合成失敗でも字幕(stt_final)は届いている
        _log(f"voice: TTS に失敗した: {exc}")
        return
    tts_ms = int((time.monotonic() - tts_started) * 1000)
    await conn.send_json({"type": "audio_start"})
    for i in range(0, len(wav), _AUDIO_FRAME_BYTES):
        await conn.send_bytes(wav[i : i + _AUDIO_FRAME_BYTES])
    await conn.send_json({"type": "audio_end"})
    # 完了条件のレイテンシ計測(voice-plan.md)。区間長は 16kHz/16bit なので 32 バイト = 1ms
    _log(f"voice: 区間 {len(pcm) // 32}ms を認識 {stt_ms}ms 合成 {tts_ms}ms 「{text}」")


@router.get(PAGE_PATH, include_in_schema=False)
async def voice_page() -> FileResponse:
    """通話画面(素の HTML/JS。テンプレートを使わない単独ページ)。"""
    return FileResponse(Path(__file__).parent / "static" / "voice.html")


@router.websocket(WS_PATH)
async def voice_ws(ws: WebSocket) -> None:
    """通話クライアントとの WebSocket 接続。

    テキストとバイナリの両方を受けるため、receive_text ではなく receive で
    生のメッセージを読む。Engines は serve が app.state.voice_engines に置く。
    未設定(voice extra 未導入など)なら制御フレームだけに応答し、音声は捨てる。
    """
    await ws.accept()
    engines: Engines | None = getattr(ws.app.state, "voice_engines", None)
    conn = _Connection(ws, engines)
    worker = asyncio.create_task(_segment_worker(conn)) if engines is not None else None
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is not None:
                await _handle_control(conn, text)
                continue
            data = message.get("bytes")
            if data and conn.segmenter is not None:
                if len(data) > _MAX_INPUT_FRAME_BYTES:
                    _log(f"voice: {len(data)} バイトの上りフレームを無視した")
                    continue
                for event in conn.segmenter.feed(data):
                    if isinstance(event, SpeechStart):
                        # 発話開始でクライアントの再生キューを破棄させる(barge-in)
                        await conn.send_json({"type": "interrupt"})
                    elif conn.segments.full():
                        _log("voice: STT が追いつかず確定区間を捨てた")
                    else:
                        conn.segments.put_nowait(event.audio)
    except WebSocketDisconnect:
        pass
    finally:
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
