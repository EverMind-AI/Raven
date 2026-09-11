// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { Box, Text, useInput } from '@hermes/ink'
import { useEffect, useRef, useState } from 'react'

import type { Theme } from '../theme.js'
import type { ApprovalReq, ClarifyReq, ConfirmReq } from '../types.js'

import { t as tr } from '../i18n/index.js'
import { approvalOptionsFor, approvalRemainingSeconds } from '../lib/approval.js'
import { CONFIRM_COUNTDOWN_SECONDS, tickCountdown } from '../lib/confirmCountdown.js'
import { isMac } from '../lib/platform.js'
import { TextInput } from './textInput.js'

const CMD_PREVIEW_LINES = 10

// 90 -> "1m 30s", 45 -> "45s". Seconds are padded so the line keeps its
// width as the countdown runs and the prompt below it does not jitter.
//
// The payload is a remaining-time subtraction, so it arrives fractional: a
// 90-second budget shows up as 89.99999912502244. Ceil before the branch, not
// inside it -- 59.7 belongs to the minutes shape, and rounding after the test
// would render it as "60s". Ceil rather than round keeps the last second on
// screen until the deadline has actually passed.
const clarifyRemainingText = (secs: number): string => {
  const whole = Math.ceil(secs)

  return whole < 60 ? `${whole}s` : `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, '0')}s`
}

