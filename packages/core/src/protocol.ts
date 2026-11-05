export const PROTOCOL_VERSION = 1 as const;
export const TOPICS = { chat: 'lk.chat', transcription: 'lk.transcription', avatar: 'avatarweave.command.v1', memoryStatus: 'avatarweave.memory-status.v1' } as const;
type BaseCommand = { version: 1; id: string; name: string };
export type AvatarCommand =
  | (BaseCommand & { type: 'motion'; index: number })
  | (BaseCommand & { type: 'expression' });

/** Strict current contract: unknown fields/versions and malformed commands are rejected. */
export function parseAvatarCommand(value: unknown): AvatarCommand | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return;
  const c = value as Record<string, unknown>;
  if (c.version !== 1 || typeof c.id !== 'string' || !c.id.length || c.id.length > 128 ||
      typeof c.name !== 'string' || c.name.length > 128) return;
  if (c.type === 'motion') {
    if (Object.keys(c).some(k => !['version', 'id', 'type', 'name', 'index'].includes(k))) return;
    if (!Number.isSafeInteger(c.index) || Number(c.index) < 0) return;
  } else if (c.type === 'expression') {
    if (!c.name.length || Object.keys(c).some(k => !['version', 'id', 'type', 'name'].includes(k))) return;
  } else return;
  return c as AvatarCommand;
}

export interface MemoryStatus { version: 1; session_id: string; generation_id: string | null; state: 'source_saved' | 'temporary' | 'failed' | 'cancelled'; source_saved: boolean; code: string | null }
export function parseMemoryStatus(value: unknown): MemoryStatus | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return;
  const v = value as Record<string, unknown>;
  if (Object.keys(v).some(key => !['version', 'session_id', 'generation_id', 'state', 'source_saved', 'code'].includes(key)) ||
      v.version !== 1 || typeof v.session_id !== 'string' || !v.session_id.length || v.session_id.length > 128 ||
      (v.generation_id !== null && (typeof v.generation_id !== 'string' || !v.generation_id.length || v.generation_id.length > 128)) ||
      !['source_saved', 'temporary', 'failed', 'cancelled'].includes(String(v.state)) || typeof v.source_saved !== 'boolean' ||
      (v.code !== null && (typeof v.code !== 'string' || v.code.length > 64)) ||
      (v.state === 'source_saved' && !v.source_saved)) return;
  return v as unknown as MemoryStatus;
}
