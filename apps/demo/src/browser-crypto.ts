// Text streams also use randomUUID for their IDs. getRandomValues remains
// available on HTTP origins, where the native randomUUID method is absent.
export function ensureRandomUUID(crypto: Pick<Crypto, 'getRandomValues'> & Partial<Pick<Crypto, 'randomUUID'>>) {
  if (typeof crypto.randomUUID === 'function') return;
  crypto.randomUUID = () => {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };
}

if (globalThis.crypto) ensureRandomUUID(globalThis.crypto);
