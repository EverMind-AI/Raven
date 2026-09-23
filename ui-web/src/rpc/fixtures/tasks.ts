/* The tasks tab's offline library: `tasks.list`, keyed by session.
 *
 * Five sessions each answer with the wire shape a real gateway would send for
 * one of the scenarios the contract's prototype demonstrates
 * (my_docs/specs/20260918_tasks_rpc_contract.md §2.6); every other session
 * (including a fresh draft) answers with none, which is the honest state for
 * a conversation that has not delegated anything yet. `subagent.interrupt`
 * and `subagent.cancel_instance` mutate the same rows in place, so a stop on
 * this page has somewhere real to land.
 *
 * English content, deliberately -- these are demo rows for a shape the real
 * gateway fills in, not the page's own words, which come from the catalogue
 * through t() the way every renderer's do.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { TaskCounts, TaskNode, TaskRow } from '../generated'

const MIN = 60000
const SEC = 1000

function node(over: Partial<TaskNode> & Pick<TaskNode, 'node_id' | 'agent' | 'status'>): TaskNode {
  return {
    node_summary: null, instance: null, depends_on: [], started_at: null, ended_at: null, error: null,
    tokens_in: null, tokens_out: null, tool_call_count: null, tool_failure_count: null, has_output: null,
    prompt_template: null, files: [],
    ...over,
  }
}

function counts(nodes: readonly TaskNode[]): TaskCounts {
  const c: TaskCounts = {
    total: nodes.length, pending: 0, running: 0, completed: 0, failed: 0, skipped: 0,
    cancelled: 0, interrupted: 0, exception: 0,
  }
  nodes.forEach((n) => {
    switch (n.status) {
      case 'pending': c.pending += 1; break
      case 'running': c.running += 1; break
      case 'completed': c.completed += 1; break
      case 'failed': c.failed += 1; break
      case 'skipped': c.skipped += 1; break
      case 'cancelled': c.cancelled += 1; break
      case 'interrupted': c.interrupted += 1; break
      case 'exception': c.exception += 1; break
    }
  })
  return c
}

function row(over: Partial<TaskRow> & Pick<TaskRow, 'id' | 'kind' | 'status' | 'nodes'>): TaskRow {
  return {
    task_summary: null, started_at: null, ended_at: null, agent: null, handle: null,
    counts: counts(over.nodes),
    ...over,
  }
}

/* Scenario 1, session 'a': a fork-join dag mid-flight, with a spawn running
   beside it -- the two kinds of delegated work sharing one screen. */
function scenarioForkJoin(env: FixtureEnv): TaskRow[] {
  const now = env.now()
  const started = now - 3 * MIN
  const nodes = [
    node({
      node_id: 'survey', agent: 'Raven-Research', status: 'completed',
      node_summary: 'Survey what changed since the last release',
      started_at: started, ended_at: started + 40 * SEC, tokens_in: 1200, tokens_out: 340,
      tool_call_count: 3, tool_failure_count: 0, has_output: true,
      prompt_template: 'Survey the changelog and open PRs since {{ inputs.since }}.',
      inputs: { since: 'v0.4.0' },
      files: [{ path: '/work/notes/survey.md', op: 'write', add: 28, del: 0, size: 1180 }],
    }),
    node({
      node_id: 'draft_notes', agent: 'Raven', status: 'completed', depends_on: ['survey'],
      node_summary: 'Draft the release notes from the survey',
      started_at: started + 41 * SEC, ended_at: started + 95 * SEC, tokens_in: 900, tokens_out: 520,
      tool_call_count: 1, tool_failure_count: 0, has_output: true,
      prompt_template: 'Draft release notes from {{ survey.output }}.',
    }),
    node({
      node_id: 'render_deck', agent: 'Raven-Code', status: 'running', depends_on: ['survey'], instance: 'w1',
      node_summary: 'Render a one-slide summary deck',
      started_at: started + 41 * SEC, tool_call_count: 2, tool_failure_count: 0,
      prompt_template: 'Render one slide summarising {{ survey.output }}.',
      skills: ['deck-render'], mcps: [],
    }),
    node({
      node_id: 'merge', agent: 'Raven', status: 'pending', depends_on: ['draft_notes', 'render_deck'],
      node_summary: 'Merge the notes and the deck into one announcement',
      prompt_template: 'Merge {{ draft_notes.output }} and {{ render_deck.output }} into one announcement.',
    }),
  ]
  const dag = row({
    id: '20260918T093000123456Z-fork01a2', kind: 'dag', status: 'running', nodes,
    task_summary: 'Put together the release announcement', started_at: started,
  })
  /* An earlier attempt at the same announcement, replanned into the run
     above: `replan.started` is true, so the row reads cancelled rather than
     failed and its why banner links to the successor instead of naming a
     bad node. */
  const supersededNodes = [
    node({
      node_id: 'render_deck_v1', agent: 'Raven-Code', status: 'exception',
      node_summary: 'Render a one-slide summary deck',
      started_at: started - 90 * SEC, tool_call_count: 1, tool_failure_count: 0,
    }),
  ]
  const superseded = row({
    id: '20260918T092500123456Z-fork0019', kind: 'dag', status: 'cancelled', nodes: supersededNodes,
    task_summary: 'Put together the release announcement', started_at: started - 90 * SEC,
    replan: {
      run_id: dag.id, from_node: 'render_deck_v1',
      reason: 'the deck renderer needed a different template', started: true,
    },
  })
  const spawnNodes = [
    node({
      node_id: 'clean-build-cache', agent: 'Raven-Code', status: 'running',
      node_summary: 'Clear the stale build cache', started_at: now - 20 * SEC, tool_call_count: 1,
      prompt_template: 'Remove everything under .cache/build older than 7 days.',
    }),
  ]
  const spawn = row({
    id: 'clean-build-cache', kind: 'spawn', status: 'running', nodes: spawnNodes,
    task_summary: 'Clear the stale build cache', agent: 'Raven-Code', handle: 'clean-build-cache',
    started_at: now - 20 * SEC,
  })
  return [spawn, superseded, dag]
}

