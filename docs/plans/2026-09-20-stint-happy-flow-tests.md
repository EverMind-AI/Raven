# The stint happy flow, and the harness that proves it

The eight scenarios asked for, written out as things a test can assert, plus the
automation that runs them. Companion to `2026-09-18-stint-acceptance.md`, which
lists what a multi-round run has to survive; this one is narrower and shallower
on purpose -- it is the path a person actually walks the first time.

Target: `feat/playbook_rounds_mode` (`mode: stint`, `.stint/`, `raven playbook
stint*`). The `feat/playbook_rounds_extension` worktree is a separate
implementation under `mode: rounds` and none of this applies to it unchanged.

**On the name.** There is no `game-stint`. The shipped builtin is `rounds`
(`raven/playbook/builtin/long-horizon-dev-stint/playbook.md`), whose `mode` is `stint`. The
acceptance doc already owns the rename question; everything below says
`rounds`, and if the builtin is renamed the fixture name is the only thing
that moves.

---

## 0. What "a prefab game-stint" has to be

Three separate fixtures, because one cannot serve all eight scenarios.

### 0.1 The playbook

| Fixture | What it is | Used by |
|---|---|---|
| `rounds` | the shipped builtin, verbatim | anything that asserts on the real product surface: the web card, the detail page, the approval text, the library listing |
| `echo-rounds` | the same shape, three roles pointing at `ThirdPartyCliSubagentConfig(name="echo", command="cat")` | every test that must not spend a model: the whole state machine, both UIs' round rendering |
| `probe-rounds` | `echo-rounds` plus one role whose `verify` command writes a sentinel file | boundary/undo and check-handback assertions |

`echo-rounds` is the load-bearing one. `tests/test_playbook_stint.py` already
uses exactly this trick (`agents=[ThirdPartyCliSubagentConfig(name="echo",
command="cat")]`), so a role is a `cat` of its own prompt: deterministic,
free, and fast enough that the full state machine runs in a test suite. Its
`stop.until` marker must be reachable -- with `cat` roles the marker appears in
the output only if it is in the prompt, which is how "the last role writes the
marker and the plan ends" is driven on demand.

### 0.2 The project the stint runs on

A "fresh repo" is not an empty directory, and the difference is the single
biggest trap in scenario 1. To reach round one a project needs **all** of:

1. **a git repository** -- `lay_out` refuses a non-repository before writing
   anything (`raven/stint/setup.py:72`);
2. **at least one commit** -- `_open_tree` cuts a worktree from
   `repository.head()`; on a `git init` with no commit that raises, is logged,
   and the stint **works in place, in the person's own tree**
   (`raven/playbook/stint.py:591-595`). See the finding in S1;
3. **a document that says what has to be true** -- a `.md` under the root,
   `docs/`, or `.stint/`, at least 200 characters, containing at least one of
   must / should / shall / required / acceptance / criteria, and not named
   `README.md` / `CHANGELOG.md` / `CLAUDE.md` / `AGENTS.md`
   (`raven/stint/bootstrap.py:44-65,119-163`). Without it the run is refused
   with "write what this project is into `.stint/SPEC.md`";
4. **a source directory the guards can name** -- one of `src`, `project`, `lib`,
   `app`, `pkg`, `cmd`, `tools`, `scripts`, `assets`, or the Builder owns
   `src/**` and every write it makes is undone as a violation.

So the fixture builder is:

```
git init && git commit --allow-empty -m init
docs/PRD.md      # >= 200 chars, several "must"/"acceptance" lines
src/main.py      # something the build check can compile
```

Ship it as one pytest fixture and one shell function, used by every layer.

### 0.3 The agent home

`RAVEN_HOME` per test. Stints live under the run root
(`plans_root -> <run root>/<STINTS_DIRNAME>`) and `_already_running` searches
**every peer store on the machine** (`raven/playbook/stint.py:338`), so a stray
stint from a previous run in a shared home will refuse the next test's start.
Test isolation here is not hygiene, it is correctness.

---

## 1. The scenarios

Each one states what has to be true before, what the person does, what a test
can see, and -- where it matters -- what today does **not** do.

### S1. Fresh repo, never set up, TUI

**Before:** the fixture repo of 0.2, no `.stint/`, no stint record anywhere.

**Steps**

1. `cd <repo> && raven tui`. The relay sends `session.create` with
   `workdir=<repo>` (`raven/cli/_tui_relay.py`), which is what makes the repo
   the project: `_stint_workspace` resolves the stint's project through the
   session's workdir (`raven/agent/loop/wiring.py:1523`).
2. Person: "run rounds" (or the Chinese equivalent).
3. Model calls `load_playbook(name="long-horizon-dev-stint")`, optionally with
   `max_rounds`.
4. Approval question appears, answered "Run it".

**Observables, in order**

