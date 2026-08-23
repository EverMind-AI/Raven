// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { Box, Text, useInput, useStdout } from '@hermes/ink'
import { useCallback, useEffect, useMemo, useState } from 'react'

import type { GatewayClient } from '../gatewayClientStub.js'
import type { SubagentRow, SubagentsListResult, SubagentsProbeResult, SubagentsTestResult } from '../rpc/generated.js'
import type { Theme } from '../theme.js'

import { fmtDuration } from '../domain/messages.js'
import { rpcErrorMessage } from '../lib/rpc.js'
import { OverlayHint, windowItems } from './overlayControls.js'
import { Spinner } from './thinking.js'

const MIN_WIDTH = 40
const MAX_WIDTH = 90
const VISIBLE = 12

const STATUS_GLYPH: Record<SubagentRow['probe_status'], string> = {
  attention: '!',
  missing: '○',
  ready: '●',
  unknown: '?'
}

/**
 * Row-flattening and offset computation, pulled out as a pure function so the
 * concatenation order and the index-to-row mapping are unit-testable without
 * rendering anything or sending a keystroke. `flat` is what `idx` indexes
 * into; `installedCount`/`uninstalledCount`/`presetsCount` are the section
 * sizes in `flat`'s order (installed, then not-installed, then presets),
 * which both the selection math and the section headers key off.
 *
 * `uninstalled` is keyed on `group` rather than on whether an entry was saved:
 * a row whose launch command is not on PATH belongs there whether or not it was
 * configured, because nothing can make it run from this overlay and offering it
 * under presets only invites a failed add. It holds both of the kinds that have
 * a command to find -- `cli` and `acp`.
 *
 * A `builtin` row is raven's own in-process agent. It is `configured: false`
 * (not writing a row is how "use the package's default" is spelled) and it is
 * never probed, so both of the predicates below would misfile it -- the first
 * into "not installed", which is the opposite of true for a loop running in this
 * very process. It is placed under `installed` on its kind alone.
 *
 * An `openai` row never lands there, no matter what `group` says. The backend
 * derives its group from whether an api key is set (see `_group`), so "not
 * installed" for that kind means "no key yet" -- and the key is typed into the
 * add/edit form reached from these very sections. Filing it as unavailable
 * would hide the one control that fixes it. So an openai row is placed on
 * `configured` alone: saved ones under `installed`, the rest under `presets`,
 * where a keyless preset is in fact addable. `probe_detail` on the row still
 * reports the missing key, so the state stays visible either way.
 *
 * Stated as one predicate so the three sections are visibly exhaustive and
 * disjoint: `uninstalled` is its exact complement, and `installed`/`presets`
 * split the rest on `configured`. No row can fall out of every section.
 */
export function flattenSubagentRows(rows: SubagentRow[]): FlattenedSubagentRows {
  // A built-in agent is this process; an openai row always has an endpoint to
  // reach. Every other kind launches a command that may not be there, so the
  // test is the probe-derived `group`, not the kind.
  const isBuiltin = (r: SubagentRow) => r.kind === 'builtin'
  const hasSomethingToRun = (r: SubagentRow) => isBuiltin(r) || r.kind === 'openai' || r.group === 'installed'
  const installed = rows.filter(r => (r.configured || isBuiltin(r)) && hasSomethingToRun(r))
  const uninstalled = rows.filter(r => !hasSomethingToRun(r))
  const presets = rows.filter(r => !r.configured && !isBuiltin(r) && hasSomethingToRun(r))

  return {
    flat: [...installed, ...uninstalled, ...presets],
    installedCount: installed.length,
    presetsCount: presets.length,
    uninstalledCount: uninstalled.length
  }
}

export interface FlattenedSubagentRows {
  flat: SubagentRow[]
  installedCount: number
  presetsCount: number
  uninstalledCount: number
}

