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
import type { TaskBoard, TaskCounts, TaskGroup, TaskNode, TaskRow } from '../generated'

const MIN = 60000
const SEC = 1000

function node(over: Partial<TaskNode> & Pick<TaskNode, 'node_id' | 'agent' | 'status'>): TaskNode {
  return {
    node_summary: null, instance: null, depends_on: [], started_at: null, ended_at: null, error: null,
    tokens_in: null, tokens_out: null, tool_call_count: null, has_output: null,
    prompt_template: null, files: [],
    ...over,
  }
}

function counts(nodes: readonly TaskNode[]): TaskCounts {
  const c: TaskCounts = {
    total: nodes.length, pending: 0, running: 0, completed: 0, failed: 0, skipped: 0,
    cancelled: 0, interrupted: 0, exception: 0, not_executed: 0,
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
      case 'not_executed': c.not_executed += 1; break
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
      tool_call_count: 3, has_output: true,
      prompt_template: 'Survey the changelog and open PRs since {{ inputs.since }}.',
      inputs: { since: 'v0.4.0' },
      files: [{ path: '/work/notes/survey.md', op: 'write', add: 28, del: 0, size: 1180 }],
    }),
    node({
      node_id: 'draft_notes', agent: 'Raven', status: 'completed', depends_on: ['survey'],
      node_summary: 'Draft the release notes from the survey',
      started_at: started + 41 * SEC, ended_at: started + 95 * SEC, tokens_in: 900, tokens_out: 520,
      tool_call_count: 1, has_output: true,
      prompt_template: 'Draft release notes from {{ survey.output }}.',
    }),
    node({
      node_id: 'render_deck', agent: 'Raven-Code', status: 'running', depends_on: ['survey'], instance: 'w1',
      node_summary: 'Render a one-slide summary deck',
      started_at: started + 41 * SEC, tool_call_count: 2,
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
      started_at: started - 90 * SEC, tool_call_count: 1,
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
      tool_call_count: 2, has_output: true,
    }),
    node({
      node_id: 'nightly-checks-a1b2c3-run_tests', agent: 'Raven-Code', status: 'failed',
      depends_on: ['nightly-checks-a1b2c3-setup'],
      node_summary: 'Run the regression suite',
      started_at: started + 31 * SEC, ended_at: started + 210 * SEC,
      error: 'pytest: 3 assertions failed in test_login.py (test_expired_token, test_locked_account, test_mfa_retry)',
      tokens_in: 4100, tokens_out: 260, tool_call_count: 5, has_output: true,
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
      tool_call_count: 2, has_output: true,
    }),
    node({
      node_id: 'patch', agent: 'Raven-Code', status: 'completed', depends_on: ['inspect'],
      node_summary: 'Fix the missing null check on the webhook signature',
      started_at: started + 23 * SEC, ended_at: started + 96 * SEC, tokens_in: 1500, tokens_out: 610,
      tool_call_count: 3, has_output: true,
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
      tool_call_count: 1, has_output: true,
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
      started_at: started, tokens_in: 4200, tokens_out: 900, tool_call_count: 41,
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
      tool_call_count: 1, has_output: true,
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
      tokens_in: 500, tokens_out: 70, tool_call_count: 4, has_output: false,
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
      tool_call_count: 4, has_output: true,
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

/* Scenario 6, session 'c': the last step waits on the step before it and on
   every step that one already waits on, the shape a real research-to-deck run
   came back in. Laid out by centring each layer, the middle research step's
   edge to the build ran straight down behind the outline's box and could not
   be seen; this is the graph that keeps that visible. */
function scenarioSkippedLayer(env: FixtureEnv): TaskRow[] {
  const now = env.now()
  const started = now - 16 * MIN
  const research = (id: string, summary: string, took: number): TaskNode => node({
    node_id: id, agent: 'Raven-Research', status: 'completed', instance: id.replace('research_', 'research-'),
    node_summary: summary, started_at: started, ended_at: started + took, tokens_in: 5200, tokens_out: 2100,
    tool_call_count: 14, has_output: true,
  })
  const nodes = [
    research('research_rl_llm', 'Survey recent progress in RL for LLMs', 15 * MIN + 9 * SEC),
    research('research_rl_algo', 'Survey recent RL algorithms and theory', 9 * MIN + 53 * SEC),
    research('research_rl_apps', 'Survey recent RL applications', 10 * MIN + 39 * SEC),
    node({
      node_id: 'ppt_outline', agent: 'Raven', status: 'running', instance: 'ppt-outline-a7e3d3',
      depends_on: ['research_rl_llm', 'research_rl_algo', 'research_rl_apps'],
      node_summary: 'Merge the research into a ten-slide outline',
      started_at: now - 57 * SEC, tool_call_count: 3,
      prompt_template: 'Merge {{ research_rl_llm.output_path }}, {{ research_rl_algo.output_path }} and '
        + '{{ research_rl_apps.output_path }} into a ten-slide outline.',
    }),
    node({
      node_id: 'ppt_build', agent: 'Raven-Design', status: 'pending', instance: 'ppt-build-5c1f09',
      depends_on: ['ppt_outline', 'research_rl_llm', 'research_rl_algo', 'research_rl_apps'],
      node_summary: 'Build the slide deck and its previews',
      prompt_template: 'Build the deck from {{ ppt_outline.output_path }}, reading the three surveys for detail.',
    }),
  ]
  return [row({
    id: '20260923T101500000000Z-rlsurvey', kind: 'dag', status: 'running', nodes,
    task_summary: 'Survey recent RL progress and build a slide deck', started_at: started,
  })]
}

/* Scenario 7, session 'f': a graph that branched. The router chose one route of two, so the
   other branch -- and the step that waited only on it -- was left out; a second decision, which
   may choose any number of routes, chose none. The task is simply completed: a branch the run
   did not take is not a step that went wrong. */
function scenarioBranched(env: FixtureEnv): TaskRow[] {
  const now = env.now()
  const started = now - 25 * MIN
  const nodes = [
    node({
      node_id: 'triage', agent: 'Raven', status: 'completed',
      node_summary: 'Decide whether the report is a question or a bug',
      started_at: started, ended_at: started + 20 * SEC, tokens_in: 420, tokens_out: 60,
      tool_call_count: 1, has_output: true,
      routes: ['question', 'bug'], select: 'one', selected_routes: ['question'],
      route_evidence: 'The user asks how to reset a password; nothing in the message describes a failure.',
    }),
    node({
      node_id: 'answer', agent: 'Raven', status: 'completed', depends_on: ['triage'], when: 'triage.question',
      node_summary: 'Answer the question from the docs',
      started_at: started + 21 * SEC, ended_at: started + 66 * SEC, tokens_in: 900, tokens_out: 310,
      tool_call_count: 3, has_output: true,
    }),
    node({
      node_id: 'reproduce', agent: 'Raven-Code', status: 'not_executed', depends_on: ['triage'], when: 'triage.bug',
      node_summary: 'Reproduce the failure', reason: 'when_false',
    }),
    node({
      node_id: 'patch', agent: 'Raven-Code', status: 'not_executed', depends_on: ['reproduce'],
      node_summary: 'Write a patch for it', reason: 'upstream_not_executed',
    }),
    node({
      node_id: 'label', agent: 'Raven', status: 'completed', depends_on: ['answer'],
      node_summary: 'Add any labels that apply',
      started_at: started + 67 * SEC, ended_at: started + 80 * SEC, tokens_in: 300, tokens_out: 40,
      tool_call_count: 1, has_output: true,
      routes: ['docs', 'billing'], select: 'many', selected_routes: [],
    }),
    node({
      node_id: 'tag_docs', agent: 'Raven', status: 'not_executed', depends_on: ['label'], when: 'label.docs',
      node_summary: 'Tag the thread as docs', reason: 'when_false',
    }),
  ]
  return [row({
    id: '20260918T091000000000Z-branch5e', kind: 'dag', status: 'completed', nodes,
    task_summary: 'Triage an incoming support message', started_at: started, ended_at: started + 80 * SEC,
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
    ['c', scenarioSkippedLayer(env)],
    ['f', scenarioBranched(env)],
  ])

  /* The host's Task Board for two of the conversations above, so the offline
     page draws both halves of the tab: groups whose steps hang those Tasks,
     and (everywhere else) the Unassigned row a session from before the board
     shows. */
  const groupOf = (id: string, rows: TaskRow[], text: string, status: TaskGroup['status']): TaskGroup => {
    const at = Math.min(...rows.map((r) => r.started_at ?? env.now()))
    return {
      id, title: rows[0]?.task_summary || id, status, created_at: at, updated_at: at,
      steps: [{
        id: `s_${id}`, turn_at: new Date(at).toISOString(), turn_id: null, text,
        tasks: rows.map((r) => ({ kind: r.kind, id: r.id, summary: r.task_summary || r.id })),
      }],
    }
  }
  const boards = new Map<string, TaskBoard>()
  const spawns = bySession.get('h') ?? []
  boards.set('h', {
    version: 1, unassigned: [],
    groups: spawns.map((r, i) => groupOf(`g_h${i}`, [r], 'Sent a sub-agent to do it.', r.status === 'running' ? 'active' : 'waiting')),
  })
  boards.set('g', {
    version: 1, unassigned: [],
    groups: [groupOf('g_g0', bySession.get('g') ?? [], 'Wrote the files and reported them back.', 'completed')],
  })

  const findRow = (id: string, kind?: string): { rows: TaskRow[]; at: number; row: TaskRow } | null => {
    for (const rows of bySession.values()) {
      const at = rows.findIndex((r) => r.id === id && (!kind || r.kind === kind))
      if (at >= 0) return { rows, at, row: rows[at]! }
    }
    return null
  }

  return {
    fixtures: {
      'tasks.board': (p) => ({
        board: boards.get(p.session_key) ?? { version: 1, groups: [], unassigned: [] },
        lineage: {},
      }),
      /* An unknown group answers the board unchanged rather than throwing: the
         offline library answers every method for any params it is handed. */
      'tasks.set_group_status': (p) => {
        const board = boards.get(p.session_key) ?? { version: 1, groups: [], unassigned: [] }
        const group = board.groups.find((g) => g.id === p.group_id)
        if (group) {
          group.status = p.status
          group.updated_at = env.now()
        }
        return { board, lineage: {} }
      },
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