/* Scenario 2, session 'b': a playbook-dispatched dag with one failed node and
   two nodes the failure cascaded past, skipped. */
function scenarioPlaybookFailed(env: FixtureEnv): TaskRow[] {
  const now = env.now()
  const started = now - 12 * MIN
  const nodes = [
    node({
      node_id: 'nightly-checks-a1b2c3-setup', agent: 'Raven', status: 'completed',
      node_summary: 'Prepare the test environment',
      started_at: started, ended_at: started + 30 * SEC, tokens_in: 300, tokens_out: 90,
      tool_call_count: 2, tool_failure_count: 0, has_output: true,
    }),
    node({
      node_id: 'nightly-checks-a1b2c3-run_tests', agent: 'Raven-Code', status: 'failed',
      depends_on: ['nightly-checks-a1b2c3-setup'],
      node_summary: 'Run the regression suite',
      started_at: started + 31 * SEC, ended_at: started + 210 * SEC,
      error: 'pytest: 3 assertions failed in test_login.py (test_expired_token, test_locked_account, test_mfa_retry)',
      tokens_in: 4100, tokens_out: 260, tool_call_count: 5, tool_failure_count: 2, has_output: true,
      skills: ['pytest-runner'],
    }),
    node({
      node_id: 'nightly-checks-a1b2c3-report', agent: 'Raven', status: 'skipped',
      depends_on: ['nightly-checks-a1b2c3-run_tests'],
      node_summary: 'Write up the results',
    }),
    node({
      node_id: 'nightly-checks-a1b2c3-notify', agent: 'Raven', status: 'skipped',
      depends_on: ['nightly-checks-a1b2c3-report'],
      node_summary: 'Post the summary to the team channel',
    }),
  ]
  return [row({
    id: '20260917T230000654321Z-nightly9f', kind: 'dag', status: 'failed', nodes,
    task_summary: 'Nightly regression sweep', started_at: started,
    ended_at: started + 211 * SEC,
  })]
}

