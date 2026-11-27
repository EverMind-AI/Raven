/* The words the status bar shows, and the colour each one is painted in.

   Every writer and every comparison goes through this table: the bar's colour
   is chosen by comparing the text, so a word spelled in two places drifts the
   moment one of them changes, and the bar silently turns grey. */

export const STATUS = {
  summoning: 'Summoning raven…',
  ready: 'Ready',
  running: 'Running…',
  forging: 'Forging session…',
  resuming: 'Resuming…',
  resumingRecent: 'Resuming most recent…',
  startingAgent: 'Starting agent…',
  waitingInput: 'Waiting for input…',
  setupRunning: 'Setup running…',
  setupRequired: 'Setup required',
  queuedNext: 'Queued for next turn',
  interpolating: 'Interpolating…',
  approvalNeeded: 'Approval needed',
  confirmNeeded: 'Confirm needed',
  sudoNeeded: 'Sudo password needed',
  secretNeeded: 'Secret input needed',
  startupTimeout: 'Gateway startup timeout',
  protocolWarning: 'Protocol warning',
  gatewayExited: 'Gateway exited',
  error: 'Error'
} as const

/** A failed turn's status line: the word the bar paints red, then the reason. */
export const errorStatus = (message: string): string => `${STATUS.error}: ${message}`

export const statusColorOf = (status: string, t: { error: string; muted: string; ok: string; warn: string }) => {
  if (status === STATUS.ready) {
    return t.ok
  }

  if (status.startsWith(STATUS.error)) {
    return t.error
  }

  if (status === 'interrupted') {
    return t.warn
  }

  return t.muted
}
