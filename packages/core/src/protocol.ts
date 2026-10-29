export const PROTOCOL_VERSION = 1 as const;
export const TOPICS = { chat: 'lk.chat', transcription: 'lk.transcription', avatar: 'avatarweave.command.v1' } as const;
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
