'use strict';

const os = require('os');
const path = require('path');

// Map a framework name to its trace state dir (where logs/audit-*.log live).
function frameworkStateDir(name) {
  const home = os.homedir();
  switch (String(name || '').toLowerCase()) {
    case 'openclaw':
      return process.env.OPENCLAW_STATE_DIR || path.join(home, '.openclaw');
    case 'hermes':
      return process.env.HERMES_HOME || path.join(home, '.hermes');
    case 'raven':
      return path.join(home, '.raven', 'traces');
    default:
      return null;
  }
}

// Parse `--state-dir <path>` / `--framework <name>` from argv, set
// TRACING_STATE_DIR, and splice the consumed flags out of process.argv so
// downstream positional parsing (e.g. trace-viewer's traceId) still works.
function applyStateDirArg(argv = process.argv) {
  const out = [];
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--state-dir' && argv[i + 1]) {
      process.env.TRACING_STATE_DIR = argv[i + 1];
      i += 1;
      continue;
    }
    if (arg.startsWith('--state-dir=')) {
      process.env.TRACING_STATE_DIR = arg.slice('--state-dir='.length);
      continue;
    }
    if (arg === '--framework' && argv[i + 1]) {
      const dir = frameworkStateDir(argv[i + 1]);
      if (dir) process.env.TRACING_STATE_DIR = dir;
      i += 1;
      continue;
    }
    if (arg.startsWith('--framework=')) {
      const dir = frameworkStateDir(arg.slice('--framework='.length));
      if (dir) process.env.TRACING_STATE_DIR = dir;
      continue;
    }
    out.push(arg);
  }
  argv.length = 0;
  argv.push(...out);
  return process.env.TRACING_STATE_DIR || null;
}

module.exports = { applyStateDirArg, frameworkStateDir };
