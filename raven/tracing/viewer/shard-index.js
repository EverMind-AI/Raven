'use strict';

// Per-file indexes over the span logs, so a reader can answer "which sessions
// exist" and "which files hold session X" without parsing every retained span.
//
// The store is already sharded -- rotation only ever writes <logs>/archive/<date>/
// -- and a rotated file never changes again, so an index over one is computed
// once and stays valid for the life of the file. The active log is the only
// mutable input and is indexed in memory instead of on disk.
//
// What makes this less trivial than it looks: a span cannot be attributed to a
// session by looking at the span. More than half the records carry no
// `session.id` at all, and the ones that do are resolved through an election run
// over the whole corpus (see `electIdentity`). So a sidecar deliberately does
// NOT record "this file contains session X". It records the raw
// (session.id, session.key) pairs it saw plus local tallies, and the election
// runs globally over the merged tallies. Counts aggregate, spans do not, which
// is the whole reason this split works: the cheap thing goes global and the
// expensive thing stays in shards.

const fs = require('fs');
const path = require('path');

const { getLogsDir, getActiveLogPath, statLogFiles, readJsonlFile } = require('./log-store');

// Bump on ANY change to what indexRecords computes, not just to the sidecar's
// shape. A sidecar is validated against its log's size and mtime, which say
// nothing about the code that produced it, so a logic change without a bump
// silently reuses indexes built by the old logic -- which is how the first
// version of the dedupe below appeared to do nothing at all.
const SCHEMA = 2;

// Spans the viewer never shows on their own, so a session whose spans are all in
// this set has nothing to look at.
//
// Note what this does NOT give: a trace count. buildTraceGroups groups by
// parent-child linkage rather than by trace id -- a child joins its parent's
// group even with a different trace id -- so the number of traces is the number
// of spans whose parent is absent from the session, which cannot be decided one
// file at a time. Making it exact would mean holding every span id and edge in
// the index, roughly 18 MB of sidecar for the store this was measured on, parsed
// on every session-list request. The list reports span counts, which are exact,
// and a trace count arrives with the session it belongs to.
const INVISIBLE_SPAN_NAMES = new Set(['session.turn', 'skills.scan', 'skills.catalog_read', 'skills.cataloged']);

function bump(map, key) {
  if (key === null || key === undefined || key === '') return;
  map[key] = (map[key] || 0) + 1;
}

function mergeCounts(target, source) {
  for (const [key, count] of Object.entries(source || {})) {
    target[key] = (target[key] || 0) + count;
  }
}

function emptyPair(id, key) {
  return {
    id: id || null,
    key: key || null,
    spans: 0,
    minTime: null,
    maxTime: null,
    visibleTraceIds: [],
    sessionKey: {},
    agentId: {},
    workspaceDir: {},
    trigger: {},
    channelId: {},
    surface: {}
  };
}

function pairKey(id, key) {
  // Serialized rather than joined on a separator character: a separator is
  // only safe if no id or key can contain it, and neither is constrained here.
  return JSON.stringify([id || null, key || null]);
}

/**
 * Index one log file's records. Pure: no filesystem access, no globals.
 *
 * Deduplicates by span id first, last one winning, which is what `dedupeSpans`
 * does to the whole corpus before it is tallied -- a span re-emitted in the same
 * turn carries a later endTime, so counting both would inflate the tallies and
 * skew the election. Doing it per file rather than globally is exact as long as
 * a span id does not straddle a rotation, which it cannot: the writer appends a
 * span once, and re-emissions land in whichever file is active at the time.
 * Measured on a real store, 22,998 of 270,344 records were duplicates and every
 * one of them sat inside a single file. The residual, if a store ever did split
 * one span id across two files, is one extra vote in one tally.
 */
