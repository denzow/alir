"""voice の実エンジン層(VOICEVOX クライアントと Engines の組み立て)のテスト。

VOICEVOX は HTTP をフェイクして呼び出しの形を確かめる。
whisper と silero はモデルのロードが重いので、ここでは触らない。
"""

from __future__ import annotations

import contextlib
import io
from typing import Any

import pytest

from alir import voice_engines


def test_voicevox_synthesizer_posts_query_then_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    def fake_urlopen(req: Any, data: Any = None, timeout: float | None = None) -> Any:
        calls.append(req)
        body = b'{"query": 1}' if len(calls) == 1 else b"wav-bytes"
        return contextlib.closing(io.BytesIO(body))

    monkeypatch.setattr(voice_engines.urllib.request, "urlopen", fake_urlopen)
    synth = voice_engines.VoicevoxSynthesizer("http://voicevox:50021/", speaker=3)
    wav = synth("こんにちは")
    assert wav == b"wav-bytes"
    assert len(calls) == 2
    assert calls[0].full_url.startswith("http://voicevox:50021/audio_query?")
    assert "speaker=3" in calls[0].full_url
    assert calls[1].full_url == "http://voicevox:50021/synthesis?speaker=3"
    # audio_query の結果をそのまま synthesis のボディに渡す
    assert calls[1].data == b'{"query": 1}'


def test_voicevox_version_unreachable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(voice_engines.urllib.request, "urlopen", fail_urlopen)
    assert voice_engines.voicevox_version("http://127.0.0.1:50021") is None


def test_voicevox_version_returns_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(url: Any, timeout: float | None = None) -> Any:
        return contextlib.closing(io.BytesIO(b'"0.20.0"'))

    monkeypatch.setattr(voice_engines.urllib.request, "urlopen", fake_urlopen)
    assert voice_engines.voicevox_version("http://127.0.0.1:50021") == "0.20.0"


def test_voice_available_reports_state() -> None:
    available, detail = voice_engines.voice_available()
    # この開発環境では extra が入っていても、いなくても、説明つきで判定が返る
    assert isinstance(available, bool)
    assert detail
