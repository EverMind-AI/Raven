# Raven-Oncall

The on-call (值守) line of Raven, installed as a third-party subagent of the host
Raven on this box.

- **Source:** `gitlab.com/npc-work/aic/ai/raven`, branch `feat/ops_round3_device`
- **Pinned at:** `cb433e11` (2026-08-13, *feat(ops): deliver the finishing report, best-effort and after the close*)
- **Installed:** 2026-08-13
- **Model:** `anthropic/claude-opus-5` via OpenRouter (`providers.custom`)

It is the same Raven codebase as this repo's `main`, plus `raven/ops/` — the
machinery for handing over a long-running remote job and having the agent watch
it: submit a round, schedule its own next look, wake up, read its own ledger,
then continue / resubmit / kill / report. The branch's own account of the line,
including what it has *not* been observed to do, is
[`Raven-Oncall/docs/oncall-quickstart.zh-CN.md`](Raven-Oncall/docs/oncall-quickstart.zh-CN.md).
Read that before trusting a run.

## Layout

```
subagents/raven-oncall/          what ships
├── Raven-Oncall/        the checkout, with its own .venv (uv sync, Python 3.13)
├── config.json          this install's runtime config (no secrets - they live in .env)
├── run.py               host-side launcher; what the gateway actually invokes
└── subagent.json        the entry registered with the host Raven
```

Everything the install writes lands under the state root instead, because the
runtime derives its data directory from the config file's own parent and `run.py`
renders the config there. `ONCALL_STATE_ROOT` moves it; the default is:

```
~/.raven/workspace/subagent_sessions/raven-oncall/
├── ops/<campaign>/      per-campaign trail: ledger, events, facts, decisions, reports
├── cron/jobs.json       this install's wake schedule (+ wake_shell.jsonl, the fire log)
├── workspace/           agent workspace; sessions live in workspace/sessions/cli/
├── runs/<session>/      per-conversation launcher log
├── logs/                runtime logs, incl. wake_shell.log
└── traces/              tracing, kept off the host raven's
```

Everything the runtime writes is derived from `config.json`'s parent directory
(`config.paths.get_data_dir`), so pointing `--config` here is what keeps this
install off the host Raven's `~/.raven`. Two things do **not** follow `--config`
and are set explicitly by `run.py`: the workspace (set in `config.json`) and
`RAVEN_TRACING_DIR`.

## How it is wired

`subagent.json` registers a stateful CLI subagent:

```
/usr/bin/python3 .../raven-oncall/run.py --prompt-file {prompt_file} --session {agent_id}
```

One spawn is one turn. Reusing the same instance handle continues the
conversation (`--session cli:<id>` → `workspace/sessions/cli/<id>.jsonl`).

### What the launcher adds, and why

The on-call line needs three things from its host that a bare `raven agent -m`
does not provide:

1. **Something resident to fire the wakes.** `ops_submit` schedules the next look
   as a cron job on channel `cli`, and `raven agent -m` exits as soon as it
   replies — so nothing would fire it, and the campaign would stall silently
   after the first turn. The branch ships the missing piece as
   `raven.ops.wake_shell`; `run.py` keeps exactly one alive per install, started
   under an `flock` so two concurrent spawns cannot start two claimers on one
   store (two claimers is how a live run gets turns nobody asked for). It is
   started with `--fire-missed`, so a wake that came due while no shell was up is
   still looked at rather than dropped.
2. **The right raven for a woken turn.** `wake_shell` resolves the child turn via
   `shutil.which("raven")`. With an unmodified `PATH` that is the *host's* raven —
   a build with no `ops_*` tools at all — running against this install's config,
   and the failure is silent: the turn starts, finds no ops tool, and improvises.
   The shell is therefore started with `Raven-Oncall/.venv/bin` first on `PATH`.
3. **A verdict on whether there is an answer.** Exit 0 does not mean the agent
   produced anything, so the launcher reads the reply back out of the transcript
   and fails loudly when the turn committed none.

The reply also carries a short `--- watch state` footer the agent cannot write
itself: whether anything is actually scheduled to come back, which process would
fire it, and what each campaign's own trail says (rounds in the ledger, whether a
report was filed, whether it asked the owner something). That last one matters —
see the limits below.

## Using it

Spawn `Raven-Oncall` with the job in prose. Two things must be in the prompt,
because the product cannot discover either:

- **the campaign's identity** — a campaign name, its ledger path, and the host.
  No tool lists existing campaigns, so an omitted name silently starts a new one.
- **the terms** — the compute budget, the value that counts as beating the
  baseline, and how much authority it has (may it kill a bad run, may it change
  the config).

