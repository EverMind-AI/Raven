# subagents

One folder here is one third-party subagent: the agent itself, the launcher the
host raven invokes, and the caller-side record of how it is wired. Nothing about
a subagent lives outside its own folder, so installing one is a matter of
building what the folder deliberately does not ship, then naming its absolute
path in the host raven's config.

Read this before installing one; read that folder's own `README.md` before
*calling* it, because the capability boundary, the prompt contract and the
multi-turn semantics are per-agent and only documented there.

## What is in a folder

| | |
|---|---|
| `<Checkout>/` | The agent's own source tree. Ships as source; its `.venv` does **not** |
| `run.py` | Host-side launcher - what the host raven actually spawns. Standard library only |
| `install.py` | Resolves the entry's placeholders and writes it into the host raven's config |
| `subagent.json` | The roster entry, with `{SUBAGENT_DIR}` and `{PYTHON}` left unresolved |
| `config.json` | The agent's run config. Holds **no** secrets, and no absolute path |
| `.env.example` | Template for the secrets and the path knobs |
| `.env` | The real secrets. Mode 600, never committed |
| `.gitignore` | The per-folder exclusion list - the enforceable form of "what ships" |

The three folders differ only in the names:

| Folder | Roster name | Checkout | `.env` prefix |
|---|---|---|---|
| `raven-code/` | `Raven-Code` | `Raven-main/` | `CODE_` |
| `raven-oncall/` | `Raven-Oncall` | `Raven-Oncall/` | `ONCALL_` |
| `raven-research/` | `Raven-Research` | `Raven-X/` | `RESEARCH_` |

## Installing, from a fresh clone

```bash
cd subagents
./install.sh                 # every folder here
./install.sh raven-code      # or only the ones named
```

`install.sh` runs the three steps below for each folder, and refuses to register
an agent whose api key is still blank - so the normal sequence is: run it, fill
in the `.env` files it created, run it again. A second run rebuilds nothing it
does not have to; the one thing it does overwrite is an entry you have since
edited by hand (see below). `--dry-run` reports what each step would do, `--no-sync` skips the
venv build, and `--config PATH` writes a config file other than the host raven's.
It exits non-zero when any folder still needs attention.

**Then restart raven (or the gateway)**, which is the one thing no installer can
do for you: the entry is written to the config file, and a running raven holds
the roster it read at startup. After the restart the agent shows up in the TUI's
`/subagents` and on the web UI's subagents page, and `/subagents test <name>`
dispatches at it for real.

### What the machine still has to provide

Two things sit outside this directory, both measured on a fresh internal GPU
worker on 2026-08-18:

**Egress the provider will actually serve.** Reachability is not availability:
`GET https://openrouter.ai/api/v1/models` answered `200` from that machine while
inference answered `403 This model is not available in your region.`, which the
launcher surfaces as a run that exits 0 with no answer committed. Exporting the
site's `http_proxy` / `https_proxy` fixed it, and the same task then answered in
22s. The agents inherit those variables; nothing here needs configuring.

**EverOS is optional.** `config.json` points `memory.backend` at a local EverOS
(`localhost:18791`). Without it the run logs `everos not found` and
`recall failed ... returning empty`, then completes normally - so a smoke test on
a bare machine needs no memory backend.

### The same three steps by hand

**1. Build the checkout's venv.** It is excluded from git on purpose: `uv sync`
rebuilds it in seconds, and a *copied* one fails outright, because `uv` bakes
absolute-path shebangs into `.venv/bin/`. Each launcher checks for
`<Checkout>/.venv/bin/raven` and tells you to do this when it is missing.

```bash
cd subagents/<folder>/<Checkout> && uv sync     # requires-python >= 3.12
```

**2. Supply the secrets.** `config.json` is published and holds none; the
launcher merges `.env` into a config it renders at launch.

```bash
cd subagents/<folder> && cp .env.example .env && chmod 600 .env
```

