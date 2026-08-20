// alir voice 通話画面。
// マイク → AudioWorklet(16kHz/16bit PCM) → WS 送信、受信 WAV → 再生キュー、の 2 経路を持つ。
// サーバから interrupt が来たら再生を破棄する(barge-in)。VAD・STT・TTS はすべてサーバ側。
"use strict";

const SAMPLE_RATE = 16000;

const state = {
  calling: false,
  ws: null,
  reconnectTimer: null,
  audioCtx: null,      // マイク取り込み用(16kHz 固定)
  playCtx: null,       // 再生用(端末ネイティブレート。VOICEVOX の 24kHz WAV を decode に任せる)
  stream: null,
  wakeLock: null,
  receivingAudio: false,
  discarding: false,
  audioChunks: [],
  playQueue: [],
  currentSource: null,
};

const statusEl = document.getElementById("status");
const callEl = document.getElementById("call");
const transcriptEl = document.getElementById("transcript");

function setStatus(text, active) {
  statusEl.textContent = text;
  statusEl.classList.toggle("active", Boolean(active));
}

function log(text, isError) {
  const line = document.createElement("div");
  line.textContent = text;
  if (isError) line.classList.add("error");
  transcriptEl.appendChild(line);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

async function acquireWakeLock() {
  if (!("wakeLock" in navigator)) return;
  try {
    state.wakeLock = await navigator.wakeLock.request("screen");
  } catch (e) {
    // 電池残量などで拒否されうる。通話自体は続ける
  }
}

async function openMicrophone() {
  state.audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
  await state.audioCtx.audioWorklet.addModule("/static/voice-worklet.js");
  state.stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,   // TTS 再生をマイクが拾うのを抑える
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
  });
  const source = state.audioCtx.createMediaStreamSource(state.stream);
  const node = new AudioWorkletNode(state.audioCtx, "pcm-capture");
  node.port.onmessage = (e) => {
    if (state.calling && state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(e.data);
    }
  };
  // 出力につながないと process が走らない環境があるため、無音で destination へ通す
  const mute = state.audioCtx.createGain();
  mute.gain.value = 0;
  source.connect(node);
  node.connect(mute);
  mute.connect(state.audioCtx.destination);
}

function connect() {
  // 再接続タイマーと visibilitychange の両方から呼ばれるため、二重コネクトを防ぐ
  if (state.ws && state.ws.readyState !== WebSocket.CLOSED) return;
  const scheme = location.protocol === "https:" ? "wss://" : "ws://";
  const ws = new WebSocket(scheme + location.host + "/voice/ws");
  ws.binaryType = "arraybuffer";
  state.ws = ws;
  ws.onopen = () => {
    setStatus("通話中", true);
    ws.send(JSON.stringify({ type: "start", sample_rate: SAMPLE_RATE }));
  };
  ws.onmessage = onMessage;
  ws.onclose = () => {
    resetReceiveState();
    if (state.calling) {
      setStatus("再接続中…");
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = setTimeout(() => {
        if (state.calling) connect();
      }, 2000);
    }
  };
  ws.onerror = () => ws.close();
}

function resetReceiveState() {
  // audio_start〜audio_end の途中で切れたときにチャンクを持ち越さない
  state.receivingAudio = false;
  state.discarding = false;
  state.audioChunks = [];
}

function onMessage(e) {
  if (e.data instanceof ArrayBuffer) {
    if (state.receivingAudio && !state.discarding) {
      state.audioChunks.push(e.data);
    }
    return;
  }
  let frame;
  try {
    frame = JSON.parse(e.data);
  } catch {
    return;
  }
  switch (frame.type) {
    case "stt_final":
      log("🎤 " + frame.text);
      break;
    case "event":
      // driver イベントの通知。読み上げ音声(speak 時)は後続の audio_start で届く。
      // 発話への応答と区別できるよう、先にチャイムを鳴らす
      playChime();
      log("🔔 " + frame.text);
      break;
    case "interrupt":
      // 発話が始まった。再生中・再生待ちの音声を捨て、受信中のストリームも破棄する
      stopPlayback();
      if (state.receivingAudio) state.discarding = true;
      break;
    case "audio_start":
      state.receivingAudio = true;
      state.discarding = false;
      state.audioChunks = [];
      break;
    case "audio_end":
      finishAudio();
      break;
    case "error":
      log("⚠ " + frame.message, true);
      break;
  }
}

