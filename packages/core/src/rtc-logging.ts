import { getLogger, LogLevel } from 'livekit-client';

let sequence = 0;

/** Scope SDK shutdown diagnostics to this owned Room; never mute active failures. */
export function createRoomLogger() {
  const name = `avatarweave-room-${++sequence}`;
  const logger = getLogger(name);
  const originalFactory = logger.methodFactory;
  let closing = false;
  logger.methodFactory = (method, level, loggerName) => {
    const output = originalFactory(method, level, loggerName);
    const debug = originalFactory('debug', level, loggerName);
    return (message: unknown, context?: { error?: unknown }) => {
      const error = context?.error;
      // Safari/proxies can report 1006 after the server receives CLIENT_REQUEST_LEAVE.
      // LiveKit 2.23 logs its reader rejection as an error even during local teardown.
      if (closing && method === 'error' && message === 'error reading from signal stream'
        && error instanceof Error && error.message === 'WS closed unexpectedly with code 1006') {
        if (level <= LogLevel.debug) debug('signal stream closed during requested room shutdown', context);
        return;
      }
      output(message, context);
    };
  };
  logger.setLevel(logger.getLevel(), false);
  return {
    name,
    beginClose: () => { closing = true; },
    connected: () => { closing = false; },
    // SDK readers may reject after Room.disconnect has resolved. The named logger
    // retains only this boolean and its factory, never the Room or AvatarClient.
  };
}
