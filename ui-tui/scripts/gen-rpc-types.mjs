#!/usr/bin/env node
// Raven TUI RPC — codegen entrypoint (Phase 6 Wave 5).
//
// Reads `ui-tui/rpc-schema/openrpc.json` (single source of truth — REQ-5)
// and emits TypeScript types into `ui-tui/src/rpc/generated.ts`.
//
// What gets emitted:
//   - All `components/schemas/*` (SessionInfo, SessionMessage, McpServerInfo,
//     McpToolInfo, SkillInfo, ModelInfo, UsageSnapshot, CliResult, StubResult,
//     all per-variant *Event types, plus the TurnEvent discriminated union)
//   - One `<MethodName>Params` + `<MethodName>Result` interface per RPC method
//     (37 methods × 2 = 74 method-scoped types)
//   - JSON-RPC 2.0 envelope types (hand-written tail — they aren't in the
//     schema since they're protocol-level not method-level)
//
// F-C fork point (per research-findings.md Finding 2 + design Q1):
//   `json-schema-to-typescript` historically struggled with OpenRPC-style
//   `discriminator` (issue bcherny/json-schema-to-typescript#239). We MUST
//   verify the emitted `TurnEvent` narrows correctly:
//
//       if (event.type === 'token.delta') { event.payload.text /* string */ }
//
//   The probe in `verifyTurnEventNarrowing()` below runs the codegen, greps
//   the output, and warns if the union looks broken. If the probe trips, the
//   documented escape hatch is to swap to `@open-rpc/typings` — see
//   `docs/RepoMem/temp/tui-ipc-bridge/01-schema-tooling-decision.md`.
//
// Usage:
//   node scripts/gen-rpc-types.mjs            # write generated.ts
//   node scripts/gen-rpc-types.mjs --check    # write to tmp + diff against
//                                              # checked-in generated.ts;
//                                              # exit 1 on drift (CI lint mode)

import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { compile } from 'json-schema-to-typescript';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..');
const SCHEMA_PATH = resolve(ROOT, 'rpc-schema/openrpc.json');
const OUT_PATH = resolve(ROOT, 'src/rpc/generated.ts');

const HEADER = `// AUTO-GENERATED — DO NOT EDIT — run \`npm run gen:rpc\`
//
// Source of truth: ui-tui/rpc-schema/openrpc.json (OpenRPC 1.2.6).
// Regenerate via: cd ui-tui && npm run gen:rpc
// Lint (drift check) via: cd ui-tui && npm run lint:rpc
//
// 74 method-scoped types (37 RPC methods × {Params, Result}) + all
// components/schemas + JSON-RPC 2.0 envelope types.

/* eslint-disable */
/* tslint:disable */
`;

// ---------------------------------------------------------------------------
// Param-name → PascalCase (e.g. "turn.send" → "TurnSend")
// ---------------------------------------------------------------------------
function methodToPascal(method) {
  return method
    .split(/[._]/)
    .map((part) => (part.length > 0 ? part[0].toUpperCase() + part.slice(1) : part))
    .join('');
}

// Convert OpenRPC method `params: [{name, required, schema}, ...]` to a single
// JSON Schema object type. This is what json-schema-to-typescript needs.
function paramsToSchema(method) {
  const properties = {};
  const required = [];
  for (const p of method.params ?? []) {
    properties[p.name] = p.schema;
    if (p.required === true) required.push(p.name);
  }
  return {
    type: 'object',
    additionalProperties: false,
    properties,
    ...(required.length > 0 ? { required } : {}),
  };
}

// Build a single composite root schema that json-schema-to-typescript can
// compile in one pass. All `$ref`s in the original schema use
// `#/components/schemas/X` — we preserve that by hoisting them to
// `#/definitions/X` (the canonical JSON-Schema location) and rewriting refs.
function buildRootSchema(openrpcDoc) {
  const defs = {};
  const componentSchemas = openrpcDoc.components?.schemas ?? {};

  for (const [name, schema] of Object.entries(componentSchemas)) {
    defs[name] = rewriteRefs(schema);
  }

  // Per-method Params + Result types.
  for (const method of openrpcDoc.methods) {
    const pascal = methodToPascal(method.name);
    defs[`${pascal}Params`] = rewriteRefs(paramsToSchema(method));
    // Result.schema is the actual type; Result.name is just a label
    defs[`${pascal}Result`] = rewriteRefs(method.result.schema);
  }

  const rootProperties = {};
  for (const name of Object.keys(defs)) {
    rootProperties[name] = { $ref: `#/definitions/${name}` };
  }

  return {
    $schema: 'http://json-schema.org/draft-07/schema#',
    title: 'RavenRpcRoot',
    type: 'object',
    properties: rootProperties,
    definitions: defs,
  };
}

