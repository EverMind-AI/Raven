'use strict';

// Reads everos-derived memory (episode / atomic_fact / foresight / agent_case /
// agent_skill) and indexes it so a `memory.store` span can be joined to the family
// of memories that this turn's memcell was distilled into.
//
// Join key: (session_id, timestamp). raven's stored messages carry no ids/timestamps,
// but every md entry header carries session_id + timestamp + parent_id, and the store
// dispatch time matches the memcell/md timestamp to the millisecond. Entries of one
// turn share one parent_id, so we group by parent_id and match the group whose
// timestamp is nearest the store time within a tolerance. See STORE_DEPOSIT_DESIGN.md.

const fs = require('fs');
const os = require('os');
const path = require('path');

// dir basename -> derived memory type
const DIR_TYPE = {
  episodes: 'episode',
  '.atomic_facts': 'atomic_fact',
  '.foresights': 'foresight',
  '.agent_cases': 'agent_case',
  '.agent_skills': 'agent_skill'
};

function resolveEverosRoot(framework) {
  if (process.env.EVEROS_ROOT) return process.env.EVEROS_ROOT;
  const app = String(framework || 'raven').toLowerCase();
  return path.join(os.homedir(), '.everos', app);
}

// Directories that cannot hold a deposit and are expensive to descend: the
// engine's own virtualenv, whose site-packages alone costs more to walk than
// every deposit in the tree, and its index storage including the dated backups
// left beside it.
//
// A denylist rather than a list of known deposit directories, because a deposit
// directory's name is not knowable: the engine writes agent cases to `.cases`
// and skills to `skills/`, neither of which appears in DIR_TYPE, and those
// files reach the index through fileType's basename fallback instead. Excluding
// anything DIR_TYPE does not name drops them.
const SKIP_DIR_NAMES = new Set(['.tmp', '.git', 'node_modules', 'site-packages']);

function isSkippedDir(name) {
  if (SKIP_DIR_NAMES.has(name)) return true;
  if (name.startsWith('.index')) return true;
  return name.endsWith('venv');
}

function walkFiles(root, out = []) {
  let entries;
  try {
    entries = fs.readdirSync(root, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const ent of entries) {
    const full = path.join(root, ent.name);
    if (ent.isDirectory()) {
      if (isSkippedDir(ent.name)) continue;
      walkFiles(full, out);
    } else if (ent.isFile() && ent.name.endsWith('.md')) {
      out.push(full);
    }
  }
  return out;
}

// Identity of the deposit tree as the index sees it: which files matter, and
// their size and mtime. Cheap enough to run per request now that the walk skips
// the venv, so a caller can tell a rebuilt index from a reusable one without
// reading a single deposit.
function depositsFingerprint(everosRoot) {
  const parts = [];
  for (const file of walkFiles(everosRoot)) {
    if (!fileType(file)) continue;
    let stat;
    try {
      stat = fs.statSync(file);
    } catch {
      continue;
    }
    parts.push(`${file}:${stat.size}:${stat.mtimeMs}`);
  }
  parts.sort();
  return parts.join('|');
}

function fileType(filePath) {
  const dir = path.basename(path.dirname(filePath));
  if (DIR_TYPE[dir]) return DIR_TYPE[dir];
  const base = path.basename(filePath);
  for (const [d, t] of Object.entries(DIR_TYPE)) {
    if (base.startsWith(t)) return t;
  }
  return null;
}

// Parse `<!-- entry:ID -->...<!-- /entry:ID -->` blocks into structured entries.
function parseEntries(text, type) {
  const entries = [];
  const re = /<!--\s*entry:([^\s]+)\s*-->([\s\S]*?)<!--\s*\/entry:\1\s*-->/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    const id = m[1];
    const body = m[2];
    const header = {};
    const hre = /^\*\*([^*]+)\*\*:\s*(.+)$/gm;
    let h;
    while ((h = hre.exec(body)) !== null) header[h[1].trim()] = h[2].trim();
    const sections = {};
    const sre = /^###\s+(.+?)\s*\n([\s\S]*?)(?=\n###\s|\n<!--|\s*$)/gm;
    let s;
    while ((s = sre.exec(body)) !== null) sections[s[1].trim()] = s[2].trim();
    const ts = header.timestamp ? Date.parse(header.timestamp) : NaN;
    entries.push({
      id,
      type,
      sessionId: header.session_id || null,
      parentId: header.parent_id || null,
      timestampMs: Number.isNaN(ts) ? null : ts,
      timestamp: header.timestamp || null,
      subject: sections.Subject || null,
      text:
        sections.Summary ||
        sections.Fact ||
        sections.Foresight ||
        sections.Content ||
        sections.Subject ||
        null,
      startTime: header.start_time || null,
      endTime: header.end_time || null
    });
  }
  return entries;
}