function indexRecords(records) {
  const deduped = new Map();
  for (const record of records) {
    if (!record || !record.spanId) continue;
    deduped.set(record.spanId, record);
  }
  records = [...deduped.values()];

  const pairs = new Map();
  const keyToId = {};
  const idMeta = {};
  const toolCallIds = new Set();
  const visibleTraceIdsByPair = new Map();

  for (const record of records) {
    const attrs = record?.attributes || {};
    const id = attrs['session.id'] || null;
    const key = attrs['session.key'] || null;
    if (!id && !key) continue;

    if (key) {
      if (!keyToId[key]) keyToId[key] = {};
      // The election counts the empty id too: a key seen mostly without an id
      // must not let one stray span decide its canonical id.
      const idBucket = id || '';
      keyToId[key][idBucket] = (keyToId[key][idBucket] || 0) + 1;
    }
    if (id) {
      if (!idMeta[id]) idMeta[id] = { keyCounts: {}, agentCounts: {}, workspaceCounts: {} };
      bump(idMeta[id].keyCounts, key);
      bump(idMeta[id].agentCounts, attrs['agent.id']);
      bump(idMeta[id].workspaceCounts, attrs['workspace.dir']);
    }

    const pk = pairKey(id, key);
    if (!pairs.has(pk)) {
      pairs.set(pk, emptyPair(id, key));
      visibleTraceIdsByPair.set(pk, new Set());
    }
    const pair = pairs.get(pk);
    pair.spans += 1;
    if (record.startTime && (pair.minTime === null || record.startTime < pair.minTime)) pair.minTime = record.startTime;
    const end = record.endTime || record.startTime;
    if (end && (pair.maxTime === null || end > pair.maxTime)) pair.maxTime = end;
    bump(pair.sessionKey, key);
    bump(pair.agentId, attrs['agent.id']);
    bump(pair.workspaceDir, attrs['workspace.dir']);
    bump(pair.trigger, attrs.trigger);
    bump(pair.channelId, attrs['channel.id']);
    bump(pair.surface, attrs.surface);
    if (record.traceId && !INVISIBLE_SPAN_NAMES.has(record.name)) {
      visibleTraceIdsByPair.get(pk).add(record.traceId);
    }

    const toolCallId = attrs['subagent.tool_call_id'];
    if (record.name === 'subagent.call' && toolCallId) toolCallIds.add(toolCallId);
  }

  for (const [pk, pair] of pairs) {
    pair.visibleTraceIds = [...visibleTraceIdsByPair.get(pk)].sort();
  }

  return {
    schema: SCHEMA,
    pairs: [...pairs.values()],
    keyToId,
    idMeta,
    toolCallIds: [...toolCallIds].sort()
  };
}

function sidecarPath(logPath) {
  const logsDir = getLogsDir();
  const relative = path.relative(logsDir, logPath);
  // Under logs/index/ rather than beside the log, so nothing new appears in a
  // directory whose entries the log reader matches on by name.
  return path.join(logsDir, 'index', `${relative}.json`);
}

function readSidecar(logPath, size, mtimeMs) {
  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(sidecarPath(logPath), 'utf8'));
  } catch {
    return null;
  }
  // Any doubt at all means rebuild. Never "skip this file": a sidecar that is
  // stale, truncated or from an older schema would otherwise delete history from
  // the panel silently, which is the one failure mode worth being paranoid about.
  if (!parsed || parsed.schema !== SCHEMA || !Array.isArray(parsed.pairs)) return null;
  if (parsed.source?.size !== size || parsed.source?.mtimeMs !== mtimeMs) return null;
  return parsed;
}

function writeSidecar(logPath, index) {
  const target = sidecarPath(logPath);
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    // Written via a temp file in the same directory so a reader never sees a
    // partial sidecar, only the old one or the new one.
    const temp = `${target}.${process.pid}.tmp`;
    fs.writeFileSync(temp, JSON.stringify(index), 'utf8');
    fs.renameSync(temp, target);
  } catch {
    // An unwritable index directory costs speed, not correctness: the caller
    // already has the index it just built.
  }
}

/**
 * Index for one log file, from its sidecar when that is still valid and by
 * parsing the file when it is not. `persist` is false for the active log, whose
 * sidecar would be stale the moment the next span lands.
 *
 * Prefer `indexFor`, which decides `persist` from the log's own identity rather
 * than leaving each caller to remember the rule.
 */
function fileIndex(entry, { persist = true } = {}) {
  const cached = readSidecar(entry.path, entry.size, entry.mtimeMs);
  if (cached) return cached;
  const built = indexRecords(readJsonlFile(entry.path));
  built.source = { size: entry.size, mtimeMs: entry.mtimeMs };
  if (persist) writeSidecar(entry.path, built);
  return built;
}

// One place that knows the active log must not be persisted, so a caller cannot
// disagree with the rule by forgetting it.
function indexFor(entry, kind) {
  return fileIndex(entry, { persist: entry.path !== getActiveLogPath(kind) });
}

function isUuidLike(value) {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

function preferredValue(counts) {
  return (
    Object.entries(counts || {}).sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])))[0]?.[0] || null
  );
}

/**
 * Elect a canonical session id per session key, and a preferred key/agent/
 * workspace per session id, from merged tallies.
 *
 * Shared by both readers on purpose. The whole-corpus path in server.js builds
 * the same tallies from spans in memory and calls this; the sharded path merges
 * them from sidecars and calls this. Equivalence is then structural rather than
 * something a test has to keep rediscovering.
 */
