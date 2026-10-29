import { Room, RoomEvent, Track, ParticipantKind, type RemoteParticipant, type RemoteTrack, type RemoteAudioTrack, type RemoteTrackPublication } from 'livekit-client';
import { TOPICS, parseAvatarCommand, type AvatarCommand } from './protocol.js';
export * from './protocol.js';
export interface Credentials { server_url: string; participant_token: string }
export interface Transcript { id: string; role: 'user' | 'agent'; text: string; final: boolean; participantIdentity: string }
export interface AvatarRenderer { setAudio(stream: MediaStream | null): void; execute(command: AvatarCommand): void | Promise<void> }
export type ConnectionMilestone = 'room_connected' | 'agent_ready' | 'first_audio_playback';
export interface ClientOptions {
  room?: Room;
  agentIdentity?: string;
  manageTranscription?: boolean;
  renderer?: AvatarRenderer;
  onTranscript?: (message: Transcript) => void;
  onState?: (state: string) => void;
  onCommand?: (command: AvatarCommand) => void;
  onError?: (error: Error) => void;
  onConnectionMilestone?: (stage: ConnectionMilestone, at: number) => void;
}
/** Attach to any existing Room, or let this client own connection and audio playback. */
export class AvatarClient {
  readonly room: Room;
  private options: ClientOptions;
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
    this.room.on(RoomEvent.ParticipantConnected, this.participantJoined);
    this.room.on(RoomEvent.TrackSubscribed, this.subscribed);
    this.room.on(RoomEvent.TrackUnsubscribed, this.unsubscribed);
    this.room.on(RoomEvent.ParticipantAttributesChanged, this.attributes);
    this.room.on(RoomEvent.DataReceived, this.data);
    this.room.on(RoomEvent.Disconnected, this.disconnected);
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
      for (const pub of p.trackPublications.values()) if (pub.track) this.subscribed(pub.track, pub, p);
    }
  }
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
    const state = p.attributes['lk.agent.state'];
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
    if (topic !== TOPICS.avatar || !participant || !this.isAgent(participant)) return;
    try {
      const command = parseAvatarCommand(JSON.parse(new TextDecoder().decode(payload)));
      if (!command || this.seenCommands.has(command.id)) return;
      this.seenCommands.add(command.id);
      if (this.seenCommands.size > 256) this.seenCommands.delete(this.seenCommands.values().next().value!);
      Promise.resolve(this.options.renderer?.execute(command)).catch(e => this.report(e));
      this.options.onCommand?.(command);
    } catch (e) { this.report(e); }
  };
  private disconnected = () => { this.transcriptSegments.clear(); this.clearAudio(); this.options.onState?.('disconnected'); };
  private clearAudio() {
    for (const timer of this.audioChecks.values()) clearTimeout(timer);
    this.audioChecks.clear();
    for (const { element, track } of this.audio.values()) { track.detach(element); element.pause(); element.srcObject = null; element.remove(); }
    this.audio.clear(); this.options.renderer?.setAudio(null);
  }
  async connect(credentials: Credentials) {
    if (this.disposed) throw new Error('AvatarClient has been disposed');
    this.milestones.clear();
    this.options.onState?.('connecting');
    await this.room.connect(credentials.server_url, credentials.participant_token);
    this.markMilestone('room_connected');
    await this.room.startAudio();
    const agent = [...this.room.remoteParticipants.values()].find(p => this.isAgent(p));
    if (agent) this.agentState(agent);
    else this.options.onState?.('connected');
  }
  async sendText(text: string) {
    if (!text.trim()) return;
    const info = await this.room.localParticipant.sendText(text, { topic: TOPICS.chat });
    this.options.onTranscript?.({ id: info.id, role: 'user', text, final: true, participantIdentity: this.room.localParticipant.identity });
  }
  async setMicrophone(enabled: boolean) { await this.room.localParticipant.setMicrophoneEnabled(enabled); }
  async disconnect() { await this.room.disconnect(); this.clearAudio(); }
  async dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.room.off(RoomEvent.ParticipantConnected, this.participantJoined);
    this.room.off(RoomEvent.TrackSubscribed, this.subscribed);
    this.room.off(RoomEvent.TrackUnsubscribed, this.unsubscribed);
    this.room.off(RoomEvent.ParticipantAttributesChanged, this.attributes);
    this.room.off(RoomEvent.DataReceived, this.data);
    this.room.off(RoomEvent.Disconnected, this.disconnected);
    if (this.options.manageTranscription !== false) this.room.unregisterTextStreamHandler(TOPICS.transcription);
    if (this.ownsRoom) await this.room.disconnect();
    this.transcriptSegments.clear();
    this.clearAudio();
  }
}