/* Scenario 3, session 'g': a finished dag whose last node wrote real files. */
function scenarioCompletedWithFiles(env: FixtureEnv): TaskRow[] {
  const now = env.now()
  const started = now - 30 * MIN
  const nodes = [
    node({
      node_id: 'inspect', agent: 'Raven-Code', status: 'completed',
      node_summary: 'Read the current payment callback module',
      started_at: started, ended_at: started + 22 * SEC, tokens_in: 800, tokens_out: 140,
      tool_call_count: 2, tool_failure_count: 0, has_output: true,
    }),
    node({
      node_id: 'patch', agent: 'Raven-Code', status: 'completed', depends_on: ['inspect'],
      node_summary: 'Fix the missing null check on the webhook signature',
      started_at: started + 23 * SEC, ended_at: started + 96 * SEC, tokens_in: 1500, tokens_out: 610,
      tool_call_count: 3, tool_failure_count: 0, has_output: true,
      /* The four verdicts a folded touch can carry, one node apart: an edit in
         place, a file the node removed (no size -- there is nothing left to
         measure), and below, a file it created against one it rewrote. */
      files: [
        { path: '/work/src/payments/callback.py', op: 'edit', add: 12, del: 4, size: 3120 },
        { path: '/work/src/payments/legacy_hook.py', op: 'delete', add: 0, del: 46, size: null },
      ],
    }),
    node({
      node_id: 'verify', agent: 'Raven-Code', status: 'completed', depends_on: ['patch'],
      node_summary: 'Run the payments test suite',
      started_at: started + 97 * SEC, ended_at: started + 150 * SEC, tokens_in: 700, tokens_out: 90,
      tool_call_count: 1, tool_failure_count: 0, has_output: true,
      files: [
        { path: '/work/reports/payments-verify.md', op: 'add', add: 18, del: 0, size: 640 },
        { path: '/work/reports/index.md', op: 'write', add: 6, del: 2, size: 210 },
      ],
    }),
  ]
  return [row({
    id: '20260917T210000000000Z-refact042', kind: 'dag', status: 'completed', nodes,
    task_summary: 'Refactor the payment callback module', started_at: started, ended_at: started + 150 * SEC,
  })]
}

/* Also session 'g': a spawn still running that has already fetched a few
   dozen images for the deck it is building -- the strip that has to fold
   rather than push the board out of its own pane. */
const DECK_ASSETS = [
  'fetch.sh', 'anthropic.png', 'aws-datacenter.jpg', 'eu-berlaymont.jpg', 'gemini.png', 'gpu.jpg',
  'huggingface.png', 'meta.png', 'ms-datacenter.jpg', 'nvidia.svg', 'openai.png', 'powerlines.jpg',
  'stripe.png', 'h100.jpg', 'nvidia-black.png', 'mistral.png', 'deepseek.png', 'xai.png', 'tsmc-fab.jpg',
  'arm.png', 'amd-mi300.jpg', 'google-tpu.jpg', 'cerebras.png', 'groq.png', 'perplexity.png',
  'cohere.png', 'baidu.png', 'alibaba-cloud.png', 'bytedance.png', 'moonshot.png', 'zhipu.png',
  'datacenter-cooling.jpg', 'chip-wafer.jpg', 'outline.md',
]

function scenarioManyFiles(env: FixtureEnv): TaskRow {
  const started = env.now() - 3.5 * MIN
  const nodes = [
    node({
      node_id: 'ai-hotspot-deck', agent: 'Raven-Design', status: 'running', instance: 'ai_hotspot_deck',
      node_summary: 'Turn the AI news survey into a deck',
      started_at: started, tokens_in: 4200, tokens_out: 900, tool_call_count: 41, tool_failure_count: 0,
      files: DECK_ASSETS.map((name, i) => ({
        path: `/work/deck/assets/${name}`, op: 'add' as const, add: 0, del: 0, size: 2300 + ((i * 7919) % 900) * 1024,
      })),
    }),
  ]
  return row({
    id: 'ai-hotspot-deck', kind: 'spawn', status: 'running', nodes,
    task_summary: 'Turn the AI news survey into a deck', agent: 'Raven-Design',
    handle: 'ai_hotspot_deck', started_at: started,
  })
}

/* Scenario 4, session 'h': two finished spawns -- one delivered a file, the
   other stopped on a vendor error. */
function scenarioTwoSpawns(env: FixtureEnv): TaskRow[] {
  const now = env.now()
  const okStart = now - 18 * MIN
  const okNodes = [
    node({
      node_id: 'write-install-section', agent: 'Raven', status: 'completed',
      node_summary: 'Write the installer README section',
      started_at: okStart, ended_at: okStart + 54 * SEC, tokens_in: 640, tokens_out: 410,
      tool_call_count: 1, tool_failure_count: 0, has_output: true,
      files: [{ path: '/work/README.md', op: 'write', add: 40, del: 0, size: 1536 }],
    }),
  ]
  const ok = row({
    id: 'write-install-section', kind: 'spawn', status: 'completed', nodes: okNodes,
    task_summary: 'Write the installer README section', agent: 'Raven',
    handle: 'write-install-section', started_at: okStart, ended_at: okStart + 54 * SEC,
  })
  const failStart = now - 9 * MIN
  const failNodes = [
    node({
      node_id: 'scan-deps-cve', agent: 'Raven-Research', status: 'failed',
      node_summary: 'Scan dependencies for known CVEs',
      started_at: failStart, ended_at: failStart + 38 * SEC,
      error: 'The vendor advisory feed answered 503 three times in a row; nothing was substituted for it.',
      tokens_in: 500, tokens_out: 70, tool_call_count: 4, tool_failure_count: 3, has_output: false,
    }),
  ]
  const failed = row({
    id: 'scan-deps-cve', kind: 'spawn', status: 'failed', nodes: failNodes,
    task_summary: 'Scan dependencies for known CVEs', agent: 'Raven-Research',
    handle: 'scan-deps-cve', started_at: failStart, ended_at: failStart + 38 * SEC,
  })
  return [failed, ok]
}

