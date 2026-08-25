/** Tests for what a conversation gets back when it is opened after a reload. */

// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { _resetForTests as sheetReset } from '../features/composer/sheets'
import { run as dagOpen, start as dagStart, _resetForTests as dagReset } from '../features/dag/mount'
import { openDeskAgent, openDeskAgentRecord, openDeskFile, openDeskTab, getState as deskState, reset as deskLeave, saved as deskSaved, setActive, toggleSolo, updateSplits, _resetForTests as deskReset } from '../features/workspace/deskStore'
import { resume } from './resume'
import { _resetForTests as sessionReset, setCurrent } from './session'
import { reset as agentsLeave, _resetForTests as agentsReset, getState as agentsState } from '../features/subagents/store'

import type { Shell } from './bridge'
import type { DagRun } from '../features/dag/types'
import type { InstanceRow } from '../features/subagents/types'

const NOTE = 'raven.gui.view.dag'

const graph = (id: string): DagRun => ({
  run_id: id,
  session: 's1',
  order: ['one'],
  nodes: new Map([
    ['one', { id: 'one', subagent: 'Researcher', depends_on: [], status: 'pending', started_at: null, ended_at: null }],
  ]),
  summary: null,
  done: false,
  folded: false,
})

const runWire = {
  run_id: 'r1',
  finalized: true,
  task_summary: 'AI news pipeline',
  files: [{ node: 'one', subagent: 'Researcher', depends_on: [], status: 'completed' }],
  summary: { total: 1, completed: 1 },
}

/* What the seams answered, so a test can tell "asked and got nothing" from
   "never asked". */
let readRuns: string[] = []
let listed: InstanceRow[] = []
let instanceCalls = 0
let listFails = false
let listSlow = false
/* A list read the test holds open, so it can do something between the ask and
   the answer -- which is where a conversation switch lands. */
let listGate: Promise<void> | null = null
let openGate: () => void = () => {}
let dagRun: (runId: string) => Promise<unknown> = () => Promise.resolve(runWire)

function wire(): void {
  const shell: Shell = { T: (key) => key, confirmAsk: () => {}, showPage: () => {} }
  window.RavenShell = shell
  window.DS = {
    transcript: {
      dagRun: (runId: string) => {
        readRuns.push(runId)
        return dagRun(runId)
      },
    },
    agents: {
      list: () => Promise.resolve([]),
      instances: () => {
        instanceCalls += 1
        if (listFails) return Promise.reject(new Error('gateway down'))
        if (listGate) return listGate.then(() => listed)
        /* Resolved a few microtasks later when a test asks for it, so a second
           waiter arrives while the first read is still in flight. */
        return listSlow ? Promise.resolve().then(() => Promise.resolve()).then(() => listed) : Promise.resolve(listed)
      },
    },
    subagents: {},
  }
  /* The same wiring main.tsx does: the panel's open verbs reach the desk
     through this bag, so replaying an open lands in a real pane. */
  window.RavenIslands = { workspace: { openAgent: openDeskAgent, openAgentRecord: openDeskAgentRecord } }
  document.body.innerHTML = '<div id="split" data-open="false"></div>'
    + '<div class="chat"><div class="dock"><div class="sheets" id="sheetRack"></div></div></div>'
}

/* The page being replaced: what the stores wrote survives, the stores do not. */
function reload(): void {
  const dag = sessionStorage.getItem(NOTE)
  const deskNote = sessionStorage.getItem('raven.gui.view.desk')
  dagReset()
  deskReset()
  agentsReset()
  sheetReset()
  wire()
  if (dag) sessionStorage.setItem(NOTE, dag)
  if (deskNote) sessionStorage.setItem('raven.gui.view.desk', deskNote)
}

const paneIds = (): string[] => deskState().panes.map((pane) => pane.id)
const sheets = (): number => document.querySelectorAll('#sheetRack .dsheet').length

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  readRuns = []
  listed = []
  instanceCalls = 0
  listFails = false
  listSlow = false
  listGate = null
  openGate = () => {}
  dagRun = () => Promise.resolve(runWire)
  dagReset()
  deskReset()
  agentsReset()
  sheetReset()
  sessionReset()
  setCurrent('s1')
  wire()
})

