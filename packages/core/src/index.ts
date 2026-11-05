import { ActionController, parseActionEnvelope, parseSessionBootstrap } from './actions-v2.js';
export * from './actions-v2.js';
import { Room, RoomEvent, Track, ParticipantKind, type RemoteParticipant, type RemoteTrack, type RemoteAudioTrack, type RemoteTrackPublication } from 'livekit-client';
import { TOPICS, parseAvatarCommand, parseMemoryStatus, type AvatarCommand, type MemoryStatus } from './protocol.js';
export * from './protocol.js';
export interface Credentials { server_url: string; participant_token: string }
export interface Transcript { id: string; role: 'user' | 'agent'; text: string; final: boolean; participantIdentity: string }
export interface AvatarRenderer { setAudio(stream: MediaStream | null): void; execute(command: AvatarCommand): void | Promise<void>;
  executeTracked?: import('./actions-v2.js').TrackedRenderer['executeTracked'];
}
export type ConnectionMilestone = 'room_connected' | 'agent_ready' | 'first_audio_playback';
export interface ClientOptions {
  room?: Room;
  agentIdentity?: string;
  memorySessionId?: string;
  privateReadyTimeoutMs?: number;
  manageTranscription?: boolean;
  renderer?: AvatarRenderer;
  onTranscript?: (message: Transcript) => void;
  onState?: (state: string) => void;
  onCommand?: (command: AvatarCommand) => void;
  onMemoryStatus?: (status: MemoryStatus) => void;
  onError?: (error: Error) => void;
  onConnectionMilestone?: (stage: ConnectionMilestone, at: number) => void;
}
/** Attach to any existing Room, or let this client own connection and audio playback. */
export class AvatarClient {
  readonly room: Room;
  private options: ClientOptions;
  private actionsV2?: ActionController;
  private helloNonce?: string;
  private helloTargets = new Set<string>();
  private privateReady = false;
  private privateReadyError?: Error;
  private helloRetry?: ReturnType<typeof setInterval>;
  private privateHandshake?: { promise: Promise<void>; resolve: () => void; reject: (error: Error) => void; timeout: ReturnType<typeof setTimeout> };
  private audio = new Map<string, { element: HTMLMediaElement; track: RemoteTrack }>();
  private seenCommands = new Set<string>();
  private disposed = false;
  private transcriptRevision = 0;
  private transcriptSegments = new Map<string, { revision: number; text: string; final: boolean }>();
  private ownsRoom: boolean;
  private milestones = new Set<ConnectionMilestone>();
  private audioChecks = new Map<string, ReturnType<typeof setTimeout>>();
  constructor(options: ClientOptions = {}) {
    this.options = options;
    this.ownsRoom = !options.room;
    this.room = options.room ?? new Room({ adaptiveStream: true, dynacast: true });
    this.resetPrivateActions();
    this.room.on(RoomEvent.ParticipantConnected, this.participantJoined);
    this.room.on(RoomEvent.TrackSubscribed, this.subscribed);
    this.room.on(RoomEvent.TrackUnsubscribed, this.unsubscribed);
    this.room.on(RoomEvent.ParticipantAttributesChanged, this.attributes);
    this.room.on(RoomEvent.DataReceived, this.data);
    this.room.on(RoomEvent.Disconnected, this.disconnected);
    this.room.on(RoomEvent.Reconnecting, this.reconnecting);
    this.room.on(RoomEvent.Reconnected, this.reconnected);
    if (options.manageTranscription !== false) this.room.registerTextStreamHandler(TOPICS.transcription, async (reader, info) => {
      const participant = this.room.remoteParticipants.get(info.identity);
      const local = info.identity === this.room.localParticipant.identity;
      if (!local && (!participant || !this.isAgent(participant))) return;
      const id = reader.info.attributes?.['lk.segment_id'] ?? reader.info.id;
      const revision = ++this.transcriptRevision;
      const key = this.transcriptKey(info.identity, id);
      const previous = this.transcriptSegments.get(key);
      this.transcriptSegments.set(key, { revision, text: previous?.text ?? '', final: previous?.final ?? false });
      if (this.transcriptSegments.size > 512) this.transcriptSegments.delete(this.transcriptSegments.keys().next().value!);
      const message = { id, role: local ? 'user' as const : 'agent' as const, participantIdentity: info.identity };
      let text = '';
      try {
        for await (const chunk of reader) {
          if (this.disposed) return;
          text += chunk;
          this.emitStreamTranscript({ ...message, text, final: false }, revision);
        }
        // An ASR snapshot closes its own stream even while the utterance is still interim.
        // Read the attributes again: a delta stream may set the final flag in its trailer.
        if (!this.disposed) this.emitStreamTranscript({ ...message, text, final: reader.info.attributes?.['lk.transcription_final'] !== 'false' }, revision);
      } catch (e) { this.report(e); }
    });
    for (const p of this.room.remoteParticipants.values()) {
      if (this.isAgent(p)) this.agentState(p);
      for (const pub of p.trackPublications.values()) if (pub.track) this.subscribed(pub.track, pub, p);
    }
  }
  private resetPrivateActions() {
    this.stopPrivateHandshake(new Error('账号对话连接已重置，请重新连接。'));
    this.actionsV2?.dispose(); this.actionsV2 = undefined;
    this.privateReady = false; this.privateReadyError = undefined; this.helloTargets.clear();
    if (!this.options.memorySessionId) return;
    const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
    this.helloNonce = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    const renderer = this.options.renderer;
    if (renderer?.executeTracked) this.actionsV2 = new ActionController(this.options.memorySessionId, { executeTracked: (...args) => renderer.executeTracked!(...args) }, receipt => {
      if (this.room.state !== 'connected') return;
      void this.room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify(receipt)), { reliable: true, topic: 'avatarweave.receipt.v1' }).catch(e => this.report(e));
    });
  }
  private stopPrivateHandshake(error?: Error) {
    clearInterval(this.helloRetry); this.helloRetry = undefined;
    const pending = this.privateHandshake;
    this.privateHandshake = undefined;
    if (pending) { clearTimeout(pending.timeout); if (error) pending.reject(error); else pending.resolve(); }
  }
  private waitForPrivateReady(): Promise<void> {
    if (!this.options.memorySessionId || this.privateReady) return Promise.resolve();
    if (this.privateHandshake) return this.privateHandshake.promise;
    let resolve!: () => void, reject!: (error: Error) => void;
    const promise = new Promise<void>((done, failed) => { resolve = done; reject = failed; });
    const requested = this.options.privateReadyTimeoutMs;
    const delay = requested !== undefined && Number.isFinite(requested) ? Math.min(60000, Math.max(1, requested)) : 20000;
    const timeout = setTimeout(() => {
      this.privateReadyError = new Error('账号对话连接超时，助手暂未就绪。请重新开始账号对话。');
      this.stopPrivateHandshake(this.privateReadyError);
    }, delay);
    this.privateHandshake = { promise, resolve, reject, timeout };
    this.helloRetry = setInterval(() => {
      for (const participant of this.room.remoteParticipants.values()) if (this.isAgent(participant)) this.sendHello(participant, true);
    }, 1000);
    for (const participant of this.room.remoteParticipants.values()) if (this.isAgent(participant)) this.sendHello(participant);
    return promise;
  }
  private sendHello(participant: RemoteParticipant, retry = false) {
    if (!this.options.memorySessionId || !this.helloNonce || this.room.state !== 'connected' || (!retry && this.helloTargets.has(participant.sid))) return;
    this.helloTargets.add(participant.sid);
    const hello = { version: 1, session_id: this.options.memorySessionId, nonce: this.helloNonce };
    void this.room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify(hello)), { reliable: true, topic: 'avatarweave.hello.v1' }).catch(e => { this.helloTargets.delete(participant.sid); this.report(e); });
  }
  private reconnecting = () => {
    this.stopPrivateHandshake(new Error('账号对话连接已中断，正在重新连接。'));
    this.actionsV2?.dispose(); this.privateReady = false;
    if (this.options.memorySessionId) this.options.onState?.('connecting');
  };
  private reconnected = () => {
    this.resetPrivateActions();
    for (const participant of this.room.remoteParticipants.values()) if (this.isAgent(participant)) this.agentState(participant);
    if (this.options.memorySessionId) void this.waitForPrivateReady().catch(error => {
      this.report(error); void this.room.disconnect().catch(error => this.report(error));
    });
  };
  private transcriptKey(identity: string, id: string) { return JSON.stringify([identity, id]); }
  private emitStreamTranscript(message: Transcript, revision: number) {
    const state = this.transcriptSegments.get(this.transcriptKey(message.participantIdentity, message.id));
    if (!state || state.revision !== revision || (state.final && !message.final)) return;
    if (!message.text || (state.text === message.text && state.final === message.final)) return;
    state.text = message.text;
    state.final = message.final;
    this.options.onTranscript?.(message);
  }
  private report(e: unknown) { this.options.onError?.(e instanceof Error ? e : new Error(String(e))); }
  private isAgent(p: RemoteParticipant) { return this.options.agentIdentity ? p.identity === this.options.agentIdentity : p.kind === ParticipantKind.AGENT; }
  private markMilestone(stage: ConnectionMilestone) {
    if (this.disposed || this.milestones.has(stage)) return;
    this.milestones.add(stage);
    this.options.onConnectionMilestone?.(stage, performance.now());
  }
  private agentState(p: RemoteParticipant) {
    this.sendHello(p);
    if (this.options.memorySessionId && !this.privateReady) { this.options.onState?.('connecting'); return; }
    const state = p.attributes?.['lk.agent.state'];
    if (state && ['listening', 'thinking', 'speaking'].includes(state)) this.markMilestone('agent_ready');
    this.options.onState?.(state ?? 'connected');
  }
  private participantJoined = (p: RemoteParticipant) => { if (this.isAgent(p)) this.agentState(p); };
  private watchFirstAudio(track: RemoteAudioTrack, sid: string, element: HTMLMediaElement) {
    if (!this.options.onConnectionMilestone || this.milestones.has('first_audio_playback')) return;
    // A published audio track can play silence before the welcome message arrives.
    // Wait for decoded audio energy and an active media element (100 ms resolution).
    const check = async () => {
      try {
        const stats = await track.getReceiverStats();
        if (this.disposed || this.audio.get(sid)?.element !== element) return;
        if (!element.paused && element.readyState >= 2 && (stats?.totalAudioEnergy ?? 0) > 0) {
          this.markMilestone('first_audio_playback');
        }
      } catch { /* Diagnostics must not interrupt playback. */ }
      if (!this.disposed && this.audio.get(sid)?.element === element && !this.milestones.has('first_audio_playback')) {
        this.audioChecks.set(sid, setTimeout(check, 100));
      }
    };
    void check();
  }
  private subscribed = (track: RemoteTrack, pub: RemoteTrackPublication, p: RemoteParticipant) => {
    if (track.kind !== Track.Kind.Audio || !this.isAgent(p)) return;
    this.options.renderer?.setAudio(new MediaStream([track.mediaStreamTrack]));
    if (this.ownsRoom && !this.audio.has(pub.trackSid)) {
      const element = track.attach();
      element.hidden = true;
      document.body.appendChild(element);
      this.audio.set(pub.trackSid, { element, track });
      this.watchFirstAudio(track as RemoteAudioTrack, pub.trackSid, element);
      element.play().catch(e => this.report(e));
    }
  };
  private unsubscribed = (track: RemoteTrack, pub: RemoteTrackPublication, p: RemoteParticipant) => {
    if (track.kind !== Track.Kind.Audio || !this.isAgent(p)) return;
    const attached = this.audio.get(pub.trackSid);
    clearTimeout(this.audioChecks.get(pub.trackSid));
    this.audioChecks.delete(pub.trackSid);
    if (attached) { track.detach(attached.element); attached.element.remove(); this.audio.delete(pub.trackSid); }
    this.options.renderer?.setAudio(null);
  };
  private attributes = (_: Record<string, string>, participant: RemoteParticipant | import('livekit-client').LocalParticipant) => {
    if (participant instanceof Object && !participant.isLocal && this.isAgent(participant as RemoteParticipant)) this.agentState(participant as RemoteParticipant);
  };
  private data = (payload: Uint8Array, participant?: RemoteParticipant, _kind?: unknown, topic?: string) => {
    if (!participant || !this.isAgent(participant)) return;
    if (topic === 'avatarweave.bootstrap.v1') {
      if (payload.byteLength > 4096) return;
      try { const bootstrap = parseSessionBootstrap(JSON.parse(new TextDecoder().decode(payload)));
        if (bootstrap && bootstrap.session_id === this.options.memorySessionId && bootstrap.nonce === this.helloNonce) {
          this.actionsV2?.bootstrap(bootstrap.owner_epoch, bootstrap.generation_sequence, bootstrap.sequence_floor); this.privateReady = true;
          this.stopPrivateHandshake(); this.agentState(participant);
        }
      } catch { /* A stale connection cannot bootstrap commands. */ }
      return;
    }
    if (topic === 'avatarweave.command.v2') {
      if (payload.byteLength > 4096 || !this.actionsV2) return;
      try { const command = parseActionEnvelope(JSON.parse(new TextDecoder().decode(payload))); if (command && command.connection_nonce === this.helloNonce) this.actionsV2.accept(command); } catch { /* Malformed commands cannot execute. */ }
      return;
    }
    if (topic === TOPICS.memoryStatus) {
      if (payload.byteLength > 4096) return;
      try { const status = parseMemoryStatus(JSON.parse(new TextDecoder().decode(payload))); if (status && status.session_id === this.options.memorySessionId) this.options.onMemoryStatus?.(status); } catch { /* Ignore malformed metadata. */ }
      return;
    }
    if (topic !== TOPICS.avatar || this.options.memorySessionId) return;
    try {
      const command = parseAvatarCommand(JSON.parse(new TextDecoder().decode(payload)));
      if (!command || this.seenCommands.has(command.id)) return;
      this.seenCommands.add(command.id);
      if (this.seenCommands.size > 256) this.seenCommands.delete(this.seenCommands.values().next().value!);
      Promise.resolve(this.options.renderer?.execute(command)).catch(e => this.report(e));
      this.options.onCommand?.(command);
    } catch (e) { this.report(e); }
  };
  private disconnected = () => { this.stopPrivateHandshake(new Error('账号对话连接已断开，请重新连接。')); this.actionsV2?.dispose(); this.privateReady = false; this.transcriptSegments.clear(); this.clearAudio(); this.options.onState?.('disconnected'); };
  private clearAudio() {
    for (const timer of this.audioChecks.values()) clearTimeout(timer);
    this.audioChecks.clear();
    for (const { element, track } of this.audio.values()) { track.detach(element); element.pause(); element.srcObject = null; element.remove(); }
    this.audio.clear(); this.options.renderer?.setAudio(null);
  }
  async connect(credentials: Credentials) {
    if (this.disposed) throw new Error('AvatarClient has been disposed');
    this.milestones.clear();
    this.resetPrivateActions();
    this.options.onState?.('connecting');
    try {
      await this.room.connect(credentials.server_url, credentials.participant_token);
      this.markMilestone('room_connected');
      await this.room.startAudio();
      const agent = [...this.room.remoteParticipants.values()].find(p => this.isAgent(p));
      if (agent) this.agentState(agent);
      else this.options.onState?.(this.options.memorySessionId ? 'connecting' : 'connected');
      await this.waitForPrivateReady();
    } catch (error) {
      this.stopPrivateHandshake(error instanceof Error ? error : new Error(String(error)));
      if (this.options.memorySessionId) await this.room.disconnect();
      throw error;
    }
  }
  async sendText(text: string) {
    if (!text.trim()) return;
    if (this.options.memorySessionId && !this.privateReady) throw this.privateReadyError ?? new Error('账号对话正在连接，请稍后再试。');
    const info = await this.room.localParticipant.sendText(text, { topic: TOPICS.chat });
    this.options.onTranscript?.({ id: info.id, role: 'user', text, final: true, participantIdentity: this.room.localParticipant.identity });
  }
  async setMicrophone(enabled: boolean) { if (enabled && this.options.memorySessionId && !this.privateReady) throw this.privateReadyError ?? new Error('账号对话正在连接，请稍后再试。'); await this.room.localParticipant.setMicrophoneEnabled(enabled); }
  async disconnect() { this.stopPrivateHandshake(new Error('账号对话连接已取消。')); this.privateReady = false; await this.room.disconnect(); this.clearAudio(); }
  async dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.stopPrivateHandshake(new Error('账号对话连接已取消。'));
    this.actionsV2?.dispose();
    this.room.off(RoomEvent.ParticipantConnected, this.participantJoined);
    this.room.off(RoomEvent.TrackSubscribed, this.subscribed);
    this.room.off(RoomEvent.TrackUnsubscribed, this.unsubscribed);
    this.room.off(RoomEvent.ParticipantAttributesChanged, this.attributes);
    this.room.off(RoomEvent.DataReceived, this.data);
    this.room.off(RoomEvent.Disconnected, this.disconnected);
    this.room.off(RoomEvent.Reconnecting, this.reconnecting);
    this.room.off(RoomEvent.Reconnected, this.reconnected);
    if (this.options.manageTranscription !== false) this.room.unregisterTextStreamHandler(TOPICS.transcription);
    if (this.ownsRoom) await this.room.disconnect();
    this.transcriptSegments.clear();
    this.clearAudio();
  }
}