export function ApprovalPrompt({ cols = 80, onChoice, req, t }: ApprovalPromptProps) {
  const options = approvalOptionsFor(req.suggestedPattern)
  // Deny-and-continue is the default: it matches what a timeout does, and an
  // accidental Enter must never grant authority or kill the turn.
  const [sel, setSel] = useState(() => options.findIndex(o => o.choice === 'deny'))
  // The deny choice a note is being attached to, or null when not typing one.
  const [noting, setNoting] = useState<null | string>(null)
  const [note, setNote] = useState('')
  // The prefix rule being edited before it is saved, or null when not editing.
  // Seeded from the runtime's suggestion; what is sent is what the human left.
  const [pattern, setPattern] = useState<null | string>(null)
  // The request this state belongs to. The mount site keys the component by
  // request id, and this is the same guarantee from the inside: should a new
  // request ever reach a reused instance, its selection and any half-typed
  // prefix or note start over rather than being sent under the new id.
  const [forId, setForId] = useState(req.approvalId)

  if (forId !== req.approvalId) {
    setForId(req.approvalId)
    setSel(options.findIndex(o => o.choice === 'deny'))
    setNoting(null)
    setNote('')
    setPattern(null)
  }
  const [remainingSeconds, setRemainingSeconds] = useState(() => approvalRemainingSeconds(req.expiresAt))

  useEffect(() => {
    // The runtime sends an absolute wall-clock deadline rather than a duration.
    // Recomputing from that value matters when rendering is delayed or the
    // terminal process is briefly suspended: mounting the component must not
    // accidentally grant a fresh approval window.
    //
    // The countdown reaching zero answers nothing. It used to send `deny`, and
    // that was the one place a lapse became a refusal: the runtime's own hard
    // ceiling sits a few seconds past this deadline precisely so it can be the
    // party that decides, and answering first took the decision away from it and
    // spelled "nobody was here" as "the user said no". The runtime closes this
    // overlay with `approval.closed` when its ceiling fires; until then a key
    // pressed inside that window is still a real answer and still lands.
    const update = () => setRemainingSeconds(approvalRemainingSeconds(req.expiresAt))

    update()
    const timer = setInterval(update, 250)

    return () => clearInterval(timer)
  }, [req.expiresAt])

  // Zero seconds is not a shorter deadline, it IS the deadline. The runtime's
  // ceiling sits a few seconds past it so that a choice made BEFORE it survives
  // event-loop and RPC lag -- transport tolerance, not five more seconds of
  // authority. `resolve` cannot tell the two apart (measured: an `allow` sent
  // after the visible deadline is accepted and runs), so the prompt is what has
  // to stop offering. It answers nothing either way; the runtime closes it.
  const expired = remainingSeconds === 0

  useInput((ch, key) => {
    if (expired) {
      return
    }

    if (noting !== null || pattern !== null) {
      if (key.escape) {
        setNoting(null)
        setPattern(null)
      }

      return
    }

    if (key.upArrow && sel > 0) {
      setSel(s => s - 1)
    }

    if (key.downArrow && sel < options.length - 1) {
      setSel(s => s + 1)
    }

    // A note only makes sense on a refusal: it travels to the model as the
    // reason, and an allowed call needs none.
    if (key.tab && options[sel]!.choice.startsWith('deny')) {
      setNote('')
      setNoting(options[sel]!.choice)

      return
    }

    // The persisted grant is not sent on the keypress that picked it: the
    // prefix opens for editing first, and Enter there is what sends.
    const pick = (option: (typeof options)[number]) => {
      if (option.choice === 'allow_always') {
        setPattern(req.suggestedPattern ?? '')
      } else {
        onChoice(option.choice, '', req.approvalId)
      }
    }

    const n = parseInt(ch, 10)

    if (n >= 1 && n <= options.length) {
      pick(options[n - 1]!)

      return
    }

    if (key.return) {
      pick(options[sel]!)
    }
  })

  const rawLines = req.command.split('\n')
  const shown = rawLines.slice(0, CMD_PREVIEW_LINES)
  const overflow = rawLines.length - shown.length

  return (
    <Box borderColor={t.color.border} borderStyle="round" flexDirection="column" paddingX={1}>
      <Text bold color={t.color.warn}>
        ⚠ {tr('gui.confirm.title', 'Approval needed')} · {req.description}
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

      {pattern !== null ? (
        <>
          <Text color={t.color.label}>
            {'  '}
            {tr('gui.confirm.pattern_for', 'prefix rule to save; it applies in every working directory')}
          </Text>

          <Box>
            <Text color={t.color.label}>{'> '}</Text>
            <TextInput
              columns={Math.max(20, cols - 6)}
              onChange={setPattern}
              /* An emptied box saves nothing and sends nothing; Esc is the way
                 back to the list, and a stray Enter must not grant. */
              onSubmit={v =>
                expired || !v.trim() ? undefined : onChoice('allow_always', '', req.approvalId, v.trim())
              }
              value={pattern}
            />
          </Box>

          <Text color={t.color.muted}>
            {tr('gui.confirm.pattern_keys', 'Enter save · Esc back · expires in {n}s', {
              n: String(remainingSeconds)
            })}
          </Text>
        </>
      ) : noting !== null ? (
        <>
          <Text color={t.color.label}>
            {'  '}
            {tr('gui.confirm.note_for', 'note for the model ({what})', {
              what:
                noting === 'deny_stop' ? tr('gui.confirm.deny_stop', 'Deny and stop') : tr('gui.confirm.deny', 'Deny')
            })}
          </Text>

          <Box>
            <Text color={t.color.label}>{'> '}</Text>
            <TextInput
              columns={Math.max(20, cols - 6)}
              onChange={setNote}
              /* Its own guard: `TextInput` submits on Enter through ink's own
                 handler, which `useInput` above never sees. */
              onSubmit={v => (expired ? undefined : onChoice(noting, v.trim(), req.approvalId))}
              value={note}
            />
          </Box>

          <Text color={t.color.muted}>
            {tr('gui.confirm.note_keys', 'Enter send · Esc back · expires in {n}s', {
              n: String(remainingSeconds)
            })}
          </Text>
        </>
      ) : (
        <>
          {options.map((option, i) => (
            <Text key={option.choice}>
              <Text bold={sel === i} color={sel === i ? t.color.warn : t.color.muted} inverse={sel === i}>
                {sel === i ? '▸ ' : '  '}
                {i + 1}. {tr(option.key, option.fallback, { pattern: req.suggestedPattern ?? '' })}
              </Text>
            </Text>
          ))}

          <Text color={t.color.muted}>
            {tr(
              'gui.confirm.keys',
              'up/down select · Enter confirm · 1-{count} quick pick · Tab add note · Ctrl+C deny · expires in {n}s',
              { count: String(options.length), n: String(remainingSeconds) }
            )}
          </Text>
        </>
      )}
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
      {total > 1 ? (
        <Text color={t.color.muted}>
          {' '}
          ({position}/{total})
        </Text>
      ) : null}
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
    } else if (n === choices.length + 1) {
      // The Other row is drawn with a number like every other row, so its
      // number has to reach it too; Enter on the row already does.
      setTyping(true)
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

        <Text color={t.color.muted}>{budget}Enter send · Esc back</Text>
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
        {budget}↑/↓ select · Enter confirm · Tab add note · 1-{choices.length + 1} quick pick · Esc/Ctrl+C cancel
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
  cols?: number
  // The id of the request THIS prompt rendered. An answer belongs to the
  // request the human was looking at (or that the countdown was armed for),
  // never to whichever one happens to occupy the slot when the callback runs.
  onChoice: (s: string, feedback?: string, approvalId?: string, pattern?: string) => void
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
