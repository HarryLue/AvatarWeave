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