/** A mutation's follow-up list call passes `probe: false` (no network probe),
 *  so every row comes back `probe_status: 'unknown'`. Carrying forward the
 *  probe columns a caller already had, keyed by name, means a toggle/test/
 *  add/remove does not visibly regress every row to unknown -- only the
 *  columns that a real probe (initial load or the `r` key) last set are
 *  shown, everything else (enabled, test_running, last_test_*, ...) comes
 *  from the fresh response.
 *
 *  `group` needs the same treatment, for every kind whose group the backend
 *  derives from `probe_status` -- which is every kind it does not derive some
 *  other way. `probe: false` legitimately reports all of those as
 *  `uninstalled`, and carrying the prior group forward keeps an installed
 *  agent from visibly relocating to NOT INSTALLED on every mutation. The two
 *  exceptions take the fresh value because it is never stale: an `openai`
 *  row's group comes from whether a key is set, so carrying it forward would
 *  keep the row reading uninstalled right after the user pastes a key, and a
 *  `builtin` row's group is a constant. Keyed on that split rather than on
 *  the probe-derived kinds by name, so a kind added to `_group`'s fallthrough
 *  branch is covered here the day it lands -- naming them is what let `acp`
 *  relocate three configured agents on every toggle.
 *
 *  `renamed` covers `subagents.update` with a `new_name`: the fresh row is
 *  keyed by the new name, which has no prior entry, so the caller (the only
 *  place that knows both names) passes the old name to look the prior row up
 *  by instead. */
export function mergeProbeColumns(
  previous: SubagentRow[],
  fresh: SubagentRow[],
  renamed?: { from: string; to: string }
): SubagentRow[] {
  const priorByName = new Map(previous.map(r => [r.name, r]))

  return fresh.map(row => {
    const prior =
      priorByName.get(row.name) ?? (renamed && row.name === renamed.to ? priorByName.get(renamed.from) : undefined)

    if (!prior) {
      return row
    }

    const probed = { ...row, probe_detail: prior.probe_detail, probe_status: prior.probe_status }
    const groupFromProbe = row.kind !== 'openai' && row.kind !== 'builtin'

    return groupFromProbe && row.probe_status === 'unknown' ? { ...probed, group: prior.group } : probed
  })
}

/** Names with a test in flight, from server truth (`row.test_running`) union
 *  local state (a test this component instance just started but whose
 *  effect on `rows` has not come back yet). Used both to decide whether Esc
 *  cancels instead of closing, and to pick which name(s) it cancels -- so a
 *  test started on a row that is no longer selected, or one still running
 *  from before the overlay was closed and reopened, is still reachable. */
export function runningTestNames(rows: SubagentRow[], testing: Map<string, number>): string[] {
  const names = new Set(testing.keys())

  for (const row of rows) {
    if (row.test_running) {
      names.add(row.name)
    }
  }

  return [...names]
}

/** Same thresholds as the web UI's `ageText` (SubagentStatus.tsx) - reimplemented
 *  locally rather than imported across apps. Pure so it stays easy to test. */
function ageText(ms: number | null | undefined): string {
  if (ms == null) {
    return ''
  }

  const mins = Math.max(0, Math.round((Date.now() - ms) / 60000))

  if (mins < 1) {
    return '<1m'
  }

  if (mins < 60) {
    return `${mins}m`
  }

  const hours = Math.round(mins / 60)

  if (hours < 24) {
    return `${hours}h`
  }

  return `${Math.round(hours / 24)}d`
}

/** The failure-detail line for the selected row, or null when none should
 *  render -- only a row whose most recent test actually failed and left a
 *  detail string gets one, so moving the cursor off a failed row (or onto a
 *  passing/untested one) hides it again. Pulled out as a pure function per
 *  this file's convention of keeping keystroke-driven terminal assertions to
 *  a minimum (see `flattenSubagentRows` above) -- the "hides when the
 *  selection moves" half of this behaviour is exercised here, not through a
 *  live keypress + frame snapshot. */
export function failureDetailLine(row: SubagentRow | undefined): null | string {
  if (!row || row.last_test_ok !== false || !row.last_test_detail) {
    return null
  }

  return `${row.name}: ${row.last_test_detail}`
}

function statusCell(row: SubagentRow): string {
  if (row.last_test_ok === true) {
    const age = ageText(row.last_test_at_ms)

    return age ? `ok ${age}` : 'ok'
  }

  if (row.last_test_ok === false) {
    const age = ageText(row.last_test_at_ms)

    return age ? `failed ${age}` : 'failed'
  }

  return row.probe_detail
}

