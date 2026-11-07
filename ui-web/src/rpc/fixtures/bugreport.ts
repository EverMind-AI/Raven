/* The bug report, canned: one report that walks the whole wizard.
 *
 * `bugreport.start` opens it, and the first look finds two suspected values
 * and one private key to decide; deciding freezes it, uploading lands it with
 * a receipt, and cancelling starts over. Each item carries its value in full
 * and masked the way the gateway masks it -- the private key only as its
 * replacement, as the gateway sends it -- and the contexts carry the one place
 * each was seen, so the two-column page has something on both sides. English
 * content, deliberately: these are demo rows; the page's own words come from
 * the catalogue through t().
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

type Status = ResultOf<'bugreport.status'>
type Item = NonNullable<Status['items']>[number]

const ID = 'rs-demo'

const ITEMS: Item[] = [
  {
    id: 'pk1',
    kind: 'confirmed',
    category: 'private-key-block',
    value: '[REDACTED:pattern.private-key-block]',
    masked_sample: '[REDACTED:pattern.private-key-block]',
    count: 1,
    sources: [{ label: 'a tool result', count: 1 }],
    linked: [],
    hint: null,
  },
  {
    id: 'tok1',
    kind: 'suspected',
    category: 'high-entropy',
    value: 'aB3xK9mQ7pL2vR8sT4wZ6yN1',
    masked_sample: 'aB3x***yN1',
    count: 2,
    sources: [{ label: 'the session transcript', count: 1 }, { label: 'a model input', count: 1 }],
    linked: [],
    hint: 'kept',
  },
  {
    id: 'jwt1',
    kind: 'suspected',
    category: 'jwt-like',
    value: 'eyJhbGciOiJIUzI1NiJ9.e30.Q5c',
    masked_sample: 'eyJh***.Q5c',
    count: 1,
    sources: [{ label: 'a tool result', count: 1 }],
    linked: [],
    hint: null,
  },
]

const CONTEXT: Record<string, { label: string; text: string; hit: string }> = {
  pk1: { label: 'a tool result', text: 'key file: [REDACTED:pattern.private-key-block]', hit: '[REDACTED:pattern.private-key-block]' },
  tok1: { label: 'the session transcript', text: '"content": "use token aB3xK9mQ7pL2vR8sT4wZ6yN1 here"', hit: 'aB3xK9mQ7pL2vR8sT4wZ6yN1' },
  jwt1: { label: 'a tool result', text: 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.Q5c', hit: 'eyJhbGciOiJIUzI1NiJ9.e30.Q5c' },
}

export interface BugReportFixture {
  fixtures: Fixtures
}

export function createBugReport(env: FixtureEnv): BugReportFixture {
  let phase: Status['phase'] = 'deciding'
  const stamp = new Date(env.now()).toISOString().slice(0, 10).replace(/-/g, '')

  const status = (): Status => ({
    report_id: ID,
    bug_report_id: `br-${stamp}-a1b2c3`,
    phase,
    error: null,
    history: { state: 'on', auto_kept: 1, auto_redacted: 0, warning: null },
    items: phase === 'deciding' ? ITEMS : null,
    summary:
      phase === 'deciding'
        ? null
        : {
            classification: 'needs_review',
            warnings: [],
            size_bytes: phase === 'uploaded' ? 81_234 : null,
            auto_replaced: 3,
            kept: 1,
            redacted: 1,
            acknowledged: 1,
            completeness: 'complete',
          },
    receipt: phase === 'uploaded' ? { receipt_id: 'rcpt-demo-0001', duplicate: false } : null,
    record_dir: phase === 'uploaded' ? `~/.raven/traces/bugreports/br-${stamp}-a1b2c3` : null,
  })

  return {
    fixtures: {
      'bugreport.probe': () => ({ reachable: true, target: 'https://bugs.example', reason: '' }),
      'bugreport.start': () => {
        phase = 'deciding'
        return { report_id: ID }
      },
      'bugreport.status': () => status(),
      'bugreport.item': (p) => {
        const context = CONTEXT[p.item_id ?? ''] ?? CONTEXT.tok1!
        const at = context.text.indexOf(context.hit)
        return { groups: [{ label: context.label, text: context.text, spans: [[at, at + context.hit.length]], count: 1 }], total_groups: 1 }
      },
      'bugreport.decide': () => {
        phase = 'ready'
        return { ok: true }
      },
      'bugreport.upload': () => {
        phase = 'uploaded'
        return { ok: true }
      },
      'bugreport.retry_upload': () => {
        phase = 'uploaded'
        return { ok: true }
      },
      'bugreport.cancel': () => {
        phase = 'deciding'
        return { ok: true }
      },
    },
  }
}