| # | Where | What |
|---|---|---|
| 1.1 | the repo | `.stint/` now holds `HUMAN_DECISIONS.md`, `AGENT_DECISIONS.md`, `FIXLOG.md`, `PLAYBOOK.md`, `planner.md`, `builder.md`, `verifier.md`, and `SPEC.md` as a symlink to the picked document |
| 1.2 | `git status` | all of it **untracked**; nothing committed to the person's branch (acceptance 32) |
| 1.3 | the approval | `Start "long-horizon-dev-stint" on <repo>?`, `up to 30 round(s) of: planner -> builder -> verifier`, `on branch stint/<id>, in a checkout of its own -- your working tree is untouched`, the literal `python3 -m compileall -q src`, one `writes` line per role, `it stops early only if the last role writes NOTHING-LEFT`, then `it has just written N file(s) here, untracked -- ...`, `the roles stint from docs/PRD.md`, `N task(s) in the backlog` |
| 1.4 | the transcript | the tool returns a receipt at once; the conversation is free (acceptance end-to-end 1) |
| 1.5 | git | a worktree at `<run root>/stints/<id>/tree` on branch `stint/<id>`, carrying a copy of what setup wrote |
| 1.6 | the record | `<run root>/stints/<id>.json`: status `running`, `round_index` 1, `project` = the repo, `workdir` = the tree |
| 1.7 | the transcript, per round | `Stint <id> (rounds) finished round K of at most 30: build=passed. The next round is starting.` plus the "this is progress, not a request" paragraph |
| 1.8 | the transcript, at the end | `ran N round(s) and stopped: <reason>`, the branch, `which nothing has merged`, and the `extend` command |
| 1.9 | the repo | the person's working tree is byte-identical apart from the untracked `.stint/` |

**Findings to decide before this is "happy"**

* **The backlog is empty at round one.** Setup picks the specification and does
  not write a backlog (acceptance 39, open). The approval says `0 task(s)`, so
  it is visible, but the planner's first round starts from nothing. Either the
  fixture pre-seeds the backlog with `raven playbook stint task add`, or S1
  accepts an empty one and asserts the planner fills it.
* **`git init` with no commit runs in the person's own tree.** The
  non-repository refusal does not catch it, `worktree_add` fails on `head()`,
  and the stint then works in place *with hard enforcement*: a role's stray
  write is undone by putting **the person's tree** back. The approval in that
  case omits the "your working tree is untouched" line, which is honest but
  easy to miss. A repository with no commits is a plausible reading of "a new
  repo". Suggest refusing it the way a non-repository is refused.

### S2. Same, but the web UI

**The page has no per-session or per-run choice of project, and which directory
it does get depends on which binary is serving it.** There are two, with two
different policies, and they serve the same page:

| Entry | Policy | Where a session lands |
|---|---|---|
| `raven serve` (what `raven web --supervise` starts) | `WorkdirPolicy.LAUNCH_DIR` (`raven/core/engine_stack.py:110`) | **the directory the process was launched from**, for every session, with no channel suffix |
| `raven gateway` | `WorkdirPolicy.PER_CHANNEL` (`raven/cli/gateway_commands.py:373`) | `<session_root>/<channel>`; with no `-w` that is `~/.raven/tmp/tui`, because `session_root` defaults to `default_channel_root(workspace_path)` and every RPC-minted session is keyed `tui:<chat_id>` (`raven/rpc/methods/session.py:403`) |

Verified on the author's machine: `raven web --supervise --port 18792` ->
`raven serve`, cwd `/Users/admin/workspace/ledger-demo`, and a fresh web
conversation answers that its workdir is `/Users/admin/workspace/ledger-demo`.

So the good news first: **S2 is walkable today**, by launching the server from
inside the repository. The test harness needs no `-w` and no config -- it starts
`raven serve` with `cwd` set to the fixture repo.

What is still missing is choice, and three things follow from that:

1. **One server, one project.** Two repositories at once is not expressible, and
   switching repositories means restarting the server somewhere else.
2. **The page never sends a `workdir`.** `ui-web/src/live/080-overrides.js:425`
   calls `rpc.call('session.create', {})`; the param exists and is the TUI
   relay's route. `info.cwd` comes back and is used read-only, to set the file
   browser's root (`wsSetRoot`, `ui-web/src/live/170-workspace.js:20`).
3. **The same page gives a stint two different projects depending on how it was
   started** -- a real repository under `serve`, a scratch directory under
   `gateway`. That is worth settling on its own; a person reading the approval
   cannot tell which one they are getting from the page.

**Observables** (launched from the fixture repo): 1.1-1.9 of S1, with these
substitutions -- the approval renders as the page's `ask_user` prompt and is
answered there; the round messages appear in the web transcript; and
additionally the **stints panel** shows a card with the live badge,
`round N / 30`, the tree path, and the unanswered-question count.

### S3. Already set up, a stint still unfinished

The scenario asked for is "it asks whether to continue or start a new one". The
code refuses rather than asks, deliberately and with a written rationale
(`_second_plan_refused`, `raven/playbook/stint.py:856`): the surfaces that
cannot ask -- cron, a reconnecting client -- are the ones where quietly opening
a second stint is worst. In a conversation the refusal *becomes* the question,
because the model relays it. That is the contract to test, and it splits three
ways by what "unfinished" means.

