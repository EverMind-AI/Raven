# raven-ppt

A **deck builder**: it reads the source documents you name, drafts an outline,
writes each slide against measured text budgets, renders every page, reviews the
rendered pages, and publishes a `.pptx` only once the deck passes its own fact
and visual gates.

This folder is our caller-side record. The agent itself is the checkout inside it.

## Where it comes from

| | |
|---|---|
| Source | `github.com/daoxize1/Raven`, branch `refactor/ppt_on_upstream`, commit `284cf4c` |
| Fetched from | `/Evermind/sh_evermind/chenhongda/repo/fork-raven-ppt-refactor` (the owner's checkout) |
| Local checkout | `./Raven-PPT` - the agent itself lives inside this folder |
| Package / version | `raven` with the `ppt` extra; the ppt capability is `raven/ppt/` |
| Owner | chenhongda. **The branch moves**: its tip advanced twice during the 2026-08-20 update, so a pin is the only reproducible reference |

`refactor/ppt_on_upstream` is not a continuation of the earlier `feat/raven_ppt`
line - the two diverge at `0640a25` (2026-07-29). Anything written about the old
line, including the container recipe below, does not describe this one.

## It no longer runs in a container

Until 2026-08-20 this agent ran as a 1.6 GB docker image
(`localhost:5000/agent-eval/raven-ppt:latest`), launched through the docker SDK.
The image existed for exactly one reason, stated in its own Dockerfile: *"Built
for the sub-agent use case where the host has none of the rendering stack
installed."*

**That premise stopped being true.** This host has `soffice` (LibreOffice
7.3.7.2), `python3.12`, the DejaVu/Liberation/Droid families the image installed,
and 35 CJK-capable faces. So the checkout runs directly, the same way
raven-code, raven-oncall and raven-research do, and the image, the registry
front-end, the exported tarball and the docker SDK all drop out of the path.

What that removed, and what it cost:

| Gone | Consequence |
|---|---|
| `raven-ppt-image.tar`, 1.6 GB of `docker save` output | Nothing: the image is still in the local daemon at the digest the old README pinned (`sha256:cfb8f299...`), so a rollback needs no tarball |
| The docker SDK, and an interpreter that had it | **The launcher is now stdlib-only.** It used to require a specific venv that happened to carry the SDK, named by absolute path in the registered command; any `python3` runs it now |
| Bind mounts, and the DinD workarounds around them | The two host quirks the old launcher was built to survive - a hijacked docker CLI stdout, and a daemon resolving mount sources against `/ebs/rootfs` - stop applying |
| `RAVEN_PPT_IMAGE=1`, the in-image marker | Nothing here reads it |
| The `containers/render-worker` image | Never on this path; every render runs in-process, through the `soffice` on this host |

## Layout

```
subagents/raven-ppt/         what ships
|- Raven-PPT/         the checkout, with its own .venv (uv sync --extra ppt)
|- config.json        this install's runtime config (no secrets - they live in .env)
|- .env.example       the secrets template; copy to .env, which never ships
|- .gitignore         what stays out of the repo: .env, install backups, run state
|- README.md          this file
|- install.py         registers the entry in the host raven's config
|- run.py             host-side launcher; what the gateway actually invokes
`- subagent.json      the entry registered with the host Raven
```

Everything the runtime writes lands under the state root instead, because
`run.py` renders the config there and raven derives its data directory from the
config file's own parent. `PPT_STATE_ROOT` moves it; the default is:

```
~/.raven/workspace/subagent_sessions/raven-ppt/
|- .config.rendered.json    config + the key, mode 600, outside any published tree
`- jobs/<job>/              one directory per run
   |- materials/            the named source files, copied in
   |- deck/                 the agent's own working directories
   |- out/                  where the finished deck is published
   `- launcher.log          that run's diagnostics
```

### One directory per job, and nothing reclaims them

`jobs/<job>/` is a whole raven workspace, not just a scratch area: the staged
materials, the deck project, the rendered previews, the session transcript and
the memory lanes all live in it. Measured 2026-08-20, a five-page deck leaves
**1.4 MB**, of which 1.1 MB is `deck/` - the build script, the staged candidate
and the review renders.

**There is no TTL, no count cap and no reclaim path**, and that is a deferral
rather than an oversight: a job directory is the only record of how a deck was
built, and the deck itself is delivered out of it. The docker era had the same
shape under `/Evermind/.../.ppt_jobs`. At 1.4 MB a job, a hundred decks is
140 MB; prune by hand when that matters:

```bash
rm -rf ~/.raven/workspace/subagent_sessions/raven-ppt/jobs/<job>
```

## How it is wired

`subagent.json` registers an ACP subagent, the same way `raven-code` and
`raven-research` do -- one manifest whose own `kind` tells the host's folder scan
which schema to validate it against:

```
{PYTHON} .../raven-ppt/run.py --acp
```

The agent is a protocol peer rather than a process forked per task: one session
holds one deck, material arrives at `session/prompt` rather than at launch, and
the reply is the response plus the `session/update` stream. `run.py` without
`--acp` is still the one-shot CLI path, and both build the same prompt from the
same material -- `tests/ppt/test_prompt_claims.py` asserts they agree, because the
launcher is standard-library-only and outside the checkout, so the text cannot be
shared between them.

`install.py` resolves `{PYTHON}` and `{SUBAGENT_DIR}` at install time. It is the
same agent-agnostic installer the other three subagents use, byte for byte.

### What the launcher adds, and why

1. **Material reaches the agent through the prompt, because there is no other
   channel.** Raven's CLI sub-agent contract substitutes `{prompt}`,
   `{prompt_file}` and `{agent_id}` and nothing else, so a dispatching agent has
   no argv slot for source documents. `run.py` reads absolute paths out of the
   task text, keeps the ones that exist and look like documents, and copies them
   into the job's `materials/`. A task that names nothing resolvable is not
   refused: the run proceeds without material, and the prompt tells the agent
   which tools gather it -- `web_search` (including `kind="images"`),
   `web_fetch` (including `extractMode="images"`), `ppt_fetch` and
   `ppt_generate_image` -- and that fetching into the project is what the
   provenance checks can see. What it still cannot verify is presented as a
   guess. "A 5-page guide to using GitLab" names no file and needs none, and
   was refused before it started. Paths and URLs in the task are not inspected
   beyond that: whatever does not resolve is passed through untouched, and the
   agent reads the task text itself. A file declared in the fenced block still
   cannot be skipped: a copy that fails stops the run.
2. **The copies are what the prompt names.** The workspace is the job directory,
   with `materials/` and `out/` inside it, and the staged listing points at the
   copies rather than the originals. The workspace is not fenced:
   `tools.restrictToWorkspace` is `false`, because the guard behind it reads
   command text rather than what a command does. It refused the slashes inside
   `sed -n '/## .*/p'` and ended that run with the deck unwritten, while a script
   that opens a path it never spells out goes through untouched.
3. **A verdict on whether a deck exists.** An exit code cannot decide this: the
   container used to exit 139 on fully successful runs. Success is a deck *this
   run* wrote that opens as a zip carrying slide parts, picked out by the `MEDIA:`
   line when it names one. That test checks the artifact rather than the process,
   which is why it survived the move off docker unchanged. The run scoping is not
   decoration: `--job` is the conversation's `agent_id`, so every turn shares one
   `out/`, and without it a turn that published nothing would hand back an
   earlier turn's deck and report success.
4. **stdout is the result.** Raven's CLI backend uses a child's whole output as
   the sub-agent's reply, so the agent's own narration goes to `launcher.log`
   and stdout carries only the slide count and the deck path.

## Using it

Spawn `Raven-PPT` with the job in prose, and **state the absolute path of every
source document in the task text** - that is the only way material reaches it.
Material is optional: name none and the deck is authored from the model's
knowledge, with the prompt making it present what it cannot verify as a guess.
Say how many pages you want and the output filename.

By hand:

```bash
cd subagents/raven-ppt
python3 run.py --verbose --job my-deck --task "Build a 16-page deck from /abs/path/report.pdf"
python3 run.py --verbose --job my-deck --prompt-file task.md
```

`--session` makes a run resumable; the launcher passes `cli:<agent_id>` when the
gateway spawns it, so a reused instance handle continues one deck.

## Install

The checkout needs its own venv, with the `ppt` extra:

```bash
cd Raven-PPT
uv sync --extra ppt
.venv/bin/python -c "import PIL, pptx, fitz"     # the extra's three hard imports
soffice --version                                 # the render gate's dependency
```

Then register it:

```bash
python3 install.py --dry-run     # print the resolved entry and stop
python3 install.py
```

`install.py` writes the host raven's config, so a running raven needs a restart
or the RPC hot-apply path before it sees the change.

## Config choices

`config.json` differs from a stock install in the ways below. Most are carried
over from the container's rendered config; `context.curator_model` exists
*because* the container's config does not port cleanly to this branch, and the
memory identity is explained below the table. No count is given deliberately -
the table is the list, and a number beside it goes stale the first time a row
moves:

| Setting | Value | Why |
|---|---|---|
| `tools.restrictToWorkspace` | `false` (stock `false`) | Left where stock leaves it, unlike the other subagents in this folder, which turn it on. The guard behind it reads command text, not what a command does: it refused the slashes in a `sed` expression and ended that run, while a script that opens a path it never spells out goes through. It cost two runs their decks and stopped nothing |
| `tools.ppt` | `enabled`, profile `script_author`, 144 dpi, 2 renders at a time - every value stock | Written out rather than left implicit: the deck route is the only reason this folder exists, and a default that moves upstream would move it silently. Rendering happens in-process through LibreOffice rather than handing off to the `render-worker` image, which is not on this path |
| `tools.exec.denyPatterns` | the 8 built-ins minus `\brm\s+-[rf]{1,2}\b` (stock: all 8) | The key *replaces* the list rather than extending it, so naming seven is how one goes. `rm -rf` on a scratch directory a render check had just made was refused and ended a run; only that pattern is dropped, and dropping it costs nothing, because `_matches_delete_command` still sends any `rm` to `REQUIRE_APPROVAL` -- approvable where a responder exists, refused where none does, which is every headless deck run. The other seven stay because they are what has no second gate behind it: `dd if=`, `mkfs`/`diskpart`, `format`, a redirect into `/dev/sd*`, a fork bomb, and the Windows `del /f` / `rmdir /s`. `[]` would have dropped all of them for a `rm` that stays blocked either way. `extra_deny_patterns` is the additive key, if the list should grow rather than change |
| `tools.disabledTools` | no media generation, no `deep_research` (stock: none disabled) | Unrelated surface for a deck builder |
| `tools.web.search.maxResults` | 10 (stock 5) | A page's figure is chosen against alternatives; five results is one page of them |
| `agents.defaults.maxToolIterations` | 600 (stock 40) | A page-by-page write-render-review loop over a 16-20 page deck spends iterations the way a research run spends fetches; a 20-page deck that reviews its own renders passes 240 before it publishes |
| `agents.defaults.contextWindowTokens` | 1048576 (stock: unset) | The skill, the reference documents and a deck's worth of build script and render review. A model with a smaller window needs this lowered to its own ceiling -- the value is a promise to the runtime, not a request to the provider |
| `agents.defaults.reasoningEffort` | `high` (stock: unset) | Thinking is asked for here rather than left to each backend's default |
| `agents.defaults.maxConcurrentSubagents` | 2 (stock 4) | Half the stock cap. A deck run spawns few sub-agents; what it runs in parallel is LibreOffice, and that is capped by `renderConcurrency` |
| `memory` + `plugins.config.everos-memory` | `raven-ppt` / `raven-ppt`, slice carries `base_url` alone | Keeps this agent's runs out of the host assistant's memory. Omitting the block does not mean "no memory" - see below |
| `context.curator_model` | `""` (empty) | **The one setting that is new, not inherited.** This branch replaced turn-compaction with the Curator context engine, which is on by default and needs no config - but its slow path defaults to `gemini-2.5-flash`, and this install's single provider serves only its own pinned model. Empty is the documented value that follows the agent's model, and it is the only value that cannot send a request nothing here can answer |

No `agents.defaults.workspace`: the launcher passes `--workspace` per job, which
is what keeps two concurrent decks out of each other's files. No
`agents.defaults.maxTokens` either - `AgentDefaults` has no such field, so a
value written there would be read by nothing. And **no `temperature`**:
`anthropic/claude-sonnet-5` does not list it as a supported parameter, and a
value that is silently ignored reads as a determinism pin that is not there.
raven-research drops it for the same reason.

`tools.ppt` is the section that decides whether this folder builds decks at all,
and it is written out rather than inherited: `enabled: true`, `profile:
"script_author"`, `composerModel: ""` (the per-page calls run on the main model),
`renderDpi: 144` and `renderConcurrency: 2`. Every one of those is the upstream
default, so none is a deviation - they are in the file so the values can be read
instead of inferred. The sixth field, `deckName`, is left at `deck.pptx`.

**What did not port from the container.** Its rendered config set
`context.turn_compaction`, which this branch does not have: the whole block is
rejected as `extra_forbidden` and `raven agent` exits 1 before doing any work.
It was written for the pre-refactor line, and the refactor replaced that layer
wholesale. Anything else copied from `containers/raven-ppt/run_ppt.sh` deserves
the same suspicion - it describes a branch this one diverged from at `0640a25`.

**EverOS recall answers 503 here and the run continues.** The backend is
unreachable on this host, logs `state=unresponsive; returning empty` once per
recall, and degrades rather than failing. The container had the same gap, listed
under its own known issues. Nothing this agent does depends on cross-run recall,
so it is noise rather than a defect - but it is noise in `launcher.log`, not in
the reply.

### EverOS memory: on, and filed under this folder's own identity

```json
"memory": {"backend": "everos", "userId": "raven-ppt", "agentId": "raven-ppt", "memoryTopK": 5}
```

**Why the block must be present at all:** `memory.backend` defaults to `everos`
(`config/raven.py:1133`) and `userId` / `agentId` both default to `default` -
which is what the host Raven at `~/.raven/config.json` uses. A config that simply
omits the block, as this one originally did, does not turn memory off. It files
every run into the host assistant's track, and the omission looks identical to a
deliberate choice. raven-code's README records the same default, measured from
the other side.

The plugin slice carries `base_url` alone. The identity is deliberately not
repeated there: this checkout reads it from `memory` and treats a copy in the
slice as obsolete, and two places holding one identity is how stores and recalls
drift onto different ids. The three older folders here do repeat it, because
their checkouts stamp stored messages from the slice instead.

## The LLM it runs on

`config.json` pins the model and `subagent.json` records the same choice as
`recommendedLlm`, so the two can be compared rather than inferred. `.env`
overrides both at launch (`PPT_MODEL`, `PPT_API_BASE`) and supplies the key.

Without a `PPT_API_KEY` the folder runs on the host raven's key instead, as the
other three launchers do and as the setup step promises when it offers "this
raven's LLM" here. `run.py` copies that raven's whole provider block, which
brings its model along with its credentials, so the folder answers on whatever
this raven answers on rather than on the pin above - and the two overrides are
read only on the own-key branch, because applied to an inherited block they would
aim the host's gateway at a model it may not serve. With no key here and none to
inherit, the run exits 1 naming both places one can go.

## Secrets

Every secret is in `.env` (mode 600, never published); `config.json` holds none
and ships as-is. `.env.example` is the template - copy it, fill it, `chmod 600`.

The config loader reads no key from the environment, so the key has to be in the
config file by the time the CLI loads it. `run.py` merges `.env` into
`.config.rendered.json` under the state root, mode 600 - deliberately outside
this folder, so the file that carries the key is not in a published tree.

**Search and page reading are inherited, not configured here.** Two optional
keys reach `web_search` and page fetching: a Serper key at
`tools.web.search.apiKey` and a Jina Reader key at `tools.web.jinaApiKey`. Both
are left blank in `.env`, so the launcher reads the host raven's own values at
launch, and rotating either one there covers all four folders. Setting
`PPT_SERPER_API_KEY` or `PPT_JINA_API_KEY` overrides that for this folder alone.
The keyless `tools.web` block in `config.json` is the slot they land in; the
`maxResults` beside them is a deviation in its own right and is in the table
above.

Neither key is required and neither is fatal, but a missing Serper key is not
free: `ppt_outline` hands back gather errands whose stated method is
`web_search(kind="images")`, and with no key that is the one tool that can only
refuse. The deck still builds; it builds without pictures. `ppt_prepare`'s own
errand asks for a different thing - a sweep of what the material already cites,
by `web_fetch(extractMode="images")` - so that one survives a missing Serper key
and depends on the reader path instead.

"Missing" means all three places, not two. The runtime reads a bare
`SERPER_API_KEY` / `JINA_API_KEY` from its environment when the config carries
none, and the launcher hands the child its own environment, so a host that
exports either one searches and reads without anything reaching `tools.web`.
The launcher's `[run] web:` line reports only what it resolved itself and says
so - it cannot see that third source, and must not claim the tool will refuse.

## File permissions

Checked on 2026-08-20, because dropping the container removed the one boundary
that used to make this interesting. The old launcher forced modes by hand --
`exports.chmod(0o777)`, each material `chmod(0o644)` -- for a container whose
UID did not match the host's and an NFS mount that squashed root. Neither
applies now: the agent runs as the caller, so `umask 022` produces the right
thing on its own and the explicit chmods are gone.

What a real run leaves, verified rather than assumed:

| Path | Mode | Why it is right |
|---|---|---|
| `$PPT_STATE_ROOT/.config.rendered.json` | `600` | The only file here that holds the key |
| `subagents/raven-ppt/.env` | `600` | Same, and `subagents/install.sh` creates it that way from the template |
| `jobs/<job>/`, `materials/`, `out/` | `755` | Traversable, so anything that reads the deck can reach it |
| the deck, the staged material, `launcher.log` | `644` | The deck is delivered to a caller who has to be able to open it |
| `Raven-PPT/.venv/bin/raven` | `755` | The launcher tests for exactly this |

Three things that would have been permission problems and are not:

- **LibreOffice does not touch `~/.config/libreoffice`.** `render/office.py`
  passes `-env:UserInstallation` pointing at a fresh temporary directory per
  render, with `--nolockcheck --norestore`. So there is no shared profile to
  contend over, no stale lock from a killed render, and nothing owned by another
  run. Only `TMPDIR` has to be writable.
- **A file the launcher cannot read stops the run, with a sentence.** It used to
  raise, and an uncaught `OSError` here is worse than it looks: the caller reads
  this process's stderr as the agent's reply, so the traceback would have been
  delivered as the answer. A file from the declared block is the path that needs
  it, since those names skip the prompt scan's `is_file` test.
- **A present-but-unexecutable venv is reported as such.** `subagents/install.sh`
  uses `[ -x ]` and the onboarding step reads executability too; testing only for
  existence here would let the installer call this folder ready while the
  launcher called it missing.

Running as a different user than the one that installed is the case none of this
covers: `.env` at `600` is readable by its owner alone, which is the intended
protection and also the thing that breaks. Re-run `install.py` as the user raven
runs as, rather than loosening the mode.

## A licence note worth reading before publishing

The `ppt` extra depends on **PyMuPDF, which is AGPL-3.0** - the only such
dependency in this tree, and one neither the host repo nor the other three
subagents carry. `Raven-PPT/NOTICES.md` documents it: PyMuPDF is imported for
reading source PDFs, not vendored, not modified, and no PyMuPDF source is
redistributed. Its own note is the part to read - AGPL obligations attach to
distributing the library or offering it over a network, not to importing it, so
an internal deployment carries none while a public service built on this should
read the AGPL or take Artifex's commercial licence.

## Updating from upstream

The source is a git repository on this machine, so the swap comes from its
objects. **Pin the commit**: this branch is actively developed and its tip moved
twice during the last update.

```bash
S=/Evermind/sh_evermind/chenhongda/repo/fork-raven-ppt-refactor
PIN=$(git -C "$S" rev-parse refactor/ppt_on_upstream)

cd subagents/raven-ppt
rm -rf Raven-PPT && mkdir Raven-PPT
git -C "$S" archive "$PIN" | tar -x -C Raven-PPT
(cd Raven-PPT && uv sync --extra ppt)
git add -A .
```

Then check that the staged tree differs from the pin in nothing but the
policy-excluded assets - `.pdf` and `.png` under `subagents/**` are barred by the
repo's own rules, and a *source* file showing as deleted means the extraction
lost something:

```bash
git diff --name-status "$PIN^{tree}" \
    $(git write-tree --prefix=subagents/raven-ppt/Raven-PPT)
```

`run.py` is host-side and not part of the checkout, so a swap cannot conflict
with it - but it reaches into two things the checkout owns, and both are worth
re-checking: the `raven agent` flags it passes (`--config`, `--workspace`,
`--session`, `-m`, `--no-markdown`), and the `MEDIA:` line the agent is asked to
end on.