afterEach(() => {
  dagReset()
  deskReset()
  agentsReset()
  sheetReset()
  sessionReset()
  window.RavenShell = undefined
  window.DS = undefined
  window.RavenIslands = undefined
  document.body.innerHTML = ''
  sessionStorage.clear()
  localStorage.clear()
})

describe('opening a conversation after a reload', () => {
  it('opens the file windows again, in the order they were opened', async () => {
    openDeskFile('/workspace/a.ts')
    openDeskFile('/workspace/b.ts', '/dl/b.ts')
    reload()
    expect(paneIds()).toEqual([])

    await resume('s1')

    expect(paneIds()).toEqual(['file:/workspace/a.ts', 'file:/workspace/b.ts'])
    /* The pane that came back has to be openable the same way, download path
       included: a file the reader could save before the reload is one they can
       still save after it. */
    const back = deskState().panes[1]
    expect(back!.kind === 'file' && back!.file.downloadPath).toBe('/dl/b.ts')
  })

  it('puts the reader back in front of the window they were looking at', async () => {
    openDeskFile('/workspace/a.ts')
    openDeskFile('/workspace/b.ts')
    /* Both onto the FIRST window, which is not where replaying the opens leaves
       them: every replayed open makes itself the active pane and drops the
       fullscreen, so this only holds if the frame is put back afterwards. */
    setActive('file:/workspace/a.ts')
    toggleSolo('file:/workspace/a.ts')
    reload()

    await resume('s1')

    expect(deskState().solo).toBe('file:/workspace/a.ts')
    expect(deskState().active).toBe('file:/workspace/a.ts')
  })

  it('opens a graph node through the panel that owns it', async () => {
    openDeskAgentRecord({ kind: 'dag', run_id: 'r1', node: 'brief', agent: 'raven', label: 'brief' })
    reload()

    await resume('s1')

    expect(paneIds()).toEqual(['agent-record:r1:brief'])
    /* Through the panel, not straight onto the desk: the panel's own view has to
       move with it, or the window shows a node the panel says is not open. */
    expect(agentsState().open).toMatchObject({ kind: 'dag', run_id: 'r1', node: 'brief' })
  })

  it('waits for the instance list before opening a direct chat', async () => {
    const row: InstanceRow = { sessionKey: 's1', agent: 'hermes', handle: 'h7', kind: 'cli', resumable: true }
    openDeskAgent(row)
    reload()
    /* The list a fresh page has not asked for yet. */
    listed = [row]

    await resume('s1')

    expect(instanceCalls).toBe(1)
    expect(paneIds()).toEqual(['agent:hermes:h7'])
  })

  it('gives up on an instance the list does not carry', async () => {
    openDeskAgent({ sessionKey: 's1', agent: 'hermes', handle: 'gone', kind: 'cli', resumable: true })
    reload()
    listed = []

    await resume('s1')

    expect(paneIds()).toEqual([])
  })

  it('raises the sheet on the run the gateway reports now', async () => {
    dagStart('s1', graph('r1'))
    reload()

    await resume('s1')

    expect(readRuns).toEqual(['r1'])
    expect(sheets()).toBe(1)
    /* The statuses are the read's, not the note's: the node was pending when
       the page went away and had finished by the time it came back. */
    const back = dagOpen('s1')!
    expect([...back.nodes.values()].map((node) => node.status)).toEqual(['completed'])
    expect(back.done).toBe(true)
  })

  it('brings the desk back even when the run cannot be read', async () => {
    /* A run whose directory has been cleaned. The two halves are independent:
       one failing must not take the other down with it. */
    dagStart('s1', graph('r1'))
    openDeskFile('/workspace/a.ts')
    reload()
    dagRun = () => Promise.reject(new Error('run dir is gone'))

    await resume('s1')

    expect(paneIds()).toEqual(['file:/workspace/a.ts'])
    expect(sheets()).toBe(0)
  })

  it('asks for nothing when the conversation had nothing open', async () => {
    await resume('s1')

    expect(readRuns).toEqual([])
    expect(paneIds()).toEqual([])
    expect(sheets()).toBe(0)
  })
})

/* A replay is not the reader opening those windows again, and the difference
   shows in what it may write: the note it is replaying FROM has to survive it,
   or a page reloaded twice in quick succession keeps only whatever had landed by
   the second reload. */