**S3a -- the stint is genuinely running.**

* `start` returns `Error: a stint is already on <project>: <id>, on round N.
  Take that one up again with 'raven playbook stints resume <id>', or end it
  with 'raven playbook stints stop <id>' and start fresh. ...`
* the executor wraps it: `kind="questions"`, `Failed to start the plan: ...`
* **assert:** no second record written, no second worktree, no second branch,
  the reply names the id, the round and both verbs, and the assistant puts the
  choice to the person rather than picking one.
* **and assert the honest answer to "continue":** there is nothing to take up.
  `resume` refuses a record that says running and was touched inside
  `STALE_AFTER_SEC` (`raven/playbook/stint.py:resume`). "Continue" here means
  "wait"; only "stop and start fresh" is a move.

**S3b -- the host died mid-round (the case people mean).**

* Precondition: a record whose `touched_at_ms` is older than `STALE_AFTER_SEC`
  (300s). In a test: write the record, monkeypatch the constant, or backdate the
  stamp.
* A read marks it: `stints list` / `stints get`, on CLI and over RPC, call
  `mark_adrift` on the way past, so the status moves `running -> interrupted`.
* `start` still refuses (interrupted is `unfinished`), naming `resume`.
* `raven playbook stints resume <id>` takes it up **from the node it stopped
  at**: finished roles are named as satisfied dependencies, not re-run.
* **assert:** the finished role's output is still what the rest of the round
  reads; exactly one new run id; the terminal is held
  (`Holding this terminal: a stint's rounds run in this process`) and releases
  when the stint ends.

**S3c -- paused.** `resume` opens the round *after* the paused one, not the
paused one again (acceptance 2). Assert the round index advances by one and no
role of the finished round runs twice.

**Gaps this scenario exposes**

* **The web UI cannot continue anything.** The gateway registers
  `playbooks.stints.{list,get,stop,answer}` and nothing else
  (`raven/rpc/methods/playbooks.py:1000-1003`). `resume`, `extend` and `pause`
  are terminal-only (acceptance 46). So in S3 on the web, the person is told to
  run a command they have no terminal for. Either add the three methods or say
  in the refusal which surface can do it.
* **"Continue" from a conversation costs a second process.** The model can only
  honour it by exec'ing `raven playbook stints resume`, which runs the rounds
  inside that short-lived CLI process (`_held_here`,
  `raven/cli/playbook_commands.py:819`) rather than in the gateway that is
  hosting the conversation. Worth deciding whether that is the intended answer
  or whether the driver should expose `resume` to the loop directly.

### S4. Already set up, the previous stint finished

What the code does: `unfinished` is false, so nothing refuses, and a **new**
stint starts -- new id, new record, new worktree cut from **the project's
HEAD**. The first stint's work is on `stint/<old id>`, which nothing merged. So
the second stint begins from before the first one's first commit and says
nothing about it (acceptance 9, known gap).

`lay_out` is idempotent, so nothing is rewritten (acceptance 37) -- but note
what that means: the project's `.stint/backlog.json` is still the one setup
wrote. Every task the first stint moved was moved inside its own checkout and
committed to its own branch. **The second stint re-plans from the original
backlog.**

So S4 as asked ("a new one that carries on") is not what happens. Pick one:

* **(a) the intended move is `extend`.** The closing summary already names it
  (`More rounds on this same tree: raven playbook stints extend <id> --rounds
  N`). Then S4's assertion is that the summary said so, that `extend` opens the
  next round on the same checkout and branch with the journal intact
  (acceptance 1), and that the budget rises rather than resetting.
* **(b) starting again really is wanted** (a new piece of work on the same
  project). Then the start path owes the person a line saying the previous
  stint's branch exists and is not in this one's base -- today it is silent.

Assert whichever is chosen. Asserting both is how the gap stays invisible.

### S4b. What happens to the branch and the checkout when a stint ends

Nothing. Stated plainly because three separate scenarios above depend on it and
it is not written down anywhere else.

* **Nothing merges.** No production code calls `ProjectGit.merge_branch`
  (`raven/stint/git.py:191`) -- the only callers are in `tests/test_stint_git.py`.
  It was built for the checkout-per-role design that the acceptance doc
  describes and does not ship. The stint's work stays on `stint/<id>` and the
  closing summary says so: `on branch <branch>, which nothing has merged`.
* **Nothing removes the checkout.** `worktree_remove` (`raven/stint/git.py:173`)
  is called only from inside `worktree_add`, to clear a path or a branch holder
  before taking it. So every stint leaves a full second checkout under
  `<run root>/stints/<id>/tree`, forever, including interrupted ones.
* **The branch is force-moved on reuse.** `worktree_add` does
  `git branch --force <branch> <base>`, which is safe only because the branch
  name carries the stint id.

Consequences worth a test each:

