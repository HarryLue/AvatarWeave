import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { getLogger, ParticipantKind } from 'livekit-client';
import { AvatarClient } from '../packages/core/dist/index.js';
import { createRoomLogger } from '../packages/core/dist/rtc-logging.js';

test('only the exact signal 1006 error during an explicitly requested close is diagnostic', () => {
  const saved = console.error;
  const errors = [];
  console.error = (...args) => errors.push(args);
  const scoped = createRoomLogger();
  const logger = getLogger(scoped.name);
  const error = new Error('WS closed unexpectedly with code 1006');
  try {
    logger.error('error reading from signal stream', { error });
    assert.equal(errors.length, 1, 'active network failure must remain an error');
    scoped.beginClose();
    logger.error('error reading from signal stream', { error });
    assert.equal(errors.length, 1, 'expected shutdown must not be reported as failure');
    logger.error('error reading from signal stream', { error: new Error('invalid message') });
    assert.equal(errors.length, 2, 'other errors during shutdown must remain visible');
    scoped.connected();
    logger.error('error reading from signal stream', { error });
    assert.equal(errors.length, 3, 'a new active connection must not inherit close suppression');
  } finally {
    console.error = saved;
  }
});

test('concurrent disposal awaits a single owned room disconnect', async () => {
  const client = new AvatarClient();
  let calls = 0;
  let complete;
  const gate = new Promise(resolve => { complete = resolve; });
  client.room.disconnect = async () => { calls++; await gate; };
  const first = client.dispose();
  const second = client.dispose();
  assert.equal(first, second);
  assert.equal(calls, 1);
  let finished = false;
  void second.then(() => { finished = true; });
  await Promise.resolve();
  assert.equal(finished, false, 'all callers must wait until teardown completes');
  complete();
  await Promise.all([first, second]);
  assert.equal(finished, true);
});

test('late transcript cancellation after disposal does not create a user-facing error', async () => {
  class FakeRoom extends EventEmitter {
    localParticipant = { identity: 'user' };
    remoteParticipants = new Map([['agent', { identity: 'agent', kind: ParticipantKind.AGENT, trackPublications: new Map() }]]);
    handlers = new Map();
    registerTextStreamHandler(topic, handler) { this.handlers.set(topic, handler); }
    unregisterTextStreamHandler(topic) { this.handlers.delete(topic); }
  }
  const room = new FakeRoom();
  const errors = [];
  const client = new AvatarClient({ room, onError: error => errors.push(error) });
  let cancel;
  const gate = new Promise(resolve => { cancel = resolve; });
  const reader = {
    info: { id: 'late-stream', attributes: {} },
    async *[Symbol.asyncIterator]() {
      await gate;
      throw new Error('stream cancelled during teardown');
    },
  };
  const pending = room.handlers.get('lk.transcription')(reader, { identity: 'agent' });
  await client.dispose();
  cancel();
  await pending;
  assert.deepEqual(errors, []);
});
