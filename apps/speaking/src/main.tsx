import '../../demo/src/browser-crypto';
import { StrictMode, useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { AvatarClient, type Credentials, type Transcript } from '@avatarweave/core';
import { AvatarView, type AvatarRenderer } from '@avatarweave/react';
import { api, asset, publicData, type Practice, type Profile, type Review, type Scenario } from './api';
import './style.css';

const labels: Record<string, string> = {
  disconnected: '连接已断开，可结束并保存练习', connecting: '正在连接伙伴…',
  connected: '等待伙伴加入…', listening: '轮到你了，慢慢说',
  thinking: 'Alex 正在想一想…', speaking: 'Alex 正在说话…',
};

function App() {
  const [page, setPage] = useState<'home' | 'practice' | 'review' | 'history'>('home');
  const [scenario, setScenario] = useState<Scenario>();
  const [profile, setProfile] = useState<Profile>();
  const [practice, setPractice] = useState<Practice>();
  const [records, setRecords] = useState<Practice[]>([]);
  const [review, setReview] = useState<Review>();
  const [messages, setMessages] = useState<Transcript[]>([]);
  const [state, setState] = useState('disconnected');
  const [ready, setReady] = useState(false);
  const [audioReady, setAudioReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [mic, setMic] = useState(false);
  const [error, setError] = useState('');
  const [text, setText] = useState('');
  const [seconds, setSeconds] = useState(360);
  const [helpBusy, setHelpBusy] = useState(false);
  const avatar = useRef<AvatarRenderer | null>(null);
  const audio = useRef<MediaStream | null>(null);
  const client = useRef<AvatarClient | null>(null);
  const livePractice = useRef<Practice | undefined>(undefined);
  const mounted = useRef(true);
  const bottom = useRef<HTMLDivElement>(null);
  const reportPending = useRef(false);
  const starting = useRef(false);
  const closingId = useRef<string | undefined>(undefined);
  const finalizingId = useRef<string | undefined>(undefined);
  const readyCallback = useCallback((value: AvatarRenderer | null) => {
    avatar.current = value; setReady(!!value);
    value?.setAudio(audio.current);
  }, []);
  const fail = useCallback((reason: unknown) => {
    if (mounted.current) setError(reason instanceof Error ? reason.message : String(reason));
  }, []);

  useEffect(() => {
    mounted.current = true;
    void Promise.all([
      publicData<Scenario>('/api/practice/scenario'),
      publicData<{ default_avatar: string; avatars: (Profile & { id: string })[] }>('/api/avatars'),
    ]).then(([scene, catalog]) => {
      if (!mounted.current) return;
      setScenario(scene);
      setProfile(catalog.avatars.find(p => p.id === catalog.default_avatar));
    }).catch(fail);
    return () => {
      mounted.current = false;
      void client.current?.dispose();
    };
  }, [fail]);

  useEffect(() => { bottom.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }, [messages]);

  const loadReport = useCallback(async (id: string) => {
    if (reportPending.current) return;
    reportPending.current = true;
    setBusy(true); setError('');
    try {
      const result = await api<Review>(`/sessions/${id}/report`, 'POST');
      if (mounted.current) { setReview(result); setPage('review'); }
    } catch (e) { fail(e); }
    finally { reportPending.current = false; if (mounted.current) setBusy(false); }
  }, [fail]);

  const settlePractice = useCallback(async (item: Practice) => {
    if (finalizingId.current === item.id) return;
    finalizingId.current = item.id;
    closingId.current = item.id;
    setBusy(true);
    try {
      // Detach immediately so finish and a concurrent status poll share one close.
      const previous = client.current;
      client.current = null;
      await previous?.dispose();
      if (!mounted.current) return;
      setMic(false); setPage('review');
      if (item.report) setReview(item.report);
      else await loadReport(item.id);
    } catch (e) { fail(e); }
    finally {
      finalizingId.current = undefined;
      if (mounted.current) setBusy(false);
    }
  }, [loadReport, fail]);

  useEffect(() => {
    if (page !== 'practice' || !practice) return;
    let disposed = false;
    let polling = false;
    let ticks = 0;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const item = await api<Practice>(`/sessions/${practice.id}`);
        if (disposed) return;
        // A GET started before finish can arrive after its POST; never reopen the UI.
        if (closingId.current === item.id && item.status === 'active') return;
        setPractice(item); livePractice.current = item;
        if (['ended', 'report_failed', 'complete'].includes(item.status)) {
          await settlePractice(item);
        }
      } catch (e) { if (!disposed) fail(e); }
      finally { polling = false; }
    };
    const timer = setInterval(() => {
      setSeconds(Math.max(0, Math.ceil((new Date(practice.expires_at).getTime() - Date.now()) / 1000)));
      if (++ticks % 2 === 0) void poll();
    }, 1000);
    return () => { disposed = true; clearInterval(timer); };
  }, [page, practice?.id, settlePractice, fail]);

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (livePractice.current?.status === 'active' || livePractice.current?.status === 'ending') {
        event.preventDefault(); event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, []);

  async function start() {
    if (starting.current || !scenario) return;
    starting.current = true;
    closingId.current = undefined;
    setBusy(true); setError(''); setReview(undefined); setMessages([]); setAudioReady(false);
    let created: Practice | undefined;
    const next = new AvatarClient({
      renderer: { setAudio: stream => { audio.current = stream; avatar.current?.setAudio(stream); }, execute: command => avatar.current?.execute(command) },
      onError: fail,
      onConnectionMilestone: stage => { if (stage === 'first_audio_playback') setAudioReady(true); },
      onState: value => { if (mounted.current) { setState(value); if (value === 'disconnected') setMic(false); } },
      onTranscript: message => setMessages(previous => {
        const found = previous.some(m => m.id === message.id && m.participantIdentity === message.participantIdentity);
        return found ? previous.map(m => m.id === message.id && m.participantIdentity === message.participantIdentity ? message : m) : [...previous, message];
      }),
    });
    client.current = next;
    try {
      // Start browser audio and request permission inside the click gesture.
      let permission: Promise<MediaStream> | undefined;
      if (scenario.mode !== 'mock') {
        if (!navigator.mediaDevices?.getUserMedia) throw new Error('请使用 HTTPS 地址，并在系统浏览器中打开以启用麦克风。');
        permission = navigator.mediaDevices.getUserMedia({ audio: true });
      }
      const [, stream] = await Promise.all([next.room.startAudio(), permission]);
      stream?.getTracks().forEach(track => track.stop());
      created = await api<Practice & { credentials: Credentials }>('/sessions', 'POST');
      livePractice.current = created; setPractice(created); setPage('practice'); setSeconds(360);
      await next.connect((created as Practice & { credentials: Credentials }).credentials);
      if (scenario.mode !== 'mock') { await next.setMicrophone(true); setMic(true); }
    } catch (e) {
      fail(e);
      await next.dispose(); client.current = null;
      if (created) {
        try { const item = await api<Practice>(`/sessions/${created.id}/finish`, 'POST'); setPractice(item); livePractice.current = item; }
        catch (reason) { fail(reason); }
      }
    } finally { starting.current = false; setBusy(false); }
  }

  async function finish() {
    if (!practice || closingId.current === practice.id) return;
    const previousStatus = practice.status;
    closingId.current = practice.id;
    setPractice({ ...practice, status: 'ending' });
    setBusy(true); setError('');
    // Stop capture now; keep signaling connected until the worker confirms storage.
    const muted = client.current?.setMicrophone(false).catch(() => undefined);
    setMic(false);
    try {
      const item = await api<Practice>(`/sessions/${practice.id}/finish`, 'POST');
      setPractice(item); livePractice.current = item;
      await muted;
      if (['ended', 'report_failed', 'complete'].includes(item.status)) await settlePractice(item);
    } catch (e) {
      closingId.current = undefined;
      setPractice(current => current?.id === practice.id && current.status === 'ending'
        ? { ...current, status: previousStatus } : current);
      fail(e);
    }
    finally { setBusy(false); }
  }

  async function help(kind: 'meaning' | 'hint') {
    if (!practice || !client.current) return;
    setHelpBusy(true); setError('');
    try {
      const result = await api<{ message: string }>(`/sessions/${practice.id}/help`, 'POST', { kind });
      await client.current.sendText(result.message);
    } catch (e) { fail(e); }
    finally { setHelpBusy(false); }
  }

  async function toggleMic() {
    if (!client.current) return;
    try { await client.current.setMicrophone(!mic); setMic(!mic); } catch (e) { fail(e); }
  }

  async function send(event: React.FormEvent) {
    event.preventDefault();
    if (!client.current || !text.trim()) return;
    try { await client.current.sendText(text.trim()); setText(''); } catch (e) { fail(e); }
  }

  async function showHistory() {
    setBusy(true); setError('');
    try { setRecords(await api<Practice[]>('/sessions')); setPage('history'); }
    catch (e) { fail(e); }
    finally { setBusy(false); }
  }

  async function openRecord(item: Practice) {
    setPractice(item); livePractice.current = item; setReview(item.report ?? undefined);
    if (item.report) setPage('review');
    else if (['ended', 'report_failed', 'reporting'].includes(item.status)) {
      setPage('review'); await loadReport(item.id);
    } else { setPage('practice'); setState('disconnected'); setMessages([]); }
  }

  const done = practice?.goals.map(g => g.id) ?? [];
  const usable = practice?.status === 'active' && !['disconnected', 'connecting', 'connected'].includes(state);
  const statusText = practice?.status === 'ending' ? '正在保存对话，请稍候…' : labels[state] ?? state;
  const visibleMessages = messages.filter(m => !m.text.startsWith('[练习帮助：'));

  return <div className="shell">
    <header className="topbar"><a href="/speaking/" onClick={e => { e.preventDefault(); if (page !== 'practice') setPage('home'); }} className="brand"><span className="brand-icon">✳</span> 开口旅行<span className="beta">BETA</span></a>{page !== 'practice' && <button className="text-button" disabled={busy} onClick={showHistory}>练习记录 ↗</button>}</header>
    {error && <div className="error" role="alert"><span>{error}</span><button aria-label="关闭提示" onClick={() => setError('')}>×</button></div>}
    {page === 'home' && <main className="home">
      <section className="intro"><div className="eyebrow">A LITTLE PRACTICE. A BIGGER WORLD.</div><h1>下一站，<br /><span>自信开口。</span></h1><p>和卡通伙伴练一次旅行中的小对话。<br />不用说得完美，先把想法说出来。</p><div className="pills"><span>◷ 约 5 分钟</span><span>初级英语</span><span>中文帮助</span></div></section>
      <section className="postcard"><div className="postcard-top"><span>YOUR FIRST STOP</span><span>01 / HOTEL</span></div><div className="hotel-sign">WILLOW<br /><small>HOTEL & A LITTLE COURAGE</small></div><div className="avatar-scene">{profile && <AvatarView coreUrl={asset(profile.assets.core_url)!} modelUrl={asset(profile.assets.model_url)!} motionSyncUrl={asset(profile.assets.motion_sync_url)} draggable={false} onReady={readyCallback} onError={fail} />}</div><span className="hello-bubble">Welcome!<br /><small>我是你的练习伙伴 Alex</small></span><div className="postcard-bottom"><span>CHECK IN TO YOUR NEXT ADVENTURE</span><span>✦</span></div></section>
      <section className="task-card"><div className="eyebrow">TODAY'S SMALL ADVENTURE</div><h2>入住一家海外酒店</h2><p>{scenario?.brief ?? '正在准备你的练习…'}</p><ol className="task-list">{scenario?.goals.map((goal, i) => <li key={goal.id}><span>{String(i + 1).padStart(2, '0')}</span>{goal.label}</li>)}</ol><div className="brief-note">预订姓名 <b>Alex Chen</b> · 住宿 <b>2 晚</b></div><button className="primary start" disabled={busy || !scenario || !ready} onClick={start}>{busy ? '正在准备…' : !ready ? '伙伴正在就位…' : '开始我的第一次练习'} <span>↗</span></button><p className="privacy">{scenario?.mode === 'mock' ? '当前为文字演示，语音练习需配置模型服务。' : '开始时将申请麦克风权限；语音会交由模型服务处理。'}<br />保存对话文字用于反馈；本产品不保存原始录音。试用每天 5 次。</p></section>
    </main>}
    {page === 'practice' && <main className="practice-page">
      <div className="practice-heading"><div><div className="eyebrow">HOTEL CHECK-IN</div><h1>和 Alex 练习入住</h1></div><span className="timer">{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, '0')}</span></div>
      <div className="practice-layout"><section className="practice-stage" data-audio-ready={audioReady}><div className="stage-caption">WILLOW HOTEL <span>前台 · Alex</span></div><div className="practice-avatar">{profile && <AvatarView coreUrl={asset(profile.assets.core_url)!} modelUrl={asset(profile.assets.model_url)!} motionSyncUrl={asset(profile.assets.motion_sync_url)} draggable={false} onReady={readyCallback} onError={fail} />}</div><div className="state"><i className={usable ? 'online' : ''} />{statusText}</div></section>
      <section className="conversation"><div className="progress-label">今天的三个小目标 <b>{done.length}/3</b></div><ul className="goal-list">{scenario?.goals.map(goal => <li key={goal.id} className={done.includes(goal.id) ? 'done' : ''}><span>{done.includes(goal.id) ? '✓' : '○'}</span>{goal.label}</li>)}</ul>
        <div className="transcripts" aria-live="polite">{!visibleMessages.length && <p className="empty">{scenario?.mode === 'mock' ? '文字演示模式：在下方输入一句英语开始。' : 'Alex 会先向你问好。放轻松，慢慢说。'}</p>}{visibleMessages.map(message => <div className={`bubble ${message.role}`} key={`${message.participantIdentity}:${message.id}`}><small>{message.role === 'user' ? '你' : 'Alex'}</small><p>{message.text}{!message.final && <span className="cursor">▍</span>}</p></div>)}<div ref={bottom} /></div>
        <div className="help-row"><button disabled={!usable || helpBusy || busy} onClick={() => help('meaning')}>这句话什么意思？</button><button disabled={!usable || helpBusy || busy} onClick={() => help('hint')}>给我一点提示 ✧</button></div>
        <form className="composer" onSubmit={send}><input aria-label="输入英语消息" value={text} onChange={e => setText(e.target.value)} maxLength={1000} disabled={!usable || busy} placeholder="也可以输入英语…" /><button aria-label="发送" disabled={!usable || busy || !text.trim()}>↑</button></form>
        <div className="call-controls"><button className={`mic ${mic ? 'enabled' : ''}`} disabled={!usable || busy || scenario?.mode === 'mock'} onClick={toggleMic}>{mic ? '麦克风已开启' : '开启麦克风'}</button><button className="primary" disabled={busy || practice?.status === 'ending'} onClick={finish}>{busy || practice?.status === 'ending' ? '正在保存…' : done.length === 3 ? '完成练习，看看反馈 ↗' : '结束并查看反馈'}</button></div>
      </section></div><p className="fineprint">先表达，再完善。需要思考时可以停一下；每次练习最多 6 分钟。</p>
    </main>}
    {page === 'review' && <main className="review-page"><div className="eyebrow">ONE STEP FORWARD</div><h1>{review ? '你又多了一点开口的勇气。' : '正在整理这次练习…'}</h1>{review ? <><div className="review-stats"><span><b>{review.user_turns}</b>次表达</span><span><b>{review.goals.length}/3</b>个目标</span><span><b>{Math.floor(review.duration_seconds / 60)}:{String(review.duration_seconds % 60).padStart(2, '0')}</b>练习时长</span></div><section className="paper"><h2>这次练得怎么样</h2><p>{review.summary}</p>{review.mode === 'mock' && <p className="notice">文字演示记录，不代表真实口语评估。</p>}<ul className="goal-list">{scenario?.goals.map(goal => { const evidence = review.goals.find(g => g.id === goal.id); return <li className={evidence ? 'done' : ''} key={goal.id}><span>{evidence ? '✓' : '○'}</span><div>{goal.label}{evidence && <small className="evidence">你说：“{evidence.evidence}”</small>}</div></li>; })}</ul></section>{review.corrections.length > 0 && <section className="paper"><h2>下次，这样说更自然</h2>{review.corrections.map((correction, i) => <article className="correction" key={i}><small>你说</small><p>{correction.original}</p><small>可以试试</small><p className="improved">{correction.revised}</p><p className="reason">{correction.reason}</p></article>)}</section>}<section className="focus"><span>✦ 下次的小目标</span><p>{review.next_focus}</p></section><p className="fineprint">反馈基于对话转写，由 AI 生成，仅供练习参考；不包含发音评分。</p></> : <section className="paper"><p>{busy ? '正在根据你的原话准备建议，请稍候。' : '对话已保留，可以重新生成反馈。'}</p>{!busy && <button className="primary" onClick={() => practice && loadReport(practice.id)}>重试生成反馈</button>}</section>}<div className="review-actions"><button className="primary" disabled={busy} onClick={() => { livePractice.current = undefined; setPage('home'); }}>再练一次 ↗</button><button className="text-button" disabled={busy} onClick={showHistory}>查看练习记录</button></div></main>}
    {page === 'history' && <main className="history-page"><div className="eyebrow">YOUR LITTLE MILESTONES</div><h1>每一次开口，都算数。</h1><p className="muted">记录保存在当前浏览器的试用身份下。</p>{!records.length && <section className="paper"><p>你的第一段旅行对话，还在等你开启。</p></section>}<div className="records">{records.map(item => <button className="record" key={item.id} onClick={() => openRecord(item)} disabled={busy}><span className="record-icon">⌂</span><span><strong>酒店入住练习</strong><small>{new Date(item.created_at).toLocaleString('zh-CN')} · {item.goals.length}/3 个目标</small></span><span className="record-status">{item.report ? '查看反馈' : ['active', 'ending'].includes(item.status) ? '结束并保存' : '生成反馈'} ↗</span></button>)}</div><button className="primary" disabled={busy} onClick={() => setPage('home')}>开始新练习 ↗</button></main>}
    <footer className="footer">开口旅行 <span>让世界，离你近一点。</span></footer>
  </div>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
