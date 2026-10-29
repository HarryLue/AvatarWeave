import { useEffect, useRef, useState } from 'react';

export type Voice = { id: string; name: string; description: string; language: string; language_label: string; gender: string };
type Props = { providerLabel: string; notice: string; canPreview: boolean; voices: Voice[]; selected: string; disabled: boolean; accessKey: string; onChange: (voice: string) => void; onError: (error: Error) => void };

export function VoicePicker({ providerLabel, notice, canPreview, voices, selected, disabled, accessKey, onChange, onError }: Props) {
  const [playing, setPlaying] = useState(false);
  const playback = useRef<{ controller: AbortController; context: AudioContext } | null>(null);
  const stop = () => {
    const active = playback.current;
    playback.current = null;
    active?.controller.abort();
    if (active && active.context.state !== 'closed') void active.context.close();
    setPlaying(false);
  };
  useEffect(() => () => { const active = playback.current; active?.controller.abort(); if (active && active.context.state !== 'closed') void active.context.close(); }, []);
  useEffect(() => { stop(); }, [selected, disabled]);
  async function preview() {
    if (playing) { stop(); return; }
    const active = { controller: new AbortController(), context: new AudioContext() };
    playback.current = active;
    setPlaying(true);
    try {
      await active.context.resume();
      const response = await fetch('/api/tts/preview', { method: 'POST', headers: { 'Content-Type': 'application/json', ...(accessKey ? { Authorization: `Bearer ${accessKey}` } : {}) }, body: JSON.stringify({ voice: selected }), signal: active.controller.signal });
      if (!response.ok || !response.body) throw new Error(`音色试听失败 (${response.status})`);
      const sampleRate = Number(response.headers.get('X-Audio-Sample-Rate') ?? 24000);
      const channels = Number(response.headers.get('X-Audio-Channels') ?? 1);
      if (!Number.isInteger(sampleRate) || sampleRate < 8000 || sampleRate > 192000 || !Number.isInteger(channels) || channels < 1 || channels > 2) throw new Error('不支持的试听音频格式');
      const reader = response.body.getReader();
      let pending = new Uint8Array(0);
      let nextTime = active.context.currentTime + .05;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (playback.current !== active) return;
        const data = new Uint8Array(pending.length + value.length);
        data.set(pending); data.set(value, pending.length);
        const count = Math.floor(data.length / (2 * channels));
        pending = data.slice(count * 2 * channels);
        if (!count) continue;
        const buffer = active.context.createBuffer(channels, count, sampleRate);
        const view = new DataView(data.buffer);
        for (let channel = 0; channel < channels; channel++) {
          const samples = buffer.getChannelData(channel);
          for (let i = 0; i < count; i++) samples[i] = view.getInt16((i * channels + channel) * 2, true) / 32768;
        }
        const source = active.context.createBufferSource();
        source.buffer = buffer; source.connect(active.context.destination);
        nextTime = Math.max(nextTime, active.context.currentTime + .03);
        source.start(nextTime); nextTime += buffer.duration;
      }
      if (pending.length) throw new Error('试听音频数据不完整');
      // Keep scheduled audio alive until the final sample has played.
      await new Promise<void>(resolve => {
        const timer = window.setTimeout(resolve, Math.max(0, (nextTime - active.context.currentTime) * 1000));
        active.controller.signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
      });
    } catch (error) { if (!active.controller.signal.aborted) onError(error as Error); }
    finally { if (playback.current === active) stop(); }
  }
  const languages = [...new Set(voices.map(v => v.language))];
  const voice = voices.find(v => v.id === selected);
  return <div className="voice-picker">
    <div className="voice-controls"><label htmlFor="tts-voice">语音音色</label><select id="tts-voice" value={selected} disabled={disabled} onChange={e => onChange(e.target.value)}>{languages.map(language => <optgroup key={language} label={voices.find(v => v.language === language)?.language_label ?? language}>{voices.filter(v => v.language === language).map(v => <option key={v.id} value={v.id}>{v.name} · {v.gender} ({v.id})</option>)}</optgroup>)}</select>{canPreview && <button type="button" disabled={disabled || !voice} onClick={() => { void preview(); }}>{playing ? '停止试听' : '试听'}</button>}</div>
    <p>{providerLabel} · {voice?.description}</p>{notice && <small>{notice}<br /></small>}<small>{disabled ? '结束对话后可切换音色。' : '先试听再开始对话；所选音色将在本次对话中使用。'}</small>
  </div>;
}
