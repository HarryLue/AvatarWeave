import { useEffect, useRef, type CSSProperties } from 'react';
import { AvatarRenderer, type RendererOptions } from '@avatarweave/renderer';
export interface AvatarViewProps extends Omit<RendererOptions, 'canvas'> {
  className?: string;
  style?: CSSProperties;
  onReady?: (avatar: AvatarRenderer | null) => void;
  onError?: (error: Error) => void;
}
export function AvatarView({ className, style, onReady, onError, ...options }: AvatarViewProps) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const callbacks = useRef({ onReady, onError });
  callbacks.current = { onReady, onError };
  useEffect(() => {
    const avatar = new AvatarRenderer({ ...options, canvas: canvas.current!, onError: error => callbacks.current.onError?.(error) });
    let cancelled = false;
    avatar.init().then(() => { if (!cancelled) callbacks.current.onReady?.(avatar); }).catch(e => { avatar.dispose(); if (!cancelled) callbacks.current.onError?.(e); });
    return () => { cancelled = true; callbacks.current.onReady?.(null); avatar.dispose(); };
  }, [options.coreUrl, options.modelUrl, options.motionSyncUrl, options.draggable, options.onTouch]);
  return <div className={className} style={{ width: '100%', height: '100%', overflow: 'hidden', ...style }}><canvas ref={canvas} style={{ display: 'block', touchAction: 'none' }} /></div>;
}
export { AvatarClient } from '@avatarweave/core';
export { AvatarRenderer } from '@avatarweave/renderer';