// Build: Map<sessionId, Array<familyGroup>> where a familyGroup is all entries
// sharing one parent_id (one turn's memcell) grouped by type.
let depositIndexCache = null;

function buildDepositIndex(everosRoot) {
  const fingerprint = depositsFingerprint(everosRoot);
  if (depositIndexCache && depositIndexCache.root === everosRoot && depositIndexCache.fingerprint === fingerprint) {
    return depositIndexCache.index;
  }
  const index = buildDepositIndexUncached(everosRoot);
  depositIndexCache = { root: everosRoot, fingerprint, index };
  return index;
}

function buildDepositIndexUncached(everosRoot) {
  const files = walkFiles(everosRoot);
  const byParent = new Map();
  for (const file of files) {
    const type = fileType(file);
    if (!type) continue;
    let text;
    try {
      text = fs.readFileSync(file, 'utf8');
    } catch {
      continue;
    }
    for (const entry of parseEntries(text, type)) {
      if (!entry.parentId) continue;
      if (!byParent.has(entry.parentId)) {
        byParent.set(entry.parentId, {
          parentId: entry.parentId,
          sessionId: entry.sessionId,
          timestampMs: entry.timestampMs,
          types: {}
        });
      }
      const group = byParent.get(entry.parentId);
      if (group.timestampMs == null && entry.timestampMs != null) group.timestampMs = entry.timestampMs;
      if (!group.sessionId && entry.sessionId) group.sessionId = entry.sessionId;
      if (!group.types[type]) group.types[type] = [];
      group.types[type].push(entry);
    }
  }
  const bySession = new Map();
  for (const group of byParent.values()) {
    const sid = group.sessionId || '__unknown__';
    if (!bySession.has(sid)) bySession.set(sid, []);
    bySession.get(sid).push(group);
  }
  return bySession;
}

// Resolve the deposit family for one store span.
// Returns { parentId, timestamp, counts, types } or null (not yet distilled).
function resolveDeposit(index, sessionId, storeTimeMs, toleranceMs = 20000) {
  if (!sessionId || storeTimeMs == null) return null;
  const groups = index.get(sessionId);
  if (!groups || !groups.length) return null;
  let best = null;
  let bestDelta = Infinity;
  for (const g of groups) {
    if (g.timestampMs == null) continue;
    const delta = Math.abs(g.timestampMs - storeTimeMs);
    if (delta < bestDelta) {
      bestDelta = delta;
      best = g;
    }
  }
  if (!best || bestDelta > toleranceMs) return null;
  const counts = {};
  for (const [t, arr] of Object.entries(best.types)) counts[t] = arr.length;
  return { parentId: best.parentId, timestamp: best.timestampMs, deltaMs: bestDelta, counts, types: best.types };
}

function summarize(deposit) {
  if (!deposit) return null;
  const label = { episode: 'episode', atomic_fact: 'fact', foresight: 'foresight', agent_case: 'case', agent_skill: 'skill' };
  const parts = [];
  for (const [t, n] of Object.entries(deposit.counts)) {
    const name = label[t] || t;
    parts.push(`${n} ${name}${n === 1 ? '' : 's'}`);
  }
  return parts.join(' · ');
}

module.exports = {
  resolveEverosRoot,
  buildDepositIndex,
  depositsFingerprint,
  resolveDeposit,
  summarize,
  parseEntries
};
