/* The stand-in rows the tasks panel draws while it has no server.
 *
 * Every other domain's offline answers live in src/rpc/fixtures/, behind the
 * fixture transport, because every other domain has a wire method to answer.
 * This one has none: there is no `tasks.*` on the contract yet, so there is
 * nothing for a responder to respond to, and rows shaped like the panel's own
 * types are the honest place to hold what it draws instead.
 *
 * A source that answered empty everywhere would leave the view unreachable in
 * every mode, and a surface nobody can open is a surface nobody can review.
 * Which page gets these is the wiring's decision, not this file's
 * (features/tasks/source.ts).
 *
 * English content, deliberately. The rest of the page reads its Chinese from
 * the catalogue through t(), which is what the renderer does for every label it
 * draws; these are stand-in rows for a shape the server will fill, and putting
 * demo prose in the shipped catalogue to dodge the source-language rule would
 * be paying a permanent cost for a temporary fixture.
 */

import type { TaskRow } from './types'

export const TASK_FIXTURES: TaskRow[] = [
  {
    id: 't-gold-cross', name: 'Cross-check gold quotes across sources', source: 'dag',
    agent: 'Raven-Research', state: 'run', step: [3, 5], duration: '2m47s', confirm: false,
    artifacts: [], diffs: [{ file: 'fetch_gold.py', add: 5, del: 1 }],
    nodes: [
      { id: 'spot_prices', title: 'Read the spot price from each source', agent: 'Raven-Research', status: 'completed', from: [], duration: '48s', skills: ['gold-brief'], mcps: ['playwright'], records: 5, toolCalls: 3,
        prompt: 'Take one quote for spot gold from TradingEconomics, BullionVault and COMEX, and for each one:\n1. convert to USD per ounce, two decimals;\n2. record the fetch time and the source URL;\n3. where a source cannot be read, write why. Do not fill the gap with an estimate.\n\nOutput a three-row markdown table: source / quote / fetched at / URL. The table only.',
        record: {
          dispatch: 'Take one quote for spot gold from TradingEconomics, BullionVault and COMEX, and for each one:\n1. convert to USD per ounce, two decimals;\n2. record the fetch time and the source URL;\n3. where a source cannot be read, write why. Do not fill the gap with an estimate.\n\nOutput a three-row markdown table: source / quote / fetched at / URL. The table only.',
          steps: [
            { kind: 'think', text: 'Three vendors, three page shapes. Read each one on its own rather than guessing a shared selector, and convert at the end so one bad rate cannot move all three.' },
            { kind: 'tool', tool: 'web_fetch', arg: 'tradingeconomics.com/commodity/gold', result: '3.4k chars' },
            { kind: 'tool', tool: 'web_fetch', arg: 'bullionvault.com/gold-price-chart', result: '2.1k chars' },
            { kind: 'think', text: 'BullionVault quotes its overnight settlement, which is not the same basis as the other two. Worth a note in the table rather than a silent conversion.' },
            { kind: 'tool', tool: 'web_fetch', arg: 'comex.quote/GC', result: '1.8k chars' },
          ],
          answer: 'Three rows, all USD per ounce, 13 September close:\n\nTradingEconomics 4,312.5 / BullionVault 4,309.8 / COMEX 4,315.2.\n\nBullionVault is on an overnight settlement basis; noted in the table rather than adjusted.',
        } },
      { id: 'futures_data', title: 'Pull the futures side for cross-checking', agent: 'Raven-Research', status: 'completed', from: ['spot_prices'], duration: '1m12s', mcps: ['playwright'], records: 4, toolCalls: 2,
        prompt: 'Spot quotes so far:\n{{ spot_prices.output }}\n\nPull the December contract range for the same dates so the two sides can be compared.' },
      { id: 'market_analysis', title: 'Study what moved the market', agent: 'Raven-Research', status: 'running', from: ['spot_prices', 'futures_data'], skills: ['gold-brief'], mcps: ['playwright'], records: 5, toolCalls: 3,
        record: {
          dispatch: 'Spot quotes:\n[BEGIN UNTRUSTED subagent #spot_prices]\nTradingEconomics 4,312.5 / BullionVault 4,309.8 / COMEX 4,315.2 (USD per ounce, 13 Sep close).\n[END UNTRUSTED subagent #spot_prices]\n\nFutures and range data:\n[BEGIN UNTRUSTED subagent #futures_data]\nDecember contract 4,483.20 to 4,521.50.\n[END UNTRUSTED subagent #futures_data]\n\nWorking from those two:\n1. align spot and futures onto one date, and for every gap over 0.5% say whether it is a definition difference (tax, settlement point) or a timing one;\n2. name the main cause of each of the two jumps since mid-August, with at least one dated source per judgement;\n3. write "unverified" where you are unsure rather than concluding.\n\nOutput: the aligned table plus under 300 words of reasoning.',
          steps: [
            { kind: 'think', text: 'The spot and futures rows are on different dates. Align first, then judge the gaps, or every difference reads as a definition one.' },
            { kind: 'tool', tool: 'exec', arg: 'python align_quotes.py --tolerance 0.005', result: 'exit 0' },
          ],
        },
        prompt: 'Spot quotes:\n{{ spot_prices.output }}\n\nFutures and range data:\n{{ futures_data.output }}\n\nWorking from those two:\n1. align spot and futures onto one date, and for every gap over 0.5% say whether it is a definition difference (tax, settlement point) or a timing one;\n2. name the main cause of each of the two jumps since mid-August, with at least one dated source per judgement;\n3. write "unverified" where you are unsure rather than concluding.\n\nOutput: the aligned table plus under 300 words of reasoning.' },
      { id: 'write_report', title: 'Write the analysis up', agent: 'Raven', status: 'pending', from: ['market_analysis'],
        prompt: 'Turn {{ market_analysis.output }} into the report body.' },
      { id: 'render_html', title: 'Render it as one HTML file', agent: 'Raven-Code', status: 'pending', from: ['write_report'],
        prompt: 'Render {{ ref:@nodes/write_report.out.md }} as a single self-contained HTML file.' },
    ],
  },
  {
    id: 't-weekly', name: 'Weekly gold report, three ways', source: 'playbook', sourceName: 'Playbook 02',
    agent: 'Raven-Research', state: 'run', step: [4, 7], duration: '3m05s', confirm: true,
    artifacts: [], diffs: [],
    nodes: [
      { id: 'collect_spot', title: 'Collect spot prices', agent: 'Raven-Research', status: 'completed', from: [], duration: '39s', records: 3, toolCalls: 2, prompt: 'Collect spot prices for {{ inputs.week }}.' },
      { id: 'collect_etf', title: 'Collect ETF flows', agent: 'Raven-Research', status: 'completed', from: [], duration: '44s', records: 3, toolCalls: 2, prompt: 'Collect ETF flows for {{ inputs.week }}.' },
      { id: 'collect_macro', title: 'Collect the macro calendar', agent: 'Raven-Research', status: 'completed', from: [], duration: '51s', records: 2, toolCalls: 1, prompt: 'Collect the macro calendar for {{ inputs.week }}.' },
      { id: 'merge', title: 'Merge the three legs', agent: 'Raven', status: 'running', from: ['collect_spot', 'collect_etf', 'collect_macro'], prompt: 'Merge:\n{{ collect_spot.output }}\n{{ collect_etf.output }}\n{{ collect_macro.output }}' },
      { id: 'draft', title: 'Draft the report', agent: 'Raven', status: 'pending', from: ['merge'], prompt: 'Draft from {{ merge.output }}.' },
      { id: 'chart', title: 'Chart the week', agent: 'Raven-Code', status: 'pending', from: ['merge'], prompt: 'Chart {{ merge.output_path }}.' },
      { id: 'layout', title: 'Lay the deck out', agent: 'Raven-Code', status: 'pending', from: ['draft', 'chart'], prompt: 'Lay out {{ ref:@nodes/draft.out.md }}.' },
    ],
  },
  {
    id: 't-bullion', name: 'Verify the BullionVault overnight quote', source: 'spawn',
    agent: 'Raven-Research', state: 'run', step: [1, 1], duration: '0m41s',
    artifacts: [], diffs: [],
    nodes: [
      { id: 'verify_bullion', title: 'Verify the BullionVault overnight quote', agent: 'claude_code', status: 'running', from: [], records: 2, toolCalls: 0,
        prompt: 'BullionVault settles overnight, so its number is not the same definition as the other two. Read the vendor page and say which basis the quote is on.',
        record: {
          dispatch: 'BullionVault settles overnight, so its number is not the same definition as the other two. Read the vendor page and say which basis the quote is on.',
          steps: [],
          unrecorded: true,
        } },
    ],
  },
  {
    id: 't-deck', name: 'Build the gold market deck', source: 'playbook', sourceName: 'Playbook 01',
    agent: 'Raven-Code', state: 'done', duration: '4m12s', confirm: true,
    artifacts: [{ name: 'gold-market-v1.pptx' }], diffs: [],
    nodes: [
      { id: 'outline', title: 'Outline the deck', agent: 'Raven', status: 'completed', from: [], duration: '36s', records: 2, toolCalls: 0, prompt: 'Outline four slides.' },
      { id: 'slides', title: 'Fill the slides', agent: 'Raven-Code', status: 'completed', from: ['outline'], duration: '2m18s', skills: ['ppt-engine'], records: 6, toolCalls: 4, prompt: 'Fill the slides from {{ outline.output }}.' },
      { id: 'export', title: 'Export the file', agent: 'Raven-Code', status: 'completed', from: ['slides'], duration: '1m18s', records: 2, toolCalls: 1, prompt: 'Export {{ slides.output_path }} as pptx.' },
    ],
  },
  {
    id: 't-comex', name: 'Fetch the COMEX futures quote', source: 'spawn',
    agent: 'Raven-Research', state: 'fail', duration: '38s',
    artifacts: [], diffs: [],
    failure: 'The vendor answered 429 rate-limited. Fell back to the 31 August quote; nothing was written.',
    nodes: [
      { id: 'fetch_comex', title: 'Fetch the COMEX futures quote', agent: 'Raven-Research', status: 'failed', from: [], duration: '38s', records: 4, toolCalls: 4,
        prompt: 'Read the COMEX December contract quote and record the fetch time.',
        record: {
          dispatch: 'Read the COMEX December contract quote and record the fetch time.',
          steps: [
            { kind: 'tool', tool: 'web_fetch', arg: 'comex.quote/GC', result: 'HTTP 429' },
            { kind: 'tool', tool: 'web_fetch', arg: 'comex.quote/GC', result: 'HTTP 429' },
            { kind: 'think', text: 'Two refusals in a row and the same status. Backing off once more, then stopping rather than reading a cached page that does not say when it was cached.' },
            { kind: 'tool', tool: 'web_fetch', arg: 'comex.quote/GC?retry=2', result: 'HTTP 429' },
            { kind: 'tool', tool: 'web_fetch', arg: 'comex.quote/GC?retry=3', result: 'HTTP 429' },
          ],
          stopped: 'Stopped without a quote. The vendor answered 429 four times; I did not substitute an estimate or an undated cached figure.',
        } },
      { id: 'fallback', title: 'Fall back to the last good quote', agent: 'Raven-Research', status: 'completed', from: ['fetch_comex'], duration: '2s', records: 1, toolCalls: 0,
        prompt: 'Use the 31 August quote and mark it as stale.' },
    ],
  },
]
