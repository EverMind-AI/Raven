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

function listLogFiles(kind) {
  const rootDir = getLogsDir();
  const baseName = KIND_FILES[kind];
  const files = walkFiles(rootDir).filter((filePath) => {
    const name = path.basename(filePath);
    if (name === baseName) return true;
    if (!name.startsWith(baseName.replace('.log', ''))) return false;
    return name.endsWith('.log');
  });
  files.sort((a, b) => {
    const statA = fs.existsSync(a) ? fs.statSync(a) : null;
    const statB = fs.existsSync(b) ? fs.statSync(b) : null;
    const timeA = statA ? statA.mtimeMs : 0;
    const timeB = statB ? statB.mtimeMs : 0;
    if (timeA !== timeB) return timeA - timeB;
    return a.localeCompare(b);
  });
  return files;
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
  appendJsonl,
  rotateIfNeeded,
  getDateKey
};