| Assertion | Why |
|---|---|
| after a stint ends, `git branch --list 'stint/*'` still names it and `git worktree list` still lists the tree | so nobody writes a test that assumes cleanup |
| the project's own branch is unchanged, at the same sha it started at | the promise the approval makes |
| the closing summary names the branch and says nothing merged it | the only place a person learns where their work is |
| the second stint of S4 has a base that does not contain the first's branch | the S4 gap, pinned |

What is missing, in the order it will be asked for: a way to see a stint's diff
(`git -C <tree> log <base>..`), a way to merge it (the function exists, nothing
calls it), and a way to throw the checkout away when it is merged or abandoned.
Disk cost is one full checkout per stint on the project, and an interrupted run
leaves one nobody will look for.

### S5. `mode: prompt` playbooks are unaffected

Not a stint scenario -- a blast-radius scenario. The stint change touched the
shared path: `executor.py`, `params.py`, `validate.py`, `types.py`, `store.py`,
`load_playbook.py`, `rpc/methods/playbooks.py`, `rpc/models.py`. Pin the
pre-existing behaviour at each seam:

| Seam | Assertion |
|---|---|
| library listing | a `prompt` playbook still appears in `load_playbook`'s per-turn description, ranked against the turn message |
| gaps | a missing required param still comes back as `kind="gaps"` with the param names, before anything dispatches |
| composition | `mode: prompt` still composes a graph via one provider call, revalidates it, and dispatches through the same chain |
| no composer | with `compose_prompt_mode` off, the caller still gets `kind="guidance"` |
| CLI | `raven playbook run <prompt playbook>` still composes and runs to completion in-process, and still exits non-zero on gaps |
| RPC | `playbooks.get` for a `prompt` playbook carries no `stint` key at all (`raven/rpc/methods/playbooks.py:227`) |
| web | the detail page still draws the prompts box, and the card still draws nothing where a stint draws roles |

### S6. `mode: dag` and the raw graph tool are unaffected

Higher risk than S5: the stint change rewrote `dag_tool.py` (+385),
`dag_runner.py` (+102), `manager.py`, `dag_verdict.py`, and added
`dispatch_ledger.py` (new, 161 lines). Pin:

| Seam | Assertion |
|---|---|
| confirm gate | a model-composed graph with `confirm: true` still asks through `ask_user` with `["Run it", "Not now"]`, and "Not now" still stops it |
| no asker | with no ask channel wired the graph still runs, and still logs that it did |
| dispatch accounting | one graph is charged once; the new per-plan ceiling (`PLAN_MAX_PARALLEL = 2`) does not lower an ordinary graph's concurrency |
| control tools | `dag_status`, `cancel_dag`, `resolve_dag_node` still reachable and still hidden from the schema |
| verdicts | node handback / `maxHandbacks` / follow-up unchanged for a non-stint graph |
| progress | an ordinary run's progress events carry **no** `stint_id` / `round_index` (there is already a test for this, `test_an_ordinary_run_says_nothing_new`) |
| CLI | `raven playbook run <dag playbook>` unchanged, including `--fill` |

### S7. A `mode: stint` playbook is legible before it runs

**Web -- held, and worth keeping held.** `playbooks.get` returns a `stint`
section (`_stint_wire`) and the page renders: the card draws the role chain as a
graph; the detail page's stint tab shows the round budget, the `until` marker,
whether it reports every round or only at the end, each role with its
owns/appends/reads/artifacts, which role is terminal, **every check command in
full**, and the carried memory entries. `PlaybooksPage.test.tsx` already covers
it; the e2e adds that the same thing is true through a real gateway with the
real builtin.

**TUI -- there is nothing.** `ui-tui/src` has no playbook surface at all; the
only matches for "stint" are in generated RPC types. So in the TUI a playbook is
legible only inside the conversation, through `load_playbook`'s description and
`PlaybookRuntime._detail`. Assert that much -- "what playbooks do you have"
lists `rounds` with its description -- and record the rest as a gap, or
build a TUI page. Asserting a card that does not exist is how a test suite
starts lying.

### S8. Rounds are legible in the conversation

One mechanism, two surfaces. A finished round calls `_report`, which calls
`dag_tool.say`, which goes down the **announce** route -- the same route a
finished run's result takes, because it is the only one that reaches the main
agent and starts a turn there (`raven/agent/subagent/dag_tool.py:811`).

**Assert, on both surfaces:** after each round, one assistant message shaped

```
Stint <id> (rounds) finished round 2 of at most 30: build=passed
[; N boundary violation(s) undone][; N unanswered question(s)].
The next round is starting.
```

followed by the "this is progress, not a request" paragraph, or -- when a
question is waiting -- the paragraph naming the question and the
`stints answer` command. And assert the negative: `stop.report: end` produces
exactly one message, at the end.

**What does not exist, and should be said plainly:** there is no live round
indicator in either UI. Progress events do carry `stint_id` and `round_index`
(`SubAgentDagTool._emitter`), and no reader uses them (acceptance 47). The web
has the stints panel, which is a page rather than the conversation. So S8's
honest scope is: the announced messages (both surfaces), the web stints panel
updating, and no in-conversation round widget anywhere.

