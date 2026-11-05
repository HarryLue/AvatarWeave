import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { ParticipantKind, RoomEvent } from 'livekit-client';
import { AvatarClient } from '../packages/core/dist/index.js';

const tick = () => new Promise(resolve => setImmediate(resolve));
test('private SDK binds a fresh nonce and drops pre-reconnect commands while preserving renderer this', async () => {
  const published = [];
  const participant = { sid: 'agent-sid', identity: 'agent', kind: ParticipantKind.AGENT, attributes: { 'lk.agent.state': 'listening' }, trackPublications: new Map() };
  class Room extends EventEmitter {
    state = 'connected';
    remoteParticipants = new Map([['agent', participant]]);
    localParticipant = { identity: 'user', publishData: async (data, options) => published.push({ body: JSON.parse(new TextDecoder().decode(data)), topic: options.topic }), sendText: async () => ({ id: 'message' }) };
  }
  const room = new Room();
  const renderer = { calls: [], setAudio() {}, execute() {}, async executeTracked(command, signal, observe) { this.calls.push(command.id); observe('started'); observe('completed'); } };
  const client = new AvatarClient({ room, memorySessionId: 'session', manageTranscription: false, renderer });
  const deliver = (body, topic) => room.emit(RoomEvent.DataReceived, new TextEncoder().encode(JSON.stringify(body)), participant, undefined, topic);
  let connectionNonce;
  const command = (id, sequence, nonce = connectionNonce) => ({ version: 2, command_id: id, connection_nonce: nonce, session_id: 'session', generation_id: 'gen', owner_epoch: 1, generation_sequence: 1, sequence, expires_at: new Date(Date.now()+10000).toISOString(), operation: 'execute', action: { type: 'motion', name: '', index: 0 } });
  const bootstrap = (nonce, floor) => ({ version: 1, session_id: 'session', nonce, owner_epoch: 1, generation_sequence: 1, sequence_floor: floor, expires_at: new Date(Date.now()+10000).toISOString() });
  try {
    const first = published.find(value => value.topic === 'avatarweave.hello.v1').body;
    connectionNonce = first.nonce;
    deliver(command('before-join', 1), 'avatarweave.command.v2');
    deliver(bootstrap('stale-nonce', 0), 'avatarweave.bootstrap.v1');
    assert.deepEqual(renderer.calls, []);
    await assert.rejects(client.sendText('hello'), /正在连接/);
    deliver(bootstrap(first.nonce, 1), 'avatarweave.bootstrap.v1');
    deliver(command('fresh', 2), 'avatarweave.command.v2');
    await tick();
    assert.deepEqual(renderer.calls, ['fresh']);
    room.emit(RoomEvent.Reconnecting); room.emit(RoomEvent.Reconnected);
    const second = published.filter(value => value.topic === 'avatarweave.hello.v1').at(-1).body;
    assert.notEqual(second.nonce, first.nonce);
    connectionNonce = second.nonce;
    deliver(bootstrap(first.nonce, 0), 'avatarweave.bootstrap.v1');
    deliver(command('late-unseen', 2, first.nonce), 'avatarweave.command.v2');
    deliver(bootstrap(second.nonce, 2), 'avatarweave.bootstrap.v1');
    deliver(command('after-reconnect', 3), 'avatarweave.command.v2');
    await tick();
    assert.deepEqual(renderer.calls, ['fresh', 'after-reconnect']);
    assert.ok(published.some(value => value.topic === 'avatarweave.receipt.v1' && value.body.state === 'completed'));
  } finally { await client.dispose(); }
});

test('private connect waits for bootstrap, retries a missed hello, and then allows text and microphone', async () => {
  const published = [], states = [], sent = [];
  const participant = { sid: 'agent-sid', identity: 'agent', kind: ParticipantKind.AGENT, attributes: { 'lk.agent.state': 'listening' }, trackPublications: new Map() };
  class Room extends EventEmitter {
    state = 'disconnected';
    remoteParticipants = new Map([['agent', participant]]);
    localParticipant = {
      publishData: async (data, options) => published.push({ body: JSON.parse(new TextDecoder().decode(data)), topic: options.topic }),
      sendText: async text => { sent.push(text); return { id: 'sent' }; },
      setMicrophoneEnabled: async enabled => sent.push(enabled),
    };
    async connect() { this.state = 'connected'; }
    async startAudio() {}
    async disconnect() { this.state = 'disconnected'; this.emit(RoomEvent.Disconnected); }
  }
  const room = new Room();
  const client = new AvatarClient({ room, memorySessionId: 'session', privateReadyTimeoutMs: 3000, manageTranscription: false, onState: state => states.push(state) });
  try {
    let connected = false;
    const connection = client.connect({ server_url: 'fixture', participant_token: 'fixture' }).then(() => { connected = true; });
    await tick();
    assert.equal(connected, false);
    assert.ok(!states.includes('listening'));
    await assert.rejects(client.sendText('before readiness'), /正在连接/);
    await new Promise(resolve => setTimeout(resolve, 1100));
    const hellos = published.filter(item => item.topic === 'avatarweave.hello.v1');
    assert.ok(hellos.length >= 2);
    assert.equal(hellos[0].body.nonce, hellos[1].body.nonce);
    const bootstrap = { version: 1, session_id: 'session', nonce: hellos[0].body.nonce, owner_epoch: 1, generation_sequence: 0, sequence_floor: 0, expires_at: new Date(Date.now() + 10000).toISOString() };
    room.emit(RoomEvent.DataReceived, new TextEncoder().encode(JSON.stringify(bootstrap)), participant, undefined, 'avatarweave.bootstrap.v1');
    await connection;
    assert.ok(connected && states.includes('listening'));
    await client.sendText('hello');
    await client.setMicrophone(true);
    assert.deepEqual(sent, ['hello', true]);
  } finally { await client.dispose(); }
});

test('private handshake timeout disconnects and lets the UI offer another attempt', async () => {
  class Room extends EventEmitter {
    state = 'disconnected';
    remoteParticipants = new Map();
    localParticipant = {};
    async connect() { this.state = 'connected'; }
    async startAudio() {}
    async disconnect() { this.state = 'disconnected'; this.emit(RoomEvent.Disconnected); }
  }
  const room = new Room();
  const client = new AvatarClient({ room, memorySessionId: 'session', privateReadyTimeoutMs: 20, manageTranscription: false });
  try {
    await assert.rejects(client.connect({ server_url: 'fixture', participant_token: 'fixture' }), /连接超时/);
    assert.equal(room.state, 'disconnected');
  } finally { await client.dispose(); }
});
