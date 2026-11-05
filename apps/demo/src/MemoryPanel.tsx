import { useEffect, useState } from 'react';

type Memory = { memory_id: string; version: number; text: string; kind: string; state: string; pinned: boolean; label: string; valid_time: { start?: string | null; end?: string | null; precision: string }; evidence: { source_label: string; granularity: string; quote?: string | null }[] };
type Preferences = { preference_version: number; long_term_memory_enabled: boolean; raw_transcript_days: number; retain_evidence_capsules: boolean; allow_proactive_followup: boolean; allow_anniversary_recall: boolean; cross_companion_share: 'none'; permitted_shared_predicates: string[] };
type Loop = { loop_id: string; version: number; title: string; status: string; user_allows_followup: boolean; event_time: Memory['valid_time'] };
type ExportView = { export_id: string; state: string; download_handle: string | null; conflicts: string[] };
type Transcript = { event_id: string; source_revision: number; text: string };
type Person = { entity_id: string; version: number; canonical_name: string; aliases: string[] };
type Reflection = { reflection_id: string; version: number; text: string; state: 'needs_confirmation' | 'confirmed' | 'rejected' };
const savedSessionKey = 'avatarweave.private-session';
type Identity = { state: 'anonymous' | 'account'; companion_ids: string[] };
export type PrivateCredentials = { server_url: string; participant_token: string; session_id: string };

function csrf() {
  const item = document.cookie.split('; ').find(value => value.startsWith('__Host-avatarweave-csrf='));
  return item ? decodeURIComponent(item.slice(item.indexOf('=') + 1)) : '';
}
async function api<T>(path: string, method = 'GET', body?: unknown): Promise<{ data: T; pending: boolean }> {
  const response = await fetch(`/api/v2${path}`, { method, credentials: 'same-origin', cache: 'no-store',
    headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...(method !== 'GET' ? { 'X-CSRF-Token': csrf() } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}) });
  const result = await response.json();
  if (!response.ok || result.status === 'error') {
    const messages: Record<string, string> = { unauthorized: '登录已失效，请重新登录。', version_conflict: '信息已发生变化，请刷新后再试。', privacy_changed: '记忆设置或授权已变化，请刷新后再试。', overloaded: '当前处理繁忙，请稍后再试。', quota_exceeded: '本次操作超过处理上限，请稍后再试。', not_found: '这项内容已不可用，请刷新查看。', unsupported_capability: '这项功能尚未启用。', invalid_model_output: '内容格式无法处理，请检查后重试。' };
    throw new Error(messages[result.error?.code] ?? '记忆服务暂不可用，请稍后重试。');
  }
  return { data: result.data as T, pending: result.status === 'partial' };
}

