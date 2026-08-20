// マイク入力を 16bit PCM に変換してメインスレッドへ渡す AudioWorklet。
// AudioContext を 16kHz で作るため、ここに来るサンプルは既に 16kHz になっている。
// 128 サンプルずつ来る入力を 1024 サンプル(64ms)にまとめて送り、メッセージ数を抑える。
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = new Int16Array(1024);
    this._filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) {
      return true;
    }
    for (let i = 0; i < channel.length; i++) {
      const sample = Math.max(-1, Math.min(1, channel[i]));
      this._buffer[this._filled++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      if (this._filled === this._buffer.length) {
        const out = this._buffer.slice(0);
        this.port.postMessage(out.buffer, [out.buffer]);
        this._filled = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);
