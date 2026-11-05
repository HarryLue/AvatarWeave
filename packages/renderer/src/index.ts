import type { Application } from 'pixi.js';
import type { Live2DModel } from 'pixi-live2d-display/cubism4';
import type { MotionSync } from 'live2d-motionsync/stream';
import { AudioEnvelope } from './audio-envelope.js';
export interface RendererOptions {
  canvas: HTMLCanvasElement;
  coreUrl: string;
  modelUrl: string;
  motionSyncUrl?: string;
  draggable?: boolean;
  onTouch?: (areas: string[]) => void;
  onError?: (error: Error) => void;
}
const cores = new Map<string, Promise<void>>();
export function loadRendererCore(url: string): Promise<void> {
  if (typeof window === 'undefined') return Promise.reject(new Error('AvatarWeave renderer requires a browser'));
  if ((window as unknown as { Live2DCubismCore?: unknown }).Live2DCubismCore) return Promise.resolve();
  let promise = cores.get(url);
  if (!promise) {
    promise = new Promise<void>((resolve, reject) => {
      const script = document.createElement('script');
      const timer = window.setTimeout(() => { script.remove(); reject(new Error('Cubism Core load timeout')); }, 20000);
      script.src = url;
      script.onload = () => {
        clearTimeout(timer);
        if ((window as unknown as { Live2DCubismCore?: unknown }).Live2DCubismCore) resolve();
        else { script.remove(); reject(new Error('Missing Live2DCubismCore global')); }
      };
      script.onerror = () => { clearTimeout(timer); script.remove(); reject(new Error('Cannot load Cubism Core')); };
      document.head.appendChild(script);
    }).catch(e => { cores.delete(url); throw e; });
    cores.set(url, promise);
  }
  return promise;
}
/** No hardcoded DOM ids, global event bus or application-specific resources. */
export class AvatarRenderer {
  private app?: Application;
  private model?: Live2DModel;
  private sync?: MotionSync;
  private envelope?: AudioEnvelope;
  private observer?: ResizeObserver;
  private disposed = false;
  private audio: MediaStream | null = null;
  private audioTask = Promise.resolve();
  private initTask?: Promise<this>;
  private trackedController?: AbortController;
  private pendingTracked = 0;
  private trackedTail: Promise<void> = Promise.resolve();
  constructor(readonly options: RendererOptions) {}
  init(): Promise<this> {
    return this.initTask ??= this.initialize();
  }
  private async initialize() {
    await loadRendererCore(this.options.coreUrl);
    // Cubism must be loaded before evaluating the renderer module.
    const [PIXI, { Live2DModel }, { MotionSync }] = await Promise.all([
      import('pixi.js'), import('pixi-live2d-display/cubism4'), import('live2d-motionsync/stream'),
    ]);
    if (this.disposed) return this;
    Live2DModel.registerTicker(PIXI.Ticker);
    this.app = new PIXI.Application({ view: this.options.canvas, backgroundAlpha: 0, autoDensity: true, resolution: Math.min(devicePixelRatio || 1, 2) });
    const model = await Live2DModel.from(this.options.modelUrl);
    if (this.disposed) { model.destroy(); return this; }
    this.model = model;
    this.app.stage.addChild(model);
    const supportsAudioProcessing = typeof AudioContext !== 'undefined' && typeof AudioWorkletNode !== 'undefined' && 'audioWorklet' in AudioContext.prototype;
    if (this.options.motionSyncUrl && !supportsAudioProcessing) {
      const envelope = this.envelope = new AudioEnvelope();
      const manager = model.internalModel.motionManager as unknown as { lipSyncIds?: string[] };
      const parameters = model.internalModel.coreModel as { setParameterValueById(id: string, value: number): void };
      const mouthIds = manager.lipSyncIds?.length ? manager.lipSyncIds : ['ParamMouthOpenY'];
      model.internalModel.on('beforeModelUpdate', () => {
        const level = envelope.readLevel();
        for (const id of mouthIds) parameters.setParameterValueById(id, level);
      });
    } else if (this.options.motionSyncUrl) {
      this.sync = new MotionSync(model.internalModel);
      // Ignore delayed AudioWorklet callbacks after reset/disposal.
      const internals = this.sync as unknown as { _buffer: unknown; onMessage: (event: MessageEvent) => void };
      const onMessage = internals.onMessage.bind(internals);
      internals.onMessage = e => { if (!this.disposed && internals._buffer != null) onMessage(e); };
      await this.sync.loadMotionSyncFromUrl(this.options.motionSyncUrl);
      if (this.disposed) return this;
    }
    model.on('hit', (areas: string[]) => this.options.onTouch?.(areas));
    if (this.options.draggable) {
      let offset: { x: number; y: number } | undefined;
      model.interactive = true;
      model.on('pointerdown', (e: import('pixi.js').InteractionEvent) => { offset = { x: e.data.global.x - model.x, y: e.data.global.y - model.y }; });
      model.on('pointermove', (e: import('pixi.js').InteractionEvent) => { if (offset) model.position.set(e.data.global.x - offset.x, e.data.global.y - offset.y); });
      model.on('pointerup', () => { offset = undefined; });
      model.on('pointerupoutside', () => { offset = undefined; });
    }
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(this.options.canvas.parentElement ?? this.options.canvas);
    this.resize();
    const pendingAudio = this.audio;
    this.audio = null;
    if (pendingAudio) this.setAudio(pendingAudio);
    return this;
  }
  resize() {
    if (!this.app || !this.model || this.disposed) return;
    const box = this.options.canvas.parentElement?.getBoundingClientRect();
    const w = Math.max(1, box?.width ?? 600), h = Math.max(1, box?.height ?? 600);
    this.app.renderer.resize(w, h);
    const scale = Math.min(w / this.model.internalModel.width, h / this.model.internalModel.height) * .95;
    this.model.scale.set(scale);
    this.model.position.set((w - this.model.width) / 2, h - this.model.height);
  }
  setAudio(stream: MediaStream | null) {
    if (this.disposed) return;
    if (this.audio === stream) return;
    this.audio = stream;
    this.audioTask = this.audioTask.then(async () => {
      if (this.disposed) return;
      await this.sync?.reset();
      await this.envelope?.reset();
      if (stream && stream === this.audio && !this.disposed) {
        await this.sync?.play(stream);
        await this.envelope?.play(stream);
      }
    }).catch(e => this.options.onError?.(e instanceof Error ? e : new Error(String(e))));
  }
  async execute(command: { type: 'expression' | 'motion'; name: string; index?: number }) {
    if (!this.model || this.disposed) return;
    this.trackedController?.abort();
    if (command.type === 'expression') await this.model.expression(command.name);
    else await this.model.motion(command.name, command.index);
  }
  async executeTracked(command: { type: 'expression' | 'motion'; name: string; index?: number }, external: AbortSignal,
    observe: (state: 'accepted' | 'started' | 'completed' | 'cancelled' | 'failed' | 'unknown') => void): Promise<void> {
    if (this.pendingTracked >= 2 || this.disposed) { observe('failed'); return; }
    this.trackedController?.abort();
    const previous = this.trackedTail;
    let release!: () => void;
    this.trackedTail = new Promise<void>(resolve => { release = resolve; });
    this.pendingTracked++;
    try {
      await previous;
      if (external.aborted || this.disposed) { observe('cancelled'); return; }
      await this.performTracked(command, external, observe);
    } finally { this.pendingTracked--; release(); }
  }
  private async performTracked(command: { type: 'expression' | 'motion'; name: string; index?: number }, external: AbortSignal,
    observe: (state: 'accepted' | 'started' | 'completed' | 'cancelled' | 'failed' | 'unknown') => void): Promise<void> {
    const model = this.model;
    if (!model || this.disposed || command.type !== 'motion' || command.index === undefined) { observe('failed'); return; }
    const manager = model.internalModel.motionManager;
    const definition = manager.definitions[command.name]?.[command.index] as { Sound?: string; sound?: string } | undefined;
    if (!definition || definition.Sound || definition.sound) { observe('failed'); return; }
    this.trackedController?.abort();
    const own = this.trackedController = new AbortController();
    const forward = () => own.abort();
    external.addEventListener('abort', forward, {once:true});
    if (external.aborted) own.abort();
    let restoreLoop: (() => void) | undefined;
    let starting: Promise<boolean> | undefined;
    try {
      const motion = await manager.loadMotion(command.name, command.index);
      if (own.signal.aborted || this.disposed) { observe('cancelled'); return; }
      if (!motion) { observe('failed'); return; }
      const finite = motion as unknown as { isLoop(): boolean; setIsLoop(loop: boolean): void; getLoopDuration(): number };
      if (typeof finite.isLoop !== 'function' || typeof finite.setIsLoop !== 'function' || typeof finite.getLoopDuration !== 'function' ||
          !Number.isFinite(finite.getLoopDuration()) || finite.getLoopDuration() <= 0 || finite.getLoopDuration() > 30) { observe('failed'); return; }
      const originalLoop = finite.isLoop(); finite.setIsLoop(false);
      restoreLoop = () => finite.setIsLoop(originalLoop);
      await new Promise<void>((resolve, reject) => {
        let settled = false;
        const finish = (state: 'completed' | 'cancelled' | 'failed') => {
          if (settled) return;
          settled = true; manager.off('motionFinish', done); own.signal.removeEventListener('abort', cancel);
          observe(state); resolve();
        };
        const done = () => finish(own.signal.aborted ? 'cancelled' : 'completed');
        const cancel = () => { if (!manager.destroyed) manager.stopAllMotions(); finish('cancelled'); };
        own.signal.addEventListener('abort', cancel, {once:true});
        starting = model.motion(command.name, command.index);
        void starting.then(started => {
          if (own.signal.aborted) { if (!manager.destroyed) manager.stopAllMotions(); finish('cancelled'); return; }
          if (!started) { finish('failed'); return; }
          manager.once('motionFinish', done); observe('started');
        }).catch(error => { own.signal.removeEventListener('abort', cancel); reject(error); });
      });
    } finally {
      if (starting) await starting.catch(() => {});
      restoreLoop?.(); external.removeEventListener('abort', forward);
      if (this.trackedController === own) this.trackedController = undefined;
    }
  }
  dispose() {
    if (this.disposed) return;
    this.trackedController?.abort();
    this.disposed = true;
    this.observer?.disconnect();
    const sync = this.sync;
    const envelope = this.envelope;
    const app = this.app;
    app?.stop();
    void Promise.resolve(this.initTask).catch(() => {}).then(() => this.audioTask).finally(async () => {
      await sync?.reset();
      await envelope?.reset();
      app?.destroy(false, { children: true, texture: true, baseTexture: true });
    }).catch(() => {});
    this.sync = undefined;
    this.envelope = undefined;
    this.audio = null;
    this.app = undefined;
    this.model = undefined;
  }
}
