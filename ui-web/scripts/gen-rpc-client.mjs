// Raven ui -- RPC client codegen.
//
// Reads `rpc-schema/openrpc.json` (the repo-level contract, single source of
// truth) and emits `src/rpc/generated.ts`.
//
// What this emits that `ui-tui/scripts/gen-rpc-types.mjs` does not: the
// `RpcMethods` map. That map is what turns the contract from *available* into
// *enforced*. The TUI generates the same component and per-method types, but
// its client signature is `rpc<R, P>(method: string, params: P)` -- the method
// name is an unchecked string and the result type is a hand-written assertion,
// so nothing makes a call site agree with the contract. The page's ~74 call
// sites are strings with no check at all; a misspelled or not-yet-registered
// method fails at runtime with -32601 (subagent.list did exactly this before
// the contract caught up with the dispatcher).
//
// With the map, `transport.call('subagent.lst', {})` does not compile.
//
// Naming matches the TUI generator on purpose (methodToPascal, <Name>Params /
// <Name>Result), so the two outputs can be hoisted into one shared package
// without renaming anything downstream.
//
// Adapted from ui-web/scripts/gen-rpc-client.mjs (!105) by Blockchain-Key.
//
// Usage:
//   node scripts/gen-rpc-client.mjs           # write generated.ts
//   node scripts/gen-rpc-client.mjs --check   # exit 1 on drift (CI mode)

import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { compile } from 'json-schema-to-typescript'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '..')
const SCHEMA_PATH = resolve(ROOT, '..', 'rpc-schema/openrpc.json')
const OUT_PATH = resolve(ROOT, 'src/rpc/generated.ts')

function methodToPascal(method) {
  return method
    .split(/[._]/)
    .map(part => (part.length > 0 ? part[0].toUpperCase() + part.slice(1) : part))
    .join('')
}

function paramsToSchema(method) {
  const properties = {}
  const required = []
  for (const p of method.params ?? []) {
    properties[p.name] = p.schema
    if (p.required === true) required.push(p.name)
  }
  return {
    type: 'object',
    additionalProperties: false,
    properties,
    ...(required.length > 0 ? { required } : {})
  }
}

function rewriteRefs(node) {
  if (Array.isArray(node)) return node.map(rewriteRefs)
  if (node && typeof node === 'object') {
    const out = {}
    for (const [k, v] of Object.entries(node)) {
      out[k] =
        k === '$ref' && typeof v === 'string' ? v.replace('#/components/schemas/', '#/definitions/') : rewriteRefs(v)
    }
    return out
  }
  return node
}

function buildRootSchema(doc) {
  const defs = {}
  for (const [name, schema] of Object.entries(doc.components?.schemas ?? {})) {
    defs[name] = rewriteRefs(schema)
  }
  for (const method of doc.methods) {
    const pascal = methodToPascal(method.name)
    // A unique title per definition, or json-schema-to-typescript dedupes
    // structurally identical results (a dozen methods return {ok: boolean})
    // into one interface and every other name in the RpcMethods map dangles.
    defs[`${pascal}Params`] = { title: `${pascal}Params`, ...rewriteRefs(paramsToSchema(method)) }
    defs[`${pascal}Result`] = { title: `${pascal}Result`, ...rewriteRefs(method.result.schema) }
  }
  const properties = {}
  for (const name of Object.keys(defs)) properties[name] = { $ref: `#/definitions/${name}` }
  return {
    $schema: 'http://json-schema.org/draft-07/schema#',
    title: 'RavenRpcRoot',
    type: 'object',
    properties,
    definitions: defs
  }
}

// The map is the whole point of this generator: a method name that is not a key
// is not callable, and params/result are looked up from the name rather than
// restated by the caller.
function buildMethodMap(doc) {
  const rows = doc.methods
    .map(m => {
      const p = methodToPascal(m.name)
      return `  '${m.name}': { params: ${p}Params; result: ${p}Result };`
    })
    .join('\n')
  return `
// ---------------------------------------------------------------------------
// Method map -- generated from the contract's method list.
// ---------------------------------------------------------------------------

/** Every method the contract declares, mapped to its params and result. */
export interface RpcMethods {
${rows}
}

/** The literal union of callable method names. */
export type RpcMethod = keyof RpcMethods;

export type ParamsOf<M extends RpcMethod> = RpcMethods[M]['params'];
export type ResultOf<M extends RpcMethod> = RpcMethods[M]['result'];

/** Method names present in the contract, for a runtime guard at the edges. */
export const RPC_METHODS = ${JSON.stringify(doc.methods.map(m => m.name).sort(), null, 2)} as const;
`
}

const ENVELOPE = `
// ---------------------------------------------------------------------------
// JSON-RPC 2.0 envelope -- protocol level, not part of the method schema.
// ---------------------------------------------------------------------------

export interface JsonRpcRequest<M extends RpcMethod = RpcMethod> {
  jsonrpc: '2.0';
  id: number;
  method: M;
  params: ParamsOf<M>;
}

export interface JsonRpcError {
  code: number;
  message: string;
  data?: unknown;
}

export interface JsonRpcResponse<R = unknown> {
  jsonrpc: '2.0';
  id: number;
  result?: R;
  error?: JsonRpcError;
}

/** A server-initiated frame: no id, and the method is outside the call map. */
export interface JsonRpcNotification<P = unknown> {
  jsonrpc?: '2.0';
  method: string;
  params?: P;
}
`

async function main() {
  const check = process.argv.includes('--check')
  const doc = JSON.parse(await readFile(SCHEMA_PATH, 'utf8'))

  const header = `// AUTO-GENERATED -- DO NOT EDIT -- run \`npm run gen\`
//
// Source of truth: rpc-schema/openrpc.json (OpenRPC ${doc.openrpc}).
// Drift check: \`npm run gen:check\` (CI runs this; a stale file fails the build).
//
// ${doc.methods.length} methods, ${Object.keys(doc.components?.schemas ?? {}).length} component schemas.

/* eslint-disable */
`

  const types = await compile(buildRootSchema(doc), 'RavenRpcRoot', {
    bannerComment: '',
    additionalProperties: false,
    style: { singleQuote: true, printWidth: 100 }
  })

  // The root interface only exists to force every definition to be emitted;
  // nothing imports it.
  const body = types.replace(/export interface RavenRpcRoot \{[\s\S]*?\n\}\n/, '')
  const out = header + body + buildMethodMap(doc) + ENVELOPE

  if (check) {
    const current = await readFile(OUT_PATH, 'utf8').catch(() => null)
    if (current !== out) {
      console.error('!! src/rpc/generated.ts is out of sync with rpc-schema/openrpc.json')
      console.error('   run `npm run gen` and commit the result')
      process.exit(1)
    }
    console.log(`generated.ts matches the contract (${doc.methods.length} methods)`)
    return
  }

  await writeFile(OUT_PATH, out, 'utf8')
  console.log(`wrote src/rpc/generated.ts (${doc.methods.length} methods, ${out.length} bytes)`)
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
