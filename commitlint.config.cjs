const fs = require("fs");
const path = require("path");

const TYPES = [
  "build",
  "chore",
  "ci",
  "docs",
  "feat",
  "fix",
  "perf",
  "refactor",
  "revert",
  "test",
];

// The canonical scope set mirrors README.md's repo-layout table: every
// top-level package under raven/ (plus the home.py module), computed from
// the tree at config-load time so the enum cannot rot behind a refactor.
function ravenPackages() {
  const root = path.join(__dirname, "raven");
  return fs
    .readdirSync(root, { withFileTypes: true })
    .filter(
      (entry) =>
        entry.isDirectory() &&
        fs.existsSync(path.join(root, entry.name, "__init__.py")),
    )
    .map((entry) => entry.name);
}

// A change living wholly in a top-level tree outside raven/ uses that tree
// as its scope (see README.md's repo-layout preamble).
const TOP_LEVEL_TREES = [
  "agents",
  "benchmarks",
  "bridge",
  "docs",
  "evolver",
  "plugins-dist",
  "schemas",
  "scripts",
  "tests",
  "ui-tui",
  "ui-web",
];

const PRODUCTS = ["raven-code", "raven-design", "raven-oncall", "raven-ppt", "raven-research"];

// The plugin distributions beside the host wheel: each is its own package
// with its own version, so a change confined to one scopes by its wheel name.
const WHEEL_DISTRIBUTIONS = ["design-engine", "everos-memory", "ppt-engine"];

// Scopes already used on refactor/raven_v0_2_0 before the enum existed; a PR
// from that branch lints its whole history, so they stay legal until the
// branch merges. Retire this list afterwards.
const LEGACY_SCOPES = [
  "changelog",
  "context",
  "integration",
  "plans",
  "subagent",
  "subagents",
  "ui",
  "web_rpc",
];

const SCOPES = [
  ...new Set([
    ...ravenPackages(),
    "home",
    ...TOP_LEVEL_TREES,
    ...PRODUCTS,
    ...WHEEL_DISTRIBUTIONS,
    ...LEGACY_SCOPES,
    "*",
    "deps",
  ]),
].sort();

module.exports = {
  rules: {
    "header-max-length": [2, "always", 100],
    "scope-enum": [2, "always", SCOPES],
    "subject-case": [2, "never", ["sentence-case", "start-case", "pascal-case", "upper-case"]],
    "subject-empty": [2, "never"],
    "subject-full-stop": [2, "never", "."],
    "type-case": [2, "always", "lower-case"],
    "type-empty": [2, "never"],
    "type-enum": [2, "always", TYPES],
  },
};