function rewriteRefs(node) {
  if (Array.isArray(node)) return node.map(rewriteRefs);
  if (node && typeof node === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(node)) {
      if (k === '$ref' && typeof v === 'string') {
        out[k] = v.replace('#/components/schemas/', '#/definitions/');
      } else {
        out[k] = rewriteRefs(v);
      }
    }
    return out;
  }
  return node;
}

// ---------------------------------------------------------------------------
// JSON-RPC 2.0 envelope — appended verbatim (protocol-level, not in schema).
// ---------------------------------------------------------------------------
const JSON_RPC_ENVELOPE = `
// ---------------------------------------------------------------------------
// JSON-RPC 2.0 envelope (specs/tui-ipc.md §2.1/2.2/2.3/2.4)
// ---------------------------------------------------------------------------

export interface JsonRpcRequest<P = unknown> {
  jsonrpc: '2.0';
  id: string | number;
  method: string;
  params: P;
}

export interface JsonRpcSuccess<R = unknown> {
  jsonrpc: '2.0';
  id: string | number;
  result: R;
}

export interface JsonRpcErrorObject {
  code: number;
  message: string;
  data?: unknown;
}

export interface JsonRpcErrorResponse {
  jsonrpc: '2.0';
  id: string | number;
  error: JsonRpcErrorObject;
}

export type JsonRpcResponse<R = unknown> = JsonRpcSuccess<R> | JsonRpcErrorResponse;

export interface JsonRpcNotification<P = unknown> {
  jsonrpc: '2.0';
  method: string;
  params: P;
}

export interface EventNotificationParams<E = unknown> {
  subscription_id: string;
  event: E;
}

export function isJsonRpcError<R>(
  resp: JsonRpcResponse<R>,
): resp is JsonRpcErrorResponse {
  return (resp as JsonRpcErrorResponse).error !== undefined;
}
`;

// ---------------------------------------------------------------------------
// F-C check — verify TurnEvent narrows correctly.
// ---------------------------------------------------------------------------
function verifyTurnEventNarrowing(emitted) {
  // The "good" emit either:
  //   (a) inlines a tagged union like:  { type: 'token.delta'; payload: ... }
  //       inside `export type TurnEvent = ...`
  //   (b) emits TurnEvent as `TurnEventMessageStart | TurnEventTokenDelta | ...`
  //       where each variant has a literal `type` discriminator.
  // The "bad" emit collapses payload into Record<string, any> or drops the
  // `type` literal, making narrowing impossible.

  const turnEventBlock = emitted.match(/export type TurnEvent\s*=([\s\S]*?);/);
  if (!turnEventBlock) {
    return {
      ok: false,
      reason: 'TurnEvent type not found in output — codegen failed to emit it.',
    };
  }
  const body = turnEventBlock[1];
  // Heuristic: ensure each of the 8 discriminator literals appears as a
  // literal type either inline (`type: 'token.delta'`) or as a referenced
  // interface (which itself contains the literal). We accept either form by
  // also checking the full file for the literals.
  const literals = [
    'message.start',
    'token.delta',
    'thinking.delta',
    'tool.start',
    'tool.progress',
    'tool.complete',
    'message.complete',
    'error',
  ];
  const missing = literals.filter(
    (lit) => !body.includes(`"${lit}"`) && !body.includes(`'${lit}'`) && !emitted.match(
      new RegExp(`type:\\s*['"]${lit.replace('.', '\\.')}['"]`),
    ),
  );
  if (missing.length > 0) {
    return {
      ok: false,
      reason: `TurnEvent missing discriminator literals: ${missing.join(', ')}`,
    };
  }
  // Verify it's a union (contains `|`) of object-shapes with `type` discriminator.
  if (!body.includes('|')) {
    return {
      ok: false,
      reason: 'TurnEvent is not a union — narrowing impossible.',
    };
  }
  return { ok: true };
}

