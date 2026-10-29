import type { Application } from 'pixi.js';
import type { Live2DModel } from 'pixi-live2d-display/cubism4';
import type { MotionSync } from 'live2d-motionsync/stream';
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
  private observer?: ResizeObserver;
  private disposed = false;
  private audio: MediaStream | null = null;
  private audioTask = Promise.resolve();
  private initTask?: Promise<this>;
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
    if (this.options.motionSyncUrl) {
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
      if (stream && stream === this.audio && !this.disposed) await this.sync?.play(stream);
    }).catch(e => this.options.onError?.(e instanceof Error ? e : new Error(String(e))));
  }
  async execute(command: { type: 'expression' | 'motion'; name: string; index?: number }) {
    if (!this.model || this.disposed) return;
    if (command.type === 'expression') await this.model.expression(command.name);
    else await this.model.motion(command.name, command.index);
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.observer?.disconnect();
    const sync = this.sync;
    const app = this.app;
    app?.stop();
    void Promise.resolve(this.initTask).catch(() => {}).then(() => this.audioTask).finally(async () => {
      await sync?.reset();
      app?.destroy(false, { children: true, texture: true, baseTexture: true });
    }).catch(() => {});
    this.sync = undefined;
    this.audio = null;
    this.app = undefined;
    this.model = undefined;
  }
}
