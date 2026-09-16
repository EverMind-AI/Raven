// Indents the body of every part's install() by two spaces.
//
// The A3 codemod left those bodies at column 0 because eighteen sandbox
// harnesses sliced statements out of the layers as TEXT and anchored on the
// columns the concatenated script had. They import the parts now (the plan's
// A3b), so the bodies can read like the rest of the file.
//
// What must not move: a line whose start lies inside a multi-line template
// literal (two spaces would land in the string) or inside a block comment.
// Both are found with the TypeScript scanner rather than by eye, and the run
// refuses to write if any string or template literal's own text changed.
//
//   node scripts/codemod/a3b-indent-install.mjs [--dry]
import { readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import ts from 'typescript'

const SRC = new URL('../../src/legacy/', import.meta.url)
const LAYERS = ['seam', 'demo', 'live']
const DRY = process.argv.includes('--dry')

/* The sentence the A3 note ends with is about the text harnesses, which are
   gone; the rest of it still says what install() is. */
const OLD_TAIL = ` The body keeps the
   statements' original column: the sandbox harnesses slice them out by text. */`
const NEW_TAIL = ' */'

const parse = (rel, text) => ts.createSourceFile(rel, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS)

/* Every literal's own text, so the run can prove it changed none of them. */
function literals(sf) {
  const out = []
  const walk = (node) => {
    if (
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateHead(node) ||
      ts.isTemplateMiddle(node) ||
      ts.isTemplateTail(node)
    ) {
      out.push(node.text)
    }
    ts.forEachChild(node, walk)
  }
  walk(sf)
  return out
}

/* Lines whose first character sits inside a multi-line literal or block
   comment, and which therefore belong to that literal or comment rather than
   to the statement list. */
function protectedLines(sf, text) {
  const line = (pos) => sf.getLineAndCharacterOfPosition(pos).line
  const out = new Set()
  const span = (start, end) => {
    const from = line(start)
    const to = line(end)
    for (let n = from + 1; n <= to; n += 1) out.add(n)
  }
  const walk = (node) => {
    if (
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateExpression(node)
    ) {
      span(node.getStart(sf), node.end)
    }
    for (const range of ts.getLeadingCommentRanges(text, node.pos) ?? []) {
      if (range.kind === ts.SyntaxKind.MultiLineCommentTrivia) span(range.pos, range.end)
    }
    for (const range of ts.getTrailingCommentRanges(text, node.end) ?? []) {
      if (range.kind === ts.SyntaxKind.MultiLineCommentTrivia) span(range.pos, range.end)
    }
    ts.forEachChild(node, walk)
  }
  walk(sf)
  return out
}

function indentInstall(rel, text) {
  const sf = parse(rel, text)
  const install = sf.statements.find(
    (st) => ts.isFunctionDeclaration(st) && st.name?.text === 'install'
  )
  if (!install?.body) throw new Error(`${rel}: no install() to indent`)
  const first = sf.getLineAndCharacterOfPosition(install.body.getStart(sf)).line
  const last = sf.getLineAndCharacterOfPosition(install.body.end).line
  const skip = protectedLines(sf, text)
  const lines = text.split('\n')
  let moved = 0
  for (let n = first + 1; n < last; n += 1) {
    if (skip.has(n) || !lines[n].trim()) continue
    lines[n] = `  ${lines[n]}`
    moved += 1
  }
  return { out: lines.join('\n'), moved }
}

let files = 0
let statements = 0
for (const layer of LAYERS) {
  const dir = new URL(`${layer}/`, SRC)
  for (const name of readdirSync(dir).filter((n) => n.endsWith('.js')).sort()) {
    const rel = `${layer}/${name}`
    const path = join(dir.pathname, name)
    const before = readFileSync(path, 'utf8')
    const { out, moved } = indentInstall(rel, before)
    const noted = out.includes(OLD_TAIL) ? out.replace(OLD_TAIL, NEW_TAIL) : out
    const was = literals(parse(rel, before))
    const now = literals(parse(rel, noted))
    if (was.length !== now.length || was.some((t, i) => t !== now[i])) {
      throw new Error(`${rel}: a string or template literal changed -- refusing to write`)
    }
    if (!DRY && noted !== before) writeFileSync(path, noted)
    files += 1
    statements += moved
    console.log(`${rel}: ${moved} lines indented`)
  }
}
console.log(`\ntotals: ${files} parts, ${statements} lines indented${DRY ? ' (dry run)' : ''}`)