/** A leaf-rendered clock: its own interval, so a running test's elapsed
 *  seconds don't force the whole row list to re-render every tick. */
function TestingCell({ startedAtMs, t }: { startedAtMs: number; t: Theme }) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)

    return () => clearInterval(id)
  }, [])

  return (
    <Text color={t.color.warn}>
      <Spinner color={t.color.warn} variant="tool" /> {fmtDuration(now - startedAtMs)}
    </Text>
  )
}

function SubagentRowLine({
  row,
  selected,
  startedAt,
  t
}: {
  row: SubagentRow
  selected: boolean
  startedAt: number | undefined
  t: Theme
}) {
  const glyph = STATUS_GLYPH[row.probe_status] ?? '?'
  // `[new]` means "not added yet", which a built-in row never is: it is on the
  // table already, so its switch state is the whole truth about it.
  const toggleLabel = row.configured || row.kind === 'builtin' ? (row.enabled ? '[on ]' : '[off]') : '[new]'

  // The gap between the two columns is a margin, never a `<Text> </Text>`
  // child. A text node is flex-shrinkable: once name + detail exceed the
  // overlay width, yoga shrinks the spacer, its wrap measure comes back two
  // rows tall, and the whole row renders as two screen lines -- an apparently
  // blank line under any row long enough to overflow, and the name column
  // truncated hard enough to lose its `[on ]`. `flexShrink={0}` keeps the name
  // column whole and makes the detail absorb the shrink on its own.
  return (
    <Box flexDirection="row">
      <Box flexShrink={0} marginRight={1}>
        <Text bold={selected} color={selected ? t.color.accent : t.color.muted} inverse={selected} wrap="truncate-end">
          {selected ? '▸ ' : '  '}
          {glyph} {row.name} · {row.preset ?? '-'} {toggleLabel}
        </Text>
      </Box>
      {startedAt != null ? (
        <TestingCell startedAtMs={startedAt} t={t} />
      ) : (
        <Text color={t.color.muted} wrap="truncate-end">
          {statusCell(row)}
        </Text>
      )}
    </Box>
  )
}

const SECTION_TITLES = { installed: 'INSTALLED', presets: 'AVAILABLE PRESETS', uninstalled: 'NOT INSTALLED' } as const

/** Which list the overlay is showing: the roster, or the drill-in that holds
 *  the not-installed rows the roster folds away. */
export type SubagentView = 'main' | 'uninstalled'

/** One selectable entry. `idx` indexes into an array of these, so the
 *  not-installed group has to be one of them rather than a header: on the
 *  main view it is a single line the user selects and opens, and the rows it
 *  stands for are not selectable there at all. */
export type SubagentItem = { kind: 'group'; rows: SubagentRow[] } | { kind: 'row'; row: SubagentRow }

/** One renderable line: a section header, or a selectable item tagged with its
 *  index. Headers and items share one array so `windowItems` can window across
 *  the whole list in render order -- a section boundary is not a reason for the
 *  selected row to be able to scroll out of view. */
export type DisplayLine = { item: SubagentItem; itemIndex: number; kind: 'item' } | { kind: 'header'; title: string }

export interface SubagentDisplay {
  items: SubagentItem[]
  lines: DisplayLine[]
}

/** The selectable entries and the lines that render them, for one view.
 *
 *  On `main` the not-installed rows collapse into a single `group` entry:
 *  a roster's uninstalled presets are the rows a user is least likely to act
 *  on, so they cost one line instead of N until Enter drills into them. The
 *  `uninstalled` view is that drill-in, and the only place those rows can be
 *  selected -- which is why `items` is derived per view rather than being
 *  `flattenSubagentRows`'s `flat` in every case. Pure, per this file's
 *  convention of keeping keystroke-driven terminal assertions to a minimum. */
