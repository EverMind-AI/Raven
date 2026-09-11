// Approval is intentionally narrower than a generic prompt: the user may
// authorize this exact request once or refuse it. Centralizing the protocol
// helpers keeps UI call sites from introducing persistent/session authority.
// Deny continues the turn (the model reads the refusal and goes another way);
// deny_stop is the one choice that ends it.
// Labels are keys, not text: the locale can change under a running TUI (`/lang`),
// and a literal captured in this module would keep the language it was written
// in. The English text stays beside its key as the fallback the renderer passes
// to `t`, so this list still reads as the three choices it offers.
export const APPROVAL_OPTIONS = [
  { choice: 'allow', fallback: 'Allow once', key: 'gui.confirm.allow' },
  { choice: 'deny', fallback: 'Deny (agent continues)', key: 'gui.confirm.deny_hint' },
  { choice: 'deny_stop', fallback: 'Deny and stop the turn', key: 'gui.confirm.deny_stop_hint' }
] as const

export const buildApprovalRespond = (approvalId: string, sessionId: string, choice: string, feedback = '') => {
  // Echo both opaque identities so a delayed response cannot resolve a newer
  // request that happens to display the same command.
  return {
    approval_id: approvalId,
    choice,
    session_id: sessionId,
    ...(feedback ? { feedback } : {})
  }
}

// Missing or malformed acknowledgements fail closed; only the broker's
// explicit acceptance means that the command was authorized.
export const approvalResponseAccepted = (response: null | { ok?: boolean }) => response?.ok === true

// The runtime supplies one absolute deadline. Deriving the countdown from it
// avoids drift when event delivery or React rendering is delayed.
export const approvalRemainingSeconds = (expiresAt: number, now = Date.now()) =>
  Math.max(0, Math.ceil((expiresAt - now) / 1000))
