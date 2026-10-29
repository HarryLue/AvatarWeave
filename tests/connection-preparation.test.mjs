import test from 'node:test';
import assert from 'node:assert/strict';
import { ConnectionPreparation } from '../apps/demo/src/connection-preparation.ts';

function fixture(t, fetcher) {
  const clients = [], requests = [];
  t.mock.method(globalThis, 'fetch', async (...args) => {
    requests.push(args);
    return fetcher ? fetcher(...args) : Response.json({ server_url: 'ws://test', participant_token: 'test-token' });
  });
  const preparation = new ConnectionPreparation(() => {
    const client = { disposed: false, warmed: [], room: {
      prepareConnection: async (...args) => { client.warmed.push(args); },
    }, dispose: async () => { client.disposed = true; } };
    clients.push(client);
    return client;
  });
  t.after(() => preparation.invalidate());
  return { preparation, clients, requests };
}

test('click reuses prefetched token and the warmed Room, then consumes the cache', async t => {
  const f = fixture(t);
  await f.preparation.warm('kei', '{"avatar_id":"kei"}', 'secret');
  const taken = await f.preparation.take('kei', '{"avatar_id":"kei"}', 'secret');
  assert.equal(taken.source, 'prefetched');
  assert.equal(taken.client, f.clients[0]);
  assert.deepEqual(taken.client.warmed, [['ws://test', 'test-token']]);
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0][1].headers.Authorization, 'Bearer secret');
  f.preparation.invalidate();
  assert.equal(taken.client.disposed, false, 'cleanup must not dispose a consumed client');
  const next = await f.preparation.take('kei', '{}', 'secret');
  assert.equal(next.source, 'fresh');
  assert.notEqual(next.client, taken.client);
});

test('click shares an in-flight prefetch without waiting for connection warm-up', async t => {
  let resolveFetch;
  const f = fixture(t, () => new Promise(resolve => { resolveFetch = resolve; }));
  const warm = f.preparation.warm('key', '{}', '');
  await Promise.resolve();
  f.clients[0].room.prepareConnection = () => new Promise(() => {});
  const pending = f.preparation.take('key', '{}', '');
  f.preparation.invalidate();
  assert.equal(f.requests[0][1].signal.aborted, false);
  resolveFetch(Response.json({ server_url: 'ws://test', participant_token: 'token' }));
  const result = await pending;
  await warm;
  assert.equal(result.source, 'in_flight');
  assert.equal(f.requests.length, 1);
});

test('changed selections cancel old requests and cannot reuse their results', async t => {
  let resolveOld;
  const f = fixture(t, () => new Promise(resolve => { resolveOld = resolve; }));
  const old = f.preparation.warm('old-selection', '{}', '');
  const rejected = assert.rejects(old, { name: 'AbortError' });
  await Promise.resolve();
  const fresh = f.preparation.take('new-selection', '{}', '');
  assert.equal(f.requests[0][1].signal.aborted, true);
  resolveOld(Response.json({ server_url: 'ws://test', participant_token: 'old' }));
  await Promise.resolve();
  resolveOld(Response.json({ server_url: 'ws://test', participant_token: 'new' }));
  await rejected;
  assert.equal((await fresh).credentials.participant_token, 'new');
  assert.equal(f.clients[0].disposed, true);
});

test('expired tokens and failed prefetches are fetched again on click', async t => {
  let now = 0;
  t.mock.method(performance, 'now', () => now);
  const f = fixture(t);
  await f.preparation.warm('key', '{}', '');
  now = 14 * 60 * 1000;
  assert.equal((await f.preparation.take('key', '{}', '')).source, 'fresh');
  assert.equal(f.clients[0].disposed, true);
  t.mock.method(globalThis, 'fetch', async () => new Response('', { status: 401 }));
  await assert.rejects(f.preparation.warm('key', '{}', 'bad'), /401/);
  t.mock.method(globalThis, 'fetch', async () => Response.json({ server_url: 'ws://test', participant_token: 'retry' }));
  assert.equal((await f.preparation.take('key', '{}', 'good')).source, 'fresh');
});
