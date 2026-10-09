# EverOS Cloud memory plugin -- acceptance cases

Status: cases; G2 passed 2026-10-08
Date: 2026-10-08
Spec: `docs/specs/2026-10-08-everos-cloud-memory-design.md` (A1-A39, C1-C18, H1-H16)

Every case below names the acceptance item it proves (`T<A>.<n> <- A<A>`), where
it is observed, why that observation cannot be faked by the code under test,
what would turn it red, the evidence that the things its verdict names exist,
and which rings of evidence it leaves behind. "Real host" means the process and
entry a person uses: `raven onboard` in a terminal, `raven serve` with the web
page in a browser, `raven doctor`, `raven agent`, `raven import run`.

What this document proves, and what it does not: every case but A32's runs
real Raven against a fake EverOS Cloud (section 1.2) written from the service's
documentation. Passing all of them proves the host wiring and the plugin's own
logic; it proves nothing about EverOS Cloud itself. The live tests of A32 are
the delivery gate for that half, and until a key exists the delivery states
that no request has reached the real service (spec C14-C18).

## 1. Environment

### 1.1 Shared-resource sweep (before the first case and after any interruption)

Several Claude sessions and the owner's own Raven share this machine. The
sweep looks for what this document's processes actually put on their command
lines -- the ledger path and the ports -- because `RAVEN_HOME` is an
environment variable and never appears in `ps`:

```bash
echo "EVEROS_CLOUD_API_KEY=${EVEROS_CLOUD_API_KEY:-unset}"                      # must print unset outside T4.1
for p in $FAKE_PORT $SERVE_PORT $RPC_PORT; do lsof -nP -iTCP:$p -sTCP:LISTEN; done   # all empty, or pids in run.md
ps -axo pid,ppid,etime,args | grep -E "[_]everos_cloud_fake .*--ledger $SCRATCH/ecm-fake|raven serve --port $SERVE_PORT"
tmux -L ecm list-sessions 2>/dev/null                                                 # none, or only ours
playwright-cli -s=ecm list 2>/dev/null | grep -c 'status: open'                       # 0 or 1
ls -d $SCRATCH/ecm-* 2>/dev/null
```

A non-empty line is a signal to reconcile against `run.md`, not a normal
state. Anything not listed there is somebody else's: leave it and pick other
ports. Cleanup is always by pid recorded in `run.md`, never by pattern.

### 1.2 The fake cloud (spec H16)

`tests/_everos_cloud_fake.py`: one module, two faces. `FakeCloud` is an
`httpx.MockTransport` for the contract cases; `python -m tests._everos_cloud_fake`
serves the same logic over HTTP for the real-host cases. It speaks the four
wire-table routes and, with `--with-health`, the OSS `GET /health` the local
plugin probes (used only by the baseline cases T21.2, T26.2, T26.3).

Every request -- known route or not, 404s included -- is appended by a
catch-all, before routing, to the ledger `requests.jsonl` as
`{ts, method, path, headers, body, status}`. The ledger is the counterparty's
own record: a header or body asserted from it was sent by the host process.

Modes: `ok`, `empty`, `401`, `403`, `429`, `500`, `418`, `hang` (per route,
`--hang-on /api/v2/memory/flush`), switched at run time with
`POST /_fake/mode {"mode": ..., "hang_on": [...], "auth": true|false}` so one server serves a
journey. The control route never hangs, whatever the mode. `auth: false` is the
OSS face for the local-backend cases (T21.2, T26.2, T26.3, T35.1): the local plugin
sends no key and the local server asks for none, so a request without
`Authorization` is admitted; every other case keeps the cloud's 401. In `ok` mode the data carries sentinels nothing in Raven's config
contains: episode subject `ECM-EP-5c1d`, totals 7/1/2/3, error message
`ECM-REFUSED-401` in `401` mode.

Every field the backend sends is either honoured or refused; nothing is read
and ignored (`prove-it-works` section 3):

| Field | Route | Fake's treatment |
|---|---|---|
| `user_id` / `agent_id` | search, get | exactly one required else 422 (`user_id, agent_id` named); selects the user-track rows (episodes, profiles) or the agent-track rows (agent_cases, agent_skills); the other arrays come back empty |
| `query` | search | required; recorded; does not change the scripted rows |
| `top_k` | search | truncates each returned array |
| `include_profile` | search | profiles returned only when true and the owner is a user |
| `method`, `min_score`, `radius`, `enable_llm_rerank`, `with_readable_episode`, `filters` | search | accepted and recorded; `filters.session_id` narrows `get` and `search` rows to that session |
| `memory_type`, `page`, `page_size` | get | `memory_type` must match the owner's track else 422; `page_size` truncates; `total_count` is the scripted total |
| `session_id`, `messages[]` | add | required; each message must carry `sender_id`, `role`, `timestamp` (integer), `content`, else 400 naming the field; body over 300 KB or over 500 messages -> 413 |
| `mode` | add | recorded; `agent` or `chat` only |
| `async_mode`, `app_id`, `project_id` | add | accepted and recorded |
| `session_id` | flush | required; answers `extracted` when the session has unflushed adds, else `no_extraction` |
| any other field | any | 400 naming the field |

Started through the host's background runner with the `prove-it-works`
section 8 template (trap on `EXIT INT TERM`, a flag file, a 7,200 s lifetime
cap), never with a trailing `&` in a foreground command:

```bash
sh -c 'F=$(mktemp); ( cd $SCRATCH && exec python -m tests._everos_cloud_fake --port $FAKE_PORT --mode ok --ledger $SCRATCH/ecm-fake/requests.jsonl ) > $SCRATCH/ecm-fake/server.log 2>&1 < /dev/null & C=$!;
       trap "rm -f $F; kill $C 2>/dev/null" EXIT INT TERM;
       ( sleep 7200; [ -e "$F" ] && kill -9 $C 2>/dev/null ) & wait $C'
```

The pid is read back from `lsof -tiTCP:$FAKE_PORT -sTCP:LISTEN` and written to
`run.md`; `raven serve` is started the same way.

### 1.3 The isolated Raven

- Tree: `/Users/admin/Raven-b`, branch `feat/everos_cloud_memory_plugin`;
  `git -C /Users/admin/Raven-b branch --show-current` is checked and written to
  `run.md` before every run. Binary: `/Users/admin/Raven-b/.venv/bin/raven`.
  Both memory plugins are installed there as workspace members; a case that
  needs "only one installed" disables the other through `plugins.disabled`
  (`raven/config/raven.py:1112`), which keeps a plugin out of
  `build_onboard_steps` because activation skips it.
- Config home: `RAVEN_HOME=$SCRATCH/ecm-home` (`raven/home.py:48`), which
  moves `config.json`, `serve.json`, `everos-server.pid` and `logs/`. The
  `raven agent --home` option is **not** used: it sets
  `agents.defaults.workspace`, not the config home (`raven/core/config_stack.py:27-28`).
  `raven doctor` and `raven import run` take no home option at all; the
  variable is the only lever. `HOME=$SCRATCH/ecm-home-user` for the import
  scanner, which reads `Path.home()/.claude/projects`
  (`raven/importer/scanners/claude_code.py:194-196`).