### S9 (added). The approval gate itself

It is in every scenario above and belongs to none of them. `confirm: true` on a
stint routes through `_confirm_graph` -> `ask_user.ask_direct` with
`["Run it", "Not now"]` (`raven/agent/loop/wiring.py:1423`). Assert: "Not now"
writes no worktree and no record beyond the refusal; a conversation busy at the
deadline is a "Not now"; with no asker wired the stint runs and logs that it
did; and the approval text is the stint's own (the callable), never the generic
node list.

### S10 (added). A question round-trips

The reason `_report` exists. A role calls `raven playbook stint ask`, the round
carries on, the person answers with `raven playbook stints answer <id> -q N -t
"..."` or in the web panel, and the answer reaches the round *after* the one in
flight (acceptance 8). Assert the count in the round message, the answer landing
in the record, and the text reaching the next round's prompt.

---

## 2. The automation

Five layers. Each one is the cheapest thing that can hold its claim, and nothing
is asserted twice.

```
L0  existing unit suites            seconds      every commit
L1  engine e2e, no model, no UI     ~1 min       every commit
L2  surface e2e over real RPC       ~2 min       every commit
L3  TUI in a pty                    ~3 min       nightly / on ui-tui + stint changes
L4  web in a browser                ~4 min       nightly / on ui-web + stint changes
L5  one round against a real model  minutes, $   manual, before a release
```

### L0 -- what is already there

`tests/test_playbook_stint.py` (2174 lines), `tests/test_stint_*.py` (11 files),
`tests/test_cli_stint_commands.py`, `tests/test_playbook_types.py`,
`tests/test_playbook_validate.py`, `tests/test_rpc_playbooks.py`, and
`ui-web/src/features/playbooks/PlaybooksPage.test.tsx`. Gate:

```bash
uv run pytest tests -k "stint or playbook" -x
npm test --prefix ui-web
```

These cover the state machine's cells. They do not cover a person walking in,
which is what the rest of this is.

### L1 -- the engine e2e

New file: `tests/integration/test_stint_happy_flow_e2e.py` (naming per
AGENTS.md 5.2: scope, then `e2e`, no version or ticket in the name).

Real everything except the roles: a real fixture repo on disk, real git, real
`SubAgentDagTool`, real `StintDriver`, real record store, `echo-rounds` for the
roles, `announce` and `charge` captured into lists the test reads -- the shape
`tests/test_playbook_stint.py` already uses.

| Test | Scenario |
|---|---|
| a repo with no `.stint/` is laid out, approved, and runs both rounds | S1 logic, 1.1-1.9 |
| the approval text carries budget, branch, every check command, the stop rule, the spec pick and the backlog count | 1.3 |
| nothing is committed to the person's branch and the tree is untouched | 1.2, 1.9 |
| `git init` with no commit: assert what is decided -- refusal, or in-place with the branch line absent | S1 finding |
| a second start on a project with a live stint is refused, names the id and both verbs, and writes nothing | S3a |
| a backdated record is marked interrupted by a read, and `resume` re-runs no finished role | S3b |
| a paused stint resumes on the *next* round | S3c |
| after a stint finishes, a second start opens a new tree from HEAD and does not carry the first's branch | S4, pinning whichever answer is chosen |
| `extend` opens the next round on the same checkout, branch and journal | S4 (a) |
| `stop.report: end` says one thing, at the end | S8 negative |

Deterministic-time note: monkeypatch `HEARTBEAT_EVERY_SEC` and
`STALE_AFTER_SEC` rather than sleeping.

### L2 -- the surface e2e, over real RPC

The highest-value new layer, because **both** UIs speak the same dialect-A
JSON-RPC: what is proven here is proven for the TUI and the page at once, and
L3/L4 shrink to "does it render".

Pattern: `tests/integration/test_tui_cancel_inflight_e2e.py`, which already
stands up a real `RpcServer` over a unix socket with a real
`SubscriptionEmitter` and a fake agent loop. Here, instead of a fake loop, use a
**real `AgentLoop` with a scripted provider** -- an `LLMProvider` stub that
returns a `ToolCallRequest` for `load_playbook(name="echo-rounds")` on the first
turn and plain text afterwards. `tests/integration/test_agent_playbook_e2e.py`
already builds a loop against a stub provider this way.

New file: `tests/integration/test_stint_conversation_e2e.py`.

| Test | Scenario |
|---|---|
| `session.create` with `workdir=<repo>`, then `turn.send "run it"`: an `ask_user` frame goes out carrying the stint approval, not the generic node list | S1.3, S9 |
| answering "Run it" produces a receipt message and the turn completes while the stint runs on | S1.4 |
| each finished round arrives as its own assistant message of the documented shape | S8, both surfaces |
| "Not now" writes no record and no worktree | S9 |
| a second `turn.send "run it"` while one is live returns the refusal text with both verbs in it | S3a |
| `playbooks.get` for `rounds` carries the `stint` section with roles, checks and budget | S7 web data |
| `playbooks.stints.{list,get,answer,stop}` over the wire against a live stint | S8 panel data, S10 |