export function buildSubagentView(rows: SubagentRow[], view: SubagentView): SubagentDisplay {
  const { flat, installedCount, uninstalledCount } = flattenSubagentRows(rows)
  const installed = flat.slice(0, installedCount)
  const uninstalled = flat.slice(installedCount, installedCount + uninstalledCount)
  const presets = flat.slice(installedCount + uninstalledCount)

  const items: SubagentItem[] = []
  const lines: DisplayLine[] = []
  const push = (item: SubagentItem) => {
    lines.push({ item, itemIndex: items.length, kind: 'item' })
    items.push(item)
  }

  if (view === 'uninstalled') {
    if (uninstalled.length) {
      lines.push({ kind: 'header', title: SECTION_TITLES.uninstalled })

      for (const row of uninstalled) {
        push({ kind: 'row', row })
      }
    }

    return { items, lines }
  }

  if (installed.length) {
    lines.push({ kind: 'header', title: SECTION_TITLES.installed })

    for (const row of installed) {
      push({ kind: 'row', row })
    }
  }

  if (presets.length) {
    lines.push({ kind: 'header', title: SECTION_TITLES.presets })

    for (const row of presets) {
      push({ kind: 'row', row })
    }
  }

  // Last, below every actionable row: nothing in here can be configured, so it
  // is the least likely thing on the roster to be wanted and has no claim on
  // the reading order the actionable sections need.
  if (uninstalled.length) {
    push({ kind: 'group', rows: uninstalled })
  }

  return { items, lines }
}

/** The collapsed not-installed group, as one selectable line. Carries its own
 *  count so the roster still says how much is hidden behind it. */
function GroupLine({ count, selected, t }: { count: number; selected: boolean; t: Theme }) {
  return (
    <Text bold color={selected ? t.color.accent : t.color.label} inverse={selected} wrap="truncate-end">
      {selected ? '▸ ' : '  '}
      {SECTION_TITLES.uninstalled} ({count}) - Enter to open
    </Text>
  )
}

type Stage = 'confirm-delete' | 'form' | 'list'
type FormMode = 'add' | 'edit'
type FormField = 'description' | 'key' | 'name'

/** One labeled input line for the form stage. Marker/color/masking mirror
 *  the key-entry stage in modelPicker.tsx exactly (`▸ `/`  `, accent/muted,
 *  caret only when not saving) so a secret typed here is never rendered
 *  in the clear -- `display` carries the masked text for the key field,
 *  and callers must never pass the raw input through it. */
function FormFieldLine({
  display,
  focused,
  label,
  saving,
  t,
  value
}: {
  display?: string
  focused: boolean
  label: string
  saving: boolean
  t: Theme
  value: string
}) {
  const caret = saving ? '' : '▎'

  // Both lines of a field carry the same colour, so focus is what the accent
  // marks. modelPicker.tsx keeps every value line accent regardless of focus,
  // which reads as "all rows are live" -- fine there, where Enter only commits
  // from the last field, but wrong here now that Enter commits from any field:
  // the accent has to say which field the caret and the next keystroke belong
  // to, or three equally bright values leave that ambiguous.
  const color = focused ? t.color.accent : t.color.muted

  return (
    <>
      <Text color={color} wrap="truncate-end">
        {focused ? '▸ ' : '  '}
        {label}:
      </Text>

      <Text color={color} wrap="truncate-end">
        {'  '}
        {(display ?? value) || '(empty)'}
        {focused ? caret : ''}
      </Text>
    </>
  )
}