export function MemoryPanel({ disabled, onConnect }: { disabled: boolean; onConnect: (credentials: PrivateCredentials) => Promise<void> }) {
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [configured, setConfigured] = useState(true);
  const [preferences, setPreferences] = useState<Preferences | null>(null);
  const [items, setItems] = useState<Memory[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<Memory | null>(null);
  const [correction, setCorrection] = useState('');
  const [deleting, setDeleting] = useState<Memory | null>(null);
  const [mode, setMode] = useState<'memories' | 'timeline' | 'loops' | 'data' | 'transcripts' | 'entities' | 'reflections'>('memories');
  const [reflections, setReflections] = useState<Reflection[]>([]);
  const [reflectionEdit, setReflectionEdit] = useState<Reflection | null>(null);
  const [reflectionText, setReflectionText] = useState('');
  const [reflectionBoundary, setReflectionBoundary] = useState(false);
  const [reflectionNegative, setReflectionNegative] = useState(false);
  const [people, setPeople] = useState<Person[]>([]);
  const [personEdit, setPersonEdit] = useState<Person | null>(null);
  const [personDelete, setPersonDelete] = useState<Person | null>(null);
  const [personName, setPersonName] = useState('');
  const [personAliases, setPersonAliases] = useState('');
  const [loops, setLoops] = useState<Loop[]>([]);
  const [exported, setExported] = useState<ExportView | null>(null);
  const [imported, setImported] = useState<ExportView | null>(null);
  const [rawExport, setRawExport] = useState(false);
  const [importConfirmed, setImportConfirmed] = useState(false);
  const [transcripts, setTranscripts] = useState<Transcript[]>([]);
  const [transcriptEdit, setTranscriptEdit] = useState<Transcript | null>(null);
  const [deletionJob, setDeletionJob] = useState<string | null>(null);
  const [remember, setRemember] = useState(false);
  async function load(append = false, nextMode = mode) {
    if (nextMode === 'data') { setCursor(null); return; }
    if (nextMode === 'transcripts') {
      const saved = sessionStorage.getItem(savedSessionKey);
      if (!saved) { setTranscripts([]); setCursor(null); return; }
      const { id } = JSON.parse(saved) as { id: string };
      const result = await api<Transcript[]>(`/sessions/${encodeURIComponent(id)}/transcripts`);
      setTranscripts(result.data); setCursor(null); return;
    }
    const resource = nextMode === 'loops' ? 'open-loops' : nextMode;
    const path = `/${resource}?limit=20${append && cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`;
    if (nextMode === 'loops') {
      const result = await api<{ items: Loop[]; next_cursor: string | null }>(path);
      setLoops(previous => append ? [...previous, ...result.data.items] : result.data.items);
      setCursor(result.data.next_cursor); return;
    }
    if (nextMode === 'entities') {
      const result = await api<{ items: Person[]; next_cursor: string | null }>(path);
      setPeople(previous => append ? [...previous, ...result.data.items] : result.data.items);
      setCursor(result.data.next_cursor); return;
    }
    if (nextMode === 'reflections') {
      const result = await api<{ items: Reflection[]; next_cursor: string | null }>(path);
      setReflections(previous => append ? [...previous, ...result.data.items] : result.data.items);
      setCursor(result.data.next_cursor); return;
    }
    if (nextMode === 'timeline') {
      const result = await api<{ items: { memory_id: string; version: number; title: string; period: Memory['valid_time']; status: string }[]; next_cursor: string | null }>(path);
      const mapped: Memory[] = result.data.items.map(item => ({ memory_id: item.memory_id, version: item.version, text: item.title, valid_time: item.period,
        kind: 'episode', state: item.status === 'uncertain' ? 'uncertain' : 'historical', pinned: false, label: item.status, evidence: [] }));
      setItems(previous => append ? [...previous, ...mapped] : mapped); setCursor(result.data.next_cursor); return;
    }
    const result = await api<{ items: Memory[]; next_cursor: string | null }>(path);
    setItems(previous => append ? [...previous, ...result.data.items] : result.data.items);
    setCursor(result.data.next_cursor);
  }
  useEffect(() => {
    let active = true;
    void api<Identity>('/me').then(async result => {
      if (!active) return;
      setIdentity(result.data);
      if (result.data.state === 'account') {
        const settings = await api<Preferences>('/memory-preferences');
        if (!active) return;
        setPreferences(settings.data);
        const saved = sessionStorage.getItem(savedSessionKey);
        if (saved) { try { setRemember(JSON.parse(saved).memory_enabled === true && settings.data.long_term_memory_enabled); } catch { sessionStorage.removeItem(savedSessionKey); } }
        await load();
      }
    }).catch(() => { if (active) setConfigured(false); });
    return () => { active = false; };
  }, []);
  async function work(action: () => Promise<void>) {
    setBusy(true); setError('');
    try { await action(); } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  if (!configured) return <section className="memory-panel"><h2>我的记忆</h2><p>长期记忆服务正在配置，当前对话沿用演示模式。</p></section>;
  if (!identity) return <section className="memory-panel" aria-busy="true">正在读取账号…</section>;
  if (identity.state === 'anonymous') return <section className="memory-panel"><h2>让记忆跟随你的账号</h2><p>登录后可选择保存长期记忆，在不同设备查看、纠正或删除。</p><a className="memory-login" href="/api/v2/auth/login">登录账号</a></section>;
  async function savePreferences(next: Preferences) {
    const { preference_version, ...settings } = next;
    const result = await api<Preferences>('/memory-preferences', 'PUT', { ...settings, expected_version: preference_version, idempotency_key: crypto.randomUUID() });
    setPreferences(result.data); setNotice('记忆设置已保存。');
  }
  return <section className="memory-panel" aria-label="我的记忆">
    <div className="memory-heading"><h2>我的记忆</h2><div><button disabled={busy} onClick={() => void work(() => load())}>刷新</button><button disabled={busy || disabled} onClick={() => void work(async () => { await api('/auth/logout', 'POST'); sessionStorage.removeItem(savedSessionKey); setItems([]); setPreferences(null); setIdentity({ state: 'anonymous', companion_ids: identity.companion_ids }); })}>退出账号</button></div></div>
    {preferences && <div className="memory-preferences">
      <label><input type="checkbox" checked={preferences.long_term_memory_enabled} disabled={busy || disabled} onChange={event => void work(() => savePreferences({ ...preferences, long_term_memory_enabled: event.target.checked }))} />允许保存长期记忆</label>
      <label><input type="checkbox" checked={preferences.retain_evidence_capsules} disabled={busy} onChange={event => void work(() => savePreferences({ ...preferences, retain_evidence_capsules: event.target.checked }))} />允许长期保留必要的来源片段</label>
      <label>完整转写保留天数<input type="number" min={1} max={3653} defaultValue={preferences.raw_transcript_days} disabled={busy} onBlur={event => { const days = Number(event.target.value); if (Number.isInteger(days) && days >= 1 && days <= 3653 && days !== preferences.raw_transcript_days) void work(() => savePreferences({ ...preferences, raw_transcript_days: days })); }} /></label>
      <label><input type="checkbox" checked={preferences.allow_proactive_followup} disabled={busy} onChange={event => void work(() => savePreferences({ ...preferences, allow_proactive_followup: event.target.checked }))} />允许在对话中关心未结束的事项</label>
      <p>完整转写保留 {preferences.raw_transcript_days} 天。来源片段和结构化事实分开管理。</p>
      <label><input type="checkbox" checked={remember} disabled={busy || disabled || !preferences.long_term_memory_enabled} onChange={event => setRemember(event.target.checked)} />本次对话使用记忆</label>
      <button disabled={busy || disabled} onClick={() => void work(async () => {
        const capabilities = { command_versions: [1, 2], action_cancel: true, action_completed_receipt: true, playback_granularity: 'none', media_clock_supported: false };
        const enabled = remember && preferences.long_term_memory_enabled;
        type SessionView = { session_id: string; memory_enabled: boolean; rtc: Omit<PrivateCredentials, 'session_id'> | null };
        let saved: { id: string; memory_enabled: boolean } | null = null;
        try { saved = JSON.parse(sessionStorage.getItem(savedSessionKey) ?? 'null'); } catch { sessionStorage.removeItem(savedSessionKey); }
        let result: { data: SessionView; pending: boolean } | undefined;
        if (saved && saved.memory_enabled === enabled) {
          try { result = await api<SessionView>(`/sessions/${encodeURIComponent(saved.id)}/resume`, 'POST', { capabilities, idempotency_key: crypto.randomUUID() }); }
          catch { sessionStorage.removeItem(savedSessionKey); }
        } else if (saved) { await endPrivateSession(saved.id).catch(() => {}); }
        result ??= await api<SessionView>('/sessions', 'POST', { companion_id: identity.companion_ids[0], memory_enabled: enabled, capabilities, idempotency_key: crypto.randomUUID() });
        sessionStorage.setItem(savedSessionKey, JSON.stringify({ id: result.data.session_id, memory_enabled: result.data.memory_enabled }));
        if (!result.data.rtc) { setNotice('账号会话已建立，私人语音尚未启用。'); await endPrivateSession(result.data.session_id); return; }
        await onConnect({ ...result.data.rtc, session_id: result.data.session_id });
      })}>开始账号对话</button>
    </div>}
    <div className="memory-tabs">{([['memories', '事实与偏好'], ['entities', '重要人物'], ['reflections', '待确认推测'], ['timeline', '人生时间线'], ['loops', '未结束的事项'], ['transcripts', '原话与纠正'], ['data', '导出与恢复']] as const).map(([tab, label]) => <button key={tab} disabled={busy} aria-pressed={mode === tab} onClick={() => { setMode(tab); setEditing(null); setDeleting(null); void work(() => load(false, tab)); }}>{label}</button>)}</div>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {(mode === 'memories' || mode === 'timeline') && !items.length && <p>还没有形成长期记忆。来源保存后仍需整理，记忆会在这里显示。</p>}
    {(mode === 'memories' || mode === 'timeline') && <ul className="memory-list">{[...items].sort((a, b) => mode === 'timeline' ? (a.valid_time.start ?? '').localeCompare(b.valid_time.start ?? '') : 0).map(item => <li key={`${item.memory_id}:${item.version}`}>
      <div><span className="memory-label">{item.state === 'uncertain' ? '待核验' : item.label === 'hypothesis' ? '推测' : item.state === 'historical' ? '过去阶段' : '你提供的信息'}</span>{mode === 'timeline' && <time>{item.valid_time.start ? new Date(item.valid_time.start).toLocaleDateString('zh-CN') : '时间未确定'}</time>}</div>
      <p>{item.text}</p><div className="memory-actions">{mode === 'memories' && <button disabled={busy} onClick={() => void work(async () => { await api(`/memories/${item.memory_id}/pin`, 'PATCH', { expected_version: item.version, pinned: !item.pinned, idempotency_key: crypto.randomUUID() }); await load(); })}>{item.pinned ? '取消置顶' : '置顶'}</button>}<button disabled={busy} onClick={() => { setEditing(item); setCorrection(item.text); }}>纠正</button><button disabled={busy} onClick={() => setDeleting(item)}>删除</button><button disabled={busy} onClick={() => void work(async () => { const result = await api<Memory>(`/memories/${item.memory_id}/evidence`); const quotes = result.data.evidence.filter(e => e.quote).map(e => e.quote); setNotice(quotes.length ? quotes.join('；') : '完整原文或片段已不可用，目前只保留来源元数据。'); })}>查看来源</button></div>
    </li>)}</ul>}
    {mode === 'loops' && <ul className="memory-list">{loops.map(loop => <li key={loop.loop_id}><p>{loop.title}</p><p>{({ planned: '计划中', pending_outcome: '等待你告诉我结果', completed: '已完成', cancelled: '已取消', rescheduled: '已改期' } as Record<string, string>)[loop.status]}</p>{(['completed', 'cancelled', 'planned'] as const).map(status => <button key={status} disabled={busy} onClick={() => void work(async () => { await api(`/open-loops/${loop.loop_id}`, 'PATCH', { expected_version: loop.version, status, event_time: null, user_allows_followup: loop.user_allows_followup, idempotency_key: crypto.randomUUID() }); await load(); })}>{status === 'completed' ? '标记完成' : status === 'cancelled' ? '取消事项' : '恢复计划'}</button>)}<label><input type="checkbox" checked={loop.user_allows_followup} disabled={busy || !preferences?.allow_proactive_followup} onChange={event => void work(async () => { await api(`/open-loops/${loop.loop_id}`, 'PATCH', { expected_version: loop.version, status: loop.status === 'pending_outcome' ? 'planned' : loop.status, event_time: null, user_allows_followup: event.target.checked, idempotency_key: crypto.randomUUID() }); await load(); })} />可以在对话中问起这件事</label></li>)}{!loops.length && <li>还没有待跟进的事项。</li>}</ul>}
    {mode === 'reflections' && <div><p>这些推测来自过往信息，需要你审核。确认时请用自己的话填写希望记住的偏好或互动边界。</p><ul className="memory-list">{reflections.map(item => <li key={item.reflection_id}><p>{item.text}</p><p>{item.state === 'needs_confirmation' ? '待你确认的推测' : item.state === 'confirmed' ? '已按你的表述保存' : '已拒绝'}</p>{item.state === 'needs_confirmation' && <><button disabled={busy} onClick={() => { setReflectionEdit(item); setReflectionText(''); }}>填写我的确认</button><button disabled={busy} onClick={() => void work(async () => { await api(`/reflections/${item.reflection_id}`, 'PATCH', { expected_version: item.version, action: 'reject', confirmation_text: '', predicate: 'preference', polarity: 'positive', idempotency_key: crypto.randomUUID() }); await load(); })}>拒绝这条推测</button></>}</li>)}</ul>{!reflections.length && <p>当前没有需要审核的推测。</p>}{reflectionEdit && <form className="memory-edit" onSubmit={event => { event.preventDefault(); void work(async () => { await api(`/reflections/${reflectionEdit.reflection_id}`, 'PATCH', { expected_version: reflectionEdit.version, action: 'confirm', confirmation_text: reflectionText, predicate: reflectionBoundary ? 'interaction_boundary' : 'preference', polarity: reflectionNegative ? 'negative' : 'positive', idempotency_key: crypto.randomUUID() }); setReflectionEdit(null); setNotice('已按你填写的表述保存。'); await load(); }); }}><label>我希望记住的表述<textarea required maxLength={2048} value={reflectionText} onChange={event => setReflectionText(event.target.value)} /></label><label><input type="checkbox" checked={reflectionBoundary} onChange={event => setReflectionBoundary(event.target.checked)} />这是互动边界</label><label><input type="checkbox" checked={reflectionNegative} onChange={event => setReflectionNegative(event.target.checked)} />这是不喜欢或不希望的事项</label><button disabled={busy || !reflectionText.trim()}>确认我的表述</button><button type="button" onClick={() => setReflectionEdit(null)}>取消</button></form>}</div>}
    {mode === 'transcripts' && <div><p>这里只显示仍在保留期内的本次账号对话原话。</p>{transcripts.map(item => <div key={item.event_id}><p>{item.text}</p><button disabled={busy} onClick={() => { setTranscriptEdit(item); setCorrection(item.text); }}>纠正原话</button></div>)}{!transcripts.length && <p>当前没有可查看的账号对话原话。</p>}</div>}
    {mode === 'entities' && <div><p>确认你提到的是谁。同名人物请填写不同称呼，例如“同事小林”和“室友小林”。</p><ul className="memory-list">{people.map(person => <li key={person.entity_id}><p>{person.canonical_name}</p><p>{person.aliases.join('、')}</p><button disabled={busy} onClick={() => { setPersonEdit(person); setPersonName(person.canonical_name); setPersonAliases(person.aliases.join('、')); }}>修改人物称呼</button><button disabled={busy} onClick={() => setPersonDelete(person)}>删除人物</button></li>)}</ul><form className="memory-edit" onSubmit={event => { event.preventDefault(); void work(async () => { await api(personEdit ? `/entities/${encodeURIComponent(personEdit.entity_id)}` : '/entities', personEdit ? 'PATCH' : 'POST', { canonical_name: personName.trim(), aliases: personAliases.split(/[，,、]/).map(value => value.trim()).filter(Boolean), expected_version: personEdit?.version ?? null, idempotency_key: crypto.randomUUID() }); setPersonEdit(null); setPersonName(''); setPersonAliases(''); setNotice('人物称呼已确认，相关待确认原话会继续整理。'); await load(); }); }}><label>人物姓名<input required maxLength={128} disabled={busy} value={personName} onChange={event => setPersonName(event.target.value)} /></label><label>日常称呼，用顿号分隔<input maxLength={2048} disabled={busy} value={personAliases} onChange={event => setPersonAliases(event.target.value)} /></label><button disabled={busy || !personName.trim() || !preferences?.long_term_memory_enabled}>{personEdit ? '保存人物称呼' : '确认新人物'}</button>{personEdit && <button type="button" onClick={() => { setPersonEdit(null); setPersonName(''); setPersonAliases(''); }}>取消修改</button>}</form></div>}
    {transcriptEdit && <form className="memory-edit" onSubmit={event => { event.preventDefault(); void work(async () => { await api('/transcripts', 'PATCH', { source_event_id: transcriptEdit.event_id, expected_source_revision: transcriptEdit.source_revision, corrected_text: correction, idempotency_key: crypto.randomUUID() }); setTranscriptEdit(null); setNotice('新原话已保存，旧来源及相关记忆已停止使用，正在重新整理。'); await load(); }); }}><label>正确的原话<textarea value={correction} maxLength={16000} onChange={event => setCorrection(event.target.value)} /></label><button disabled={busy || !correction.trim()}>保存原话纠正</button><button type="button" onClick={() => setTranscriptEdit(null)}>取消</button></form>}
    {mode === 'data' && <div className="memory-data"><p>导出文件包含你的个人信息，请妥善保管。下载授权有效期为 15 分钟。</p><label><input type="checkbox" checked={rawExport} onChange={event => setRawExport(event.target.checked)} />同时导出仍合法保留的完整原话</label><button disabled={busy} onClick={() => void work(async () => { const result = await api<ExportView>('/exports', 'POST', { include_source_capsules: true, include_raw_transcript: rawExport, confirmed: true, idempotency_key: crypto.randomUUID() }); setExported(result.data); })}>创建导出文件</button>{exported?.download_handle && <a href={`/api/v2/exports/${encodeURIComponent(exported.export_id)}/download`}>下载我的记忆</a>}<label>选择此前导出的文件<input type="file" accept=".json,application/json" disabled={busy} onChange={event => { const file = event.target.files?.[0]; if (!file) return; void work(async () => { if (file.size > 9 * 1024 * 1024) throw new Error('导入文件过大。'); const result = await fetch('/api/v2/imports/upload', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() }, body: file }); const view = await result.json(); if (!result.ok) throw new Error('文件校验失败，请选择此前导出的原始文件。'); setImported(view.data); setImportConfirmed(false); }); }} /></label>{imported && <><button disabled={busy} onClick={() => void work(async () => { const result = await api<ExportView>('/imports/dry-run', 'POST', { uploaded_package_handle: imported.export_id, dry_run: true, confirmed: false, idempotency_key: crypto.randomUUID() }); setImported(result.data); })}>检查恢复内容</button>{imported.conflicts.map((conflict, index) => <p key={index} role="alert">{conflict}</p>)}{imported.state === 'verified' && <><label><input type="checkbox" checked={importConfirmed} onChange={event => setImportConfirmed(event.target.checked)} />我确认将检查通过的内容恢复到当前账号</label><button disabled={busy || !importConfirmed} onClick={() => void work(async () => { const result = await api<ExportView>('/imports', 'POST', { uploaded_package_handle: imported.export_id, dry_run: false, confirmed: true, idempotency_key: crypto.randomUUID() }); setImported(result.data); setNotice(result.data.state === 'imported' ? '恢复已完成。' : '恢复内容需要进一步确认。'); })}>确认恢复</button></>}{imported.state === 'imported' && <p>恢复已完成。</p>}</>}</div>}
    {deletionJob && <button disabled={busy} onClick={() => void work(async () => { const result = await api<{ physical_cleanup_complete: boolean }>(`/deletion-jobs/${deletionJob}`); setNotice(result.data.physical_cleanup_complete ? '删除清理已完成。' : '内容已停止提供；物理文件和备份清理仍在核验。'); })}>查看最近删除进度</button>}
    {cursor && <button disabled={busy} onClick={() => void work(() => load(true))}>加载更多</button>}
    {editing && <form className="memory-edit" onSubmit={event => { event.preventDefault(); void work(async () => { const result = await api<Memory>(`/memories/${editing.memory_id}`, 'PATCH', { expected_version: editing.version, correction_text: correction, idempotency_key: crypto.randomUUID() }); setNotice(result.pending ? '纠正已记录，旧信息已标记待核验。' : '这条记忆已更新。'); setEditing(null); await load(); }); }}><label>更正后的信息<textarea value={correction} maxLength={16000} onChange={event => setCorrection(event.target.value)} /></label><button disabled={busy || !correction.trim()}>保存纠正</button><button type="button" onClick={() => setEditing(null)}>取消</button></form>}
    {personDelete && <div className="memory-delete" role="alertdialog" aria-label="确认删除人物"><p>删除“{personDelete.canonical_name}”、关联记忆及其来源原话？同一段原话中的其他信息也会停止提供。</p><button disabled={busy} onClick={() => void work(async () => { const result = await api<{ deletion_job_id: string }>(`/entities/${encodeURIComponent(personDelete.entity_id)}`, 'DELETE', { expected_version: personDelete.version, confirmed: true, delete_related_derivatives: true, idempotency_key: crypto.randomUUID() }); setDeletionJob(result.data.deletion_job_id); setPersonDelete(null); setNotice('人物及相关内容已停止提供。'); await load(); })}>确认删除人物</button><button disabled={busy} onClick={() => setPersonDelete(null)}>取消</button></div>}
    {deleting && <div className="memory-delete" role="alertdialog" aria-label="确认删除记忆"><p>删除“{deleting.text}”及其派生内容？相关完整原文也会停止提供，以免旧任务再次提取。</p><button disabled={busy} onClick={() => void work(async () => { const result = await api<{ deletion_job_id: string }>(`/memories/${deleting.memory_id}`, 'DELETE', { expected_version: deleting.version, confirmed: true, delete_related_derivatives: true, idempotency_key: crypto.randomUUID() }); setDeletionJob(result.data.deletion_job_id); setNotice('记忆已停止提供，物理清理进度可在删除任务中查看。'); setDeleting(null); await load(); })}>确认删除</button><button disabled={busy} onClick={() => setDeleting(null)}>取消</button></div>}
  </section>;
}

export async function endPrivateSession(id: string) {
  sessionStorage.removeItem(savedSessionKey);
  await api(`/sessions/${id}/end`, 'POST', { reason: 'user_end', idempotency_key: crypto.randomUUID() });
}
