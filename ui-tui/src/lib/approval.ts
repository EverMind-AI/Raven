// Approval is narrower than a generic prompt: the user grants this request --
// once, for this session, or as a prefix rule the runtime suggested and they
// may edit -- or refuses it. The runtime is what remembers or writes; this
// module only carries the choice, so no UI call site can mint authority of its
// own. Deny continues the turn (the model reads the refusal and goes another
// way); deny_stop is the one choice that ends it.
// Labels are keys, not text: the locale can change under a running TUI (`/lang`),
// and a literal captured in this module would keep the language it was written
// in. The English text stays beside its key as the fallback the renderer passes
// to `t`, so this list still reads as the three choices it offers.
export const APPROVAL_OPTIONS = [
  { choice: 'allow', fallback: 'Allow once', key: 'gui.confirm.allow' },
  { choice: 'allow_session', fallback: 'Allow for this session', key: 'gui.confirm.allow_session' },
  { choice: 'deny', fallback: 'Deny (agent continues)', key: 'gui.confirm.deny_hint' },
  { choice: 'deny_stop', fallback: 'Deny and stop the turn', key: 'gui.confirm.deny_stop_hint' }
] as const

// Offered only when the runtime found a prefix safe to suggest; the label
// carries that prefix, and picking the row opens it for editing before it is
// sent. A blank suggestion means the row is absent, not a blank box.
export const ALWAYS_OPTION = {
  choice: 'allow_always',
  fallback: "Allow and don't ask again for: {pattern}",
  key: 'gui.confirm.allow_always'
} as const

export type ApprovalOption = (typeof APPROVAL_OPTIONS)[number] | typeof ALWAYS_OPTION

// The persisted grant sits after the session one and before the refusals, so
// the two refusals keep the last two numbers whatever the runtime suggested.
export const approvalOptionsFor = (suggestedPattern?: string): readonly ApprovalOption[] =>
  suggestedPattern
    ? [APPROVAL_OPTIONS[0], APPROVAL_OPTIONS[1], ALWAYS_OPTION, APPROVAL_OPTIONS[2], APPROVAL_OPTIONS[3]]
    : APPROVAL_OPTIONS

export const buildApprovalRespond = (
  approvalId: string,
  sessionId: string,
  choice: string,
  feedback = '',
  pattern = ''
) => {
  // Echo both opaque identities so a delayed response cannot resolve a newer
  // request that happens to display the same command.
  return {
    approval_id: approvalId,
    choice,
    session_id: sessionId,
    ...(feedback ? { feedback } : {}),
    ...(pattern ? { pattern } : {})
  }
}

// Missing or malformed acknowledgements fail closed; only the broker's
// explicit acceptance means that the command was authorized.
export const approvalResponseAccepted = (response: null | { ok?: boolean }) => response?.ok === true

// The runtime supplies one absolute deadline. Deriving the countdown from it
// avoids drift when event delivery or React rendering is delayed.
export const approvalRemainingSeconds = (expiresAt: number, now = Date.now()) =>
  Math.max(0, Math.ceil((expiresAt - now) / 1000))