- Every command runs under `env -u EVEROS_CLOUD_API_KEY` except T4.1, which
  exports it in its own subshell; the sweep prints the variable first.
- `config.json` is copied from the owner's and edited: `memory.userId` ->
  `ecm-user`, `memory.agentId` -> `ecm-agent`, `memory.backend` per case,
  `plugins.config["everos-cloud-memory"]` -> `{"base_url": "http://127.0.0.1:$FAKE_PORT"}`
  (no key unless the case says so), `plugins.config["everos-memory"]` ->
  `{"owned": false, "base_url": "http://127.0.0.1:$FAKE_PORT", "port": <a free port>, "root": "$SCRATCH/ecm-everos"}`
  (`owned: false` keeps the *backend* from spawning; the local plugin's onboard
  screen still spawns on its default answer, so every case that reaches that
  screen answers `I run my own EverOS -- connect to it`, the port is moved off
  the owner's, and the root is a copy of the owner's `everos.toml` + `ome.toml`
  under `$SCRATCH` so nothing writes into the owner's root), `gateway.port` -> `$RPC_PORT`, `gateway.heartbeat.enabled` ->
  false, `agents.defaults.workspace` -> `$SCRATCH/ecm-ws` (the one workspace;
  session files and sub-agent records land under it), `skillForge.enabled` ->
  false unless the case says otherwise. The owner's provider credentials stay,
  because the chat cases need a real model; **every config snapshot archived
  as evidence is passed through `jq 'walk(if type == "object" then with_entries(if (.key | test("api_?[Kk]ey")) and (.value | type) == "string" and .value != "" then .value = "<redacted>" else . end) else . end)'`
  first**, so no live key lands in `.work_context`.
- Per-case isolation (`make-it-fail` section 6): each case starts by restoring
  `config.json` from `config.before.json`, setting the fake's mode, and
  recording `LEDGER_START=$(wc -l < requests.jsonl)`; "the ledger" in a case
  means the lines after `LEDGER_START`. Each case ends by killing what it
  started (pids in its `run.md`) and restoring the config.
- Web: `raven serve --port $SERVE_PORT` started from `$SCRATCH/ecm-ws`
  (never from the repo tree); login by `POST /auth/nonce` with
  `X-Raven-Token` from `$RAVEN_HOME/serve.json`, then `goto /auth#<nonce>`;
  driver `playwright-cli -s=ecm`; `make build-ui` beforehand and the startup
  log checked for the absence of `built page is older than`.
- Terminal: `tmux -L ecm new -d -s wiz`; `send-keys` types, `capture-pane -p`
  reads; typing and Enter are sent separately with a pause. A command that
  must finish (T29.x) runs through the host's background runner with the
  section 8 watchdog; `timeout` is not assumed to exist.
- Baseline tree for T9.2 and T35.1's "before": `git -C /Users/admin/Raven-b archive 3632e6040 raven plugins-dist | tar -x -C $SCRATCH/base-src`,
  then `PYTHONPATH=$SCRATCH/base-src:$SCRATCH/base-src/plugins-dist/everos-memory`
  in front of the slot venv. Before a baseline half runs, the same environment
  must print the base paths for both packages:
  `python -c "import raven.cli.onboard_commands as m, raven_everos as e; print(m.__file__, e.__file__)"`
  -> both under `$SCRATCH/base-src/`; the output goes into `run.md`. The
  check and every baseline command run from `$SCRATCH`, never from the repo
  root: `python -c` puts the working directory ahead of `PYTHONPATH`, and
  from the root that is the branch's `raven/`. A
  baseline half whose import check prints the venv paths is not a baseline and
  the case is not run.
- "Plugin not installed" for T36.1: `PYTHONPATH=$SCRATCH/ecm-block` with a
  `sitecustomize.py` that prepends a meta-path finder answering `None` for
  `raven_everos_cloud`, which is what `importlib.util.find_spec` answers for an
  absent distribution -- the host's own test for "installed"
  (`raven/core/plugin_stack.py:76-88`).
- Recorded baselines (T9.1, T21.1, T23.1, T26.1, T35.2): produced once on a
  detached worktree of the base commit
  (`git -C /Users/admin/Raven-b worktree add $SCRATCH/base-wt 3632e6040 --detach`,
  `uv sync` there), by the commands each case names; the raw output is kept in
  the case's evidence directory as `baseline.txt` with the command and time.

### 1.4 Evidence

`.work_context/everos_cloud_memory/acceptance/evidence/T<id>/` holds, per
case: `run.md` (branch, commands, times, pids, exit codes, `LEDGER_START`),
`ledger.jsonl` (the case's slice), `pane.txt` or `snapshot.txt` / `shot.png`,
redacted `config.before.json` / `config.after.json` where the case writes
config, `pytest.txt` for suite cases, `baseline.txt` where a baseline is
compared. The rings follow `prove-it-works` section 10: trigger, process,
effect, visible, rerunnable.

### 1.5 Teardown (end of a run, and after any interruption)

```bash
for pid in $(grep -h '^pid ' $EVIDENCE/*/run.md | awk '{print $2}' | sort -u); do
  ps -o pid,args -p $pid | grep -E "ecm-fake|--port $SERVE_PORT|--page-port $RPC_PORT" && kill $pid
done
for p in $FAKE_PORT $SERVE_PORT $RPC_PORT; do                       # a stopped process still holds its port while it drains
  n=0; while lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null && [ $n -lt 100 ]; do sleep 0.2; n=$((n+1)); done
  pid=$(lsof -tiTCP:$p -sTCP:LISTEN); [ -n "$pid" ] && grep -q "^pid $pid" $EVIDENCE/*/run.md && kill -9 $pid
done
tmux -L ecm kill-server 2>/dev/null
playwright-cli -s=ecm close-all 2>/dev/null
command rm -rf $SCRATCH/ecm-home $SCRATCH/ecm-home-user $SCRATCH/ecm-ws $SCRATCH/ecm-fake $SCRATCH/ecm-block   # the homes hold provider keys
ps -Ao pcpu,pid,ppid,etime,args -r | awk '$3==1 && $1>50'                                               # no escaped orphan
```

## 2. Cases

Columns: **Host** -- `real` (the entry a person uses, against the fake cloud),
`pytest` (the requirement is a suite's verdict, and the suite's own output is
the fact), `contract` (fake transport, or a JSON-RPC frame sent straight to the
gateway; supports a real case, never replaces it), `live` (the real service;
needs `EVEROS_CLOUD_API_KEY`). **Exists** -- for each thing the verdict names:
`today:` a command run on `3632e6040` that shows it; `new:` the spec section
that introduces it and the command the run uses to confirm it on the built
code.

### 2.1 Wizard (A1-A9, A28, A29, A38)

| T <- A | Host | Observation, and why it cannot lie | Red when | Exists | Evidence |
|---|---|---|---|---|---|
| T1.1 <- A1 | real (tmux) | Step 4 pane shows `Which memory backend?` with entries `everos`, `everos-cloud`, `Off`, cursor on `everos` (config has `memory.backend: "everos"`). The entries are the registry's contribution names (`PluginRegistry.onboard_names`): the wizard cannot print a name no activated plugin contributes. | no question before a plugin screen; a missing or extra entry; cursor not on the current backend | today: `grep -n "def onboard_names" raven/plugins/registry.py` -> 425; new: spec H1, confirmed by `grep -n "Which memory backend" raven/cli/onboard_commands.py` | trigger (command, time), visible (`pane.txt`), rerunnable |
| T2.1 <- A2 | real (tmux) | Continue T1.1: pick `everos-cloud`, type `ecm-key-7f3a`, Enter. Pane shows `EverOS Cloud connected.`; `config.after.json` has `memory.backend == "everos-cloud"` and `plugins.config["everos-cloud-memory"].api_key == "ecm-key-7f3a"`; the ledger holds one `POST /api/v2/memory/get` with `Authorization: Bearer ecm-key-7f3a` and `page_size: 1`. The file is the fact; the ledger proves the key typed is the key probed. | either field missing or elsewhere; probe absent or under another key | today: `set_plugin_config_fields` merge (`raven/config/update.py:365`), `set_memory_backend` writes `section["backend"]` (`:455`); new: spec onboard.py sketch, probe = health table | trigger, process (ledger), effect (`config.after.json`), visible, rerunnable |
| T3.1 <- A3 | real (tmux) | Pick `Off`. `config.after.json`: `memory.backend == null`; `jq 'del(.memory.backend, .a2a)'` of before and after are byte-identical (`.a2a` is excluded because the wizard provisions `a2a.server.token` on every run, `_initialize_a2a_face`, before and after this change alike). | any other key changes; backend keeps a name | new: spec H1 (`Off` -> `None` -> `set_memory_backend(None)`); today: `set_memory_backend(None)` writes `null` (`update.py:455`) | trigger, effect, rerunnable |
| T4.1 <- A4 | real (tmux) | In a subshell with `EVEROS_CLOUD_API_KEY=ecm-env-key` exported, start the wizard. Pane shows `Using the API key from EVEROS_CLOUD_API_KEY` and no key prompt; `config.after.json` slice has no `api_key`; ledger probe carries `Bearer ecm-env-key`. The ledger is the only place the env key is visible; the file not having it is read from the file. | a prompt appears; `api_key` lands in the file; probe uses another key | new: spec C7, onboard.py `source == "env"` branch; confirmed by `grep -n 'EVEROS_CLOUD_API_KEY' plugins-dist/everos-cloud-memory/raven_everos_cloud/*.py` | trigger, process, effect, visible, rerunnable |
| T5.1 <- A5 | real (tmux) | Fake in `401` mode. After typing a key of at least 8 characters (`ecm-bad-key-401`; the host's key prompt refuses shorter ones before the plugin sees them) the pane shows the 401 sentence (`API key rejected (401)`) and the choices `Re-enter` / `Skip long-term memory`. | the sentence is the generic "cannot reach"; no Re-enter | today: `failure_choice=_failure_choice` lent by `_onboard_ui` (`onboard_commands.py:1912-1926`); new: health table row "key rejected" | trigger, visible, rerunnable |
| T5.2 <- A5 | real (tmux) | Fake in `403` mode: pane shows `refused (403); this may mean the account is not authorized for the v2 memory API`, same two choices. Pick Skip: `config.after.json` `memory.backend == null`. | 403 gets the 401 sentence; Skip leaves a backend name | new: health table row "not authorized"; H1's `set_memory_backend(chosen if outcome is CONFIGURED else None)` writes `null` for `DISABLED` | trigger, effect, visible, rerunnable |
| T6.1 <- A6 | real (tmux) | Slice `base_url` set to `http://127.0.0.1:$DEAD_PORT` where `lsof -nP -iTCP:$DEAD_PORT -sTCP:LISTEN` is empty. Pane shows `cannot reach http://127.0.0.1:$DEAD_PORT` and the two choices. | URL absent from the sentence | new: health table row "unreachable" | trigger, visible, rerunnable |
| T7.1 <- A7 | real (tmux) | At the key prompt, send the back gesture the prompt's placeholder names (read from `pane.txt`; it is `_back_placeholder`'s text). Pane shows the step 3 header again. | Back ignored; wizard exits | today: `back_placeholder=_back_placeholder`, `prompt_api_key=_prompt_api_key` in `_onboard_ui` (`onboard_commands.py:1912-1926`); `StepOutcome.BACK -> return _BACK` (`:2020-2021`) | trigger, visible, rerunnable |
| T8.1 <- A8 | real (tmux) | `plugins.disabled: ["everos-memory"]`. Step 4 pane shows the header `Long-term memory: EverOS Cloud` directly, no `Which memory backend?`. | a chooser with one entry | today: `plugins.disabled` (`raven/config/raven.py:1112`) honoured by activation; new: spec H1 `len(steps) > 1` | trigger, visible, rerunnable |
| T9.1 <- A9 | pytest | `git -C /Users/admin/Raven-b diff 3632e6040 -- tests/test_cli_onboard_commands.py` touches only `test_step4_memory_first_configured_wins`, `test_step4_memory_all_disabled_clears_backend` and `test_step4_memory_back_returns_sentinel_without_writing` (which pinned the retired run-every-screen behaviour with two fake plugins; deviations.md D2) and adds tests; no other existing test body changes; `uv run pytest tests/test_cli_onboard_commands.py -q` passes with the base count plus the added tests. | a hunk in any other existing test; a failure | today: file exists (`ls tests/test_cli_onboard_commands.py`) | process (`pytest.txt`, `baseline.txt`), rerunnable |
| T9.2 <- A9 | real (tmux) | `plugins.disabled: ["everos-cloud-memory"]`, `memory.backend: "everos"`, `--skip-test`. Run step 4 twice from the same `config.before.json`: on the branch, and under the baseline `PYTHONPATH` after the import check of 1.3 printed the base paths. `diff <(normalise pane.branch.txt) <(normalise pane.base.txt)` is empty. Step 4 itself prints no timing under `--skip-test`; `normalise` strips the one varying thing earlier steps can leave in the same scrollback, step 1's probe duration (`f"{elapsed:.1f}s"`, `onboard_commands.py:1044`), with `sed -E 's/[0-9]+\.[0-9]s/<t>s/'`, and nothing else -- a diff that needed more stripping than that is a real difference. **Positive control, run once per session**: with one word of the step-4 header changed in a scratch copy of the branch's `onboard_commands.py`, the same diff is non-empty. The base tree is the fact about "before"; the import check proves it is the tree being run. | any differing line; the import check printed venv paths; the positive control stayed empty | today: `git archive` of `3632e6040`; `test -n "$(ls $SCRATCH/base-src/raven)"`; the import check in 1.3 | trigger, visible (both panes), process (import check output, positive control), rerunnable |
| T28.1 <- A28 | real (tmux) | In T2.1's pane the hint line contains `memory.userId` and `ecm-user`. `ecm-user` is the isolated config's owner id; a hint printing `default` would be reading something other than the locator. | hint absent; prints another id | new: spec onboard.py sketch | visible, rerunnable |
| T29.1 <- A29 | real | Both plugins, `memory.backend: null`: `raven onboard --non-interactive --yes --provider <p> --api-key <p's key> --model <the current default> --skip-test --skip-sandbox --skip-channel --skip-web --skip-subagents --skip-import` (`--yes` because the home already holds a config and `--api-key` because non-interactive step 1 demands it, each missing one exits 2 before step 4; `--model` pins the current default so step 1 writes nothing; `--skip-test` keeps step 1 from billing a test message) under the section 8 watchdog (60 s cap) prints `Long-term memory stays off`, exits 0 before the cap; no `Which memory backend?` in stdout; `config.after.json` `memory.backend == null`. | the watchdog fires (a prompt blocked); chooser text in stdout | today: sentence at `onboard_commands.py:2004`; early return `if not steps or skip or non_interactive` (`:1981`); `--non-interactive` (`:2873`) | trigger, effect, visible, rerunnable |
| T29.2 <- A29 | real | Same command with `memory.backend: "everos-cloud"` and a key in the slice: same sentence; `config.after.json` identical to `config.before.json` (sha256). | the configured backend is cleared | today: `_memory_enabled()` guard (`:2000-2001`); new: `CloudKeyScreen.configured()` returns true on a key | trigger, effect, rerunnable |
| T38.1 <- A38 | real (tmux) | At `Which memory backend?` send `C-c`. Pane shows the shell prompt; `echo $?` prints `1`; `config.after.json` identical to before (sha256). The exit code alone proves little -- Click exits 1 on any `KeyboardInterrupt` (`onboard_commands.py:668` leaves it to propagate); the load-bearing assertion is the unchanged config, which only H1's "nothing written on cancel" produces. | `memory.backend` becomes `null` or any key changes; a screen runs | new: spec H1 (`typer.Exit(1)` on a cancelled select, nothing written) | trigger, effect (config sha), visible, rerunnable |

### 2.2 Recall, store and the two explicit ends (A10-A18, A33, A34)

| T <- A | Host | Observation, and why it cannot lie | Red when | Exists | Evidence |
|---|---|---|---|---|---|
| T10.1 <- A10 | contract | `FakeCloud` serves two episodes (scores 0.3, 0.9) and one profile of 1,500 chars. `recall("q", user_id="ecm-user", top_k=5)` returns three `Memory`, scores descending, the profile text ending in `[profile truncated, N chars omitted]`; the ledger request has `Authorization: Bearer <key>`, `include_profile: true`, `user_id: "ecm-user"`. | order wrong; no marker; header or flag missing | new: spec backend.py `recall`; `_flatten_profile` cap copied from `raven_everos/backend.py:1785,1832-1840` | process (ledger), effect (returned list), rerunnable |
| T10.2 <- A10 | real | `raven agent -m "What do you remember about me?"` with the owner's real model. Ledger: a `POST /search` with `Bearer`, `include_profile: true`, `user_id: "ecm-user"`, no `agent_id`. The fake is the counterparty: a request in its ledger was sent by the host process a person runs. | no search; wrong owner; header missing | today: `raven agent -m` (`agent_commands.py:222`); user-track recall path `raven/context_engine/segments/memory.py:27` | trigger, process (ledger), rerunnable |
| T11.1 <- A11 | contract | 20 plain `store` calls on one session: 20 `/add`, each `mode: "agent"`, user message `sender_id == "ecm-user"`, assistant `sender_id == "ecm-agent"`, `timestamp` integer > 10^12; zero `/flush` (C6). | `mode` missing or `chat`; a slice-sourced id; any flush | new: spec C5, C6; copied `convert_messages` (`raven_everos/backend.py` `def convert_messages`) | process, rerunnable |
| T11.2 <- A11 | real | The T10.2 run's ledger slice: exactly one `/add` after the `/search`, `mode: "agent"`, sender ids `ecm-user` / `ecm-agent`; no `/flush` between the add and the process's last request (the one flush at exit belongs to T18.2). | a flush right after the add; `mode` absent | as T11.1 | process, rerunnable |
| T12.1 <- A12 | contract | `FakeCloud` in `hang` mode: `recall` returns `[]` after 4.0-4.5 s (measured with `time.monotonic`), `recall_session` after 10.0-10.5 s; in `500` and `429` modes both return at once and `store` returns `False`; no exception in any mode. | an exception; a bound missed | new: spec C8 constants `RECALL_TIMEOUT_S = 4.0`, `SESSION_TIMEOUT_S = 10.0` | process, rerunnable |
| T12.2 <- A12 | real | Fake `hang` on `/search` only. `raven agent -m "hello" --logs` exits 0 and answers; the log carries the plugin's recall-timeout warning, `recall timed out after 4.0 s; returning empty` (spec backend sketch); the ledger shows the `/search` and then the turn's `/add`. The log line is written by the plugin inside the real process; the exit code and the following add show the turn was not held hostage. | the run hangs past the model's own time plus 10 s (watchdog); no warning; no add after the hung search | today: `raven agent --logs` (`agent_commands.py:268`); new: spec C8, the warning text in backend `recall` | trigger, process (log, ledger), rerunnable |
| T13.1 <- A13 | contract | `recall("q", agent_id="ecm-agent", top_k=5)` sends `agent_id` only and maps `agent_skills` -> `type: skill`, `agent_cases` -> `type: case` from the fake's agent-track rows (the fake serves agent rows only to an `agent_id` owner, so a request that sent `user_id` would get none); `recall("q", top_k=5)` and `recall("q", user_id="u", agent_id="a", top_k=5)` return `[]` with no request in the ledger and a warning in caplog. | both ids on the wire; a request on the invalid calls; agent rows answered to a user-track request | new: spec backend.py `recall` (XOR copied from `raven_everos/backend.py` `recall`); fake field table | process, rerunnable |
| T13.2 <- A13 | real | `skillForge.enabled: true`; `raven agent -m "hello"`. Ledger holds a `/search` with `agent_id: "ecm-agent"` and no `user_id` (the skill router's recall), beside the user-track `/search` of T10.2. | no agent-track search; both ids on one request | today: `BackendSkillSource.recall(agent_id=...)` (`raven/memory_engine/skill_forge/backend_source.py:95-99`); new: spec backend `recall` | trigger, process (ledger), rerunnable |
| T14.1 <- A14 | contract | `store(sid, msgs, metadata={"flush": True, "user_id": "u2", "agent_id": "a2"})`: ledger shows one `/add` with sender ids `u2` / `a2`, then exactly one `/flush {"session_id": sid}`. | no flush; two; host ids | new: spec "explicit end"; `_owner_override` copied from `raven_everos/backend.py:501-513` | process, rerunnable |
| T14.2 <- A14 | contract | Same call spelled `{"flush": True, "userId": "u2", "agentId": "a2"}` produces the same two requests with the same ids. | the camelCase call falls back to `ecm-user` / `ecm-agent` | today: `raven/agent/subagent_memory.py:69-76` reads both spellings; `_owner_override` as above | process, rerunnable |
| T14.3 <- A14, A15 | real | A sub-agent configured with a `memory` block (`SubagentMemoryConfig`, `raven/config/schema.py:1379`: `userId: "sub-u"`, `agentId: "sub-a"`, `source: "trace"` -- the source is what makes the host write and read back the trace, `manager.py:712`; the default `agent` source joins on an instance id a stateless sub-agent never has; the agent itself is an `openai`-kind sub-agent on an endpoint whose key has quota); `raven agent -m "Use the spawn tool to have a sub-agent reply with the single word pineapple"`. **Precondition asserted first**: a node directory appeared under `$SCRATCH/ecm-ws/sessions/*/subagents/` (the model did call spawn); a run without one is recorded as "precondition not met" and repeated, not as a failure of the plugin. Fake `ok` serves, for `/get` with `filters.session_id` of that spawn, one episode `ECM-EP-5c1d`. Ledger: `/add` with sender ids `sub-u` / `sub-a` for that session (its body size recorded in `run.md`, as the measured headroom under 250 KB), immediately followed by `/flush` for it, then `/get {user_id: "sub-u", memory_type: "episode", filters: {session_id: ...}}`. `<node>.memory.json` holds `status: "settled"` and, under `memories`, the fake's episode text (`Prefers short answers; names files.`, the row whose subject is `ECM-EP-5c1d`; the host maps the row's text, not its subject) and a case item carrying `ECM-CASE-5c1d`. Both strings exist only in the fake; the record file is written by the host from what `/get` returned. | the add carries the host's ids; no flush before the read-back; the record is `pending` / `unavailable`; the text is absent | today: handoff `metadata={"flush": True, **scope.block}` (`subagent_memory.py:199`), record dir `_HISTORY_DIRNAME = "subagents"` (`raven/agent/subagent/history.py:44`), workspace from `agents.defaults.workspace`; new: spec backend `recall_session` | trigger, process, effect (`*.memory.json`), rerunnable |
| T15.1 <- A15 | contract | `recall_session(sid, user_id="u2")`: ledger `/get` body has `user_id: "u2"`, `memory_type: "episode"`, `filters: {"session_id": sid}`, `page_size: 100`; rows map to `Memory`. `recall_session(sid, agent_id="a2")` asks `memory_type: "agent_case"`. | filter missing; wrong type for the track | new: spec wire table `/get` row | process, rerunnable |
| T16.1 <- A16 | contract | Importer-shaped calls: three `store` with `{"bulk": True, "is_final": False}` then one with `is_final: True`: four `/add`, one `/flush` after the fourth add, none earlier. | a flush after a non-final batch; none after the final | today: `raven/importer/orchestrator.py:253` metadata shape; new: spec "explicit end" | process, rerunnable |
| T16.2 <- A16 | real | `$HOME/.claude/projects/ecm-proj/one.jsonl` (a 30-message Claude Code transcript fixture); `raven import run --platform claude_code --tier full --yes` (`full`: the `memory_files` tier keeps only memory files, `raven/importer/types.py:85-89`, and this fixture is a conversation). Ledger for that session id: three `/add` (10 messages each, `_BATCH_MSG_LIMIT = 10`), then exactly one `/flush`, as the last request for the session; the command exits 0. | zero or several flushes; a flush before the last add | today: `import run` subcommand with `--platform/--tier/--yes` (`raven/cli/import_commands.py:512-516`; the group is `import`, `raven/cli/commands.py:218`), scanner root (`claude_code.py:194-196`), `_BATCH_MSG_LIMIT = 10` (`orchestrator.py:30`) | trigger, process, rerunnable |
| T17.1 <- A17 | contract | A 600-message `store`: two `/add` (500 + 100), both `mode: "agent"`. A slice whose JSON is 400 KB of 40 messages: adds split at a message boundary, each body < 250 KB. | one request; a split inside a message | new: spec C10, `ADD_MAX_MESSAGES`, `ADD_MAX_BYTES` | process, rerunnable |
| T17.2 <- A17 | contract | A slice with one 300 KB message and four small ones: the four go out in one `/add`; `store` returns `False`; caplog has a warning naming the session id and the message index. | the big message is sent; the small ones are dropped; `True` | new: spec failure row "request body" | process, rerunnable |
| T17.3 <- A17 | real | `raven agent --message-file $SCRATCH/big.txt` where the file holds 300 KB of text. Ledger: the turn's `/add` carries the assistant message and not the 300 KB user message (no body in the slice exceeds 250 KB); the log has the warning naming the session and message index; the run exits 0. The ledger is the counterparty's: a 300 KB body would be in it. | a body over 250 KB in the ledger; no warning; the assistant message also missing | today: `--message-file` (`agent_commands.py:223`); new: spec failure row "request body" | trigger, process (ledger, log), rerunnable |
| T18.1 <- A18 | contract | Plain stores on `s1`, `s2`, `s3`; explicit-end store on `s2`; `stop()`: ledger gains exactly `/flush s1` and `/flush s3` (any order), then the client is closed (`client.is_closed`). With the fake hanging on `/flush`, `stop()` returns within 5.5 s and caplog names the sessions left. | `s2` flushed again; a session missing; `stop()` past 5.5 s | new: spec backend `stop`, `SHUTDOWN_FLUSH_BUDGET_S = 5.0` (copied from `_SHUTDOWN_FLUSH_BUDGET_S`, `raven_everos/backend.py:159`) | process, rerunnable |
| T18.2 <- A18 | real | The T14.3 run has two sessions in one process: the main turn (plain adds, never flushed) and the sub-agent's (explicit end, flushed). At exit the ledger gains exactly one more `/flush`, for the main session, and none for the sub-agent's; the plugin's stop-sweep line, `flushed 1 session(s) at stop` (spec backend sketch), is INFO on a standard-library logger, which `raven agent --logs` does not bridge (only WARNING and above reach the terminal); it is read in T34.2's `import.log`, where the importer installs the bridge. Two sessions with different histories give the sweep a way to be wrong; the sweep line is the plugin's own account of what it did. | the sub-agent session flushed twice; the main session not flushed; a sweep line naming two | today: `backend.stop()` at one-shot exit (`raven/cli/agent_commands.py:512`); new: spec backend `stop` and its log line | trigger, process (ledger, log), rerunnable |
| T33.1 <- A33 | pytest | `uv run pytest tests/test_everos_cloud_backend_contract.py -q` passes; `--collect-only -q` lists every test `MemoryBackendContractTests` and `LifecycleContractTests` define (same count as `tests/test_everos_backend_contract.py` collects for the local plugin, which inherits the same two classes and defines no test of its own). | a test skipped or overridden; counts differ | today: `raven/memory_engine/contract_test.py:34,169`; `tests/test_everos_backend_contract.py:10` imports both | process (`pytest.txt`), rerunnable |
| T34.1 <- A34 | contract | Fake `ok` for `/add`, `hang` for `/flush`; `RAVEN_EVEROS_CLOUD_FLUSH_TIMEOUT_S=1`: explicit-end `store` returns `False` after 1.0-1.5 s; the session is still in `_unflushed`; a following `stop()` (fake back to `ok`) issues `/flush` for it. | `True`; the session dropped | new: spec backend `store` / `_unflushed`, `RAVEN_EVEROS_CLOUD_FLUSH_TIMEOUT_S` (Decisions) | process, rerunnable |
| T34.2 <- A34 | real | `RAVEN_EVEROS_CLOUD_FLUSH_TIMEOUT_S=2`, fake `ok` with `--hang-on /api/v2/memory/flush`; `raven import run --platform claude_code --tier full --yes` on the T16.2 fixture. The importer's final batch fails its flush and retries by calling `store` again each time (`_STORE_RETRY_BACKOFF_S = (30, 60, 120)`: four attempts, about four minutes in all); every attempt is a whole `store`, so the ledger shows for the final batch four `/add` of the same ten messages (the duplicate-add exposure of spec Open question 3, visible here) each followed by a hung `/flush`, then one more `/flush` at exit (the stop sweep, which the hang also defeats); the command exits non-zero reporting the batch not accepted; the log's sweep warning names the session. The attempt count and the exit are the importer's, the extra flush is the sweep's -- neither can be produced by a session that left the set. | fewer `/add` than attempts (a store that skipped its add), a `/flush` count equal to the attempts (no sweep), exit 0, or no warning | today: importer retry loop (`raven/importer/orchestrator.py:254-270`); new: spec backend `store`, `stop`, the env hook | trigger, process (ledger, log), effect (exit code), rerunnable |

### 2.3 Memory page (A19-A21, A36, A37)

| T <- A | Host | Observation, and why it cannot lie | Red when | Exists | Evidence |
|---|---|---|---|---|---|
| T19.1 <- A19 | real (web) | `memory.backend: "everos-cloud"`, key in slice, fake `ok`. Open the memory page: `snapshot` shows the four `.memstats` values `7`, `1`, `2`, `3`; the Episodes list shows a row with `ECM-EP-5c1d`. Ledger slice: every request carries `Authorization: Bearer`, and no line has `"path": "/health"` -- the fake's catch-all records unknown routes, so a probe would be there as a 404. The numbers and the subject exist only in the fake. | the `TwoPaneNone` "off" pane renders; a request without the header; a `/health` line; a count not from the fake | today: `.memstats` (`ui-web/src/features/memory/MemoryPage.tsx:110`), `if (s.note) return <TwoPane><TwoPaneNone` (`:65`); new: spec H2-H5, H16 catch-all | trigger, process (ledger), visible (`snapshot.txt`, `shot.png`), rerunnable |
| T20.1 <- A20 | real (web) | Fake `empty` mode: `.memstats` values all `0`; no `TwoPaneNone` in the snapshot; the Episodes tab shows the "0 total" note; the ledger slice since page load holds a `POST /api/v2/memory/get` with `page_size: 1` and `Bearer` for each of the four kinds (the page asks for the stats more than once per open, so the count is per kind, not a total). The page half alone is what an idle page also shows; the requests are what make it a measurement. | a kind never asked; a note; a retry control | today: `memory_stats` asks one `/get` per kind (`raven/rpc/methods/memory.py:220-233`) | trigger, process (ledger), visible, rerunnable |
| T20.2 <- A20 | contract | `memory.stats` over the gateway's WebSocket (JSON-RPC, cookie `raven_session_$SERVE_PORT`, `Origin` header) with the fake `empty`: `ok: true`, `note: null`, four zeros. | `ok: false`; a note | today: `memory.stats` shape (`memory.py:198-240`) | effect (RPC reply), rerunnable |
| T21.1 <- A21 | pytest | `git diff --stat 3632e6040 -- tests/test_rpc_memory.py` empty; `uv run pytest tests/test_rpc_memory.py -q` passes with the count in `baseline.txt`. | a hunk; a failure | today: file exists | process, rerunnable |
| T21.2 <- A21 | real (web) | `memory.backend: "everos"`, fake `--with-health`. Open the memory page: the ledger slice has no `Authorization` header on any request and includes `GET /health`. | a header appears; `/health` absent | today: `_cfg` everos arm (`memory.py:55-69`), `_search_tuning` probes (`:81-102`), `owned: false` means no spawn (`CONTEXT.md` EverOS; `raven-plugin.toml` `owned`) | trigger, process, visible, rerunnable |
| T36.1 <- A36 | real | `memory.backend: "everos-cloud"`, `PYTHONPATH=$SCRATCH/ecm-block` (1.3). **Preflight**: `raven doctor --json` under the same environment parses as JSON (the command itself runs). Then `raven doctor; echo $?` prints a memory line containing `did not build` and exits `2`; `raven serve` under the same `PYTHONPATH`: the memory page's `TwoPaneNone` text contains `everos-cloud-memory`. | exit 0; the note names nothing; the preflight fails (a usage error also exits 2) | today: doctor "did not build" branch (`raven/cli/doctor_commands.py:688-697`), exit on `faults` (`:255`), `--json` (`:1077`), no `--home` option (`:1075-1089`); new: spec H5 note naming the distribution | trigger, effect, visible, rerunnable |
| T37.1 <- A37 | real (web) | Fake `401` mode: the Episodes tab's error text contains `everos refused the request:` followed by the fake's `error.message` (`ECM-REFUSED-401`). | the list error says "unreachable" | today: `_everos_error` (`memory.py:105-122`) | trigger, visible, rerunnable |
| T37.2 <- A37 | contract | `memory.stats` over the gateway WebSocket with the fake in `401`: `ok: false`, four zeros, `note: null`. | `ok: true` | today: `memory_stats` degrades per kind (`memory.py:227-233`) | effect, rerunnable |

### 2.4 Settings page and the restart guard (A22-A27, A35, A39)

| T <- A | Host | Observation, and why it cannot lie | Red when | Exists | Evidence |
|---|---|---|---|---|---|
| T22.1 <- A22, A23 | real (web) | `memory.backend: "everos-cloud"`. Model page snapshot: none of the three EverOS-only role labels (`Memory extraction`, rerank, multimodal) is present in any form -- in particular not the dim `The EverOS memory plugin isn't installed` pill that today's `available: false` draws; the embedding row is present (knowledge bases embed with that pin); a card titled by `gui.settings.cloud.title` with a status chip, a key row and the endpoint `http://127.0.0.1:$FAKE_PORT`. | one of the three rows visible in any form; the not-installed sentence; the embedding row missing; card absent | today: `Roles` maps every `ROLES` entry (`Roles.tsx:514-526`), the not-installed pill (`:265-272`, string `gui.settings.roles.everos_missing`), label `gui.settings.roles.memllm` = "Memory extraction" (`i18n/messages.json`); new: spec H6 `reason`, H13 filter, H8, cloud strings | trigger, visible, rerunnable |
| T23.1 <- A23 | contract (pytest) | `settings_everos({})` with `memory.backend = "everos-cloud"` -> `reason: "other_backend"`, note contains `everos-cloud`, `set(sections) == {"embedding"}`, `required` holds nothing but `embedding`; with `"everos"` and with `None` -> the dict in `baseline.txt` for that config. | sections reported for the cloud; `reason` missing; a change for `everos` / `None` | today: `settings_everos` (`console.py:1243-1264`); new: spec H6 | process, rerunnable |
| T24.1 <- A24 | real (web) | Type `ecm-key-rot8` in the card's key row, Save: `config.after.json` slice `api_key == "ecm-key-rot8"`; chip reads connected; ledger shows a `/get page_size: 1` with `Bearer ecm-key-rot8`. Switch the fake to `401`, save `ecm-key-bad`: chip text contains `rejected (401)`. The file and the ledger carry the typed value. | key written elsewhere; chip unchanged; probe under the old key | today: `KeyRow` writes `store.write(keyName, v)` (`ui-web/src/features/settings/pages/Tools.tsx:53-70`); new: spec H7 route, H8 RPC | trigger, process, effect, visible, rerunnable |
| T25.1 <- A25 | contract (pytest) | `settings_set({"key": "plugins.config.everos-cloud-memory.nope", "value": "x"})`, `(... ".api_key", 42)`, `("plugins.config.not-a-plugin.api_key", "x")` each raise `ConfigValidationError` whose message contains the key; `config.json` unchanged after all three. | a write lands; a message without the key | today: `settings_set` final `raise` (`console.py:725`); new: spec H7 | process, rerunnable |
| T25.2 <- A25 | contract | The same three frames sent to the running gateway over its WebSocket: each answer is a JSON-RPC error containing the key; `config.after.json` identical to before. | an `applied` result; a config change | today: `/rpc` WebSocket with cookie and `Origin` check; new: spec H7 | effect (reply, config sha), rerunnable |
| T26.1 <- A26 | pytest | `git diff --stat 3632e6040 -- tests/test_rpc_settings.py ui-web/src/features/settings/providers/Roles.test.tsx ui-web/src/features/settings/source.test.ts` empty; `uv run pytest tests/test_rpc_settings.py -q` and `cd ui-web && npm test -- Roles source` pass with the counts in `baseline.txt`. | a hunk; a failure | today: the three files exist | process, rerunnable |
| T26.2 <- A26 | real (web) | `memory.backend: "everos"` (fake `--with-health`): Model page shows the four EverOS role rows and no cloud card. | a card; a hidden row | today: Roles.tsx role rows (`:43-56`, `:514-526`) | trigger, visible, rerunnable |
| T26.3 <- A26, A23 | real (web) | `memory.backend: null`: Model page shows the four EverOS role rows (the `None` arm answers as today) and no cloud card. | a card; a hidden row | today: `settings_everos` answers for any backend when the plugin is installed (`console.py:1243-1264`) | trigger, visible, rerunnable |
| T27.1 <- A27 | real | **Preflight**: `raven doctor --json` parses as JSON. Then `raven doctor; echo $?` under seven conditions -- fake `ok`, `401`, `403`, `429`, `hang`, `418`, and no key with the fake up -- prints the health table's sentence for each (the seven literal fragments: the base URL after `ok`, `rejected (401)`, `refused (403)`, `rate limited by`, `cannot reach`, `answered 418`, `no API key`) and exits `2` for the five `missing` answers, `0` for `ok` and `429`. `--json` shows the same seven under `memory`. | two conditions share a sentence; 429 exits 2; `ok` exits non-zero; the preflight fails | today: rendering `_render_memory_capabilities` (`doctor_commands.py:725-745`), exit on `faults` only (`:124`, `:255`), `--json` (`:1077`), options list without `--home` (`:1075-1089`); new: health table | trigger, effect (stdout, exit codes), rerunnable |
| T35.1 <- A35 | real (web) | `memory.backend: "everos-cloud"`, both plugins active, `plugins.config["everos-memory"].owned: false` (as 1.3; nothing can spawn here). Record before: the set `P0` of pids whose args match `everos server start --root $RAVEN_HOME/everos` (empty), the line count `L0` of `$RAVEN_HOME/logs/*.log` (asserted to exist and be non-empty first), absence of `$RAVEN_HOME/everos-server.pid`. On the Model providers page save a key for the main model's provider, then change the main model. After each: the pid set is still empty, the pidfile still absent, and `grep "everos restart .* begin" $RAVEN_HOME/logs/*.log` finds no line after `L0`; the page snapshot has no banner mentioning a memory restart. **Before (baseline `PYTHONPATH`, import check passed)**: the same two actions make `everos restart <id> begin` appear after `L0` -- with `owned: false` the local plugin logs the restart and stops short of spawning (`server.py:1273,1283`), so the log line is the whole symptom and nothing needs killing. The log lines are written by the local plugin inside the gateway; the pid set is the operating system's; the before/after difference is the guard. | a restart line after `L0` in the after-run; a pid or pidfile appears; no line in the before-run (the before-run did not exercise the path) | today: `everos_follows_provider` (`console.py:1362-1398`) called from `model.py:578`; `everos_follows_main_model` from `config.py:665`; pidfile `everos-server.pid` (`raven_everos/server.py:304`), argv `everos server start --root` (`:1017`), log `everos restart {} begin` (`:1273`), `owned: false` path (`:1283`); new: spec H15 | trigger, process (log before/after), effect (pid set, pidfile), visible, rerunnable |
| T35.2 <- A35 | contract (pytest) | `settings_everos_set({"section": "llm", "model": "m", "provider": "p"})` with `memory.backend = "everos-cloud"` raises `ConfigValidationError` containing `everos-cloud`; nothing written; the same call for `section: "embedding"` is accepted (knowledge bases keep their pin). With `"everos"` the call behaves as `baseline.txt` records. | the write succeeds; `everos` changes | today: `settings_everos_set` (`console.py:1426-`); new: spec H15 | process, rerunnable |
| T35.3 <- A35 | pytest | `uv run pytest tests/test_rpc_settings.py -q -k "restart or queued"` collects the base tree's set (the seven `TestTheSaveRestartsTheService` tests, among them `test_a_session_that_queued_a_restart_hears_how_it_went` and `test_a_cancelled_restart_still_reports`) plus this change's `test_provider_save_does_not_restart_everos_for_the_cloud_backend`, and passes, with no existing line of the file edited (T26.1's check). | a base test missing from the collection; a failure | today: `tests/test_rpc_settings.py:339,422`; `-k "follows"` deliberately not used (it also matches `:1047`, an unrelated test) | process, rerunnable |
| T39.1 <- A39 | contract (pytest) | `settings_everos({})` with `memory.backend = "mem0"` -> `reason: "other_backend"`, sections holding only `embedding`, note contains `mem0`. | sections reported; `reason` missing | new: spec H6 (any non-`everos`, non-`None` name) | process, rerunnable |
| T39.2 <- A39 | real (web) | `memory.backend: "mem0"`: Model page snapshot has none of the memllm / rerank / multimodal rows (the embedding row stays) and no cloud card; the memory page shows the "runs on 'mem0'" note. | a role row | today: memory page note branch (`memory.py:188-195`); new: spec H6, H13 | trigger, visible, rerunnable |

### 2.5 Structure, release and the live service (A30-A32)

| T <- A | Host | Observation, and why it cannot lie | Red when | Exists | Evidence |
|---|---|---|---|---|---|
| T30.1 <- A30 | pytest | `uv run pytest tests/test_plugin_boundary.py -q` passes, and `test_scan_roots_exist` now asserts both plugin roots are non-empty (so an empty scan cannot pass vacuously). A deliberate `import raven_everos_cloud` added to `raven/core/plugin_stack.py` turns `test_host_does_not_import_plugin_internals` red (then reverted; `git diff` clean). | the mutation stays green | today: `tests/test_plugin_boundary.py:20-22,50`; new: spec H9 | process (`pytest.txt` with the mutation run), rerunnable |
| T30.2 <- A30 | pytest | A test imports every module under `raven_everos_cloud` with `sys.modules["everos"] = None` and `sys.modules["raven_everos"] = None`; passes. Adding `import raven_everos` to `backend.py` turns it red (then reverted). | the mutation stays green | new: spec C3 | process, rerunnable |
| T31.1 <- A31 | pytest | `git diff --stat 3632e6040 -- tests/test_release_plugin_list.py` empty; `uv run pytest tests/test_release_plugin_list.py -q` passes; `grep -c "uv build --wheel plugins-dist/" .github/workflows/release.yml` prints `4` (3 on the base); `grep -c -- "-eq 3" .github/workflows/release.yml` prints `1`. | the list grows; the wheel is not built | today: `release.yml:85-90,187` (3 and 1 on the base) | process, rerunnable |
| T32.1 <- A32 | pytest | `N=$(grep -c "^def test_" tests/integration/test_everos_cloud_real_cloud.py)` equals 6, the number of tests C14-C18 name; without the variable, `uv run pytest tests/integration/test_everos_cloud_real_cloud.py -rs -q` reports 6 skipped: five with a reason containing `EVEROS_CLOUD_API_KEY`, and `test_add_without_flush_is_searchable_within_two_minutes` with its own reason (run by hand; spec C17). | N is not 6; a test runs or passes without a key; a reason missing | new: spec C14-C18 test names | process, rerunnable |
| T32.2 <- A32 | live | With `EVEROS_CLOUD_API_KEY` set (`zsh -ic` to load it): the five key-gated tests pass; the C17 test is run by hand once and its output kept. **Deferred until a key exists; the delivery says so.** | any failure; a skip hidden as a pass | spec C14-C18 | trigger, process, effect (the service's own replies), rerunnable |

## 3. Human walk-through (★)

Five journeys, at most one screen each, for the owner to take by hand after
the agent has run them. Each names what the person looks at, not what to
assert.

| ★ | Journey | Cases | Look at |
|---|---|---|---|
| ★1 | Configure the cloud backend in the wizard, once with a good key and once with a rejected one | T1.1, T2.1, T5.1 | the chooser's three entries and preselection; the connected line; the 401 sentence with Re-enter; `config.json` afterwards |
| ★2 | Open the memory page on the cloud backend | T19.1 | four counts that match the fake's 7/1/2/3; an episode row reading `ECM-EP-5c1d`; nothing saying the memories are elsewhere |
| ★3 | Change the key on the settings card | T22.1, T24.1 | no memllm / rerank / multimodal rows at all (not even the "plugin isn't installed" line), the embedding row still there; the card's chip turning connected, then showing the 401 wording after a bad key |
| ★4 | Run `raven doctor` against the seven conditions | T27.1 | seven different sentences; exit code 2 only for the five faults |
| ★5 | Save a provider key with the cloud backend active | T35.1 | no restart line in the gateway log, no EverOS process, no pidfile, no banner -- and the same action on the base tree writing the restart line |

## 4. Coverage

Counts from the machine check in section 5: 39 A items, 64 T cases; 35 `real`,
9 `pytest`, 19 `contract`, 1 `live`. Every A has at least one case; every A
has a `real` case except these, each with the reason it cannot have one:

| A | Why no real-host case | What stands instead |
|---|---|---|
| A25 (undeclared settings key refused) | no page control sends an undeclared key or a wrong type; the refusal guards against a client the page is not, so the only entry that reaches it is a JSON-RPC frame | T25.1 in process, T25.2 against the running gateway (both `contract`) |
| A30, A31, A33 | the requirement is a suite's verdict; the suite's output is the fact | the `pytest` cases, with a mutation run for A30 |
| A32 | the real service; deferred until a key exists, and the delivery says so | T32.1 proves the skip reasons and the count; T32.2 runs live later |

No A is marked "not built", "unit only" or "fixture only".

## 5. Machine check

```bash
python3 - <<'EOF'
import re, pathlib
doc = pathlib.Path("docs/specs/2026-10-08-everos-cloud-memory-acceptance.md").read_text()
spec = pathlib.Path("docs/specs/2026-10-08-everos-cloud-memory-design.md").read_text()
a_in_spec = set(re.findall(r"^\| (A\d+)", spec, re.M))
rows = [l for l in doc.splitlines() if re.match(r"^\| T\d+\.\d+ <- A", l)]
covered, hosts, real_as = set(), {}, set()
for r in rows:
    cells = [c.strip() for c in r.strip("|").split("|")]
    aa = re.findall(r"A\d+", cells[0]); covered.update(aa)
    h = cells[1].split()[0]; hosts[h] = hosts.get(h, 0) + 1
    if h == "real": real_as.update(aa)
print("A in spec:", len(a_in_spec), "covered:", len(covered), "missing:", sorted(a_in_spec - covered, key=lambda x: int(x[1:])))
print("T rows:", len(rows), hosts)
print("A without a real-host case:", sorted(a_in_spec - real_as, key=lambda x: int(x[1:])))
assert a_in_spec == covered
EOF
```