`<PREFIX>_API_KEY` is required - the run exits 1 without it. The Serper and Jina
keys are optional, and an *exhausted* Jina key is worse than none at all (402 vs
200). `<PREFIX>_STATE_ROOT` moves everything the agent persists; it defaults to
`~/.raven/workspace/subagent_sessions/<folder>` and never lands in this tree.

**3. Register the entry.**

```bash
python3 install.py --dry-run   # print the resolved entry and the interpreter
python3 install.py             # back up the current list, then register
```

The launcher also runs by hand, which is the shortest way to prove the venv and
the keys are right before involving the host raven at all:

```bash
python3 run.py --task "..." --verbose
```

## What `install.py` does

It resolves `{SUBAGENT_DIR}` and `{PYTHON}` in `subagent.json` **against its own
location**, then writes the resulting entry into `subagents.thirdParty` in the
host raven's config.

The placeholders exist so the published file carries no path from the machine
that built it, and they have to be resolved at install time rather than left to
the runtime: the gateway substitutes only `{agent_id}`, `{prompt}` and
`{prompt_file}`, and it spawns with the *session workspace* as cwd, so neither a
relative command nor the entry's `cwd` field can stand in for an absolute path.
**Moving or renaming a folder therefore means re-running `install.py`** - that is
the whole migration.

The write goes through `raven.config.update_subagents`, which validates the entry
against the schema, replaces the entry sharing this one's name, refuses a list that
would hold duplicates, and replaces the file atomically.
Hand-editing `~/.raven/config.json` gets none of that, and a half-written config
is one raven will not start on. That module is importable only from the host
raven's environment, which is usually not the interpreter running the installer,
so it is reached in a subprocess: the shebang of `raven` on `PATH`, or
`$RAVEN_PYTHON`, or `--raven-python`.

Two consequences worth stating outright:

- **Nothing needs to be running**, and nothing running notices. Restart raven or
  the gateway before expecting the dispatching model to see the agent.
- **The entry is overwritten by name.** An edit made in the web UI afterwards is
  lost on the next install, so read `subagents.thirdParty` back out of the config
  before re-running one you have since tuned by hand. The previous list is saved
  to `subagents-backup-<timestamp>.json` beside the installer, exactly as it sits
  on disk (mode 600 - an `openai` entry among the others carries its own api
  key). Nothing reclaims those: one accumulates per install, they are gitignored,
  and deleting the ones you no longer want to restore from is safe.

## Removing one

There is no CLI for it; go through the same module, and restart raven after:

```bash
# Unquoted on purpose: the shebang may be an `env` line, which is two words.
$(sed -n '1s/^#!//p' "$(command -v raven)") -c \
  'from raven.config.update_subagents import remove_third_party_subagent as rm; print(rm("Raven-Code"))'
```

The folder itself can stay - an unregistered folder is inert.

## Adding a new folder

`.gitignore` excludes `subagents/*` and names each tracked folder, because a
folder here can hold a docker image tar or a private checkout. A new one is
opt-in: whitelist it in the repo's `.gitignore`, and give it its own `.gitignore`
covering the secrets, the rendered config, the runtime state (`runs/`,
`sessions/`, `cache/`, `logs/`, ...) and the vendored `.venv`. Anchor those
runtime entries with a leading slash - unanchored, `raven/ops/` and
`.../schedulers/cron/` inside the vendored tree match too, and get silently
dropped from the commit while still sitting on the machine that made them.

To be installable by anyone else, a new folder needs the file contract above:
placeholders left unresolved in `subagent.json`, no absolute path and no secret
in `config.json`, a `.env.example` that documents every knob, and a launcher that
is standard-library only - the host raven invokes it with a bare interpreter, and
the runtime it drives lives in its own venv, not the caller's.

## What never gets committed

`.env` and any rendered config (they hold live keys - if one reaches a published
tree, rotate first and work out how it got there second), the `subagents-backup-*`
and `config.json.bak-*` records (this machine's absolute paths, plus every other
subagent registered on it), the vendored `.venv/`, and the runtime state
directories. Those last ones are transcripts of real tasks against real
repositories - work data, not sample data, and nothing in them is safe to ship on
the grounds of being small.
