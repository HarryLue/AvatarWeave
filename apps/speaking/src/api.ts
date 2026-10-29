export type Goal = { id: string; label: string; hint: string };
export type Evidence = { id: string; evidence: string };
export type Review = {
  summary: string; next_focus: string;
  corrections: { original: string; revised: string; reason: string }[];
  goals: Evidence[]; user_turns: number; help_count: number;
  duration_seconds: number; mode: string;
};
export type Practice = {
  id: string; status: string; created_at: string; expires_at: string;
  goals: Evidence[]; help_count: number; report: Review | null;
};
export type Scenario = { title: string; brief: string; goals: Goal[]; mode: string };
export type Profile = { name: string; assets: { core_url: string; model_url: string; motion_sync_url?: string } };

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

let pendingGuest: Promise<string> | undefined;
async function guestToken(): Promise<string> {
  const saved = localStorage.getItem('avatarweave.practice.guest');
  if (saved) return saved;
  if (!pendingGuest) pendingGuest = fetch('/api/practice/guest', { method: 'POST' })
    .then(read<{ token: string }>).then(result => {
      localStorage.setItem('avatarweave.practice.guest', result.token);
      return result.token;
    }).finally(() => { pendingGuest = undefined; });
  return pendingGuest;
}

async function read<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(typeof body.detail === 'string' ? body.detail : '服务暂时不可用，请重试', response.status);
  }
  return response.json();
}

export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const token = await guestToken();
  return read<T>(await fetch(`/api/practice${path}`, {
    method, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.timeout(55000),
  }));
}

export async function publicData<T>(path: string): Promise<T> {
  return read<T>(await fetch(path, { signal: AbortSignal.timeout(15000) }));
}

export function asset(path: string | undefined): string | undefined {
  return path?.startsWith('/avatar-assets/') ? `/speaking${path}` : path;
}
