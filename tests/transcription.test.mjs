import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { RoomEvent, ParticipantKind } from 'livekit-client';
import { AvatarClient } from '../packages/core/dist/index.js';

class FakeRoom extends EventEmitter {
  localParticipant = { identity: 'user', isLocal: true };
  remoteParticipants = new Map([['agent', { identity: 'agent', kind: ParticipantKind.AGENT, trackPublications: new Map() }]]);
  handlers = new Map();
  registerTextStreamHandler(topic, handler) { assert.ok(!this.handlers.has(topic)); this.handlers.set(topic, handler); }
  unregisterTextStreamHandler(topic) { this.handlers.delete(topic); }
}
function reader(id, chunks, { final = 'false', onDone, beforeChunk } = {}) {
  const info = { id: crypto.randomUUID(), attributes: { 'lk.segment_id': id, 'lk.transcription_final': final } };
  return { info, async *[Symbol.asyncIterator]() {
    for (const chunk of chunks) { await beforeChunk?.(); yield chunk; }
    onDone?.(info);
  } };
}
function fixture(options = {}) {
  const room = new FakeRoom(), updates = [], messages = new Map();
  const client = new AvatarClient({ room, ...options, onTranscript: message => {
    updates.push({...message});
    messages.set(`${message.participantIdentity}:${message.id}`, message);
  } });
  return { room, client, updates, messages, stream: (r, identity = 'user') => room.handlers.get('lk.transcription')(r, {identity}) };
}
test('ASR uses one transport, progressively updates one segment, and respects its final flag', async () => {
  const f = fixture();
  await f.stream(reader('utterance', ['你好']));
  assert.equal(f.updates.at(-1).final, false, 'closing an interim snapshot must not finalize it');
  await f.stream(reader('utterance', ['你好，', '今天怎么样']));
  assert.equal(f.updates.at(-1).text, '你好，今天怎么样');
  assert.equal(f.messages.size, 1);
  f.room.emit(RoomEvent.TranscriptionReceived, [{ id: 'event-id', text: '你好，今天怎么样', final: true }], f.room.localParticipant);
  assert.equal(f.messages.size, 1, 'deprecated transport must not produce a bubble');
  await f.stream(reader('utterance', ['你好，今天怎么样'], {final:'true'}));
  assert.equal(f.messages.size, 1);
  assert.equal(f.updates.at(-1).final, true);
  assert.ok(f.updates.some(m => m.text === '你好，' && !m.final));
  await f.client.dispose();
});
test('agent delta streams read final state from the trailer; repeated sentences remain separate turns', async () => {
  const f = fixture();
  await f.stream(reader('reply-1', ['你', '好'], {onDone: info => {info.attributes['lk.transcription_final'] = 'true';}}), 'agent');
  assert.equal(f.updates.at(-1).text, '你好');
  assert.equal(f.updates.at(-1).final, true);
  await f.stream(reader('reply-2', ['你好'], {final:'true'}), 'agent');
  assert.equal(f.messages.size, 2, 'deduplication must not discard a genuinely repeated utterance');
  await f.client.dispose();
});
test('a stale interim stream cannot overwrite a newer final result', async () => {
  const f = fixture();
  let resume;
  const gate = new Promise(resolve => {resume = resolve;});
  const pending = f.stream(reader('utterance', ['旧识别'], {beforeChunk:()=>gate}));
  await f.stream(reader('utterance', ['修正后的识别'], {final:'true'}));
  resume(); await pending;
  assert.equal(f.updates.at(-1).text, '修正后的识别');
  assert.equal(f.updates.at(-1).final, true);
  await f.client.dispose();
});
test('host-managed mode leaves the current text stream handler alone', async () => {
  const host = fixture({manageTranscription:false});
  assert.equal(host.room.handlers.size, 0);
  assert.equal(host.room.listenerCount(RoomEvent.TranscriptionReceived), 0);
  await host.client.dispose();
});

