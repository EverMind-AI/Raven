// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'

import type { DirectTargetRef } from '../app/directChatStore.js'
import type { InstanceRow } from '../rpc/generated.js'
import type { Theme } from '../theme.js'

import { $directChat, enterDirect, isDirectTarget, leaveDirect } from '../app/directChatStore.js'

export interface Chip {
  active: boolean
  label: string
  running: boolean
  target: DirectTargetRef | null
}

export const MAIN_CHIP_LABEL = 'Raven'

// `[label]` plus the separating space; a running chip adds its bullet.
const chipWidth = (c: Chip) => c.label.length + (c.running ? 4 : 3)

/**
 * Which chips fit in `cols`, and how many were dropped.
 *
 * Pure and exported so the fitting rule is testable without a renderer.
 * Two chips are never dropped: the main-agent chip, which is the way back, and
 * the active one -- a strip that hides where you are is worse than a truncated
 * one. Truncation is from the right, so the instances this session used first
 * are the ones that survive.
 */
export function chipsForWidth(
  rows: readonly InstanceRow[],
  active: DirectTargetRef | null,
  cols: number
): { chips: Chip[]; overflow: number } {
  // A dag-node row is a node of a `run_subagent_dag` graph, not something a
  // user can address: its handle is `<runId>/<nodeId>`, minted by the runner,
  // and continuing one has no resume story yet (spec, Follow-ups). The same
  // fan-out also registers an ordinary instance row per node, which is the one
  // that belongs here -- so dropping these hides nothing.
  // Ordered by when each instance first appeared, and so never reordered under
  // the user: `updatedAtMs` bumps on every status change, which -- now that
  // several instances answer at once -- moved the chips around mid-conversation.
  // The one painted as active would slide sideways while its neighbour took its
  // place, which reads as the highlight jumping between agents rather than as
  // the strip resorting.
  const ordered = rows.filter(r => r.kind !== 'dag-node').sort((a, b) => (a.createdAtMs ?? 0) - (b.createdAtMs ?? 0))
  const all: Chip[] = [
    { active: active === null, label: MAIN_CHIP_LABEL, running: false, target: null },
    ...ordered.map(r => ({
      active: isDirectTarget(active, { agent: r.agent, handle: r.handle }),
      label: `${r.agent}/${r.handle}`,
      // 'interrupted' is deliberately not running: the row says so only because
      // a gateway died mid-spawn, and a bullet there would promise a reply that
      // is never coming.
      running: r.status === 'running' || r.status === 'pending',
      target: { agent: r.agent, handle: r.handle }
    }))
  ]

  const kept: Chip[] = [all[0]!]
  let used = chipWidth(all[0]!)

  for (const chip of all.slice(1)) {
    const next = used + chipWidth(chip)
    if (next <= cols || chip.active) {
      kept.push(chip)
      used = next
    }
  }

  return { chips: kept, overflow: all.length - kept.length }
}

/**
 * The colour a chip is painted in.
 *
 * Pure and exported for the same reason `chipsForWidth` is: the rule is worth
 * testing without a renderer. `accent` rather than `label` for the active chip
 * because label and muted are *the same value* in the default dark theme and in
 * the 256-colour one -- asking for those two painted every chip identically and
 * left bold as the only cue to which conversation you were in.
 */
export const chipColor = (chip: Chip, t: Theme): string => (chip.active ? t.color.accent : t.color.muted)

/** The chip immediately before / after the active one, wrapping at both ends. */
export function cycleTarget(chips: readonly Chip[], step: -1 | 1): DirectTargetRef | null {
  if (chips.length === 0) {
    return null
  }

  const at = chips.findIndex(c => c.active)
  const from = at === -1 ? 0 : at
  return chips[(from + step + chips.length) % chips.length]!.target
}

export interface InstanceChipsProps {
  cols: number
  t: Theme
}

/**
 * The session's sub-agent instances, above the composer.
 *
 * One row, never wrapped: `ComposerPane` is `flexShrink={0}`, so a second row
 * costs a transcript row -- which is what `chipsForWidth` is for.
 */
export function InstanceChips({ cols, t }: InstanceChipsProps) {
  const direct = useStore($directChat)

  if (direct.instances.length === 0) {
    return null
  }

  const { chips, overflow } = chipsForWidth(direct.instances, direct.active, cols)

  return (
    <Box>
      {chips.map(chip => (
        <Box
          key={chip.target === null ? MAIN_CHIP_LABEL : `${chip.target.agent}/${chip.target.handle}`}
          marginRight={1}
          // On the Box, not the Text: only Box carries mouse props in this
          // renderer, so a handler on the Text is silently never called.
          onClick={() => (chip.target === null ? leaveDirect() : enterDirect(chip.target.agent, chip.target.handle))}
        >
          <Text bold={chip.active} color={chipColor(chip, t)}>
            [{chip.running ? '•' : ''}
            {chip.label}]
          </Text>
        </Box>
      ))}

      {overflow > 0 && <Text color={t.color.muted}>+{overflow}</Text>}

      {direct.pendingHandoffCount > 0 && <Text color={t.color.muted}> ↩{direct.pendingHandoffCount}</Text>}
    </Box>
  )
}
