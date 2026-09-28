# Self-configuration eval

Does Raven notice when a problem is really its own configuration, look before
it answers, and fix it the right way? Most of the cases never mention settings:
the user asks why a sub-agent failed, pastes a GitHub link, or complains that
it stops early, and the cause sits in `config.json` or in an integration that
is not connected.

## What a case is

`cases.yaml`, one entry per scenario:

| key | meaning |
|---|---|
| `id` | unique name |
| `kind` | `implicit` (the cause is configuration, the user does not say so), `explicit` (a settings request), `negative` (nothing to do with settings), `safety` (a change requested by something other than the user) |
| `config` | merged over the base config for this case's isolated home |
| `history` | messages written into the session before the turn |
| `message` | what the user sends |
| `answer` | what the harness replies to a clarifying question (default: "decide yourself") |
| `approve` | how the harness answers approval prompts: `allow` or `deny` (default); delegating, browsing and the plugin catalog are always allowed |
| `expect.must` | matchers that at least one tool call has to satisfy |
| `expect.must_not` | matchers no tool call may satisfy |
| `expect.config_after` | dotted paths and the value `config.json` must hold after the turn |
| `rubric` | what the optional LLM judge checks in the reply |

A matcher takes `tool`, `action` (list), `path`, `path_prefix`,
`path_contains`, `value` (compared after JSON-decoding the call's `value`) and
`args_contain` (a substring of the arguments). Calls made through `tool_call`
are unwrapped to the tool they forwarded to.

## How it runs

Each case gets its own `RAVEN_HOME` and its own process, and talks to Raven
through the same RPC stack the desktop page uses, so `raven_config` has the
settings writers the page lends it and approvals arrive as real prompts. The
child's `HOME` is its case directory, so `~/.raven` never reaches the machine's own. The
base config takes only `providers` and the default model from
`--source-config` (default `~/.raven/config.json`); web tool keys in the
environment are scrubbed, so "no search configured" really is unconfigured.

```bash
uv run --all-extras python -m benchmarks.self_config_eval.run
uv run --all-extras python -m benchmarks.self_config_eval.run --kind implicit
uv run --all-extras python -m benchmarks.self_config_eval.run --case github_link_not_connected
uv run --all-extras python -m benchmarks.self_config_eval.run \
    --model openrouter/anthropic/claude-sonnet-5 --provider openrouter \
    --judge-model openrouter/anthropic/claude-sonnet-5
```

A case passes when its matchers hold, the turn finished, and -- with
`--judge-model` -- the judge accepts the reply against the rubric. The report
lands in `results/` (git-ignored); the temporary homes are kept and printed so
a failing case's `run.log` can be read.

Each run spends real model calls: one turn per case, plus one judge call per
case when judging.
