/* Every method name the page's own modules call the gateway with.
 *
 * Collected with the TypeScript API rather than by regex, because what tells
 * `gateway().call('x')` apart from `source.call('x')` is that the callee is a
 * property of a call to `gateway` -- and because a name that is not a string
 * literal has to be reported as such rather than missed.
 *
 * Two gates read this: scripts/gates/rpc-names.test.mjs holds the names to the
 * contract, and offline-coverage.test.mjs holds them to the offline library.
 * One collector so the two cannot disagree about what "a call" is.
 */
import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import ts from 'typescript'

/* Every feature's source module and everything under state/ and app/: a wire
   call may only be written in one of those (rpc-names holds every domain to
   it), and the two sets together are the page's whole traffic. Paths are from
   src/. */
const sourceModules = () => readdirSync(resolve(process.cwd(), 'src/features'), { withFileTypes: true })
  .filter((e) => e.isDirectory())
  .map((e) => `features/${e.name}/source.ts`)
  .filter((rel) => {
    try { readFileSync(resolve(process.cwd(), 'src', rel)); return true } catch { return false }
  })

/* Every .ts under one of those trees, at any depth, tests excluded. */
const treeModules = (dir) => readdirSync(resolve(process.cwd(), 'src', dir), { withFileTypes: true })
  .flatMap((e) => (e.isDirectory()
    ? treeModules(`${dir}/${e.name}`)
    : e.name.endsWith('.ts') && !e.name.includes('.test.') ? [`${dir}/${e.name}`] : []))

/** The modules a wire call may be written in. */
export const FILES = () => [...sourceModules(), ...treeModules('state'), ...treeModules('app')]

/* A `gateway().call(...)` or `gateway().binary(...)`: the callee is a property
   of a call to `gateway`, which is what tells it apart from `source.call(...)`
   or any other object's method of the same name. */
function isGatewayMember(node, member) {
  if (!ts.isCallExpression(node)) return false
  const callee = node.expression
  if (!ts.isPropertyAccessExpression(callee) || callee.name.text !== member) return false
  const object = callee.expression
  return ts.isCallExpression(object)
    && ts.isIdentifier(object.expression)
    && object.expression.text === 'gateway'
}

/**
 * Every [file:line, name] the layer calls `member` with. A name that is not a
 * string literal is reported as such: an expression is a name nothing can
 * check, which is the whole failure mode here.
 */
export function names(member) {
  const found = []
  for (const rel of FILES()) {
    const text = readFileSync(resolve(process.cwd(), 'src', rel), 'utf8')
    const kind = rel.endsWith('.ts') ? ts.ScriptKind.TS : ts.ScriptKind.JS
    const sf = ts.createSourceFile(rel, text, ts.ScriptTarget.ES2022, true, kind)
    const walk = (node) => {
      if (isGatewayMember(node, member)) {
        const first = node.arguments[0]
        const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1
        found.push([
          `${rel}:${line}`,
          first && ts.isStringLiteral(first) ? first.text : `<not a literal: ${first ? first.getText(sf) : 'no argument'}>`,
        ])
      }
      ts.forEachChild(node, walk)
    }
    walk(sf)
  }
  return found
}