And **the campaign's `meta.json` must already exist**, because every field
missing from it is a silent default rather than an error (no `backend` → docker;
no `budget_minutes_total` → the compute budget is not enforced at all; no
`interruption_contract` → interruptions are never refused), while the job still
runs to the end of its allowance:

```bash
OPS=~/.raven/workspace/subagent_sessions/raven-oncall/ops   # $ONCALL_STATE_ROOT/ops
mkdir -p "$OPS"/<campaign>
cat > "$OPS"/<campaign>/meta.json <<'EOF'
{
  "backend": "process",
  "host": "<ip>",
  "port": 58717,
  "key": "~/.ssh/id_rsa",
  "remote_dir": "/remote/workdir",
  "command": "python3 /remote/train.py --config {config} --run-dir {job_dir}",
  "budget_minutes_total": 90,
  "interruption_contract": { "min_expected_loss_ms": 1800000 },
  "reference_values": { "ndcg": 0.3674 },
  "expected_baseline": { "ndcg": 0.3674 }
}
EOF
```

`{config}` and `{job_dir}` are placeholders the harness substitutes per trial.

By hand, without the gateway:

```bash
cd subagents/raven-oncall
/usr/bin/python3 run.py --verbose --session my-run --task "..."     # one turn
/usr/bin/python3 run.py --verbose --session my-run --prompt-file task.md
```

`--no-wake-shell` skips the resident shell (use it only when something else — a
TUI on this same config — is polling the store).

## Where to look

```bash
cd ~/.raven/workspace/subagent_sessions/raven-oncall   # $ONCALL_STATE_ROOT
cat ops/<campaign>/events.jsonl      # submitted, wake scheduled, woke, report accepted/refused
cat ops/<campaign>/ledger.json       # each trial's state and result
cat ops/<campaign>/state_facts.json  # what the tools observed (the model cannot write this)
cat ops/<campaign>/decisions.json    # the readings it cited as basis
cat ops/<campaign>/reports.jsonl     # only appears once a report is ACCEPTED
cat cron/wake_shell.jsonl            # every wake the shell fired, with the child's argv
tail logs/wake_shell.log             # the resident shell's own log
cat runs/<session>/launcher.log      # one turn's launcher diagnostics
```

A refused report is not written and does not consume its dedupe key, so the
agent can fix it and resend — **the final report will read as if it got it right
the first time; the truth is in the refusal count in `events.jsonl`.**

The wake shell is started on demand by `run.py`. To stop it:

```bash
kill "$(python3 -c 'import json;print(json.load(open("wake_shell.pid"))["pid"])')"
```

## Limits worth knowing before a real run

From the branch's own doc (`docs/oncall-quickstart.zh-CN.md` §5), measured, not
guessed:

- **It has not yet been observed to intervene on a job that is going badly.** It
  checks, it reads the number, it says "below baseline" — and then schedules the
  next look too far out to act. Keeping time and reporting honestly are the two
  cells that pass.
- It often reaches for `ssh` directly instead of the ops tools; `ExecTool` has no
  host restriction, so that path is always open.
- The per-batch loss series is printed but unreadable — point noise spans ~550x
  while the drift is ~6x. The primary metric's series is readable.
- Killing a job under-counts the compute already spent, bounded by one log
  interval.
- `ReadFileTool` has no directory restriction, so it can read its own evaluation
  apparatus and any earlier round left under the same ops home. **For a
  controlled comparison, use a separate campaign directory.**

Added by *this* hosting:

- **`ops_ask_owner` does not reach a person live.** It records the question on the
  campaign trail and delivers to a `cli` channel with nobody on it. The launcher
  surfaces an unanswered question in the reply footer of the next spawn, which is
  the only way you will see it. Tell it in the prompt what to do when it cannot
  decide alone.
- **A woken turn is a cold start** with no memory of the spawn that started it —
  by design, since picking up from its own ledger is the behaviour under test.
- The wake shell does not survive a reboot; the next spawn restarts it.

## Config choices

`config.json` differs from a stock install in five deliberate ways:

| Setting | Value | Why |
|---|---|---|
| `agents.defaults.workspace` | `<here>/workspace` | Sessions live under the workspace in this build; keeping it here keeps them off the host's. |
| `tools.restrictToWorkspace` | `false` | It has to read the campaign's ops home and an ssh key, both outside the workspace. |
| `tools.disabledTools` | no `spawn` / `cron` / skills / hub / media / deep_research | `cron` is off so the only scheduling path is the ops contract; the rest are unrelated surface. `message` stays enabled because `ops_ask_owner` delivers through it. |
| `memory.backend` | `null` | Cross-round contamination is a measured hazard on this line; a memory that carries yesterday's campaign into today's would be another. Set it to `everos` with an isolated `userId`/`agentId` if you want recall. |
| `agents.defaults.llmCallTimeout` / `tools.exec.maxTimeout` | 1800s / 1200s | Bound a *stalled* backend or command. There is deliberately no wall-clock cap on a turn. |