function electIdentity({ keyToId, idMeta }) {
  const canonicalIdBySessionKey = new Map();
  for (const [sessionKey, counts] of Object.entries(keyToId || {})) {
    const entries = Object.entries(counts).filter(([sessionId]) => sessionId);
    if (!entries.length) continue;
    entries.sort((a, b) => {
      const [idA, countA] = a;
      const [idB, countB] = b;
      const uuidBias = Number(isUuidLike(idB)) - Number(isUuidLike(idA));
      if (uuidBias !== 0) return uuidBias;
      if (countB !== countA) return countB - countA;
      return idA.localeCompare(idB);
    });
    canonicalIdBySessionKey.set(sessionKey, entries[0][0]);
  }

  const sessionMetaById = new Map();
  for (const [sessionId, meta] of Object.entries(idMeta || {})) {
    sessionMetaById.set(sessionId, {
      sessionKey: preferredValue(meta.keyCounts),
      agentId: preferredValue(meta.agentCounts),
      workspaceDir: preferredValue(meta.workspaceCounts)
    });
  }

  return { canonicalIdBySessionKey, sessionMetaById };
}

/**
 * Which session a raw (id, key) pair resolves to. Mirrors the resolution
 * `projectSpanForDisplay` applies to a span, which is why file selection can be
 * decided from pairs alone without touching a span.
 */
function resolvePairSessionId(pair, canonicalIdBySessionKey) {
  const { id, key } = pair;
  if (isUuidLike(id)) return id;
  const alias = id ? canonicalIdBySessionKey.get(id) || null : null;
  return id || alias || (key ? canonicalIdBySessionKey.get(key) || null : null) || null;
}

/**
 * Merge every shard's index for one log kind, run the election once, and fold
 * the pairs into per-session rows plus the files each session touches.
 */
function mergedIndex(kind) {
  const entries = statLogFiles(kind);

  const keyToId = {};
  const idMeta = {};
  const toolCallIds = new Set();
  const perFilePairs = [];

  for (const entry of entries) {
    const index = indexFor(entry, kind);
    mergeNested(keyToId, index.keyToId);
    for (const [id, meta] of Object.entries(index.idMeta || {})) {
      if (!idMeta[id]) idMeta[id] = { keyCounts: {}, agentCounts: {}, workspaceCounts: {} };
      mergeCounts(idMeta[id].keyCounts, meta.keyCounts);
      mergeCounts(idMeta[id].agentCounts, meta.agentCounts);
      mergeCounts(idMeta[id].workspaceCounts, meta.workspaceCounts);
    }
    for (const id of index.toolCallIds || []) toolCallIds.add(id);
    perFilePairs.push({ path: entry.path, pairs: index.pairs || [] });
  }

  const identity = electIdentity({ keyToId, idMeta });
  const sessions = new Map();

  for (const { path: filePath, pairs } of perFilePairs) {
    for (const pair of pairs) {
      const sessionId = resolvePairSessionId(pair, identity.canonicalIdBySessionKey);
      if (!sessionId) continue;
      if (!sessions.has(sessionId)) {
        sessions.set(sessionId, {
          sessionId,
          spans: 0,
          startedAt: null,
          updatedAt: null,
          files: new Set(),
          visibleTraceIds: new Set(),
          counts: { sessionKey: {}, agentId: {}, workspaceDir: {}, trigger: {}, channelId: {}, surface: {} }
        });
      }
      const row = sessions.get(sessionId);
      row.spans += pair.spans;
      if (pair.minTime && (row.startedAt === null || pair.minTime < row.startedAt)) row.startedAt = pair.minTime;
      if (pair.maxTime && (row.updatedAt === null || pair.maxTime > row.updatedAt)) row.updatedAt = pair.maxTime;
      row.files.add(filePath);
      // Union across files, not a sum: one trace can straddle a rotation, and it
      // is visible if any of its spans is. Used to decide whether a session has
      // anything worth showing, and to find which session owns a trace id.
      for (const traceId of pair.visibleTraceIds) row.visibleTraceIds.add(traceId);
      for (const field of Object.keys(row.counts)) mergeCounts(row.counts[field], pair[field]);
    }
  }

  return { identity, toolCallIds, sessions };
}

function mergeNested(target, source) {
  for (const [outer, inner] of Object.entries(source || {})) {
    if (!target[outer]) target[outer] = {};
    mergeCounts(target[outer], inner);
  }
}

module.exports = {
  SCHEMA,
  INVISIBLE_SPAN_NAMES,
  indexRecords,
  sidecarPath,
  fileIndex,
  indexFor,
  electIdentity,
  resolvePairSessionId,
  preferredValue,
  isUuidLike,
  mergedIndex
};
