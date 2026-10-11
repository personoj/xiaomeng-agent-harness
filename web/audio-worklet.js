class MicrophonePCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.sum = 0; this.count = 0; this.phase = 0;
    this.frame = new Int16Array(320); this.offset = 0;
  }
  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!samples) return true;
    for (const sample of samples) {
      this.sum += sample; this.count++; this.phase += 16000;
      if (this.phase >= sampleRate) {
        this.phase -= sampleRate;
        const value = Math.max(-1, Math.min(1, this.sum / this.count));
        this.frame[this.offset++] = value < 0 ? value * 32768 : value * 32767;
        this.sum = 0; this.count = 0;
        if (this.offset === this.frame.length) {
          this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
          this.frame = new Int16Array(320); this.offset = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('microphone-pcm', MicrophonePCM);
