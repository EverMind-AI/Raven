/* Reads a legacy module as script text, for the sandbox harnesses.
 *
 * Six tests evaluate a whole part of src/legacy/ inside `new Function(...)`
 * with its collaborators injected as parameters -- which is the only way to
 * drive the real function against fakes while the part still belongs to the
 * page rather than to a store of its own. `new Function` compiles a script,
 * not a module, so the three pieces of module syntax have to come off first.
 *
 * The injected parameter list stands in for exactly what the import
 * statements bind, so removing them changes nothing the harness relies on.
 * The harnesses move to real imports when the parts they test do (the plan's
 * A3b), and this helper goes with them.
 */
import { readFileSync } from 'node:fs'

export function sandboxSource(url, installAs) {
  return stripModuleSyntax(readFileSync(url, 'utf8'), installAs)
}

/* `installAs` renames the part's install(), so a harness that evaluates
   several parts at once keeps one callable per part instead of three
   declarations of the same name shadowing each other. */
export function stripModuleSyntax(text, installAs) {
  const out = text
    .replace(/^import[^\n]*\n/gm, '')
    .replace(/^export (function|const|let|var|class) /gm, '$1 ')
    .replace(/^export \{[^}]*\}\n?/gm, '')
  return installAs ? out.replace(/^function install\(\)/m, `function ${installAs}()`) : out
}
