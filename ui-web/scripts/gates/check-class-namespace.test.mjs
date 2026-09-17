/* `node scripts/check-class-namespace.mjs`, run by `npm test`.
 *
 * Same arrangement as check-css.test.mjs: the tool reads src/ alone, so the
 * inner loop can run it, and it is spawned rather than imported because it
 * reports by exit code.
 */

import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const script = fileURLToPath(new URL('../check-class-namespace.mjs', import.meta.url))

describe('the island class-namespace check', () => {
  it('passes over the tree as it stands', () => {
    let output = ''
    let failed = null
    try {
      output = execFileSync(process.execPath, [script], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
    } catch (error) {
      failed = `${error.stdout ?? ''}${error.stderr ?? ''}`.trim() || String(error)
    }
    expect(failed, 'run `node scripts/check-class-namespace.mjs` and fix what it names').toBe(null)
    expect(output).toContain('check-class-namespace: OK')
  })
})
