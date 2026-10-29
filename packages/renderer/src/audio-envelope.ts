/** Basic mouth movement for browsers without secure audio processing support. */
export class AudioEnvelope {
  private context?: AudioContext;
  private source?: MediaStreamAudioSourceNode;
  private analyser?: AnalyserNode;
  private mute?: GainNode;
  private samples = new Float32Array(512);
  private level = 0;

  async play(stream: MediaStream) {
    await this.reset();
    if (!stream.getAudioTracks().length) return;
    const context = this.context = new AudioContext();
    this.source = context.createMediaStreamSource(stream);
    this.analyser = context.createAnalyser();
    this.analyser.fftSize = this.samples.length;
    this.mute = context.createGain();
    this.mute.gain.value = 0;
    // Keep the analysis graph active; audible playback belongs to the room.
    this.source.connect(this.analyser);
    this.analyser.connect(this.mute);
    this.mute.connect(context.destination);
    await context.resume();
  }

  readLevel() {
    if (!this.analyser) return 0;
    this.analyser.getFloatTimeDomainData(this.samples);
    const energy = this.samples.reduce((sum, value) => sum + value * value, 0) / this.samples.length;
    const target = Math.min(1, Math.max(0, Math.sqrt(energy) - 0.008) * 8);
    this.level += (target - this.level) * (target > this.level ? 0.65 : 0.25);
    return this.level;
  }

  async reset() {
    this.source?.disconnect();
    this.analyser?.disconnect();
    this.mute?.disconnect();
    const context = this.context;
    this.source = this.analyser = this.mute = this.context = undefined;
    this.level = 0;
    if (context && context.state !== 'closed') await context.close();
  }
}
