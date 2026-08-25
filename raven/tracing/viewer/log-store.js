const fs = require('fs');
const os = require('os');
const path = require('path');

const DEFAULT_MAX_BYTES = 50 * 1024 * 1024;

const KIND_FILES = {
  events: 'audit-events.log',
  spans: 'audit-spans.log'
};

function getStateDir() {
  // Generic across frameworks: TRACING_STATE_DIR wins (set from --state-dir
  // by the entry scripts), then the legacy OpenClaw var, then ~/.openclaw.
  return (
    process.env.TRACING_STATE_DIR ||
    process.env.OPENCLAW_STATE_DIR ||
    path.join(os.homedir(), '.openclaw')
  );
}

function getLogsDir() {
  return path.join(getStateDir(), 'logs');
}

function ensureDir(dir) {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function getActiveLogPath(kind) {
  return path.join(ensureDir(getLogsDir()), KIND_FILES[kind]);
}

function getArchiveDir() {
  return ensureDir(path.join(getLogsDir(), 'archive'));
}

function getDateKey(date = new Date()) {
  return date.toISOString().slice(0, 10);
}

function getMaxBytes() {
  const raw = Number(process.env.TRACE_LOG_MAX_BYTES || DEFAULT_MAX_BYTES);
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_MAX_BYTES;
}

function toJsonText(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function statOrNull(filePath) {
  try {
    return fs.statSync(filePath);
  } catch {
    return null;
  }
}

function readJsonlFile(filePath) {
  if (!fs.existsSync(filePath)) return [];
  return fs
    .readFileSync(filePath, 'utf8')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null;
      }
    })
    .filter(Boolean);
}

function walkFiles(rootDir) {
  const result = [];
  if (!fs.existsSync(rootDir)) return result;
  const stack = [rootDir];
  while (stack.length) {
    const dir = stack.pop();
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        stack.push(full);
        continue;
      }
      result.push(full);
    }
  }
  return result;
}

function isLogName(name, baseName) {
  if (name === baseName) return true;
  if (!name.startsWith(baseName.replace('.log', ''))) return false;
  return name.endsWith('.log');
}

function shallowLogFiles(dir, baseName) {
  const result = [];
  let entries = [];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return result;
  }
  for (const entry of entries) {
    if (entry.isDirectory()) continue;
    if (!isLogName(entry.name, baseName)) continue;
    result.push(path.join(dir, entry.name));
  }
  return result;
}

// A log is either the active file directly in <logs> or a rotated one under
// <logs>/archive/<date>/, since that subtree is the only place either writer
// renames one to -- `rotateIfNeeded` here, `TraceStore._rotate_if_needed` on the
// Python side. Recursing all of <logs> instead descends audit-artifacts/, which
// holds one file per captured payload and reaches hundreds of thousands of
// entries, to find a few dozen logs.
function statLogFiles(kind) {
  const logsDir = getLogsDir();
  const baseName = KIND_FILES[kind];
  const candidates = [
    ...shallowLogFiles(logsDir, baseName),
    ...walkFiles(path.join(logsDir, 'archive')).filter((filePath) => isLogName(path.basename(filePath), baseName))
  ];
  const stamped = [];
  for (const filePath of candidates) {
    const stat = statOrNull(filePath);
    if (!stat || !stat.isFile()) continue;
    stamped.push({ path: filePath, size: stat.size, mtimeMs: stat.mtimeMs });
  }
  stamped.sort((a, b) => {
    if (a.mtimeMs !== b.mtimeMs) return a.mtimeMs - b.mtimeMs;
    return a.path.localeCompare(b.path);
  });
  return stamped;
}

function listLogFiles(kind) {
  return statLogFiles(kind).map((entry) => entry.path);
}

function readJsonl(kind) {
  const records = [];
  for (const filePath of listLogFiles(kind)) {
    records.push(...readJsonlFile(filePath));
  }
  return records;
}

function rotateIfNeeded(kind, nextText = '') {
  const filePath = getActiveLogPath(kind);
  if (!fs.existsSync(filePath)) return filePath;

  const stat = fs.statSync(filePath);
  const currentDateKey = getDateKey(new Date(stat.mtimeMs));
  const todayKey = getDateKey(new Date());
  const maxBytes = getMaxBytes();
  const nextBytes = Buffer.byteLength(String(nextText), 'utf8');
  const shouldRotateByDate = currentDateKey !== todayKey;
  const shouldRotateBySize = stat.size + nextBytes > maxBytes;
  if (!shouldRotateByDate && !shouldRotateBySize) return filePath;

  const archiveDayDir = ensureDir(path.join(getArchiveDir(), currentDateKey));
  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const rotatedPath = path.join(archiveDayDir, `${KIND_FILES[kind].replace('.log', '')}-${currentDateKey}-${suffix}.log`);
  fs.renameSync(filePath, rotatedPath);
  return filePath;
}

function appendJsonl(kind, record) {
  const text = `${toJsonText(record)}\n`;
  const filePath = rotateIfNeeded(kind, text);
  fs.appendFileSync(filePath, text, { encoding: 'utf8' });
  return filePath;
}

module.exports = {
  getStateDir,
  getLogsDir,
  getActiveLogPath,
  getArchiveDir,
  ensureDir,
  readJsonl,
  statLogFiles,
  appendJsonl,
  rotateIfNeeded,
  getDateKey
};