describe('what a replay may not erase', () => {
  it('does not land one conversation frame on another conversation desk', async () => {
    /* The frame is applied after the list read, so the reader can have opened
       another conversation by then. `tab` and `splits` are not guarded the way
       `solo` and `active` are, so they used to land on whichever desk was on
       screen -- and the next thing that conversation's reader did filed them as
       its own. */
    const row: InstanceRow = { sessionKey: 's1', agent: 'hermes', handle: 'h7', kind: 'cli', resumable: true }
    openDeskFile('/workspace/a.ts')
    openDeskAgent(row)
    openDeskTab('deliverables')
    updateSplits({ column: 80 })
    reload()
    listed = [row]
    listGate = new Promise<void>((resolve) => { openGate = () => resolve() })

    const pending = resume('s1')
    /* The real switch: the workspace teardown, then the pointer. */
    agentsLeave()
    deskLeave()
    setCurrent('s2')
    /* A desk of its own, so the guard cannot pass by the second conversation
       happening to have nothing on screen. */
    openDeskFile('/workspace/s2.ts')
    expect(deskState().tab).toBe('diff')

    openGate()
    await pending

    expect(deskState().tab).toBe('diff')
    expect(deskState().splits.column).toBe(50)
    expect(paneIds()).toEqual(['file:/workspace/s2.ts'])
    /* The damage was never the flicker: the next thing this conversation's
       reader does files the other one's frame as its own. */
    expect(deskSaved('s2')).toEqual({
      tab: 'diff',
      open: [{ k: 'file', path: '/workspace/s2.ts' }],
      solo: null,
      active: 'file:/workspace/s2.ts',
      splits: { column: 50, left: 50, right: 50 },
    })
    /* And the conversation that was left keeps its own note intact, so going
       back to it still brings the desk it had. */
    expect(deskSaved('s1')!.open).toHaveLength(2)
    expect(deskSaved('s1')!.splits.column).toBe(80)
  })

  it('keeps a window on file when the list read fails', async () => {
    openDeskAgent({ sessionKey: 's1', agent: 'hermes', handle: 'h7', kind: 'cli', resumable: true })
    reload()
    listFails = true

    await resume('s1')

    /* The window could not come back this time; nothing about that says the
       reader did not have it. Asking again on the next open is the whole point
       of keeping the intent. */
    expect(paneIds()).toEqual([])
    expect(deskSaved('s1')!.open)
      .toEqual([{ k: 'agent', agent: 'hermes', handle: 'h7' }])
  })

  it('waits on the read another window already started', async () => {
    /* Opening the graph node asks for the instance list on its own account
       (that is how a node promotes to the instance it ran on). The chat window
       behind it must wait on THAT read rather than be told there is nothing to
       wait for, or it comes back only on the reload after this one. */
    const row: InstanceRow = { sessionKey: 's1', agent: 'hermes', handle: 'h7', kind: 'cli', resumable: true }
    openDeskAgentRecord({ kind: 'dag', run_id: 'r1', node: 'brief', agent: 'raven', label: 'brief' })
    openDeskAgent(row)
    reload()
    listed = [row]
    listSlow = true

    await resume('s1')

    expect(paneIds()).toEqual(['agent-record:r1:brief', 'agent:hermes:h7'])
  })

  it('keeps a window that is still being looked up while another one lands', async () => {
    /* The file window opens synchronously and the chat waits on a list, so the
       note used to be rewritten from a desk holding only the file -- dropping
       the chat before it was known whether it could come back. */
    openDeskFile('/workspace/a.ts')
    openDeskAgent({ sessionKey: 's1', agent: 'hermes', handle: 'h7', kind: 'cli', resumable: true })
    reload()
    listFails = true

    await resume('s1')

    expect(paneIds()).toEqual(['file:/workspace/a.ts'])
    expect(deskSaved('s1')!.open).toHaveLength(2)
  })

  it('records what the reader does next over the whole restored desk', async () => {
    /* The suppression lasts for the replay and no longer: the first thing the
       reader does afterwards is recorded, and it is recorded from the desk as it
       then stands rather than as a patch on the old note. */
    openDeskFile('/workspace/a.ts')
    reload()

    await resume('s1')
    openDeskFile('/workspace/b.ts')

    expect(deskSaved('s1')!.open)
      .toEqual([{ k: 'file', path: '/workspace/a.ts' }, { k: 'file', path: '/workspace/b.ts' }])
  })
})
