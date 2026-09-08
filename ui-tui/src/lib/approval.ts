// Approval is intentionally narrower than a generic prompt: the user may
// authorize this exact request once or deny it. Centralizing the protocol
// helpers keeps UI call sites from introducing persistent/session authority.
export const APPROVAL_OPTIONS = [
  { choice: 'allow', label: 'Allow once' },
  { choice: 'deny', label: 'Deny' }
] as const

export const buildApprovalRespond = (approvalId: string, sessionId: string, choice: string) => {
  // Echo both opaque identities so a delayed response cannot resolve a newer
  // request that happens to display the same command.
  return {
    approval_id: approvalId,
    choice,
    session_id: sessionId
  }
}

// Missing or malformed acknowledgements fail closed; only the broker's
// explicit acceptance means that the command was authorized.
export const approvalResponseAccepted = (response: null | { ok?: boolean }) => response?.ok === true

// The runtime supplies one absolute deadline. Deriving the countdown from it
// avoids drift when event delivery or React rendering is delayed.
export const approvalRemainingSeconds = (expiresAt: number, now = Date.now()) =>
  Math.max(0, Math.ceil((expiresAt - now) / 1000))