### L3 -- the TUI, in a pty

There is no headless TUI mode and no pty harness in the repo, so build a small
one: python's `pty`, spawn `raven tui` in the fixture repo with `RAVEN_HOME` and
the scripted-provider config in the environment, write keystrokes to the master
fd, and tee everything read back to a file.

```
scripts/tui_drive.py          # spawn, send keys, capture, strip ANSI, dump frames
tests/integration/test_stint_tui_e2e.py
```

Because L2 already proved the protocol, this layer asserts only rendering, on
the ANSI-stripped capture:

* the approval is on screen and its option row offers "Run it" / "Not now";
* selecting "Run it" (down-arrow + Enter, or whatever the Ink prompt binds) is
  followed by the receipt line;
* each round message appears in the transcript as its own block;
* the closing summary shows the branch and the `extend` command.

Keep the assertions to substring matches on stripped text. Ink redraws, uses the
alternate screen, and rewraps on width -- so pin `COLUMNS`/`LINES`, and assert
on the *final* frame plus the concatenated stream, never on a frame index. Save
every capture to an artifacts directory as the terminal equivalent of a
screenshot; do not commit them (AGENTS.md 7).

The scripted provider here cannot be monkeypatched -- the TUI is another
process. Stand up a tiny OpenAI-compatible HTTP stub (a `ThreadingHTTPServer`
returning canned chat-completions responses, including the `load_playbook` tool
call) and point the test config at it with `providers.<name>.api_base` +
`protocol: "chat"`. That stub is shared with L4.

### L4 -- the web, in a browser

Playwright, the pattern the repo already uses
(`tests/integration/test_docs_site_controls_e2e.py`: `pytest.importorskip`,
`sync_playwright`, a pinned Chrome path, a fixed viewport). Serve the built page
from a real `raven gateway -w <fixture repo>` with the same LLM stub as L3.

```
tests/integration/test_stint_web_e2e.py
```

| Check | Scenario |
|---|---|
| the playbooks page draws a `rounds` card whose graph has three cells | S7 |
| its detail page shows the stint tab: budget, `NOTHING-LEFT`, report cadence, three roles with their paths, the literal check command, the carried memory rows | S7 |
| in the chat, sending the message renders the approval and "Run it" is clickable | S2, S9 |
| round messages appear in the transcript as they land | S8 |
| the stints panel shows the live badge, `round N / 30`, the tree, and the question count; answering a question in the panel lands in the record | S8, S10 |
| stopping from the panel stops the stint the conversation started | acceptance 27, currently unverified |

Screenshot each step into the artifacts directory. `ego-browser` is the right
tool for walking this by hand and for eyeballing a layout regression; Playwright
is what runs unattended, because a screenshot nobody diffs is not a test.

### L5 -- one real round

`tests/integration/test_stint_real_llm.py` (naming per AGENTS.md 5.2:
`real_<resource>`), marked and skipped unless configured. One round of the real
`rounds` on a tiny real repo, asserting only that the planner produced a
brief at its owned path, the builder wrote under `src/`, Verifier wrote a verdict,
and the check ran. This is the only layer that can catch "the standing orders do
not actually produce a brief", and it is the only one worth paying for.

### What has to be built before any of this runs

1. `echo-rounds` / `probe-rounds` fixture playbooks, and the fixture-repo
   builder shared by all five layers;
2. the OpenAI-compatible LLM stub server (L3 + L4);
3. `scripts/tui_drive.py` (L3);
4. the decisions in section 3 -- a test cannot assert a behaviour
   nobody has chosen.

---

## 3. The decisions, and where they landed

| # | Question | Status | Where it bites |
|---|---|---|---|
| 0 | Which implementation ships: `mode: stint` (this branch) or `mode: rounds` (the extension worktree) | **decided 2026-09-20: this branch** | everything here |
| 1 | A `git init` with no commit: refuse it, rather than running in the person's own tree with enforcement on | **decided: refuse** | S1 |
| 2 | Can a web conversation name its project? | **decided 2026-09-20: deferred.** The project is the directory the server was started in. Nothing is built; see the caveat below | S2 |
| 3 | Does "continue" reach the web -- `playbooks.stints.{resume,extend,pause}` over RPC? | **decided: register them** | S3 |
| 4 | After a stint finishes, is the next move `extend`, or a new stint that is told it starts from HEAD? | open | S4 |
| 5 | Isolation: worktree, branch, or none -- and does the work ever come back? | **decided: the three-way field, default `branch`**; the diff/merge/discard verbs still open | S4b |

### Decision 2, deferred: the project is where the server was started

Settled for now: **no picker, no run parameter.** A stint works the directory
the serving process was launched from, which is what `raven serve` already does
(`WorkdirPolicy.LAUNCH_DIR`). Launch it inside the repository and the stint runs
on the repository.

