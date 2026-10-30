import '../../demo/src/browser-crypto';
import { StrictMode, useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { flushSync } from 'react-dom';
import { AvatarClient, type Credentials, type Transcript } from '@avatarweave/core';
import { AvatarView, type AvatarRenderer } from '@avatarweave/react';
import { api, asset, publicData, ApiError, type Practice, type Profile, type Review, type Scenario, type Quota } from './api';
import './style.css';

const labels: Record<string, string> = {
  disconnected: '连接已断开，可结束并保存练习', connecting: '正在连接前台，请稍候',
  connected: '正在连接前台，请稍候', listening: '直接开口，我在听',
  thinking: '已收到，Alex 正在回答', speaking: '先听 Alex 说',
};
const noReplyMessage = '伙伴暂时没有回复，你可以再说一次或结束并保存。未收到回复的连接不扣次数。';

type ChatMessage = Transcript & { receivedAt?: number };
type PracticeDetail = Practice & { messages: { id: string; role: 'user' | 'assistant'; text: string }[] };
const canResume = (item: Practice) => item.status === 'active' && new Date(item.expires_at).getTime() > Date.now();
const messageTime = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });

function App() {
  const [page, setPage] = useState<'home' | 'practice' | 'review' | 'history'>('home');
  const [scenario, setScenario] = useState<Scenario>();
  const [quota, setQuota] = useState<Quota>();
  const [profile, setProfile] = useState<Profile>();
  const [practice, setPractice] = useState<Practice>();
  const [blockedPractice, setBlockedPractice] = useState<Practice>();
  const [sessionConflict, setSessionConflict] = useState(false);
  const [records, setRecords] = useState<Practice[]>([]);
  const [review, setReview] = useState<Review>();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [state, setState] = useState('disconnected');
  const [ready, setReady] = useState(false);
  const [audioReady, setAudioReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [mic, setMic] = useState(false);
  const [micBusy, setMicBusy] = useState(false);
  const [error, setError] = useState('');
  const [text, setText] = useState('');
  const [seconds, setSeconds] = useState(360);
  const [helpBusy, setHelpBusy] = useState(false);
  const [keyboard, setKeyboard] = useState(false);
  const [unread, setUnread] = useState(false);
  const [level, setLevel] = useState(0);
  const [hintStep, setHintStep] = useState(0);
  const [retryFocus, setRetryFocus] = useState('');
  const [samplePlaying, setSamplePlaying] = useState<string>();
  const [sampleAvailable, setSampleAvailable] = useState(false);
  const [goalNotice, setGoalNotice] = useState('');
  const transcriptBox = useRef<HTMLDivElement>(null);
  const textInput = useRef<HTMLInputElement>(null);
  const sampleSpeech = useRef<SpeechSynthesisUtterance | null>(null);
  const followMessages = useRef(true);
  const previousGoals = useRef<string[]>([]);
  const avatar = useRef<AvatarRenderer | null>(null);
  const audio = useRef<MediaStream | null>(null);
  const client = useRef<AvatarClient | null>(null);
  const livePractice = useRef<Practice | undefined>(undefined);
  const mounted = useRef(true);
  const bottom = useRef<HTMLDivElement>(null);
  const reportPending = useRef(false);
  const starting = useRef(false);
  const awaitingReply = useRef(0);
  const closingId = useRef<string | undefined>(undefined);
  const finalizingId = useRef<string | undefined>(undefined);
  const readyCallback = useCallback((value: AvatarRenderer | null) => {
    avatar.current = value; setReady(!!value);
    value?.setAudio(audio.current);
  }, []);
  const fail = useCallback((reason: unknown) => {
    if (mounted.current) setError(reason instanceof Error ? reason.message : String(reason));
  }, []);
  const refreshQuota = useCallback(async () => {
    const value = await api<Quota>('/quota');
    if (mounted.current) setQuota(value);
    return value;
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

  useEffect(() => {
    if (page !== 'home') return;
    let disposed = false;
    void refreshQuota().catch(fail);
    const loadPending = () => api<Practice[]>('/sessions').then(items => {
      if (!disposed) { setBlockedPractice(items.find(canResume)); setSessionConflict(false); }
    }).catch(fail);
    void loadPending();
    const timer = window.setInterval(() => void loadPending(), 10000);
    const focused = () => { void loadPending(); };
    window.addEventListener('focus', focused);
    return () => { disposed = true; window.clearInterval(timer); window.removeEventListener('focus', focused); };
  }, [page, refreshQuota, fail]);

  useEffect(() => {
    const box = transcriptBox.current;
    if (!box) return;
    if (followMessages.current) box.scrollTop = box.scrollHeight;
    else setUnread(true);
  }, [messages]);

  useEffect(() => {
    if (page !== 'practice') return;
    const viewport = window.visualViewport;
    const resize = () => {
      document.documentElement.style.setProperty('--practice-height', `${viewport?.height ?? window.innerHeight}px`);
      document.documentElement.style.setProperty('--practice-top', `${viewport?.offsetTop ?? 0}px`);
    };
    resize();
    viewport?.addEventListener('resize', resize);
    viewport?.addEventListener('scroll', resize);
    window.addEventListener('resize', resize);
    return () => {
      viewport?.removeEventListener('resize', resize);
      viewport?.removeEventListener('scroll', resize);
      window.removeEventListener('resize', resize);
      document.documentElement.style.removeProperty('--practice-height');
      document.documentElement.style.removeProperty('--practice-top');
    };
  }, [page]);

  useEffect(() => {
    if (page !== 'practice' || !mic) { setLevel(0); return; }
    // LiveKit's level comes from the published microphone, never from a decorative animation.
    const timer = window.setInterval(() => setLevel(Math.min(1, Math.max(0, client.current?.room.localParticipant.audioLevel ?? 0))), 100);
    return () => window.clearInterval(timer);
  }, [page, mic]);

  useEffect(() => {
    const ids = practice?.goals.map(goal => goal.id) ?? [];
    const added = ids.find(id => !previousGoals.current.includes(id));
    previousGoals.current = ids;
    if (!added || page !== 'practice') return;
    setHintStep(0);
    setGoalNotice(`完成了：${scenario?.goals.find(goal => goal.id === added)?.label ?? '一个小目标'}`);
    const timer = window.setTimeout(() => setGoalNotice(''), 4500);
    return () => window.clearTimeout(timer);
  }, [practice?.goals.length, page, scenario]);

  useEffect(() => {
    if (!('speechSynthesis' in window)) return;
    const update = () => setSampleAvailable(window.speechSynthesis.getVoices().some(voice => /^en[-_]/i.test(voice.lang)));
    update();
    window.speechSynthesis.addEventListener('voiceschanged', update);
    return () => window.speechSynthesis.removeEventListener('voiceschanged', update);
  }, []);

  useEffect(() => {
    sampleSpeech.current = null; window.speechSynthesis?.cancel(); setSamplePlaying(undefined);
    return () => { sampleSpeech.current = null; window.speechSynthesis?.cancel(); };
  }, [page]);

  function listenSample(sentence: string) {
    const voice = window.speechSynthesis?.getVoices().find(value => /^en[-_]/i.test(value.lang));
    if (!voice) { fail(new Error('当前浏览器没有英语示范语音，可以带着这句话再练一次。')); return; }
    sampleSpeech.current = null; window.speechSynthesis.cancel();
    if (samplePlaying === sentence) { setSamplePlaying(undefined); return; }
    const utterance = new SpeechSynthesisUtterance(sentence);
    utterance.voice = voice; utterance.lang = voice.lang; utterance.rate = 0.85;
    utterance.onend = () => { if (sampleSpeech.current === utterance) { sampleSpeech.current = null; setSamplePlaying(undefined); } };
    utterance.onerror = event => {
      if (sampleSpeech.current !== utterance) return;
      sampleSpeech.current = null; setSamplePlaying(undefined);
      if (!['canceled', 'interrupted'].includes(event.error)) fail(new Error('示范语音未能播放，请重试。'));
    };
    sampleSpeech.current = utterance; setSamplePlaying(sentence); window.speechSynthesis.speak(utterance);
  }

  function toggleKeyboard() {
    const show = !keyboard;
    flushSync(() => setKeyboard(show));
    if (show) textInput.current?.focus();
  }

  function showLatest() {
    followMessages.current = true; setUnread(false);
    const box = transcriptBox.current;
    if (box) box.scrollTop = box.scrollHeight;
  }

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
      if (!starting.current && !closingId.current && awaitingReply.current && Date.now() - awaitingReply.current > 20000) {
        setError(previous => previous || noReplyMessage);
        awaitingReply.current = 0;
      }
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

  async function start(focus = '', resumeRecord?: Practice | true) {
    if (starting.current) return;
    if (!resumeRecord && (blockedPractice || sessionConflict)) { await goToUnfinished(); return; }
    if (!scenario) return;
    if (!resumeRecord && quota && (!quota.remaining || !quota.attempts_remaining)) return;
    sampleSpeech.current = null; window.speechSynthesis?.cancel(); setSamplePlaying(undefined);
    starting.current = true;
    setBlockedPractice(undefined);
    setKeyboard(scenario.mode === 'mock'); setHintStep(0); setRetryFocus(focus);
    setGoalNotice(''); previousGoals.current = []; followMessages.current = true; setUnread(false);
    closingId.current = undefined;
    setBusy(true); setError(''); setMessages([]); setAudioReady(false);
    let created: (Practice & { credentials: Credentials; messages?: PracticeDetail['messages'] }) | undefined;
    const next = new AvatarClient({
      renderer: { setAudio: stream => { audio.current = stream; avatar.current?.setAudio(stream); }, execute: command => avatar.current?.execute(command) },
      onError: fail,
      onConnectionMilestone: stage => { if (stage === 'first_audio_playback') setAudioReady(true); },
      onState: value => { if (mounted.current) { setState(value); if (value === 'disconnected') setMic(false); } },
      onTranscript: message => {
        awaitingReply.current = message.role === 'agent' || !message.final ? 0 : Date.now();
        if (message.role === 'agent') setError(previous => previous === noReplyMessage ? '' : previous);
        const receivedAt = Date.now();
        setMessages(previous => {
          const found = previous.some(m => m.id === message.id && m.participantIdentity === message.participantIdentity);
          return found
            ? previous.map(m => m.id === message.id && m.participantIdentity === message.participantIdentity ? { ...message, receivedAt: message.role === 'user' ? receivedAt : m.receivedAt } : m)
            : [...previous, { ...message, receivedAt }];
        });
      },
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
      let target = typeof resumeRecord === 'object' ? resumeRecord : undefined;
      if (resumeRecord === true) {
        target = (await api<Practice[]>('/sessions')).find(canResume);
        if (!target) throw new ApiError('上次练习已经结束或超时，请重新开始一场练习', 410);
      }
      created = await api<Practice & { credentials: Credentials; messages?: PracticeDetail['messages'] }>(target ? `/sessions/${target.id}/resume` : '/sessions', 'POST');
      if (resumeRecord) {
        previousGoals.current = created.goals.map(goal => goal.id);
        setMessages((created.messages ?? []).map(message => ({
          id: message.id, role: message.role === 'assistant' ? 'agent' : 'user',
          text: message.text, final: true, participantIdentity: `saved:${message.role}`,
        })));
      }
      setSessionConflict(false);
      livePractice.current = created; setPractice(created); setReview(undefined); setPage('practice'); setSeconds(Math.max(0, Math.ceil((new Date(created.expires_at).getTime() - Date.now()) / 1000)));
      awaitingReply.current = Date.now();
      await next.connect((created as Practice & { credentials: Credentials }).credentials);
      if (scenario.mode !== 'mock') { await next.setMicrophone(true); setMic(true); }
    } catch (e) {
      fail(e);
      await next.dispose(); client.current = null;
      if (!resumeRecord && e instanceof ApiError && e.status === 409 && !created) {
        setSessionConflict(true);
        try {
          const previous = await api<Practice[]>('/sessions');
          const pending = previous.find(canResume);
          setBlockedPractice(pending); setSessionConflict(!!pending);
        } catch (reason) { fail(reason); }
      }
      if (resumeRecord) {
        const closed = e instanceof ApiError && [404, 409, 410].includes(e.status);
        setBlockedPractice(closed ? undefined : typeof resumeRecord === 'object' ? resumeRecord : undefined);
        setSessionConflict(!closed && resumeRecord === true); setPage('home');
      }
      if (created && !resumeRecord) {
        try { const item = await api<Practice>(`/sessions/${created.id}/finish`, 'POST'); setPractice(item); livePractice.current = item; }
        catch (reason) { fail(reason); }
      }
    } finally { starting.current = false; setBusy(false); void refreshQuota().catch(fail); }
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

  async function help(kind: 'meaning' | 'hint' | 'hint_full' | 'replay') {
    if (!practice || !client.current) return;
    setHelpBusy(true); setError('');
    try {
      const result = await api<{ message: string }>(`/sessions/${practice.id}/help`, 'POST', { kind });
      await client.current.sendText(result.message);
      if (kind === 'hint' || kind === 'hint_full') setHintStep(value => value + 1);
      showLatest();
    } catch (e) { fail(e); }
    finally { setHelpBusy(false); }
  }

  async function toggleMic() {
    if (!client.current || micBusy) return;
    setMicBusy(true);
    try { await client.current.setMicrophone(!mic); setMic(!mic); } catch (e) { fail(e); }
    finally { setMicBusy(false); }
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

  async function goToUnfinished() {
    await start('', blockedPractice ?? true);
  }

  async function openRecord(record: Practice) {
    setBusy(true); setError('');
    try {
      const item = await api<PracticeDetail>(`/sessions/${record.id}`);
      setBlockedPractice(undefined); setSessionConflict(false);
      setPractice(item); livePractice.current = item; setReview(item.report ?? undefined);
      closingId.current = item.status === 'ending' ? item.id : undefined;
      awaitingReply.current = 0;
      setGoalNotice(''); previousGoals.current = item.goals.map(goal => goal.id);
      setSeconds(Math.max(0, Math.ceil((new Date(item.expires_at).getTime() - Date.now()) / 1000)));
      if (item.report) setPage('review');
      else if (['ended', 'report_failed', 'reporting', 'complete'].includes(item.status)) {
        setPage('review'); await loadReport(item.id);
      } else {
        followMessages.current = true; setUnread(false); setRetryFocus('');
        setMessages(item.messages.map(message => ({
          id: message.id, role: message.role === 'assistant' ? 'agent' : 'user',
          text: message.text, final: true, participantIdentity: `saved:${message.role}`,
        })));
        setMic(false); setState('disconnected'); setKeyboard(false); setPage('practice');
      }
    } catch (e) { fail(e); }
    finally { setBusy(false); }
  }

  const done = practice?.goals.map(g => g.id) ?? [];
  const usable = practice?.status === 'active' && !['disconnected', 'connecting', 'connected'].includes(state);
  const currentGoal = scenario?.goals.find(goal => !done.includes(goal.id));
  const lastMessage = messages.at(-1);
  const statusText = practice?.status === 'ending' ? '正在保存对话，请稍候…'
    : state === 'listening' && !mic && scenario?.mode !== 'mock' ? '麦克风已关闭，点击开启'
    : state === 'listening' && lastMessage?.role === 'user' && !lastMessage.final ? '正在识别你的话…'
    : labels[state] ?? state;
  const quotaBlocked = !!quota && (!quota.remaining || !quota.attempts_remaining);
  const startDisabled = blockedPractice || sessionConflict ? busy || !scenario : busy || !scenario || !ready || quotaBlocked;
  const startLabel = busy || !scenario ? '正在准备…' : blockedPractice || sessionConflict ? '回到上次练习' : !ready ? '伙伴正在就位…' : quota && !quota.remaining ? '今天的次数已用完'
    : quota && !quota.attempts_remaining ? '今天连接次数已用完' : quota?.used ? '再来一次酒店入住' : '开始我的第一次练习';
  const canReplay = messages.some(message => message.role === 'agent' && message.final);
  const reviewTitle = !review ? '正在整理这次练习…' : !review.user_turns ? '这次还没来得及开口。'
    : review.goals.length === 3 ? '这次入住，你做到了。' : '你已经迈出了开口的一步。';
  const visibleMessages = messages.map(message => {
    const helpRequest = message.role === 'user' && message.text.startsWith('[练习帮助：');
    return {
      ...message,
      helpRequest,
      text: helpRequest
        ? message.text.includes('请用中文解释') ? '请解释上一句话。' : message.text.includes('原样重复') ? '请再说一遍。' : message.text.includes('完整例句') ? '给我一个完整例句。' : '给我一点回答提示。'
        : message.text,
    };
  });
  const quotaResetTime = quota ? new Date(quota.resets_at).toLocaleString('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }) : '';

  return <div className={`shell ${page === 'practice' ? 'practice-shell' : ''} ${keyboard ? 'keyboard-open' : ''}`}>
    {page !== 'practice' && <header className="topbar"><a href="/speaking/" onClick={e => { e.preventDefault(); setPage('home'); }} className="brand"><span className="brand-icon">✳</span> 开口旅行<span className="beta">BETA</span></a><button className="text-button" disabled={busy} onClick={showHistory}>练习记录 ↗</button></header>}
    {error && page !== 'practice' && <div className="error" role="alert"><span>{error}</span><button aria-label="关闭提示" onClick={() => setError('')}>×</button></div>}
    {(blockedPractice || sessionConflict) && page !== 'practice' && <section className="quota-notice" role="status"><strong>还有一场未结束的酒店入住练习{blockedPractice && ` · ${blockedPractice.goals.length}/3 个目标`}</strong><p>回到这次对话，继续完成剩下的目标。</p><button className="primary" disabled={busy || !scenario} onClick={() => void goToUnfinished()}>前往未完成练习 ↗</button></section>}
    {page === 'home' && <main className="home">
      <section className="intro"><div className="eyebrow">YOUR NEXT LITTLE ADVENTURE</div><h1>这次入住，<span>你来开口。</span></h1><p>和卡通前台聊几句，把英语用在真实情境里。</p><div className="pills"><span>约 5 分钟</span><span>初级英语</span><span>随时有中文帮助</span></div></section>
      <section className="postcard"><div className="postcard-top">01 / HOTEL CHECK-IN</div><div className="hotel-sign">WILLOW<small>HOTEL</small></div><div className="avatar-scene">{profile && <AvatarView coreUrl={asset(profile.assets.core_url)!} modelUrl={asset(profile.assets.model_url)!} motionSyncUrl={asset(profile.assets.motion_sync_url)} draggable={false} onReady={readyCallback} onError={fail} />}</div><span className="hello-bubble">Welcome!<small>我是前台 Alex，等你来入住。</small></span></section>
      <section className="task-card"><div className="eyebrow">今天的小挑战</div><h2>用英语，拿到你的房卡</h2><p>说出预订姓名，确认住几晚，再问一个小问题。<br />不用说得完美，能表达就很好。</p><div className="brief-note">你是住客 <b>Jamie Chen</b> · 预订了 <b>2 晚</b></div><button className="primary start" disabled={startDisabled} onClick={() => void start()}>{startLabel}<span>↗</span></button><p className="privacy">{scenario?.mode === 'mock' ? '当前为文字演示，语音练习需配置模型服务。' : '开始时开启麦克风。语音交由模型处理，不保存原始录音。'}</p><details className="home-details"><summary>看看这次的三个小目标</summary><ol className="task-list">{scenario?.goals.map((goal, i) => <li key={goal.id}><span>0{i + 1}</span>{goal.label}</li>)}</ol><p>对话文字会保存，用于生成你的练习反馈。</p></details>{quota && <section className={`quota-notice ${quotaBlocked ? 'exhausted' : 'compact'}`} role="status" aria-label="今日练习额度"><strong>{!quota.remaining ? `今天的 ${quota.limit} 次练习已用完` : !quota.attempts_remaining ? '今天的连接尝试次数已用完' : `今天还可练 ${quota.remaining} 次，共 ${quota.limit} 次`}</strong><p>多轮对话只计 1 次，未收到回复不扣次数。</p>{quotaBlocked && <p>将于 {quotaResetTime} 恢复（设备当地时间）。</p>}</section>}</section>
    </main>}
    {page === 'practice' && <main className="practice-page">
      <div className="practice-heading"><div><div className="eyebrow">WILLOW HOTEL · 前台 Alex</div><h1>酒店入住</h1></div><span className="timer" aria-label="剩余时间">{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, '0')}</span><button className="text-button finish-top" disabled={busy || practice?.status === 'ending'} onClick={finish}>结束并查看反馈</button></div>
      <div className="practice-layout"><section className="practice-stage" data-audio-ready={audioReady}><div className="stage-caption">YOUR PRACTICE PARTNER <span>Alex</span></div><div className="practice-avatar">{profile && <AvatarView coreUrl={asset(profile.assets.core_url)!} modelUrl={asset(profile.assets.model_url)!} motionSyncUrl={asset(profile.assets.motion_sync_url)} draggable={false} onReady={readyCallback} onError={fail} />}</div><div className="state"><i className={usable ? 'online' : ''} />{statusText}</div></section>
      <section className="conversation"><div className="mission"><div className="progress-label"><span>{currentGoal ? '当前目标' : '三个目标都完成了'} <b>{done.length}/3</b></span><details className="mission-details"><summary>预订信息 ⓘ</summary><div className="mission-popover"><strong>你的角色：住客</strong><p>预订姓名：Jamie Chen<br />住宿：2 晚<br />还需要：询问早餐或退房时间</p><ul className="goal-list">{scenario?.goals.map(goal => <li key={goal.id} className={done.includes(goal.id) ? 'done' : ''}>{done.includes(goal.id) ? '✓' : '○'} {goal.label}</li>)}</ul>{retryFocus && <p>这次试着用：<br /><b>{retryFocus}</b></p>}</div></details></div><p className="current-goal">{currentGoal?.label ?? '房卡到手！可以看看这次的收获了。'}</p><div className="goal-track" aria-hidden="true">{scenario?.goals.map(goal => <span key={goal.id} className={done.includes(goal.id) ? 'done' : ''} />)}</div></div>
        {retryFocus && <details className="retry-focus"><summary>这次想练的一句表达</summary><p>{retryFocus}</p></details>}
        {goalNotice && <div className="goal-notice" role="status">✓ {goalNotice}</div>}
        <div className="transcripts" ref={transcriptBox} onScroll={event => { const box = event.currentTarget; followMessages.current = box.scrollHeight - box.scrollTop - box.clientHeight < 48; if (followMessages.current) setUnread(false); }} aria-live="polite" aria-relevant="additions text">{!visibleMessages.length && <p className="empty">{scenario?.mode === 'mock' ? '文字演示模式：在下方输入一句英语开始。' : state === 'disconnected' ? '这段连接已断开，你可以结束并保存记录。' : 'Alex 会先向你问好。听完后，直接开口就好。'}</p>}{visibleMessages.map(message => <div className={`bubble ${message.role}`} key={`${message.participantIdentity}:${message.id}`}><small>{message.role === 'user' ? message.helpRequest ? '你 · 请求帮助' : '你' : 'Alex'}</small><p>{message.text}{!message.final && <span className="cursor">▍</span>}</p>{message.receivedAt !== undefined && <time className="message-time" dateTime={new Date(message.receivedAt).toISOString()} title={new Date(message.receivedAt).toLocaleString('zh-CN')}>{messageTime.format(message.receivedAt)}</time>}</div>)}<div ref={bottom} /></div>
        {unread && <button className="new-messages" onClick={showLatest}>有新消息 · 回到最新 ↓</button>}
        <div className={`practice-dock ${state === 'disconnected' && practice?.status === 'active' ? 'recovery-dock' : ''}`}>
          {state === 'disconnected' && practice?.status === 'active' && <div className="recovery-action"><p>这次练习尚未结束，可能仍在其他页面进行。已保存的对话已为你打开。</p><button className="primary" disabled={busy} onClick={finish}>结束这次练习并查看反馈</button></div>}
          {error && <div className="dock-error" role="alert"><span>{error}</span><button aria-label="关闭提示" onClick={() => setError('')}>×</button></div>}
          <div className="help-row"><button disabled={!usable || helpBusy || busy} onClick={() => help('meaning')}>这句话什么意思？</button><button disabled={!usable || helpBusy || busy} onClick={() => help(hintStep ? 'hint_full' : 'hint')}>{hintStep ? '给我完整例句' : '给我一点提示 ✧'}</button><button aria-label="再听一遍" disabled={!usable || !canReplay || helpBusy || busy} onClick={() => help('replay')}>↻ 重听</button></div>
          {hintStep > 0 && <p className="help-nudge">看完提示，试着用自己的声音说一遍。</p>}
          {keyboard && <form className="composer" onSubmit={send}><input ref={textInput} aria-label="输入英语消息" value={text} onChange={e => setText(e.target.value)} maxLength={1000} disabled={!usable || busy} placeholder="输入你想说的英语…" enterKeyHint="send" autoComplete="off" /><button aria-label="发送" disabled={!usable || busy || !text.trim()}>↑</button></form>}
          <div className="call-controls"><button className={`mic ${mic ? 'enabled' : ''}`} aria-pressed={mic} aria-label={mic ? '麦克风已开启' : '开启麦克风'} disabled={!usable || busy || micBusy || scenario?.mode === 'mock'} onClick={toggleMic}><svg className="mic-symbol" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8" /></svg><span>{mic ? state === 'speaking' ? 'Alex 正在说 · 点击静音' : state === 'thinking' ? '等待回复 · 点击静音' : '正在聆听 · 点击静音' : '开启麦克风，直接说'}<small>{mic ? level > 0.02 ? '收到你的声音了' : state === 'speaking' ? '听完再说，也可以打断我' : '慢慢说，不用按住' : '随时可以停下来想一想'}</small></span><span className="input-meter" aria-hidden="true">{[0.02, 0.08, 0.2, 0.4].map(threshold => <i key={threshold} className={mic && level > threshold ? 'active' : ''} />)}</span></button><button className="keyboard-toggle" aria-expanded={keyboard} onClick={toggleKeyboard}>{keyboard ? '收起键盘' : '键盘输入'}</button></div>
          <div className="dock-secondary"><span>{statusText}</span>{scenario?.mode !== 'mock' && !audioReady && <button className="sound-unlock" disabled={!usable || busy} onClick={() => { void client.current?.room.startAudio().catch(fail); }}>听不到？开启声音</button>}</div>
          {done.length === 3 && <button className="primary completed-action" disabled={busy || practice?.status === 'ending'} onClick={finish}>完成练习，看看反馈 ↗</button>}
        </div>
      </section></div>
    </main>}
    {page === 'review' && <main className="review-page"><div className="eyebrow">YOUR NEXT STEP</div><h1>{reviewTitle}</h1>{review ? <><div className="review-stats"><span><b>{review.user_turns}</b>次表达</span><span><b>{review.goals.length}/3</b>个目标</span><span><b>{Math.floor(review.duration_seconds / 60)}:{String(review.duration_seconds % 60).padStart(2, '0')}</b>练习时长</span></div><section className="paper"><h2>这次练得怎么样</h2><p>{review.summary}</p>{review.goals[0] && <blockquote className="your-win"><small>你已经能这样表达了</small>“{review.goals[0].evidence}”</blockquote>}{review.mode === 'mock' && <p className="notice">文字演示记录，不代表真实口语评估。</p>}<details className="review-details"><summary>查看三个目标与原话</summary><ul className="goal-list">{scenario?.goals.map(goal => { const evidence = review.goals.find(g => g.id === goal.id); return <li className={evidence ? 'done' : ''} key={goal.id}><span>{evidence ? '✓' : '○'}</span><div>{goal.label}{evidence && <small className="evidence">你说：“{evidence.evidence}”</small>}</div></li>; })}</ul></details></section>{review.corrections.length > 0 && <section className="paper"><h2>下一次，先试着改进这一句</h2>{review.corrections.map((correction, i) => <details className="correction-disclosure" open={i === 0} key={i}><summary>{i === 0 ? '先练这一句' : '再看看一句表达建议'}</summary><article className={`correction ${i > 0 ? 'extra-correction' : ''}`} key={i}><small>你说</small><p>{correction.original}</p><small>可以试试</small><p className="improved">{correction.revised}</p><p className="reason">{correction.reason}</p><div className="sentence-actions"><button className="text-button" disabled={!sampleAvailable} onClick={() => listenSample(correction.revised)}>{samplePlaying === correction.revised ? '停止示范' : '听示范'}</button><button className="text-button" disabled={busy || !scenario || quotaBlocked} onClick={() => void start(correction.revised)}>带着这句再练 ↗</button></div>{i === 0 && <small>{sampleAvailable ? '示范使用设备英语语音；再练将开启一场新的酒店对话。' : '当前设备暂无英语示范语音，可以直接再练。'}</small>}</article></details>)}</section>}<section className="focus"><span>✦ 下一次，只进步一点点</span><p>{review.next_focus}</p>{retryFocus && <p>这次带着练的表达：{retryFocus}</p>}</section><p className="fineprint">反馈基于对话转写，由 AI 生成，仅供练习参考；不包含发音评分。</p></> : <section className="paper"><p>{busy ? '正在根据你的原话准备建议，请稍候。' : '对话已保留，可以重新生成反馈。'}</p>{!busy && <button className="primary" onClick={() => practice && loadReport(practice.id)}>重试生成反馈</button>}</section>}<div className="review-actions"><button className="primary" disabled={busy || !scenario || quotaBlocked} onClick={() => void start()}>{review?.user_turns ? '再练一次 ↗' : '重新开始练习 ↗'}</button><button className="text-button" disabled={busy} onClick={showHistory}>查看练习记录</button><button className="text-button" disabled={busy} onClick={() => { livePractice.current = undefined; setPage('home'); }}>回到首页</button></div></main>}
    {page === 'history' && <main className="history-page"><div className="eyebrow">YOUR LITTLE MILESTONES</div><h1>每一次开口，都算数。</h1><p className="muted">记录保存在当前浏览器的试用身份下。</p>{!records.length && <section className="paper"><p>你的第一段旅行对话，还在等你开启。</p></section>}<div className="records">{records.map(item => <button className="record" key={item.id} onClick={() => canResume(item) ? void start('', item) : void openRecord(item)} disabled={busy || (canResume(item) && !scenario)}><span className="record-icon">⌂</span><span><strong>酒店入住练习</strong><small>{new Date(item.created_at).toLocaleString('zh-CN')} · {item.goals.length}/3 个目标</small></span><span className="record-status">{item.report ? '查看反馈' : canResume(item) ? '继续练习' : item.status === 'ending' ? '正在保存' : '生成反馈'} ↗</span></button>)}</div><button className="primary" disabled={busy} onClick={() => setPage('home')}>开始新练习 ↗</button></main>}
    {page !== 'practice' && <footer className="footer">开口旅行 <span>让世界，离你近一点。</span></footer>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