/* Scenario 5, session 'd': a dag the gateway died in the middle of. The
   first node had already finished; nothing after it was ever manifested, so
   it reads as interrupted rather than as failed. */
function scenarioInterrupted(env: FixtureEnv): TaskRow[] {
  const now = env.now()
  const started = now - 50 * MIN
  const nodes = [
    node({
      node_id: 'fetch', agent: 'Raven-Research', status: 'completed',
      node_summary: 'Fetch the source documents to index',
      started_at: started, ended_at: started + 60 * SEC, tokens_in: 900, tokens_out: 120,
      tool_call_count: 4, tool_failure_count: 0, has_output: true,
    }),
    node({
      node_id: 'transform', agent: 'Raven-Code', status: 'interrupted', depends_on: ['fetch'],
      node_summary: 'Chunk and embed the fetched documents',
      started_at: started + 61 * SEC, tool_call_count: 2,
    }),
    node({
      node_id: 'reindex', agent: 'Raven-Code', status: 'interrupted', depends_on: ['transform'],
      node_summary: 'Swap the search index over to the new build',
    }),
  ]
  return [row({
    id: '20260917T190000000000Z-reindex7c', kind: 'dag', status: 'interrupted', nodes,
    task_summary: 'Rebuild the search index', started_at: started,
  })]
}

export interface TasksFixture {
  fixtures: Fixtures
}

export function createTasks(env: FixtureEnv): TasksFixture {
  /* Built once and kept as state, mutated in place by a stop: the same rows
     answer every `tasks.list`, which is what lets a reader's own click on
     "Stop" actually change what the next read says. */
  const bySession = new Map<string, TaskRow[]>([
    ['a', scenarioForkJoin(env)],
    ['b', scenarioPlaybookFailed(env)],
    ['g', [...scenarioCompletedWithFiles(env), scenarioManyFiles(env)]],
    ['h', scenarioTwoSpawns(env)],
    ['d', scenarioInterrupted(env)],
  ])

  const findRow = (id: string, kind?: string): { rows: TaskRow[]; at: number; row: TaskRow } | null => {
    for (const rows of bySession.values()) {
      const at = rows.findIndex((r) => r.id === id && (!kind || r.kind === kind))
      if (at >= 0) return { rows, at, row: rows[at]! }
    }
    return null
  }

  return {
    fixtures: {
      'tasks.list': (p) => {
        const all = bySession.get(p.session_key) || []
        const filtered = p.kind && p.id ? all.filter((r) => r.kind === p.kind && r.id === p.id) : all
        return { tasks: filtered.map((r) => ({ ...r, nodes: r.nodes.map((n) => ({ ...n })) })) }
      },
      /* A dag is stopped by its run id. Every node still open goes cancelled
         (running/exception) or skipped (pending), the same split the runtime
         itself draws (contract §3). */
      'subagent.interrupt': (p) => {
        const subagentId = p.subagent_id || ''
        const found = findRow(subagentId, 'dag')
        if (!found) return { found: false, subagent_id: subagentId }
        const nodes = found.row.nodes.map((n) => {
          if (n.status === 'running' || n.status === 'exception') return { ...n, status: 'cancelled' as const }
          if (n.status === 'pending') return { ...n, status: 'skipped' as const }
          return n
        })
        found.rows[found.at] = { ...found.row, status: 'cancelled', nodes, counts: counts(nodes) }
        return { found: true, subagent_id: subagentId }
      },
      /* A spawn is stopped by the handle it committed under. */
      'subagent.cancel_instance': (p) => {
        const agent = p.agent || ''
        const handle = p.handle || ''
        const found = [...bySession.values()].flat()
          .find((r) => r.kind === 'spawn' && r.agent === agent && r.handle === handle)
        if (!found) return { found: false, session_key: p.session_key || '', agent, handle }
        const at = findRow(found.id, 'spawn')
        if (at) {
          const nodes = at.row.nodes.map((n) => ({ ...n, status: 'cancelled' as const }))
          at.rows[at.at] = { ...at.row, status: 'cancelled', nodes, counts: counts(nodes) }
        }
        return { found: true, session_key: p.session_key || '', agent, handle }
      },
    },
  }
}
