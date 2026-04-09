// AudioWorklet processor: captures raw PCM and posts Int16 chunks to main thread
class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = new Float32Array(480); // 30ms at 16kHz
    this._pos = 0;
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0];
    if (!input || !input[0]) return true;

    const samples = input[0]; // mono channel

    for (let i = 0; i < samples.length; i++) {
      this._buffer[this._pos++] = samples[i];

      if (this._pos >= 480) {
        // Convert Float32 → Int16
        const int16 = new Int16Array(480);
        for (let j = 0; j < 480; j++) {
          const s = Math.max(-1, Math.min(1, this._buffer[j]));
          int16[j] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }
        this.port.postMessage(int16.buffer, [int16.buffer]);
        this._buffer = new Float32Array(480);
        this._pos = 0;
      }
    }

    return true;
  }
}

registerProcessor('pcm-processor', PCMProcessor);