// ---------------------------------------------------------------------------
// Codegen pipeline
// ---------------------------------------------------------------------------
async function generate() {
  const raw = await readFile(SCHEMA_PATH, 'utf-8');
  const openrpcDoc = JSON.parse(raw);
  const root = buildRootSchema(openrpcDoc);

  // Compile. Options chosen to minimize false drift between runs:
  //   - declareExternallyReferenced: false (we control the ref tree)
  //   - unreachableDefinitions: true (emit method-scoped types even though
  //     they're only referenced from the root object)
  //   - bannerComment: empty (we prepend our own HEADER)
  //   - additionalProperties: false (preserve the schema's strict closure)
  const compiled = await compile(root, 'RavenRpcRoot', {
    bannerComment: '',
    unreachableDefinitions: true,
    additionalProperties: false,
    style: { singleQuote: true, semi: true, trailingComma: 'all' },
  });

  // Drop the synthetic root interface — it's just a `Record` over every
  // definition and not useful to consumers. Keep only the named types.
  const withoutRoot = compiled
    .replace(/export interface RavenRpcRoot\s*\{[\s\S]*?\n\}\n?/, '')
    .trim();

  // json-schema-to-typescript merges structurally-identical definitions into
  // a single exported type (e.g. all 6 hermes-only stub results collapse to
  // `StubResult`; `CliDispatchResult` collapses into `CliResult`). To keep
  // every schema-named type reachable by its schema-canonical name, we emit
  // type aliases for any definition name that didn't get its own declaration.
  //
  // We detect the canonical name by parsing the deferred-comment trail that
  // json-schema-to-typescript writes ABOVE each merged declaration:
  //   /**
  //    * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
  //    * via the `definition` "CliResult".
  //    *
  //    * This interface was referenced by ... `definition` "CliDispatchResult".
  //    */
  //   export interface CliResult { ... }
  const allNames = new Set(Object.keys(root.definitions));
  const declared = new Set();
  const declRegex = /^export (?:interface|type) (\w+)\b/gm;
  let m;
  while ((m = declRegex.exec(withoutRoot)) !== null) declared.add(m[1]);

  // Walk each declaration with its preceding doc comment and map all
  // referenced definition names to the declared canonical.
  const aliasFor = {}; // missingName -> canonicalName
  const blockRegex =
    /\/\*\*([\s\S]*?)\*\/\s*export (?:interface|type) (\w+)\b/g;
  while ((m = blockRegex.exec(withoutRoot)) !== null) {
    const doc = m[1];
    const canonical = m[2];
    const refNameRegex = /`definition` "(\w+)"/g;
    let r;
    while ((r = refNameRegex.exec(doc)) !== null) {
      const refName = r[1];
      if (refName !== canonical && !declared.has(refName)) {
        aliasFor[refName] = canonical;
      }
    }
  }

  // Belt-and-suspenders: for any name still not declared and not aliased,
  // emit an empty-object fallback so downstream imports still type-check.
  const aliasLines = [];
  for (const name of allNames) {
    if (declared.has(name)) continue;
    if (aliasFor[name]) {
      aliasLines.push(`export type ${name} = ${aliasFor[name]};`);
    } else {
      // Should never happen — bail loudly.
      throw new Error(
        `codegen: definition "${name}" produced no declaration and no alias` +
          ' candidate was found in deferred-comments. Inspect compiled output.',
      );
    }
  }
  const aliasBlock = aliasLines.length
    ? '\n// ---- Schema-name aliases for structurally-deduplicated types ----\n' +
      aliasLines.sort().join('\n') +
      '\n'
    : '';

  const full = HEADER + '\n' + withoutRoot + '\n' + aliasBlock + JSON_RPC_ENVELOPE;

  // F-C check.
  const probe = verifyTurnEventNarrowing(full);
  if (!probe.ok) {
    console.error('');
    console.error('!! F-C fork point tripped: TurnEvent narrowing broken.');
    console.error(`   reason: ${probe.reason}`);
    console.error('   action: switch codegen tool to `@open-rpc/typings`.');
    console.error('   see: docs/RepoMem/temp/tui-ipc-bridge/01-schema-tooling-decision.md');
    process.exit(2);
  }

  return full;
}

function sha(s) {
  return createHash('sha256').update(s).digest('hex').slice(0, 12);
}

async function main() {
  const check = process.argv.includes('--check');
  const fresh = await generate();

  if (check) {
    let existing = '';
    try {
      existing = await readFile(OUT_PATH, 'utf-8');
    } catch {
      console.error(`!! ${OUT_PATH} does not exist — run \`npm run gen:rpc\` first.`);
      process.exit(1);
    }
    if (existing.trim() !== fresh.trim()) {
      console.error('!! generated.ts is out of sync with rpc-schema/openrpc.json');
      console.error(`   existing sha256: ${sha(existing)}`);
      console.error(`   fresh    sha256: ${sha(fresh)}`);
      console.error('   fix: cd ui-tui && npm run gen:rpc && git add src/rpc/generated.ts');
      process.exit(1);
    }
    console.log(`OK: generated.ts in sync (sha256: ${sha(fresh)})`);
    return;
  }

  await writeFile(OUT_PATH, fresh, 'utf-8');
  const methodCount = JSON.parse(await readFile(SCHEMA_PATH, 'utf-8')).methods.length;
  console.log(
    `Wrote ${OUT_PATH}\n  ${methodCount} methods × 2 (Params/Result) + components + envelope\n  sha256: ${sha(fresh)}`,
  );
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