export function SubagentsHub({ gw, onClose, t }: SubagentsHubProps) {
  const [rows, setRows] = useState<SubagentRow[]>([])
  const [idx, setIdx] = useState(0)
  const [view, setView] = useState<SubagentView>('main')
  const [stage, setStage] = useState<Stage>('list')
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  // name -> the ms timestamp the test started, so the elapsed counter is derived
  // rather than stored, and a finished test is removed instead of flagged.
  const [testing, setTesting] = useState<Map<string, number>>(new Map())
  const [formMode, setFormMode] = useState<FormMode>('add')
  const [nameInput, setNameInput] = useState('')
  const [descInput, setDescInput] = useState('')
  const [keyInput, setKeyInput] = useState('')
  const [field, setField] = useState<FormField>('name')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')

  const { stdout } = useStdout()
  const width = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, (stdout?.columns ?? 80) - 6))

  const load = useCallback(() => {
    gw.request<SubagentsListResult>('subagents.list', {})
      .then(r => {
        setRows(r?.rows ?? [])
        setErr('')
      })
      .catch((e: unknown) => setErr(rpcErrorMessage(e)))
      .finally(() => setLoading(false))
  }, [gw])

  useEffect(() => {
    load()
  }, [load])

  // Unlike `load`, a mutation's follow-up list skips the network probe
  // (`probe: false`) -- a toggle against an unreachable endpoint must not
  // cost the same up-to-10s-per-entry probe the initial load already paid
  // for. `mergeProbeColumns` keeps the probe columns already on screen so
  // the rows do not flash to 'unknown' on every keystroke.
  const refresh = useCallback(
    (renamed?: { from: string; to: string }) => {
      gw.request<SubagentsListResult>('subagents.list', { probe: false })
        .then(r => setRows(prev => mergeProbeColumns(prev, r?.rows ?? [], renamed)))
        .catch((e: unknown) => setErr(rpcErrorMessage(e)))
    },
    [gw]
  )

  // Reconciles local `testing` (spinner + elapsed-seconds source) against
  // server truth on every rows update: a row `test_running` is now true and
  // untracked (this overlay was reopened mid-test, or reflects a test
  // started elsewhere) gets a start time seeded for the elapsed counter;
  // one that has gone false gets its entry dropped. This runs off `rows`,
  // not `testing`, so it never fires between a `runTest` call and its own
  // next refresh -- an entry this instance just seeded is never clobbered
  // before the server has had a chance to reflect it.
  useEffect(() => {
    setTesting(prev => {
      let changed = false
      const next = new Map(prev)

      for (const row of rows) {
        if (row.test_running && !next.has(row.name)) {
          next.set(row.name, Date.now())
          changed = true
        } else if (!row.test_running && next.has(row.name)) {
          next.delete(row.name)
          changed = true
        }
      }

      return changed ? next : prev
    })
  }, [rows])

  // Both views are built on every rows change, not just the active one: leaving
  // the drill-in has to land the cursor back on the group entry it came
  // through, which means knowing that entry's index while still inside the
  // drill-in.
  const main = useMemo(() => buildSubagentView(rows, 'main'), [rows])
  const drilldown = useMemo(() => buildSubagentView(rows, 'uninstalled'), [rows])
  const { items, lines } = view === 'main' ? main : drilldown
  const selectedItem = items[idx]
  const selected = selectedItem?.kind === 'row' ? selectedItem.row : undefined
  const runningNames = useMemo(() => runningTestNames(rows, testing), [rows, testing])

  useEffect(() => {
    setIdx(i => Math.min(i, Math.max(0, items.length - 1)))
  }, [items.length])

  // Installing the last not-installed agent (or removing it) empties the
  // drill-in, which would otherwise leave the overlay on a blank list whose
  // only exit is Esc. Bounce back to the roster instead.
  useEffect(() => {
    if (view === 'uninstalled' && drilldown.items.length === 0) {
      setView('main')
    }
  }, [drilldown.items.length, view])

  const openDrilldown = () => {
    setView('uninstalled')
    setIdx(0)
  }
  const backToMain = () => {
    setView('main')
    setIdx(
      Math.max(
        0,
        main.items.findIndex(i => i.kind === 'group')
      )
    )
  }

  // Windowed over `lines` (headers included, in render order) rather than
  // per-section, so "the selected row never scrolls out of view" holds across
  // a section boundary instead of only within the selected row's own section.
  const selectedLine = Math.max(
    0,
    lines.findIndex(l => l.kind === 'item' && l.itemIndex === idx)
  )
  const { items: visibleLines, offset } = windowItems(lines, selectedLine, VISIBLE)

  const toggle = (row: SubagentRow) => {
    if (!row.configured) {
      setErr(`${row.name} is only a preset - press Enter to add it before it can be toggled`)

      return
    }

    gw.request('subagents.toggle', { enabled: !row.enabled, name: row.name })
      .then(() => {
        setErr('')
        refresh()
      })
      .catch((e: unknown) => setErr(rpcErrorMessage(e)))
  }

  const runTest = (row: SubagentRow) => {
    if (testing.has(row.name)) {
      return
    }

    setTesting(prev => new Map(prev).set(row.name, Date.now()))

    gw.request<SubagentsTestResult>('subagents.test', {
      name: row.name,
      source: row.configured ? 'config' : 'preset'
    })
      .then(r => {
        setErr(r && r.ok === false && !r.cancelled ? r.detail : '')
      })
      .catch((e: unknown) => setErr(rpcErrorMessage(e)))
      .finally(() => {
        setTesting(prev => {
          const next = new Map(prev)
          next.delete(row.name)

          return next
        })
        refresh()
      })
  }

  const probe = () => {
    gw.request<SubagentsProbeResult>('subagents.probe', {})
      .then(r => {
        setRows(r?.rows ?? [])
        setErr('')
      })
      .catch((e: unknown) => setErr(rpcErrorMessage(e)))
  }

  const openForm = (row: SubagentRow) => {
    setFormMode(row.configured ? 'edit' : 'add')
    setNameInput(row.name)
    setDescInput(row.description)
    // A stored key is never echoed back, masked or not: the field opens
    // empty even in edit mode, and the label (rendered below) says so.
    setKeyInput('')
    setField('name')
    setFormError('')
    setStage('form')
  }

  const submitForm = () => {
    if (!selected) {
      return
    }

    const trimmedName = nameInput.trim()

    // The backend now trims and rejects blank-after-trim too, but there is
    // no reason to pay a round trip for input the client can already tell
    // will fail -- show the same error inline instead.
    if (!trimmedName) {
      setFormError('name cannot be blank')

      return
    }

    setSaving(true)
    setFormError('')

    const trimmedKey = keyInput.trim()
    const params =
      formMode === 'add'
        ? {
            description: descInput,
            name: trimmedName,
            preset: selected.preset,
            ...(trimmedKey ? { api_key: trimmedKey } : {})
          }
        : {
            description: descInput,
            name: selected.name,
            new_name: trimmedName,
            ...(trimmedKey ? { api_key: trimmedKey } : {})
          }

    const renamedFrom = formMode === 'edit' && selected.name !== trimmedName ? selected.name : undefined

    gw.request(formMode === 'add' ? 'subagents.add' : 'subagents.update', params)
      .then(() => {
        setSaving(false)
        setKeyInput('')
        setStage('list')
        refresh(renamedFrom ? { from: renamedFrom, to: trimmedName } : undefined)
      })
      .catch((e: unknown) => {
        setSaving(false)
        setFormError(rpcErrorMessage(e))
      })
  }

  const removeSelected = () => {
    if (!selected) {
      setStage('list')

      return
    }

    setSaving(true)
    gw.request('subagents.remove', { name: selected.name })
      .then(() => {
        setSaving(false)
        setStage('list')
        refresh()
      })
      .catch((e: unknown) => {
        setSaving(false)
        setStage('list')
        setErr(rpcErrorMessage(e))
      })
  }

  useInput((ch, key) => {
    // esc must work no matter what `stage` is: a stage that swallows every
    // key because it has no handling of its own yet would trap the user with
    // no way out short of killing the TUI. Handled ahead of the stage guard
    // below so this stays true for every stage Task 7 adds. Inside the form
    // or the delete confirm, esc backs out to the list rather than closing
    // the whole overlay -- still an exit, never a dead end.
    if (key.escape) {
      if (stage === 'form' || stage === 'confirm-delete') {
        setStage('list')
        setFormError('')
        setKeyInput('')

        return
      }

      // Cancels every running test, not just the selected row's: testing A,
      // moving the cursor to B, then pressing Esc must still reach A -- a
      // test does not stop being in flight just because the cursor moved.
      // Ranked above backing out of the drill-in for the same reason it is
      // ranked above closing: a test in flight is the more urgent thing Esc
      // can be about, and the next press still gets you out.
      if (runningNames.length === 0 && view === 'uninstalled') {
        backToMain()

        return
      }

      if (runningNames.length > 0) {
        // A locally-started test's own `subagents.test` promise refreshes on
        // its way out (see `runTest`'s `finally`), but a test known only
        // from `row.test_running` (a reopened overlay, or another client's
        // test) has no such promise -- nothing else would ever clear it from
        // `rows`, so every later Esc would cancel again instead of closing.
        // Refresh unconditionally, even if the cancel call itself rejects:
        // Esc closing eventually is the invariant, not the cancel succeeding.
        Promise.all(runningNames.map(name => gw.request('subagents.test_cancel', { name }).catch(() => {}))).then(() =>
          refresh()
        )
      } else {
        onClose()
      }

      return
    }

    // 'q' only closes from the list: the form stage is free text entry, and
    // a row's name/description can legitimately contain the letter q.
    if (ch === 'q' && stage === 'list') {
      onClose()

      return
    }

    if (stage === 'form') {
      if (!selected || saving) {
        return
      }

      const fields: FormField[] = selected.kind === 'openai' ? ['name', 'description', 'key'] : ['name', 'description']
      const setForField = (f: FormField) =>
        f === 'name' ? setNameInput : f === 'description' ? setDescInput : setKeyInput

      if (key.tab) {
        const at = fields.indexOf(field)
        const delta = key.shift ? -1 : 1
        setField(fields[(at + delta + fields.length) % fields.length]!)

        return
      }

      if (key.return) {
        submitForm()

        return
      }

      if (ch === 's' && key.ctrl) {
        submitForm()

        return
      }

      if (key.backspace || key.delete) {
        setForField(field)(v => v.slice(0, -1))

        return
      }

      if (ch && !key.ctrl && !key.meta) {
        setForField(field)(v => v + ch)
      }

      return
    }

    if (stage === 'confirm-delete') {
      if (!selected || saving) {
        return
      }

      if (ch.toLowerCase() === 'y') {
        removeSelected()

        return
      }

      // Anything else backs out without deleting -- there is no destructive
      // default here.
      setStage('list')

      return
    }

    if (loading) {
      return
    }

    if (key.upArrow) {
      if (idx > 0) {
        setIdx(i => i - 1)
      }

      return
    }

    if (key.downArrow) {
      if (idx < items.length - 1) {
        setIdx(i => i + 1)
      }

      return
    }

    // Left is the drill-in's other exit, alongside Esc: a nested list is
    // conventionally left with the key that walks back out of it, and Esc is
    // already overloaded with cancelling a running test.
    if (key.leftArrow && view === 'uninstalled') {
      backToMain()

      return
    }

    // Ahead of the no-row guard below, because this is the one entry on the
    // roster that is not a row.
    if (key.return && selectedItem?.kind === 'group') {
      openDrilldown()

      return
    }

    // Re-probing is roster-wide, so it does not need a row under the cursor --
    // otherwise 'r' would be dead while the collapsed group is selected. It is
    // also the one action the drill-in keeps: a probe only reads, and "did my
    // install land yet" is the question that list exists to answer.
    if (ch.toLowerCase() === 'r') {
      probe()

      return
    }

    // A preset that was never added is view-only: there is nothing configured
    // to act on, so Enter and every configuring key below does nothing. Keyed
    // on the row rather than the view, because this list also holds configured
    // agents whose binary has since gone missing -- losing a binary must not
    // cost the ability to edit or delete the agent, or a broken one becomes
    // unremovable from here.
    if (view === 'uninstalled' && !selected?.configured) {
      return
    }

    if (!selected) {
      return
    }

    // A built-in agent takes exactly one action. There is no connection to edit,
    // no command to test, and deleting is not a thing that exists for it -- so the
    // three keys below are dropped rather than opening a form with nothing in it
    // or spending a turn to "verify" this process.
    if (selected.kind === 'builtin') {
      if (ch === ' ') {
        toggle(selected)
      }

      return
    }

    if (key.return) {
      openForm(selected)

      return
    }

    if (ch === ' ') {
      toggle(selected)

      return
    }

    if (ch.toLowerCase() === 't') {
      runTest(selected)

      return
    }

    if (ch.toLowerCase() === 'd' && selected.configured) {
      setStage('confirm-delete')
    }
  })

  if (loading) {
    return <Text color={t.color.muted}>loading subagents…</Text>
  }

  if (stage === 'form' && selected) {
    const isOpenAI = selected.kind === 'openai'
    // Copied verbatim from modelPicker.tsx's key stage (line 547): the only
    // masking logic this file is allowed to have.
    const masked = keyInput ? '•'.repeat(Math.min(keyInput.length, 40)) : ''
    const keyLabel = selected.has_api_key ? 'API key (stored - blank keeps it)' : 'API key'

    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          {formMode === 'add' ? 'Add subagent' : `Edit subagent: ${selected.name}`}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          Saved to ~/.raven/config.json · Tab switches field
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        <FormFieldLine focused={field === 'name'} label="Name" saving={saving} t={t} value={nameInput} />
        <FormFieldLine focused={field === 'description'} label="Description" saving={saving} t={t} value={descInput} />
        {isOpenAI ? (
          <FormFieldLine
            display={masked}
            focused={field === 'key'}
            label={keyLabel}
            saving={saving}
            t={t}
            value={masked}
          />
        ) : null}

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        {formError ? (
          <Text color={t.color.label} wrap="truncate-end">
            error: {formError}
          </Text>
        ) : saving ? (
          <Text color={t.color.muted} wrap="truncate-end">
            saving…
          </Text>
        ) : (
          <Text color={t.color.muted} wrap="truncate-end">
            {' '}
          </Text>
        )}

        <OverlayHint t={t}>Tab field · Enter/Ctrl+S save · Esc back</OverlayHint>
      </Box>
    )
  }

  if (stage === 'confirm-delete' && selected) {
    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          delete {selected.name}? y/n
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          This removes {selected.name} from ~/.raven/config.json.
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        {saving ? (
          <Text color={t.color.muted} wrap="truncate-end">
            removing…
          </Text>
        ) : (
          <OverlayHint t={t}>y confirm · n/Esc cancel</OverlayHint>
        )}
      </Box>
    )
  }

  if (err && !rows.length) {
    return (
      <Box flexDirection="column" width={width}>
        <Text color={t.color.label}>error: {err}</Text>
        <OverlayHint t={t}>Esc/q cancel</OverlayHint>
      </Box>
    )
  }

  const failureDetail = failureDetailLine(selected)

  return (
    <Box flexDirection="column" width={width}>
      <Text bold color={t.color.accent}>
        Subagents{view === 'uninstalled' ? ` · ${SECTION_TITLES.uninstalled}` : ''}
      </Text>

      {err ? <Text color={t.color.label}>error: {err}</Text> : null}
      {!rows.length ? <Text color={t.color.muted}>no subagents or presets available</Text> : null}

      {offset > 0 && <Text color={t.color.muted}> ↑ {offset} more</Text>}

      {visibleLines.map(line =>
        line.kind === 'header' ? (
          <Text bold color={t.color.label} key={`header:${line.title}`} wrap="truncate-end">
            {line.title}
          </Text>
        ) : line.item.kind === 'group' ? (
          <GroupLine count={line.item.rows.length} key="group:uninstalled" selected={line.itemIndex === idx} t={t} />
        ) : (
          <SubagentRowLine
            key={line.item.row.name}
            row={line.item.row}
            selected={line.itemIndex === idx}
            startedAt={testing.get(line.item.row.name)}
            t={t}
          />
        )
      )}

      {offset + VISIBLE < lines.length && <Text color={t.color.muted}> ↓ {lines.length - offset - VISIBLE} more</Text>}

      {failureDetail ? (
        <Text color={t.color.label} wrap="truncate-end">
          {failureDetail}
        </Text>
      ) : null}

      {runningNames.length > 0 ? <OverlayHint t={t}>Esc cancels the running test</OverlayHint> : null}
      <OverlayHint t={t}>
        {view === 'main'
          ? '↑/↓ select · Enter add/edit/open · space toggle · t test · d delete · r refresh · Esc/q close'
          : selected?.configured
            ? '↑/↓ select · Enter edit · space toggle · t test · d delete · r refresh · ←/Esc back · q close'
            : '↑/↓ select · r refresh · ←/Esc back · q close'}
      </OverlayHint>
      <OverlayHint t={t}>custom agents: web UI /subagents or ~/.raven/config.json</OverlayHint>
    </Box>
  )
}

interface SubagentsHubProps {
  gw: GatewayClient
  onClose: () => void
  t: Theme
}
