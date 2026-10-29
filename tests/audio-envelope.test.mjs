import test from 'node:test';
import assert from 'node:assert/strict';
import { AudioEnvelope } from '../packages/renderer/src/audio-envelope.ts';

test('fallback analyses audio without worklets, mutes its graph, and releases resources', async t => {
  const nodes = [], contexts = [];
  let amplitude = 0;
  const node = () => {
    const value = { disconnected: false, connect() {}, disconnect() { this.disconnected = true; } };
    nodes.push(value);
    return value;
  };
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'AudioContext');
  t.after(() => { if (previous) Object.defineProperty(globalThis, 'AudioContext', previous); else delete globalThis.AudioContext; });
  Object.defineProperty(globalThis, 'AudioContext', { configurable: true, value: class {
    state = 'running';
    destination = {};
    constructor() { contexts.push(this); }
    createMediaStreamSource() { return node(); }
    createAnalyser() { return Object.assign(node(), { getFloatTimeDomainData(samples) { samples.fill(amplitude); } }); }
    createGain() { return Object.assign(node(), { gain: { value: 1 } }); }
    async resume() {}
    async close() { this.state = 'closed'; }
  }});
  let stopped = false;
  const stream = { getAudioTracks: () => [{ stop() { stopped = true; } }] };
  const envelope = new AudioEnvelope();
  await envelope.play(stream);
  assert.equal(envelope.readLevel(), 0);
  assert.equal(nodes[2].gain.value, 0);
  amplitude = 0.15;
  assert.ok(envelope.readLevel() > 0.5);
  assert.ok(envelope.readLevel() <= 1);
  amplitude = 0;
  const before = envelope.readLevel();
  assert.ok(envelope.readLevel() < before);
  await envelope.reset();
  assert.equal(envelope.readLevel(), 0);
  assert.equal(contexts[0].state, 'closed');
  assert.ok(nodes.every(node => node.disconnected));
  assert.equal(stopped, false, 'room owns the source tracks');
  await envelope.reset();
});

test('empty streams do not create audio resources', async () => {
  const envelope = new AudioEnvelope();
  await envelope.play({ getAudioTracks: () => [] });
  assert.equal(envelope.readLevel(), 0);
  await envelope.reset();
});
