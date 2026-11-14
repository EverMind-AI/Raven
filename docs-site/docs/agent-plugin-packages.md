# Agent Plugin Packages

Raven reads the portable package format of
Agent Plugins 1.0: a directory
whose root holds a `plugin.json`, with optional `skills/` and `mcp.json`
components. Several ecosystems publish packages in this format -- for
example the `agentic-awesome-skills` repository ships thirty of them.

This is **not** the same thing as a Raven plugin. A Raven plugin
(`raven-plugin.toml`) ships Python factories that run inside the host
process; an Agent Plugin Package is data only: skills, and MCP servers in a
later phase. Nothing from a package is ever imported as code.

## What a package looks like

```text
my-package/
├── plugin.json          # required, at the package root
└── skills/
    └── my-skill/
        ├── SKILL.md     # required in every skill directory
        ├── scripts/     # optional
        └── references/  # optional
```

`plugin.json` is validated against the official Agent Plugins manifest
schema. A package that names an Agent Plugins version Raven does not
implement is refused, and so is a manifest that breaks the schema; an
unknown field is reported and ignored.

## Pointing Raven at a package

```json
{
  "agentPlugins": {
    "packages": ["/opt/agent-plugins"]
  }
}
```

Each entry is either a package root (a directory holding `plugin.json`) or
a directory whose immediate children are package roots. Relative paths
resolve against the working directory. `agentPlugins.disabled` names
packages to leave out.

## The commands

```bash
raven agent-plugins list                 # the packages Raven can see
raven agent-plugins inspect /opt/my-package   # validate one, import nothing
raven agent-plugins import /opt/my-package    # import its skills
raven agent-plugins check                 # report a copy that went stale
raven agent-plugins remove my-package     # delete the imported skills
```

Every command takes `--json` for scripting.

`remove` exits 0 when it finishes without a deletion failure, even if it
kept an unverified entry: the package directory and its ledger record are
still there. Read the `kept` list (`kept_details` with `--json`), not the
exit code, to know whether the package is gone.

## What an import does

The package's skills are copied into the workspace skill tree, under
`<workspace>/skills/<package name>/`. One directory per package means the
skill pool mints one source label per package rather than one per skill, so
two packages may each ship a skill called `postgres-best-practices` without
either shadowing the other.

The copy is recorded in the package ledger (`<home>/agent-plugins.json`),
one record per workspace: two workspaces sharing a home keep separate
imports of the same package, and removing from one never touches the
other. The package root and every skill root carry a `.raven-install.json`
marker whose random token is recorded with it, and that record is what
makes `remove` safe: only a directory whose marker still matches the
recorded token is ever deleted. `check` compares the ledger's fingerprint
against the package on disk, so a package that has been updated since the
import is reported as stale, and also reports whether the installed copy
is still the one that was imported.

The imported skills are ordinary workspace skills afterwards: the file
watcher picks them up, the router can retrieve them, and their scripts run
through the ordinary `exec` tool under the ordinary workspace fence.

## When Raven refuses to touch a directory

A directory that exists where an import wants to write, but that the
ledger cannot prove Raven installed there, is never written into or
deleted. That includes:

- **A directory you put there yourself.** The import refuses the whole
  package and says so.
- **A replacement at the install path.** Move the installed directory
  away and put your own at the same path: the record no longer matches
  the marker, so `import` and `remove` both refuse. Move your directory
  aside and import again to reinstall.
- **Another workspace's import.** The record belongs to the workspace it
  was imported into; a second workspace's `import` and `remove` never
  reach it, even though the home (and so the ledger file) is shared.
- **A skill directory you replaced inside an installed package.** It is
  kept as it is; the import reports it as skipped or released, and
  `remove` reports it as kept. The package directory and its ledger record
  stay, so move the kept entries away and run `remove` again.
- **A symlink or junction** standing at the install path. Raven does not
  follow it, whatever sits behind it.

There is no flag that forces any of this: if a directory must go, move it
yourself and import again.

Ledger files written before the ledger recorded install identities
(version 1) still load. Their records are kept and shown by `check` as
unverified, but they can no longer match anything on disk: `remove` on
such a record deletes nothing and says so. Importing the package again
installs a fresh, verified copy once the directory is out of the way, and
supersedes the old record. A version 1 record carries no workspace, so a
fresh import of the same name in any workspace sharing the home replaces it.

If the ledger file itself cannot be read -- a version this Raven does not
know, or data that is not valid -- `import` and `remove` refuse to run
rather than risk overwriting it, and the file is left exactly as it was.
An import whose ledger could not be saved leaves its directories on disk
unrecorded; a re-import will refuse to touch them until you move them
away. A `remove` that fails halfway keeps both the ledger record and the
install markers that prove it, so once the underlying failure clears,
running the same command again simply finishes the job.

Do not open the same home with an older Raven once a version 2 ledger has
been written. An older build cannot read the new file: it sees no records,
and its next import writes a version 1 file over it, dropping every
workspace's records. To roll back, restore the ledger and the installed
directories together.

## What a package cannot do

- **It cannot inject itself into every turn.** The `always` flag is dropped
  from the imported copy. A package's skills stay retrievable, which is
  where the router puts them when they are relevant.
- **It cannot run outside the operator's blocklist.** A skill whose name is
  on `skillForge.blocklist` is not imported, and a skill whose body points
  at another product's home directory (`~/.openclaw` and friends) is
  refused.
- **It cannot execute code on import.** Raven copies files; the scripts
  inside a skill run only when the model decides to run them, through
  `exec`.
- **It cannot overwrite your own skills.** A target directory that exists
  and is not provably the ledger's -- not this workspace's record, or not
  the copy its install marker identifies -- is left alone, and the import
  says so.

## MCP servers a package ships

A package may carry an `mcp.json` naming MCP servers. Raven reads it,
validates every entry and reports what it found; it starts nothing until
you name the package:

```json
{
  "agentPlugins": {
    "packages": ["/opt/agent-plugins"],
    "mcpPackages": ["my-package"]
  }
}
```

That opt-in is per package because a server entry is a process on your
machine. The servers a package contributes are named
`<package>__<server>`, so they never collide with your own
`tools.mcpServers` entries.

A package's servers run with the package root as the working directory
(unless the entry names another one), and with `PLUGIN_ROOT` and
`PLUGIN_DATA` in their environment: the first is the package directory,
the second is a directory Raven keeps for that package across updates
(`<home>/plugin-data/<package>/`). The format's two placeholders,
`${PLUGIN_ROOT}` and `${PLUGIN_DATA}`, are expanded in a server's
arguments, environment and working directory.

After expansion, an argument or environment value that carries a `..` path
component is refused and reported. The check splits on `/` and `\`, so a URL
with a `..` segment, such as `https://example.com/a/../b`, is refused too.

One bad server entry does not disable the package: the entry is skipped
and reported, and the rest still run. A package whose `mcp.json` declares
a different Agent Plugins version than its `plugin.json`, or whose file is
not a valid MCP configuration, has MCP disabled while its skills keep
working.

## Current limits

- Raven does not download packages. A package is a directory you already
  have; there is no registry client and no update channel.
- The host-native formats of other tools (`.claude-plugin/`, `.codex-plugin/`)
  are not read yet.
- A package's stdio server runs in its package root, which has to be
  inside a volume mounted into the sandbox. A server whose directory is
  outside every mounted volume is refused rather than launched in the
  wrong place; mount that directory or run without the sandbox.
