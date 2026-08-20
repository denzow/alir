"""voice の実エンジン: silero-VAD(pysilero-vad)・faster-whisper・VOICEVOX。

音声系の依存は重いので voice extra(uv sync --extra voice)に隔離してあり、
未導入でも alir 本体は動く。build_engines は導入状況を確認し、揃っていなければ
None を返して voice を無効化する(WS は制御フレームにだけ応答する)。
whisper のモデルはロードに時間がかかるため、最初の認識まで遅延させる。
VOICEVOX は別プロセスの HTTP API なので、ここではクライアントだけを持つ。
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from alir import db, settings, voice
from alir.driver import log_with_timestamp as _log


class FasterWhisperTranscriber:
    """faster-whisper による STT。16kHz/16bit/mono PCM を受けてテキストを返す。

    モデルは最初の呼び出しでロードする(small で数百 MB のダウンロードが走りうる)。
    モデルもロックも接続をまたいで共有し、認識は直列化する(CPU 実行前提)。
    """

    def __init__(self, model_name: str, beam_size: int) -> None:
        self._model_name = model_name
        self._beam_size = beam_size
        self._model: Any = None
        self._lock = threading.Lock()
        # faster-whisper 内部の INFO ログ(Processing audio with duration など)は
        # alir のログ形式と揃わないまま stdout に漏れるため抑制する
        logging.getLogger("faster_whisper").setLevel(logging.WARNING)

    def __call__(self, pcm: bytes) -> str:
        import numpy as np

        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                _log(f"voice: whisper モデル {self._model_name} をロード中")
                self._model = WhisperModel(self._model_name, device="cpu", compute_type="int8")
                _log("voice: whisper モデルのロード完了")
            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _info = self._model.transcribe(
                audio, language="ja", beam_size=self._beam_size
            )
            return "".join(segment.text for segment in segments)


class VoicevoxSynthesizer:
    """VOICEVOX engine(ローカル HTTP)による TTS。テキストを受けて WAV を返す。

    audio_query で合成パラメータを作り、synthesis で波形にする 2 段構成は
    VOICEVOX API の仕様。engine が落ちていれば例外になり、呼び出し側
    (voice._segment_worker)がログを残して次の区間へ進む。
    """

    def __init__(self, base_url: str, speaker: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._speaker = speaker

    def __call__(self, text: str) -> bytes:
        params = urllib.parse.urlencode({"text": text, "speaker": self._speaker})
        query_req = urllib.request.Request(
            f"{self._base_url}/audio_query?{params}", method="POST"
        )
        with urllib.request.urlopen(query_req, timeout=10) as res:
            query = res.read()
        synthesis_req = urllib.request.Request(
            f"{self._base_url}/synthesis?speaker={self._speaker}",
            data=query,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(synthesis_req, timeout=30) as res:
            return bytes(res.read())


def voicevox_version(base_url: str) -> str | None:
    """VOICEVOX engine の疎通確認。到達できなければ None。"""
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/version", timeout=3) as res:
            return str(json.loads(res.read()))
    except Exception:  # noqa: BLE001 - 疎通確認なので理由は問わない
        return None


def voice_available() -> tuple[bool, str]:
    """voice extra の導入状況を返す。(導入済みか, 状態の説明)。"""
    try:
        import faster_whisper  # noqa: F401
        import pysilero_vad  # noqa: F401
    except ImportError as exc:
        return False, f"voice extra が未導入({exc.name})。uv sync --extra voice で導入する"
    return True, "installed"


def build_engines(dbdir: Path) -> voice.Engines | None:
    """設定を読んで Engines を組み立てる。voice extra が未導入なら None。

    serve の起動時に呼ばれ、設定の変更は次回起動から効く。
    モデルのロードは遅延するので、この関数自体は軽い。
    """
    available, detail = voice_available()
    if not available:
        _log(f"voice: 音声処理を無効化({detail})")
        return None
    from pysilero_vad import SileroVoiceActivityDetector

    conn = db.connect(dbdir)
    transcriber = FasterWhisperTranscriber(
        settings.voice_whisper_model(conn), settings.voice_beam_size(conn)
    )
    synthesizer = VoicevoxSynthesizer(
        settings.voicevox_url(conn), settings.voice_speaker(conn)
    )
    return voice.Engines(
        probe_factory=lambda: SileroVoiceActivityDetector(),
        transcriber=transcriber,
        synthesizer=synthesizer,
        chunk_bytes=SileroVoiceActivityDetector.chunk_bytes(),
    )
