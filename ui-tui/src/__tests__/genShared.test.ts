// SPDX-License-Identifier: MIT
import { execFileSync } from 'node:child_process'
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Script } from 'node:vm'
import { afterEach, describe, expect, it } from 'vitest'

/**
 * `gen-shared.mjs` writes catalogue entries into TypeScript source, so an entry
 * carrying a quote has to be serialised, not pasted: the sibling `gen-i18n.mjs`
 * uses `JSON.stringify` for exactly this reason. Unescaped, the generated
 * module does not parse, and `--check` only calls the file stale -- so the
 * failure surfaces later in `tsc` and `vitest` with nothing pointing here.
 */

const SCRIPT = fileURLToPath(new URL('../../scripts/gen-shared.mjs', import.meta.url))
const APOSTROPHE_VERB = "won't stop"
const APOSTROPHE_CHARM = "it's still cooking..."

let workdir: string | undefined

afterEach(() => {
  if (workdir !== undefined) {
    rmSync(workdir, { recursive: true, force: true })
    workdir = undefined
  }
})

/**
 * Run the real generator over a throwaway copy of the repo's shape: the
 * script at `ui-tui/scripts/`, the catalogues at `ui-shared/` two levels up,
 * and the projections under `ui-tui/src/content/`.
 */
function generate(): { charms: string; verbs: string } {
  const dir = mkdtempSync(join(tmpdir(), 'gen-shared-'))
  workdir = dir
  const scripts = join(dir, 'ui-tui', 'scripts')
  const content = join(dir, 'ui-tui', 'src', 'content')
  mkdirSync(scripts, { recursive: true })
  mkdirSync(content, { recursive: true })
  mkdirSync(join(dir, 'ui-shared'))
  copyFileSync(SCRIPT, join(scripts, 'gen-shared.mjs'))
  writeFileSync(
    join(dir, 'ui-shared', 'verbs.json'),
    JSON.stringify({ rotationMs: 2500, verbs: ['circling', APOSTROPHE_VERB] }),
  )
  writeFileSync(join(dir, 'ui-shared', 'charms.json'), JSON.stringify({ charms: [APOSTROPHE_CHARM] }))
  execFileSync(process.execPath, [join(scripts, 'gen-shared.mjs')], { cwd: dir })
  return {
    charms: readFileSync(join(content, 'charms.generated.ts'), 'utf8'),
    verbs: readFileSync(join(content, 'verbs.generated.ts'), 'utf8'),
  }
}

/** Compile without executing: a syntax error here is the generated file's. */
function parseModule(source: string): void {
  new Script(source.replace(/^export /gm, ''))
}

describe('gen-shared', () => {
  it('emits a module that parses when an entry carries an apostrophe', () => {
    const { charms, verbs } = generate()
    expect(() => parseModule(verbs)).not.toThrow()
    expect(() => parseModule(charms)).not.toThrow()
  })

  it('serialises each entry so the generated literal reads back unchanged', () => {
    const { charms, verbs } = generate()
    expect(verbs).toContain(`  ${JSON.stringify(APOSTROPHE_VERB)}`)
    expect(charms).toContain(`  ${JSON.stringify(APOSTROPHE_CHARM)}`)
  })
})
