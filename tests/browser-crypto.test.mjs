import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { ensureRandomUUID } from '../apps/demo/src/browser-crypto.ts';

test('HTTP compatibility generates distinct RFC 4122 version 4 UUIDs', () => {
  const crypto = { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) };
  ensureRandomUUID(crypto);
  const ids = Array.from({ length: 100 }, () => crypto.randomUUID());
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ids) assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
});

test('HTTPS native randomUUID is preserved', () => {
  const native = () => 'native';
  const crypto = { randomUUID: native };
  ensureRandomUUID(crypto);
  assert.equal(crypto.randomUUID, native);
});
