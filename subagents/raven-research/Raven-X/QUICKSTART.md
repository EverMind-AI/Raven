# Quickstart

From a fresh clone to an answered research question. Roughly ten minutes, most of it waiting
for `uv sync`.

## 0. Prerequisites

- Python **3.12+** (`requires-python = ">=3.12"`)
- [`uv`](https://docs.astral.sh/uv/) — the only supported package manager. `pip` is not.
- An LLM endpoint. Anything OpenAI-compatible works: OpenRouter, a vLLM/SGLang server, OpenAI.
- For live-web research, two keys: [Serper](https://serper.dev) for search and
  [Jina Reader](https://jina.ai/reader) for page fetching.

## 1. Install

```bash
git clone <this-repo> Raven-X && cd Raven-X
uv sync
```

## 2. Check the install without spending a token

```bash
uv run pytest -q tests/test_agent_flow_dr.py     # 54 tests, ~8s
```

This is the flow/anchor contract. If it is green, the DR Flow is assembled correctly and you
have a working environment — no keys or network needed. The full suite is `uv run pytest -q`
(3,897 tests, ~2 min).

## 3. Configure

Run this from the repository root — every path below is relative to it:

```bash
cd /path/to/Raven-X                       # the directory containing pyproject.toml
cp examples/dr_live_web.json my_config.local.json
# edit my_config.local.json: replace PUT_YOUR_LLM_API_KEY_HERE, and set the model you want
export SERPER_API_KEY=...
export JINA_API_KEY=...
```

The `.local.json` suffix is deliberate: it is gitignored, so a config carrying your API key
cannot be staged by accident. Everything else in this file uses relative paths for
readability — if a command reports `No such file or directory`, check that you are in the
repository root before suspecting a missing file.

`examples/README.md` explains every knob and flags the two that are not priced. The LLM key
must be in the config file — there is no environment fallback for it, and search and fetch
keys are read from the environment (`tools.web.*` in the config overrides them).

## 4. Ask something

```bash
uv run raven agent --config my_config.local.json \
  -m "Which company acquired the maker of the Kinect depth sensor, and in what year?"
```

You will see `web_search` and `web_fetch` calls, then a committed answer. Those are the only
two tools the model has; that is enforced by `drFlow.toolsAllowlist` at assembly time.

Add `--logs` to watch the runtime, or `--workspace <dir>` to keep scratch files somewhere
specific. By default the agent's workspace lives in `~/.raven/workspace/` and is created on
first run — the `Created agent_memory/...` lines you see are relative to that directory, not to
where you launched from.

## 5. Read what actually happened

The reply on your terminal is rendered for you to read. The machine-readable record of the
run is the session log:

```bash
ls -t ~/.raven/workspace/sessions/cli/*.jsonl | head -1
```

The last `assistant` row with `finish_reason == "stop"` is the answer; `flow_version` on any
row names the build that produced it; and `observers` on that final row is the flow's own
account of the turn — which gates fired, how many searches and fetches, whether the context
window was hit. If you are calling this from a script, that file is the interface, not stdout.
`README.md` documents the fields, including two counters that look like failures and are not.

## What is and is not in scope

`raven --help` lists commands inherited from the general-purpose assistant this build was
carved out of — `onboard`, `gateway`, `channels`, `tui`, `sentinel`, `cron`, `sessions`, and
others. **Only `raven agent` is in scope.** The rest are unmaintained here and slated for
removal; see the Repo layout table in `README.md`, which marks each subpackage `keep` or
`prune`. Do not start with `raven onboard` — despite its inviting name it configures
subsystems this build does not use.

Note also that `raven --version` reports the **package** version (`v0.1.5`, inherited from
upstream), not the DR Flow version. The one that matters is `drFlow.version`. The example
configs deliberately **omit** it and inherit the build's current label, which is what you want
unless you are publishing a number; read the current value with:

```bash
uv run python -c "from raven.config.raven import DRFlowConfig; print(DRFlowConfig().version)"
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Error: No API key configured.` (exit 1) | `providers.<name>.apiKey` missing from the config. Not read from the environment. |
| `drFlow.version=... predates this build's flow semantics` | You used a retired label. The message names the value to use. Superseded labels are rejected on purpose — the label keys the measurement ledger, so an old label on a new build would mislabel a whole batch. The example configs omit the label for this reason; only pin one if you are publishing a number. |
| `force-finalize: salvage unavailable` in the log, or `answer_chars: 0` / `finalShape.form: "empty"` on a run that plainly answered | `drFlow.thinkClosingTagRequired` is `true` but your model does not emit `</think>`. The answer still reaches you, but every observer reports the turn as answerless and the salvage path burns extra calls. Set it to `false` for a non-think model; `dr_live_web.json` already does. |
| Every `web_fetch` fails with `402` | The Jina Reader account is out of credit. Search and fetch are **separate vendors with separate quotas** — search working tells you nothing about fetch. Probe both before a long run. |
| `web_search` returns nothing usable | Check `SERPER_API_KEY`. Behind a corporate proxy, set `tools.web.proxy` in the config, or rely on the standard `http_proxy` / `https_proxy` environment variables, which the fetch path inherits by default. |
| Tests fail in the full suite but pass alone | Suite-order pollution, not a regression. |
| `cannot execute: required file not found` after moving the directory | `uv sync --reinstall`. Bare `uv sync` leaves stale absolute-path shebangs in `.venv/bin/`. |

## Where to read next

- `README.md` — what this build is, the five observers and their fixed order, the version
  lineage, and the repo layout with prune status.
- `examples/README.md` — every config knob, and which ones we have measured.
- `AGENTS.md` — hard rules for changing this repo. Section 0 (measurement safety) applies to
  anyone touching `raven/**`, human or otherwise.
