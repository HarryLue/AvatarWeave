import type { Credentials } from '@avatarweave/core';

interface PreparedClient {
  room: { prepareConnection(url: string, token?: string): Promise<void> };
  dispose(): Promise<void>;
}
interface Preparation<T> {
  key: string;
  client: T;
  controller: AbortController;
  requestedAt: number;
  returnedAt?: number;
  credentials: Promise<Credentials>;
}

// Tokens issued by /api/token last 15 minutes. Refresh conservatively after 14.
const MAX_AGE_MS = 14 * 60 * 1000;

export class ConnectionPreparation<T extends PreparedClient> {
  private entry?: Preparation<T>;
  private createClient: () => T;
  constructor(createClient: () => T) { this.createClient = createClient; }

  invalidate() {
    const entry = this.entry;
    this.entry = undefined;
    if (entry) {
      entry.controller.abort();
      void entry.client.dispose().catch(() => {});
    }
  }

  private get(key: string, body: string, accessKey: string): Preparation<T> {
    if (this.entry?.key === key && performance.now() - this.entry.requestedAt < MAX_AGE_MS) return this.entry;
    this.invalidate();
    const client = this.createClient();
    const controller = new AbortController();
    const requestedAt = performance.now();
    const credentials = Promise.resolve().then(async () => {
      try {
        const response = await fetch('/api/token', {
          method: 'POST', signal: controller.signal,
          headers: { 'Content-Type': 'application/json', ...(accessKey ? { Authorization: `Bearer ${accessKey}` } : {}) }, body,
        });
        if (!response.ok) throw new Error(`连接请求失败 (${response.status})`);
        const credentials: Credentials = await response.json();
        if (controller.signal.aborted) throw new DOMException('Preparation cancelled', 'AbortError');
        entry.returnedAt = performance.now();
        // Keep the same client/Room for connect; never wait for warm-up on click.
        void client.room.prepareConnection(credentials.server_url, credentials.participant_token).catch(() => {});
        return credentials;
      } catch (error) {
        if (this.entry === entry) this.entry = undefined;
        await client.dispose().catch(() => {});
        throw error;
      }
    });
    const entry: Preparation<T> = { key, client, controller, requestedAt, credentials };
    this.entry = entry;
    return entry;
  }

  async warm(key: string, body: string, accessKey: string) {
    await this.get(key, body, accessKey).credentials;
  }

  async take(key: string, body: string, accessKey: string) {
    const existing = this.entry;
    const entry = this.get(key, body, accessKey);
    const source = entry !== existing ? 'fresh' : entry.returnedAt !== undefined ? 'prefetched' : 'in_flight';
    // Transfer ownership so effect cleanup cannot dispose a connecting client.
    this.entry = undefined;
    const credentials = await entry.credentials;
    return { client: entry.client, credentials, source, tokenAgeMs: Math.round(performance.now() - entry.returnedAt!) };
  }
}
