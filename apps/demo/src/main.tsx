import { StrictMode, useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { AvatarClient, type Transcript } from '@avatarweave/core';
import { AvatarView, type AvatarRenderer } from '@avatarweave/react';
import './style.css';
import { VoicePicker, type Voice } from './VoicePicker';
import { ConnectionPreparation } from './connection-preparation';

type DisplayMessage = Transcript & { receivedAt: number };
const messageTimeFormat = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });

function App() {
  const avatar = useRef<AvatarRenderer | null>(null);
  const client = useRef<AvatarClient | null>(null);
  const [ready, setReady] = useState(false);
  const [state, setState] = useState('disconnected');
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [mic, setMic] = useState(false);
  const [text, setText] = useState('');
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [error, setError] = useState('');
  const [mode, setMode] = useState('');
  const [accessKey, setAccessKey] = useState('');
  const [voices, setVoices] = useState<Voice[]>([]);
  const [voiceEnabled, setVoiceEnabled] = useState(false);
  const [voicesLoaded, setVoicesLoaded] = useState(false);
  const [voiceInfo, setVoiceInfo] = useState({ provider: '', provider_label: '', notice: '', capabilities: { preview: false } });
  type AvatarProfile = { id: string; name: string; draggable: boolean; assets: { core_url: string; model_url: string; motion_sync_url?: string }; default_action?: string; actions: Record<string, { label: string; type: 'motion' | 'expression'; name: string; index?: number }> };
  const [avatars, setAvatars] = useState<AvatarProfile[]>([]);
  const [avatarId, setAvatarId] = useState('');
  const profile = avatars.find(a => a.id === avatarId);
  const [voice, setVoice] = useState('');
  const bottom = useRef<HTMLDivElement>(null);
  const onReady = useCallback((value: AvatarRenderer | null) => { avatar.current = value; setReady(!!value); }, []);
  const onError = useCallback((e: Error) => setError(e.message), []);
  const connectionLog = useRef<((stage: string, at?: number, details?: Record<string, unknown>) => void) | null>(null);
  const receive = (m: Transcript) => {
    const receivedAt = Date.now();
    setMessages(previous => {
      const index = previous.findIndex(p => p.id === m.id);
      return index === -1
        ? [...previous, { ...m, receivedAt }]
        : previous.map((p, i) => i === index ? { ...m, receivedAt: p.receivedAt } : p);
    });
  };
  const [preparation] = useState(() => new ConnectionPreparation(() => new AvatarClient({
    renderer: {
      setAudio: stream => avatar.current?.setAudio(stream),
      execute: command => avatar.current?.execute(command),
    },
    onConnectionMilestone: (stage, at) => connectionLog.current?.({ room_connected: '房间连接完成', agent_ready: '助手就绪', first_audio_playback: '首次音频播放' }[stage], at),
    onTranscript: receive, onError,
    onState: value => { setState(value); if (value === 'disconnected') { setConnected(false); setMic(false); } },
  })));
  const tokenBody = JSON.stringify({ avatar_id: avatarId, ...(voiceEnabled && voice ? { tts_voice: voice } : {}) });
  const preparationKey = JSON.stringify([tokenBody, accessKey]);
  useEffect(() => { fetch('/api/health').then(r => { if (!r.ok) throw new Error('后端未就绪'); return r.json(); }).then(d => setMode(d.pipeline.mode)).catch(e => setError(e.message)); return () => { preparation.invalidate(); void client.current?.dispose(); }; }, [preparation]);
  useEffect(() => {
    if (!avatarId || !voicesLoaded || connected || busy) return;
    // Debounce selection/key edits; invalidation also cancels stale requests.
    const timer = setTimeout(() => {
      const started = performance.now();
      void preparation.warm(preparationKey, tokenBody, accessKey).then(() => {
        console.info('[AvatarWeave preparation]', { stage: 'token 预取完成，连接预热已发起', duration_ms: Math.round(performance.now() - started) });
      }).catch(() => { /* Click retries failures through the normal error UI. */ });
    }, 300);
    return () => { clearTimeout(timer); preparation.invalidate(); };
  }, [avatarId, voicesLoaded, connected, busy, preparation, preparationKey, tokenBody, accessKey]);
  useEffect(() => {
    fetch('/api/tts/voices').then(r => { if (!r.ok) throw new Error('无法加载音色列表'); return r.json(); }).then(data => {
      setVoices(data.voices); setVoiceEnabled(data.enabled && data.capabilities.voice_selection); setVoiceInfo(data);
      let stored: string | null = null;
      try { stored = localStorage.getItem(`avatarweave.voice.${data.provider}`); } catch { /* Storage may be disabled. */ }
      setVoice(data.voices.some((v: Voice) => v.id === stored) ? stored : data.default_voice);
      setVoicesLoaded(true);
    }).catch(onError);
  }, [onError]);
  useEffect(() => {
    fetch('/api/avatars').then(r => { if (!r.ok) throw new Error('无法加载数字人配置'); return r.json(); }).then(data => { setAvatars(data.avatars); setAvatarId(data.default_avatar); }).catch(onError);
  }, [onError]);
  function selectVoice(value: string) { setVoice(value); try { localStorage.setItem(`avatarweave.voice.${voiceInfo.provider}`, value); } catch { /* Session selection still works. */ } }
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);
  async function connect() {
    const attempt = crypto.randomUUID();
    const started = performance.now();
    let previous = started;
    const stages = new Set<string>();
    const logStage = (stage: string, at = performance.now(), details: Record<string, unknown> = {}) => {
      if (stages.has(stage)) return;
      stages.add(stage);
      console.info('[AvatarWeave connection]', {
        attempt, stage, timestamp: new Date(performance.timeOrigin + at).toISOString(),
        elapsed_ms: Math.round((at - started) * 10) / 10,
        since_previous_ms: Math.round((at - previous) * 10) / 10,
        ...details,
      });
      previous = at;
    };
    logStage('点击', started);
    connectionLog.current = logStage;
    // Consume synchronously before setBusy triggers prefetch-effect cleanup.
    const prepared = preparation.take(preparationKey, tokenBody, accessKey);
    setBusy(true); setError('');
    try {
      const { client: next, credentials, source, tokenAgeMs } = await prepared;
      logStage('token 返回', performance.now(), { token_source: source, token_age_ms: tokenAgeMs });
      const previousClient = client.current;
      client.current = next;
      await previousClient?.dispose();
      setMessages([]);
      await next.connect(credentials);
      setConnected(true);
    } catch (e) { logStage('连接失败'); onError(e as Error); await client.current?.dispose(); client.current = null; setState('disconnected'); setConnected(false); }
    finally { setBusy(false); }
  }
  async function disconnect() {
    setBusy(true);
    try { await client.current?.dispose(); client.current = null; }
    catch (e) { onError(e as Error); }
    finally { setState('disconnected'); setConnected(false); setMic(false); setBusy(false); }
  }
  async function toggleMic() {
    try { await client.current?.setMicrophone(!mic); setMic(!mic); } catch (e) { onError(e as Error); }
  }
  async function send(event: React.FormEvent) {
    event.preventDefault();
    if (!text.trim() || !connected) return;
    try { await client.current!.sendText(text); setText(''); } catch (e) { onError(e as Error); }
  }
  const labels: Record<string, string> = { disconnected: '尚未连接', connecting: '连接中', connected: '等待助手', listening: '正在聆听', thinking: '正在思考', speaking: '正在说话' };
  return <main>
    <header><a className="brand" href="/">◈ <span>AVATAR<span className="brand-light">WEAVE</span></span></a><div className="header-right"><span className="version">SDK 0.1</span><span className="status"><i className={connected ? 'online' : ''} />{labels[state] ?? state}</span></div></header>
    <section className="intro"><div><div className="eyebrow">REALTIME DIGITAL HUMAN</div><h1>让对话，<span>有温度。</span></h1><p>与你的数字人助手见面，开始一段自然的交流。</p></div><div className="mode">{mode === 'mock' ? '本地演示 · 文字模式' : (mode === 'pipeline' || mode === 'openai') ? '智能对话 · 语音模式' : '正在检查服务'}</div></section>
    <div className="workspace"><section className="stage"><div className="stage-label"><span className="dot" />AVATARWEAVE</div><div className="orb" /><div className="avatar">{profile && <AvatarView coreUrl={profile.assets.core_url} modelUrl={profile.assets.model_url} motionSyncUrl={profile.assets.motion_sync_url} draggable={profile.draggable} onReady={onReady} onError={onError} />}</div><div className="stage-footer"><div><strong>{profile?.name ?? '加载数字人…'}</strong><span>{ready ? '数字人已就绪 · 拖动可调整位置' : '正在加载数字人…'}</span></div><button className="icon-button" disabled={!ready} onClick={() => avatar.current?.resize()} title="重置位置">↺</button><button className="icon-button" disabled={!ready || !profile?.default_action} onClick={() => { void avatar.current?.execute(profile!.actions[profile!.default_action!]).catch(onError); }} title={profile?.default_action ? profile.actions[profile.default_action].label : '未配置默认动作'}>✦</button></div></section>
    <section className="chat"><div className="chat-heading"><div><h2>对话空间</h2><span>每一次交流，都是新的开始</span></div><span className="chat-symbol">⌁</span></div>{voiceEnabled && <VoicePicker providerLabel={voiceInfo.provider_label} notice={voiceInfo.notice} canPreview={voiceInfo.capabilities.preview} voices={voices} selected={voice} disabled={connected || busy} accessKey={accessKey} onChange={selectVoice} onError={onError} />}<div className="messages" aria-live="polite">{!messages.length && <div className="empty"><div className="empty-icon">✳</div><h3>你好，很高兴见到你</h3><p>连接助手后，试着打个招呼<br />或者邀请数字人做个动作。</p><div className="suggestions">{['你好，介绍一下自己', '请做一个动作'].map(s => <button key={s} onClick={() => setText(s)}>{s} ↗</button>)}</div></div>}{messages.map(m => <div key={m.id} className={`message ${m.role}`}><span className="message-label">{m.role === 'user' ? '你' : profile?.name ?? '助手'}<time className="message-time" dateTime={new Date(m.receivedAt).toISOString()} title={new Date(m.receivedAt).toLocaleString('zh-CN')}>{messageTimeFormat.format(m.receivedAt)}</time></span><div>{m.text}{!m.final && <span className="cursor">▍</span>}</div></div>)}<div ref={bottom} /></div>
    <form className="composer" onSubmit={send}><input aria-label="消息" value={text} onChange={e => setText(e.target.value)} placeholder={connected ? '输入消息，按回车发送…' : '连接后开始对话…'} maxLength={4000} /><button aria-label="发送" disabled={!connected || !text.trim()}>↑</button></form><div className="chat-note">{mode === 'mock' ? '演示模式提供真实房间连接和文字回复，语音需配置模型。' : '语音输入需要浏览器麦克风授权。'}</div></section></div>
    {error && <div role="alert" className="error">{error}<button onClick={() => setError('')} aria-label="关闭错误">×</button></div>}
    <footer><div className="connection-settings">{avatars.length > 1 && <label>数字人 <select aria-label="数字人" value={avatarId} disabled={connected || busy} onChange={e => setAvatarId(e.target.value)}>{avatars.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select></label>}<label>访问密钥 <input type="password" value={accessKey} onChange={e => setAccessKey(e.target.value)} placeholder="本地默认无需填写" autoComplete="off" /></label></div><div className="controls"><button className={`mic-button ${mic ? 'active' : ''}`} onClick={toggleMic} disabled={!connected || mode === 'mock'}>{mic ? '关闭麦克风' : '开启麦克风'}</button><button className="connect-button" disabled={busy || !ready} onClick={connected ? disconnect : connect}>{busy ? '请稍候…' : connected ? '结束对话' : '开始对话'} <span>↗</span></button></div></footer><div className="bottomline">AVATARWEAVE · REALTIME INTERACTION <span>面向自由集成而构建</span></div>
  </main>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
