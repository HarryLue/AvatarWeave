import test from 'node:test';
import assert from 'node:assert/strict';
import { ActionController, parseActionEnvelope } from '../packages/core/dist/actions-v2.js';

const envelope = (patch = {}) => ({ version: 2, command_id: 'one', connection_nonce: 'nonce', session_id: 'session', generation_id: 'gen',
  owner_epoch: 1, generation_sequence: 1, sequence: 1, expires_at: new Date(Date.now() + 10000).toISOString(),
  operation: 'execute', action: { type: 'motion', name: '', index: 0 }, ...patch });
const tick = () => new Promise(resolve => setImmediate(resolve));

test('v2 parser rejects forged capability fields, bool counters and unknown versions', () => {
  assert.ok(parseActionEnvelope(envelope()));
  for (const patch of [{ version: 3 }, { owner_epoch: true }, { sequence: -1 }, { script: 'x' },
    { operation: 'cancel', action: { type: 'motion', name: '', index: 0 } }, { action: { type: 'expression', name: 'x' } }]) {
    assert.equal(parseActionEnvelope(envelope(patch)), undefined);
  }
});

test('bounded pre-bootstrap commands wait for trusted owner attributes and cancellation wins', async () => {
  const calls = [], receipts = [];
  const controller = new ActionController('session', { executeTracked: async c => calls.push(c) }, r => receipts.push(r));
  controller.accept(envelope());
  controller.accept(envelope({ command_id: 'cancel', operation: 'cancel', action: null, sequence: 2 }));
  assert.equal(calls.length, 0);
  controller.bootstrap(1, 1);
  await tick();
  assert.equal(calls.length, 0);
  assert.equal(receipts.length, 0);
  controller.dispose();
});

test('completion needs renderer evidence and cancelled generations cannot replay', async () => {
  const receipts = [], calls = [];
  const renderer = { executeTracked: async (command, signal, observe) => {
    calls.push(command.id); observe('started');
    await new Promise(resolve => signal.addEventListener('abort', resolve, { once: true }));
    observe('completed');
  } };
  const controller = new ActionController('session', renderer, r => receipts.push(r));
  controller.bootstrap(1, 0);
  controller.accept(envelope());
  assert.deepEqual(receipts.map(r => r.state), ['accepted', 'started']);
  controller.accept(envelope({ command_id: 'cancel', operation: 'cancel', action: null, sequence: 2 }));
  controller.accept(envelope({ command_id: 'late', sequence: 3 }));
  await tick();
  assert.deepEqual(calls, ['one']);
  assert.deepEqual(receipts.map(r => r.state), ['accepted', 'started', 'cancelled']);
  controller.dispose();
});

test('new owner aborts old work; stale, wrong-session and expired commands stay inert', async () => {
  const calls = [];
  const controller = new ActionController('session', { executeTracked: async c => calls.push(c.id) }, () => {});
  controller.bootstrap(2, 1);
  for (const patch of [{}, { owner_epoch: 2, session_id: 'other' },
    { owner_epoch: 2, expires_at: new Date(0).toISOString() }]) controller.accept(envelope(patch));
  assert.equal(calls.length, 0);
  controller.accept(envelope({ owner_epoch: 2, command_id: 'new' }));
  controller.accept(envelope({ owner_epoch: 2, command_id: 'new' }));
  await tick();
  assert.deepEqual(calls, ['new']);
  controller.dispose();
});

test('renderer resolve alone reports unknown and disposal rejects all later commands', async () => {
  const receipts = [];
  const controller = new ActionController('session', { executeTracked: async () => {} }, r => receipts.push(r));
  controller.bootstrap(1, 0); controller.accept(envelope());
  await tick();
  assert.deepEqual(receipts.map(r => r.state), ['accepted', 'unknown']);
  controller.dispose(); controller.accept(envelope({ command_id: 'later', sequence: 2 }));
  assert.equal(receipts.length, 2);
});

test('reconnection floor drops commands from before the new connection, including unseen IDs', async () => {
  const calls = [];
  const controller = new ActionController('session', { executeTracked: async c => calls.push(c.id) }, () => {});
  controller.accept(envelope({ command_id: 'unseen-before-join', sequence: 3 }));
  controller.bootstrap(1, 1, 3);
  assert.deepEqual(calls, []);
  controller.accept(envelope({ command_id: 'fresh', sequence: 4 }));
  await tick();
  assert.deepEqual(calls, ['fresh']);
  controller.bootstrap(2, 2, 0);
  controller.accept(envelope({ owner_epoch: 2, generation_sequence: 2, command_id: 'new-owner', sequence: 1 }));
  await tick();
  assert.deepEqual(calls, ['fresh', 'new-owner']);
  controller.dispose();
});