**The caveat, because the sentence is not true of both entry points.**
`raven gateway` resolves `PER_CHANNEL` instead, so a stint started from its page
lands in `~/.raven/tmp/tui` rather than anywhere the operator chose. Two ways to
close that, neither urgent: say in the docs that the page is served by
`raven web` / `raven serve`, or give `gateway` the same launch-directory
default. Until one of them happens, "wherever you started it" is a rule with an
exception nobody can see from the page.

The rest of this section records the design that was considered and shelved, so
the next person does not re-derive it.

#### The shelved shape: the project is asked for, not inherited

Today a stint's project is derived -- `_stint_workspace` -> `peek_session_workdir`
(`raven/agent/loop/wiring.py:1523`) -- so it is whatever directory the *session*
resolves to: the server's launch directory under `raven serve`, `~/.raven/tmp/tui`
under `raven gateway` (see S2). Either way the person cannot choose, and the two
differ. Two ways to let them say otherwise, and they are not alternatives so much
as different scopes:

* **per run: `project` becomes a parameter of the start path.** The model that
  cannot fill it is told to ask. This is self-contained -- nothing about the
  conversation moves, and `_already_running` is already keyed by project, so one
  conversation can hold stints on two repositories. Validation has to be real:
  absolute, an existing directory, a repository root, and outside the agent home
  (`validate_override`'s rule).
* **per conversation: the page sets the session's workdir.** `session.create`
  already takes it. This moves everything -- file tools, `exec`, the workspace
  browser -- into the repository, which is what "open the web UI in this repo"
  means, and it is the larger change.

Not chosen, for now. If the one-server-one-repository rule starts to chafe --
two repositories at once, or switching without a restart -- the per-run
parameter is the cheaper half and the place to start.

**This exact design already exists in the implementation being dropped.** The
extension worktree's `ChatRoundsExecutor` (`raven/playbook/rounds/chat.py:20,63`)
takes `project` as a run parameter and refuses without it in these words: *"needs
the 'project' parameter: the absolute path of the project to work on, which must
be a clean Git worktree root. Ask the user which project, then call again."*
Port the shape, not the module.

### resume and extend, since the pair is not self-evident

They answer different questions, and which one applies depends on *why* the
stint is not advancing -- which a person often does not know without
`stints get`.

| | `resume <id>` | `extend <id> --rounds N` |
|---|---|---|
| for | a stint that stopped **before** its budget: host died, paused, interrupted | a stint that stopped **because** the budget ran out (or was stopped by hand) |
| budget | unchanged | raised by N |
| what it opens | the round it was on, re-submitted with finished roles named as satisfied dependencies -- or, if that round completed, the next one | the next round, if none is in flight; nothing if one is |
| refuses | a finished or stopped stint; one whose record was touched moments ago; one this process is working right now | fewer than 1 round; a total above `MAX_ROUNDS`; a deleted checkout; another live stint on the project |
| re-asks approval | no | no -- the person is changing a number they already approved |

The seam between them: a stint whose current round finished and whose budget is
spent is refused by `resume`, which points at `extend`
(`raven/playbook/stint.py:resume`). So the rule is "resume takes up what was
interrupted; extend buys more of what was finished", and a person who guesses
wrong is told which one they wanted. Whether that should be one verb that works
it out is a follow-up, not a gap.

### The shape of decision 1

`_open_tree` (`raven/playbook/stint.py:560`) refuses a non-repository when any
role declares `owns`/`appends`, then falls through to
`repository.worktree_add(tree, branch, repository.head())`. On a repository with
no commits `head()` answers empty, `git worktree add` fails, the `except
(HistoryError, OSError)` logs it and returns `""` -- which means "carry on in
place". The fix is to make the enforcing case refuse there too, with the same
wording as the non-repository refusal names the fix:

* enforcing + no commit -> `Error: ... has no commits yet, so there is no base to
  cut a checkout from; commit something first.`
* not enforcing -> unchanged; working in place is fine when nothing is undone.

The existing refusal path is already tested
(`a directory that is not a repository is refused before anything is written`,
acceptance 33), so this is one branch and one test beside it.

### The shape of decision 5: isolation, and what happens to the work

**First, the fact that bounds the options: nothing else in the product uses a
worktree.** `raven/stint/git.py` plus `_open_tree` is the only worktree code
there is. A DAG's nodes -- parallel branches and `instance`-pinned ones alike --
all run in one working directory (`workdir.current()`, the session's). That is
exactly why an enforced playbook may not fan out (`_concurrent_and_enforced`):
one tree, two writers, and the first role judged is graded against both. The
adjacent prior art is `raven/agent/loop/checkpoint.py`, which snapshots the
workspace into a shadow git so a turn can be undone -- undo, not isolation.

So a stint's checkout is not "the mechanism the rest of the product uses"; it is
the only one, and whether it is the right default is a live question.

**And today every stage is one node.** An enforced playbook is held to a chain:
`_concurrent_and_enforced` (`raven/playbook/validate.py:221`) refuses two roles
with no dependency path between them where either declares `owns`/`appends`, and
`rounds` declares planner -> builder -> verifier. (A stint that enforces nothing
may still fan out, capped at `STINT_MAX_PARALLEL = 2`,
`raven/agent/subagent/dag_tool.py:335`.)

That removes the correctness argument for the checkout entirely. With one writer
at a time, `touched_since(base)` is unambiguous in *any* single tree -- the
person's included. So the stint's worktree today buys exactly three things, none
of them measurement:

1. the person's branch gets no `round(NN): <role>` commits;
2. the person's own uncommitted edits cannot be read as a role's stray write and
   reverted into `violations/`;
3. the person can keep using their checkout while the run goes.

Drop 3 and `branch` is correctness-equivalent to `worktree`. Drop 1 and 2 as
well and `none` is too -- for a person who is not touching the repository while
it runs, which is a thing they can promise and nothing can check.

The one case that needs more than all three is the one that does not exist yet:
a fan of Developers. That needs a checkout **per role**, which is the design the
acceptance doc writes out and nothing implements -- and which none of the three
values below provides. Name the field so a fourth value fits later; do not try
to anticipate it now.

**What in-place actually means today**, before any option is added:

* **a round commits, per role, to the current branch.** `_commit`
  (`raven/playbook/stint_round.py:344`) runs `git commit` as
  `round(NN): <role>` after every role that left the tree dirty. In place, that
  is 3N commits on the branch the person is standing on;
* **hard enforcement reverts against that same tree.** A stray write is undone
  with `restore_from(stage_base, stray)` and `restore(..., quarantine=...)`
  (`raven/stint/enforce.py:139-150`). The baseline is the round's starting
  commit, so a file the *person* edited while the round ran reads as a role's
  stray write: reverted, and copied aside into `violations/`;
* **the tree is occupied for hours.** Switching branches under a running stint
  is what the worktree exists to make impossible.

In-place is therefore coherent only where nothing is enforced and nothing is
committed. That is a real configuration -- it is what a stint on a non-repository
already does -- but it is not the same feature with a flag flipped.

**The shape, if it is built.** One run-level field on the stint spec
(`raven/playbook/stint_spec.py`, beside `stop` / `verify`, not per role):

| value | checkout | branch | commits | `owns`/`appends` |
|---|---|---|---|---|
| `worktree` (today) | its own, under the run dir | `stint/<id>` | per role | enforced |
| `branch` | the person's | `stint/<id>`, checked out in place | per role | enforced |
| `none` | the person's | the person's | none | **refused at load** |

`branch` is the interesting middle: no second checkout (which is what costs on a
large repository), the person's branch is untouched, enforcement still measures
correctly -- and the price is that the run owns the working tree while it goes.
`none` has to refuse a playbook that declares ownership, the same way
`_concurrent_and_enforced` refuses concurrency it cannot measure; saying
"enforced" over a tree where the undo would hit the person's own edits is the
failure mode this whole section exists to avoid.

**Two rules that come with it.**

1. **The approval must say which one.** The text today reads `on branch <b>, in
   a checkout of its own -- your working tree is untouched`. Under `branch` and
   `none` that sentence is false and something else has to be true in its place.
2. **The file may ask for more isolation, not less.** A playbook travels;
   `isolation: none` in a file somebody else wrote, on a repository with
   uncommitted work, is a way to lose that work. So the spec field sets a floor
   and only a person -- the CLI flag, or the approval -- can go below it. The
   same reasoning `max_rounds` already follows: what a run costs *here* is the
   caller's, what it *is* is the author's.

**And the part that is missing whichever default wins: the work has to come
back.** `merge_branch` is written and tested and has no caller. The minimum that
makes `worktree` usable is three verbs -- see the stint's diff, merge it, throw
it away (and remove the tree when merging or discarding). If those exist, the
pressure to default to in-place mostly goes away, because the complaint behind
it is "my work is on a branch I did not ask for and cannot see".

## 4. What to take out of the implementation being dropped

`mode: rounds` (the `feat/playbook_rounds_extension` worktree, uncommitted)
loses to this branch, and three of the gaps listed above are already solved in
it. Read them before the worktree goes; port the shape, not the module -- the
two have different specs, different state and different names.

| There | What it is | Gap it closes here |
|---|---|---|
| `raven/playbook/rounds/chat.py:20,63` | `project` as a run parameter, with a refusal that tells the model to ask the user | decision 2 |
| `raven/rpc/methods/rounds.py:67` | `rounds.{list,get,start,pause,cancel,resume,answer}` -- the whole verb set over RPC, including the three this branch keeps on the terminal | decision 3 |
| `ui-web/src/features/playbooks/RoundsPanel.tsx` (376 lines) | a run console on the page | S8's web half, acceptance 46 |
| `raven/playbook/rounds/parallel.py` (245 lines) | isolated Builder candidates and a retained integration barrier | the checkout-per-role design the acceptance doc writes out and nobody built |

The last one is the valuable one and the least portable: it is the answer to
"three developers a round", which this branch refuses at load
(`_concurrent_and_enforced`) precisely because it has no such mechanism.
