// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { Box, Text, useInput } from '@hermes/ink'
import { useEffect, useRef, useState } from 'react'

import type { Theme } from '../theme.js'
import type { ApprovalReq, ClarifyReq, ConfirmReq } from '../types.js'

import { APPROVAL_OPTIONS, approvalRemainingSeconds } from '../lib/approval.js'
import { CONFIRM_COUNTDOWN_SECONDS, tickCountdown } from '../lib/confirmCountdown.js'
import { isMac } from '../lib/platform.js'
import { TextInput } from './textInput.js'

const CMD_PREVIEW_LINES = 10

// 90 -> "1m 30s", 45 -> "45s". Seconds are padded so the line keeps its
// width as the countdown runs and the prompt below it does not jitter.
const clarifyRemainingText = (secs: number): string =>
  secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${String(secs % 60).padStart(2, '0')}s`

export function ApprovalPrompt({ onChoice, req, t }: ApprovalPromptProps) {
  const [sel, setSel] = useState(0)
  const [remainingSeconds, setRemainingSeconds] = useState(() => approvalRemainingSeconds(req.expiresAt))
  const expired = useRef(false)

  useEffect(() => {
    expired.current = false

    const update = () => {
      // The runtime sends an absolute wall-clock deadline rather than a duration.
      // Recomputing from that value matters when rendering is delayed or the
      // terminal process is briefly suspended: mounting the component must not
      // accidentally grant a fresh approval window. The ref makes auto-denial
      // edge-triggered while the interval continues to render the 0-second state.
      const remaining = approvalRemainingSeconds(req.expiresAt)
      setRemainingSeconds(remaining)
      if (remaining === 0 && !expired.current) {
        expired.current = true
        onChoice('deny')
      }
    }

    update()
    const timer = setInterval(update, 250)

    return () => clearInterval(timer)
  }, [onChoice, req.approvalId, req.expiresAt])

  useInput((ch, key) => {
    if (key.upArrow && sel > 0) {
      setSel(s => s - 1)
    }

    if (key.downArrow && sel < APPROVAL_OPTIONS.length - 1) {
      setSel(s => s + 1)
    }

    const n = parseInt(ch, 10)

    if (n >= 1 && n <= APPROVAL_OPTIONS.length) {
      onChoice(APPROVAL_OPTIONS[n - 1]!.choice)

      return
    }

    if (key.return) {
      onChoice(APPROVAL_OPTIONS[sel]!.choice)
    }
  })

  const rawLines = req.command.split('\n')
  const shown = rawLines.slice(0, CMD_PREVIEW_LINES)
  const overflow = rawLines.length - shown.length

  return (
    <Box borderColor={t.color.border} borderStyle="round" flexDirection="column" paddingX={1}>
      <Text bold color={t.color.warn}>
        ⚠ approval required · {req.description}
      </Text>

      <Box flexDirection="column" paddingLeft={1}>
        {shown.map((line, i) => (
          <Text color={t.color.text} key={i} wrap="truncate-end">
            {line || ' '}
          </Text>
        ))}

        {overflow > 0 ? (
          <Text color={t.color.muted}>
            … +{overflow} more line{overflow === 1 ? '' : 's'} (full text above)
          </Text>
        ) : null}
      </Box>

      <Text />

      {APPROVAL_OPTIONS.map((option, i) => (
        <Text key={option.choice}>
          <Text bold={sel === i} color={sel === i ? t.color.warn : t.color.muted} inverse={sel === i}>
            {sel === i ? '▸ ' : '  '}
            {i + 1}. {option.label}
          </Text>
        </Text>
      ))}

      <Text color={t.color.muted}>
        ↑/↓ select · Enter confirm · 1-2 quick pick · Ctrl+C deny · expires in {remainingSeconds}s
      </Text>
    </Box>
  )
}

export function ClarifyPrompt({ cols = 80, onAnswer, onCancel, req, t }: ClarifyPromptProps) {
  const [sel, setSel] = useState(0)
  const [custom, setCustom] = useState('')
  const [typing, setTyping] = useState(false)
  // The option a note is being attached to. Distinct from `typing`, which
  // replaces the selection with free text rather than annotating it.
  const [noting, setNoting] = useState<null | string>(null)
  const [note, setNote] = useState('')
  const [remaining, setRemaining] = useState(req.timeoutS ?? 0)
  const choices = req.choices ?? []
  const total = req.total ?? 1
  const position = (req.index ?? 0) + 1
  const others = (req.batch ?? []).filter((_, i) => i !== (req.index ?? 0))

  useEffect(() => {
    if (!req.timeoutS) {
      return
    }

    setRemaining(req.timeoutS)
    const timer = setInterval(() => setRemaining(s => (s > 0 ? s - 1 : 0)), 1000)

    return () => clearInterval(timer)
  }, [req.requestId, req.timeoutS])

  const heading = (
    <Text bold>
      <Text color={t.color.accent}>ask</Text>
      {total > 1 ? <Text color={t.color.muted}> ({position}/{total})</Text> : null}
      {req.header ? <Text color={t.color.label}> [{req.header}]</Text> : null}
      <Text color={t.color.text}> {req.question}</Text>
    </Text>
  )

  // The rest of the batch, so the user can see how much is still coming rather
  // than discovering it one prompt at a time.
  const rest =
    others.length > 0 ? (
      <Box flexDirection="column" paddingLeft={1}>
        <Text color={t.color.muted} dimColor>
          also asking: {others.map(q => q.question).join(' · ')}
        </Text>
      </Box>
    ) : null

  const budget = req.timeoutS ? `${clarifyRemainingText(remaining)} left · ` : ''

  useInput((ch, key) => {
    if (key.escape) {
      if (noting !== null) {
        setNoting(null)

        return
      }

      typing && choices.length ? setTyping(false) : onCancel()

      return
    }

    if (typing || noting !== null || !choices.length) {
      return
    }

    if (key.upArrow && sel > 0) {
      setSel(s => s - 1)
    }

    if (key.downArrow && sel < choices.length) {
      setSel(s => s + 1)
    }

    if (key.tab && sel < choices.length && choices[sel]) {
      setNote('')
      setNoting(choices[sel]!)

      return
    }

    if (key.return) {
      sel === choices.length ? setTyping(true) : choices[sel] && onAnswer(choices[sel]!)
    }

    const n = parseInt(ch)

    if (n >= 1 && n <= choices.length) {
      onAnswer(choices[n - 1]!)
    }
  })

  if (noting !== null) {
    return (
      <Box flexDirection="column">
        {heading}

        <Text color={t.color.label}>
          {'  '}note for {noting}
        </Text>

        <Box>
          <Text color={t.color.label}>{'> '}</Text>
          <TextInput
            columns={Math.max(20, cols - 6)}
            onChange={setNote}
            onSubmit={v => onAnswer(v.trim() ? `${noting} (note: ${v.trim()})` : noting)}
            value={note}
          />
        </Box>

        <Text color={t.color.muted}>
          {budget}Enter send · Esc back
        </Text>
      </Box>
    )
  }

  if (typing || !choices.length) {
    return (
      <Box flexDirection="column">
        {heading}
        {rest}

        <Box>
          <Text color={t.color.label}>{'> '}</Text>
          <TextInput columns={Math.max(20, cols - 6)} onChange={setCustom} onSubmit={onAnswer} value={custom} />
        </Box>

        <Text color={t.color.muted}>
          {budget}Enter send · Esc {choices.length ? 'back' : 'cancel'} ·{' '}
          {isMac ? 'Cmd+C copy · Cmd+V paste · Ctrl+C cancel' : 'Ctrl+C cancel'}
        </Text>
      </Box>
    )
  }

  return (
    <Box flexDirection="column">
      {heading}
      {rest}

      {[...choices, 'Other (type your answer)'].map((c, i) => (
        <Text key={i}>
          <Text bold={sel === i} color={sel === i ? t.color.label : t.color.muted} inverse={sel === i}>
            {sel === i ? '▸ ' : '  '}
            {i + 1}. {c}
            {req.recommended && c === req.recommended ? ' (recommended)' : ''}
          </Text>
        </Text>
      ))}

      <Text color={t.color.muted}>
        {budget}↑/↓ select · Enter confirm · Tab add note · 1-{choices.length} quick pick · Esc/Ctrl+C cancel
      </Text>
    </Box>
  )
}

export function ConfirmPrompt({ onCancel, onConfirm, req, t }: ConfirmPromptProps) {
  const [sel, setSel] = useState(0)

  // The 30s countdown only drives the RPC path (a server-side broker waiting
  // on a response).  In-process confirms have no remote deadline, so they
  // start suspended (remaining = null) and never auto-cancel.
  const isRpc = Boolean(req.requestId)
  const [remaining, setRemaining] = useState<null | number>(isRpc ? CONFIRM_COUNTDOWN_SECONDS : null)
  const intervalRef = useRef<null | ReturnType<typeof setInterval>>(null)

  const suspend = () => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current)
      intervalRef.current = null
    }

    setRemaining(null)
  }

  useEffect(() => {
    if (!isRpc) {
      return
    }

    intervalRef.current = setInterval(() => {
      setRemaining(prev => {
        if (prev === null) {
          return prev
        }

        const { autoCancel, remaining: next } = tickCountdown(prev)

        if (autoCancel) {
          if (intervalRef.current) {
            clearInterval(intervalRef.current)
            intervalRef.current = null
          }

          onCancel()
        }

        return next
      })
    }, 1000)

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }
    // onCancel is stable for the lifetime of a given confirm overlay.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isRpc])

  useInput((ch, key) => {
    const lower = ch.toLowerCase()

    if (key.escape || (key.ctrl && lower === 'c') || lower === 'n') {
      return onCancel()
    }

    if (lower === 'y') {
      return onConfirm()
    }

    if (key.upArrow) {
      setSel(0)
      suspend()

      return
    }

    if (key.downArrow) {
      setSel(1)
      suspend()

      return
    }

    if (key.return) {
      return sel === 0 ? onCancel() : onConfirm()
    }

    // Any other key suspends the countdown without answering.
    suspend()
  })

  const accent = req.danger ? t.color.error : t.color.warn
  const countdownLabel = remaining === null ? '' : ` (${remaining}s)`

  const rows = [
    { color: t.color.text, label: `${req.cancelLabel ?? 'No'}${countdownLabel}` },
    { color: req.danger ? t.color.error : t.color.text, label: req.confirmLabel ?? 'Yes' }
  ]

  return (
    <Box borderColor={t.color.border} borderStyle="round" flexDirection="column" paddingX={1}>
      <Text bold color={accent}>
        {req.danger ? '⚠' : '?'} {req.title ?? req.prompt ?? 'Continue?'}
      </Text>

      {req.detail ? (
        <Box paddingLeft={1}>
          <Text color={t.color.text} wrap="truncate-end">
            {req.detail}
          </Text>
        </Box>
      ) : null}

      <Text />

      {rows.map((row, i) => (
        <Text key={row.label}>
          <Text color={sel === i ? accent : t.color.muted}>{sel === i ? '▸ ' : '  '}</Text>
          <Text color={sel === i ? row.color : t.color.muted}>{row.label}</Text>
        </Text>
      ))}

      <Text color={t.color.muted}>↑/↓ select · Enter confirm · Y/N quick · Esc cancel</Text>
    </Box>
  )
}

interface ApprovalPromptProps {
  onChoice: (s: string) => void
  req: ApprovalReq
  t: Theme
}

interface ClarifyPromptProps {
  cols?: number
  onAnswer: (s: string) => void
  onCancel: () => void
  req: ClarifyReq
  t: Theme
}

interface ConfirmPromptProps {
  onCancel: () => void
  onConfirm: () => void
  req: ConfirmReq
  t: Theme
}