async function finishAudio() {
  const chunks = state.audioChunks;
  const discarded = state.discarding;
  state.audioChunks = [];
  state.receivingAudio = false;
  state.discarding = false;
  if (discarded || chunks.length === 0) return;
  if (!state.playCtx) state.playCtx = new AudioContext();
  if (state.playCtx.state === "suspended") await state.playCtx.resume();
  try {
    const buf = await new Blob(chunks).arrayBuffer();
    const audioBuf = await state.playCtx.decodeAudioData(buf);
    state.playQueue.push(audioBuf);
    playNext();
  } catch (err) {
    log("⚠ 音声の再生に失敗: " + err, true);
  }
}

function playNext() {
  if (state.currentSource || state.playQueue.length === 0) return;
  const buf = state.playQueue.shift();
  const source = state.playCtx.createBufferSource();
  source.buffer = buf;
  source.connect(state.playCtx.destination);
  source.onended = () => {
    state.currentSource = null;
    playNext();
  };
  state.currentSource = source;
  source.start();
}

async function playChime() {
  // 音源ファイルなしで済むよう、WebAudio の 2 音(上昇)で短いチャイムを作る
  if (!state.playCtx) state.playCtx = new AudioContext();
  if (state.playCtx.state === "suspended") await state.playCtx.resume();
  const ctx = state.playCtx;
  const t0 = ctx.currentTime;
  [660, 880].forEach((freq, i) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = freq;
    const start = t0 + i * 0.16;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.25, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.15);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(start);
    osc.stop(start + 0.16);
  });
}

function stopPlayback() {
  state.playQueue = [];
  if (state.currentSource) {
    try {
      state.currentSource.stop();
    } catch {
      // 既に停止済みなら無視する
    }
    state.currentSource = null;
  }
}

function releaseMicrophone() {
  if (state.stream) {
    state.stream.getTracks().forEach((t) => t.stop());
    state.stream = null;
  }
  if (state.audioCtx) {
    state.audioCtx.close();
    state.audioCtx = null;
  }
}

async function startCall() {
  callEl.disabled = true;
  try {
    await openMicrophone();
  } catch (err) {
    // 許可拒否などで途中失敗しても AudioContext を増やしたまま残さない
    releaseMicrophone();
    log("⚠ マイクを開けない: " + err, true);
    callEl.disabled = false;
    return;
  }
  state.calling = true;
  await acquireWakeLock();
  connect();
  callEl.textContent = "通話終了";
  callEl.classList.add("calling");
  callEl.disabled = false;
}

function stopCall() {
  state.calling = false;
  clearTimeout(state.reconnectTimer);
  state.reconnectTimer = null;
  if (state.ws) {
    if (state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ type: "stop" }));
    }
    // CONNECTING 中でも close は合法。閉じ忘れると後から open して start を送ってしまう
    state.ws.close();
    state.ws = null;
  }
  stopPlayback();
  resetReceiveState();
  releaseMicrophone();
  if (state.wakeLock) {
    state.wakeLock.release();
    state.wakeLock = null;
  }
  setStatus("未接続");
  callEl.textContent = "通話開始";
  callEl.classList.remove("calling");
}

callEl.addEventListener("click", () => {
  if (state.calling) {
    stopCall();
  } else {
    startCall();
  }
});

// 画面復帰時に wake lock と接続を取り直す(バックグラウンドで切れるため)
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || !state.calling) return;
  acquireWakeLock();
  if (state.audioCtx && state.audioCtx.state === "suspended") state.audioCtx.resume();
  connect(); // 既に接続があれば connect 側の二重コネクト防止で何もしない
});