test('only the trusted agent can report speech service errors and repeated state changes do not repeat errors', async () => {
  const errors = [];
  const f = fixture({onError: error => errors.push(error.message)});
  const agent = f.room.remoteParticipants.get('agent');
  agent.isLocal = false;
  agent.attributes = {'lk.agent.state': 'listening', 'avatarweave.service_error': '语音服务暂时不可用，你可以先看文字回复。'};
  f.room.emit(RoomEvent.ParticipantAttributesChanged, {}, agent);
  f.room.emit(RoomEvent.ParticipantAttributesChanged, {}, agent);
  assert.deepEqual(errors, ['语音服务暂时不可用，你可以先看文字回复。']);
  const other = {isLocal: false, kind: ParticipantKind.STANDARD, attributes: {'avatarweave.service_error': 'untrusted'}};
  f.room.emit(RoomEvent.ParticipantAttributesChanged, {}, other);
  assert.equal(errors.length, 1);
  await f.client.dispose();
});

test('only the current action topic is consumed, with strict validation and ID deduplication', async () => {
  const commands=[];
  const f=fixture({renderer:{setAudio(){},execute(command){commands.push(command);}}});
  const sender=f.room.remoteParticipants.get('agent');
  const payload=new TextEncoder().encode(JSON.stringify({version:1,id:'one-command',type:'motion',name:'',index:3}));
  f.room.emit(RoomEvent.DataReceived,payload,sender,undefined,'avatar.command.v1');
  assert.equal(commands.length,0);
  f.room.emit(RoomEvent.DataReceived,payload,sender,undefined,'avatarweave.command.v1');
  f.room.emit(RoomEvent.DataReceived,payload,sender,undefined,'avatarweave.command.v1');
  assert.equal(commands.length,1);
  await f.client.dispose();
});

test('connection timing distinguishes room connection from agent readiness and ignores duplicate states', async () => {
  const milestones = [];
  const f = fixture({ onConnectionMilestone: (stage, at) => milestones.push({ stage, at }) });
  const agent = f.room.remoteParticipants.get('agent');
  agent.attributes = { 'lk.agent.state': 'initializing' };
  f.room.connect = async () => {};
  f.room.startAudio = async () => {
    assert.deepEqual(milestones.map(m => m.stage), ['room_connected']);
  };
  await f.client.connect({ server_url: 'ws://test', participant_token: 'test' });
  f.room.emit(RoomEvent.ParticipantConnected, agent);
  assert.equal(milestones.length, 1);
  agent.attributes['lk.agent.state'] = 'listening';
  f.room.emit(RoomEvent.ParticipantAttributesChanged, {}, agent);
  agent.attributes['lk.agent.state'] = 'speaking';
  f.room.emit(RoomEvent.ParticipantAttributesChanged, {}, agent);
  assert.deepEqual(milestones.map(m => m.stage), ['room_connected', 'agent_ready']);
  assert.ok(milestones.every(m => Number.isFinite(m.at)));
  await f.client.dispose();
});

test('first audio timing waits for non-silent audio and playback, and stops polling on disposal', async () => {
  const milestones = [];
  const f = fixture({ onConnectionMilestone: stage => milestones.push(stage) });
  const element = { paused: false, readyState: 2, pause() {}, remove() {} };
  let energy = 0;
  const track = { getReceiverStats: async () => ({ totalAudioEnergy: energy }), detach() {} };
  f.client.audio.set('audio', { element, track });
  f.client.watchFirstAudio(track, 'audio', element);
  await new Promise(resolve => setTimeout(resolve, 120));
  assert.deepEqual(milestones, [], 'silent tracks must not count as first audio');
  energy = 1;
  element.paused = true;
  await new Promise(resolve => setTimeout(resolve, 120));
  assert.deepEqual(milestones, [], 'blocked playback must not count as first audio');
  element.paused = false;
  await new Promise(resolve => setTimeout(resolve, 120));
  assert.deepEqual(milestones, ['first_audio_playback']);
  await f.client.dispose();
  assert.equal(f.client.audioChecks.size, 0);
});
