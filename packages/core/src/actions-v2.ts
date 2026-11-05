import type { AvatarCommand } from './protocol.js';

export type ActionEnvelope = { version: 2; command_id: string; connection_nonce: string; session_id: string; generation_id: string; owner_epoch: number; generation_sequence: number; sequence: number; expires_at: string; operation: 'execute' | 'cancel'; action: { type: 'motion'; name: string; index: number } | null };
export type ExecutionState = 'accepted' | 'started' | 'completed' | 'cancelled' | 'failed' | 'unknown';
export type ActionReceipt = { version: 1; command_id: string; session_id: string; generation_id: string; client_sequence: number; state: ExecutionState; observed_at: string };
export interface TrackedRenderer { executeTracked(command: AvatarCommand, signal: AbortSignal, observe: (state: ExecutionState) => void): Promise<void> }
export type SessionBootstrap = { version: 1; session_id: string; nonce: string; owner_epoch: number; generation_sequence: number; sequence_floor: number; expires_at: string };
const identifier = (v: unknown): v is string => typeof v === 'string' && v.length > 0 && v.length <= 128;
export function parseSessionBootstrap(value: unknown): SessionBootstrap | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return;
  const v = value as Record<string, unknown>;
  if (Object.keys(v).some(key => !['version','session_id','nonce','owner_epoch','generation_sequence','sequence_floor','expires_at'].includes(key)) ||
      v.version !== 1 || !identifier(v.session_id) || !identifier(v.nonce) ||
      !Number.isSafeInteger(v.owner_epoch) || Number(v.owner_epoch) < 1 ||
      !Number.isSafeInteger(v.generation_sequence) || Number(v.generation_sequence) < 0 ||
      !Number.isSafeInteger(v.sequence_floor) || Number(v.sequence_floor) < 0 ||
      typeof v.expires_at !== 'string' || !Number.isFinite(Date.parse(v.expires_at)) || Date.parse(v.expires_at) <= Date.now()) return;
  return v as unknown as SessionBootstrap;
}
export function parseActionEnvelope(value: unknown): ActionEnvelope | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return;
  const v = value as Record<string, unknown>;
  if (Object.keys(v).some(k => !['version','command_id','connection_nonce','session_id','generation_id','owner_epoch','generation_sequence','sequence','expires_at','operation','action'].includes(k)) ||
      v.version !== 2 || !identifier(v.command_id) || !identifier(v.connection_nonce) || !identifier(v.session_id) || !identifier(v.generation_id) ||
      !Number.isSafeInteger(v.owner_epoch) || Number(v.owner_epoch) < 1 || !Number.isSafeInteger(v.generation_sequence) || Number(v.generation_sequence) < 1 ||
      !Number.isSafeInteger(v.sequence) || Number(v.sequence) < 0 || typeof v.expires_at !== 'string' || !Number.isFinite(Date.parse(v.expires_at))) return;
  if (v.operation === 'cancel') { if (v.action !== null) return; }
  else if (v.operation === 'execute') {
    if (!v.action || typeof v.action !== 'object' || Array.isArray(v.action)) return;
    const a = v.action as Record<string, unknown>;
    if (Object.keys(a).some(k => !['type','name','index'].includes(k)) || a.type !== 'motion' || typeof a.name !== 'string' || a.name.length > 128 ||
        !Number.isSafeInteger(a.index) || Number(a.index) < 0) return;
  } else return;
  return v as unknown as ActionEnvelope;
}

/** One active renderer operation and a monotonic cancellation fence per room session. */
export class ActionController {
  private owner = 0;
  private generation = 0;
  private cancelled = false;
  private sequence = -1;
  private active?: AbortController;
  private expiry?: ReturnType<typeof setTimeout>;
  private seen = new Set<string>();
  private receiptSequence = 0;
  private sequenceFloor = -1;
  private pending: ActionEnvelope[] = [];
  private disposed = false;
  constructor(private sessionId: string, private renderer: TrackedRenderer, private receipt: (value: ActionReceipt) => void) {}
  bootstrap(owner: number, generation: number, sequenceFloor = -1) {
    if (this.disposed) return;
    if (!Number.isSafeInteger(owner) || owner < 1 || !Number.isSafeInteger(generation) || generation < 0 || !Number.isSafeInteger(sequenceFloor) || sequenceFloor < -1) return;
    if (owner < this.owner || (owner === this.owner && generation < this.generation)) return;
    if (owner > this.owner || (owner === this.owner && generation > this.generation)) {
      this.active?.abort(); clearTimeout(this.expiry);
      if (owner > this.owner) this.sequenceFloor = -1;
      this.owner = owner; this.generation = generation; this.cancelled = false; this.sequence = -1;
    }
    this.sequenceFloor = Math.max(this.sequenceFloor, sequenceFloor);
    const waiting = this.pending; this.pending = [];
    for (const command of waiting.filter(value => value.operation === 'cancel')) this.accept(command);
    for (const command of waiting.filter(value => value.operation !== 'cancel')) this.accept(command);
  }
  accept(command: ActionEnvelope) {
    if (this.disposed || command.session_id !== this.sessionId || Date.parse(command.expires_at) <= Date.now()) return;
    if (!this.owner || command.owner_epoch > this.owner) {
      if (this.pending.length < 16 && !this.pending.some(value => value.command_id === command.command_id)) this.pending.push(command);
      return;
    }
    if (
        command.owner_epoch < this.owner || (command.owner_epoch === this.owner && command.generation_sequence < this.generation)) return;
    this.bootstrap(command.owner_epoch, command.generation_sequence);
    if (command.operation === 'cancel') { this.cancelled = true; this.active?.abort(); clearTimeout(this.expiry); return; }
    if (this.cancelled || command.sequence <= this.sequenceFloor || command.sequence <= this.sequence || this.seen.has(command.command_id) || !command.action) return;
    this.sequence = command.sequence;
    this.seen.add(command.command_id); if (this.seen.size > 256) this.seen.delete(this.seen.values().next().value!);
    this.active?.abort(); clearTimeout(this.expiry);
    const controller = this.active = new AbortController();
    let terminal = false;
    const observe = (state: ExecutionState) => {
      if (terminal) return;
      if (controller.signal.aborted && state !== 'cancelled') return;
      if (['completed','failed','unknown','cancelled'].includes(state)) terminal = true;
      this.receipt({ version: 1, command_id: command.command_id, session_id: command.session_id, generation_id: command.generation_id,
        client_sequence: ++this.receiptSequence, state, observed_at: new Date().toISOString() });
    };
    controller.signal.addEventListener('abort', () => observe('cancelled'), {once:true});
    this.expiry = setTimeout(() => controller.abort(), Math.min(30000, Math.max(1, Date.parse(command.expires_at) - Date.now())));
    observe('accepted');
    const action = { version: 1 as const, id: command.command_id, ...command.action };
    void this.renderer.executeTracked(action, controller.signal, observe).then(() => {
      if (!terminal) observe(controller.signal.aborted ? 'cancelled' : 'unknown');
    }).catch(() => observe('failed')).finally(() => {
      if (this.active === controller) { clearTimeout(this.expiry); this.active = undefined; }
    });
  }
  dispose() { this.disposed = true; this.pending = []; this.active?.abort(); clearTimeout(this.expiry); }
}
