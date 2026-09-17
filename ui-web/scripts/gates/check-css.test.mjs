/* `node scripts/check-css.mjs`, run by `npm test`.
 *
 * The three artifact checks are CLI tools because build.py and CI call them
 * where vitest may not be installed, and check-page.mjs really needs the built
 * dist/. This one and check-class-namespace.mjs read src/ alone -- so they can
 * run in the inner loop, and the only reason they did not was that nothing
 * asked them to. Spawned rather than imported: the tool reports by exit code
 * and process.exit(1) inside vitest would take the runner down with it.
 *
 * The CLI entry point stays -- this is a second caller, not a replacement.
 */

import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const script = fileURLToPath(new URL('../check-css.mjs', import.meta.url))

describe('the stylesheet color-token check', () => {
  it('passes over the tree as it stands', () => {
    let output = ''
    let failed = null
    try {
      output = execFileSync(process.execPath, [script], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
    } catch (error) {
      failed = `${error.stdout ?? ''}${error.stderr ?? ''}`.trim() || String(error)
    }
    expect(failed, 'run `node scripts/check-css.mjs` and fix what it names').toBe(null)
    expect(output).toContain('check-css: OK')
  })
})