## Verified on 2026-08-13

| Check | Result |
|---|---|
| `uv sync` in the checkout | own `.venv`, Python 3.13.13, `raven` v0.1.6; the repo's own `.venv` untouched |
| config loads, paths resolve | data dir, cron store, ops home and sessions all under `subagents/raven-oncall/` |
| one turn end to end | 11 `ops_*` tools visible to the agent; reply extracted from the transcript |
| resume | second spawn on the same session read as `turn=resume` and returned only the new answer |
| **wake fires** | a one-shot job injected 45s out fired ~60s later; `cron/wake_shell.jsonl` shows the child argv as `Raven-Oncall/.venv/bin/raven`, i.e. the PATH fix holds and a wake does not silently run the host build |
| traces stay local | `traces/` created here; nothing new under `~/.raven/traces` |
| registered live | `PUT /raven/subagents` → `{"ok":true,"count":8}`; no existing entry lost; `POST /raven/subagents/test` → `ok: true, "the agent ran and replied"` |

Not verified: a real campaign against a real remote host. Nothing here exercises
`ops_submit` end to end — the budget gate, the report gates and the interruption
contract are all untested on this install.

## Updating from upstream

```bash
cd subagents/raven-oncall/Raven-Oncall
git pull --ff-only origin feat/ops_round3_device
uv sync
```

Then re-run the checks above. `run.py` is host-side and not part of the
checkout, so a pull cannot conflict with it — but a pull *can* move
`SessionManager._get_session_path` or the `wake_shell` CLI, which are the two
things `run.py` reaches into.

## Secrets

Every secret is in `.env` (mode 600, never published); `config.json` holds none
and ships as-is. `.env.example` is the template - copy it, fill it, `chmod 600`.

The config loader does **no** environment-variable substitution and reads no key
from the environment, so the key has to be *in the config file* by the time the
CLI loads it. `run.py` merges `.env` into a rendered config at launch and hands
the runtime that.

Two properties fix that file's location and lifetime, and getting either wrong
breaks the watch rather than the run:

- **its parent is the runtime's data directory.** `config.paths.get_data_dir`
  returns the config file's own parent, and there is no separate knob for any of
  the things hanging off it, so rendering under

  ```
  ~/.raven/workspace/subagent_sessions/raven-oncall/
  ```

  is what moves the transcripts, the cron store, the ops home, the agent's own
  workspace and the wake shell's pid file and logs out of this folder. Every
  consumer agrees on it by construction: `cron_store` and `ops_home` derive from
  the same config path. `ONCALL_STATE_ROOT` moves the root;
- **it is a fixed name that is not deleted afterwards**, unlike the other
  launchers here. `ensure_wake_shell` starts a resident process holding that path
  and that process outlives the spawn by design, re-reading the file on every
  wake - so deleting it at the end of a run would strand the whole campaign.

Both `.env` and the rendered config are mode 600, and the rendered one now lives
outside any published tree entirely.

## Publishing

`.gitignore` is the manifest: everything it lists stays out of a published tree.
Three entries are worth stating outright rather than leaving to a pattern match.

**The checkout ships, but neither its `.venv` nor its `.git`.** `Raven-Oncall/`
is published as source. Two things inside it are not:

- `.venv/`, ~700 MB of third-party wheels whose console scripts carry this
  machine's absolute paths baked into their shebangs - a copied one fails
  outright where `uv sync` rebuilds it correctly;
- `.git/`, which is a **full clone of the private internal GitLab repo**. It
  carries the remote URL, every internal branch and commit, and the personal
  email address of every contributor. The working tree is what is being
  published; its history is a separate decision that has not been made. Deleting
  the directory before packaging is not enough if the archive is built from
  anything that follows it - check the manifest, not the intent.

The tree also sits on an internal feature branch rather than a release, so what
ships is a snapshot of work in progress; the README says as much, and the
subagent description says so to callers.

**`ops/`, `runs/`, `traces/` and `workspace/` are work data, not samples.**
`traces/logs/audit-artifacts/` holds the literal LLM inputs and outputs of real
campaigns, and `ops/<campaign>/meta.json` holds remote hosts, ssh ports and key
paths. Nothing in them is safe to ship on the grounds of being small.

**`.env` and `.config.rendered.json` are the only files that ever hold a key.**
If either reaches a published tree, treat the keys as leaked: rotate first, then
work out how it got there. Deleting the commit does not undo it.
