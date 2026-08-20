# Running raven as a coding agent or a data agent

There is one raven, not two. The coding/data distinction is two independent
switches:

| Switch | What it changes | Default |
| --- | --- | --- |
| `domain` | Which identity prompt is rendered (`prompts/<domain>/<model-family>.txt`) and which discipline block is substituted into it | `coding` |
| `plugins.enabled` containing `data-agent` | Whether the 11 DuckDB-backed evidence tools exist at all | off (the plugin ships `enabled_by_default = false`) |

They are orthogonal on purpose. `domain=data` without the plugin gives a data
analyst with no query plane; the plugin without `domain=data` gives SQL tools
to an agent still being told to reproduce the failing code path and rank the
project's existing tests first.

`domain` is also orthogonal to `--profile` (`interactive` / `oneshot` /
`eval_coding` / `eval_answer`): one profile serves several domains, so a config
that pins a domain does not also have to pin a profile.

## coding

The default. Nothing to configure.

```bash
raven agent -m "fix the failing test in parser.py"
```

## data

Two things, neither optional.

```bash
# 1. the extra (duckdb + pandas)
uv sync --extra data-agent

# 2. the domain
RAVEN_DOMAIN=data raven agent -m "..."
# or
raven agent --domain data -m "..."
```

...and the plugin plus its sources, which only live in config:

```json
{
  "agents": { "defaults": { "domain": "data" } },
  "plugins": {
    "enabled": ["data-agent"],
    "config": {
      "data-agent": {
        "sources": [
          { "alias": "sales", "kind": "sqlite", "ref": "/data/sales.db" },
          { "alias": "logs",  "kind": "duckdb", "ref": "/data/logs.duckdb" }
        ]
      }
    }
  }
}
```

### Where each knob is read

- `domain` — precedence `--domain` > `RAVEN_DOMAIN` > `agents.defaults.domain` >
  the preset's own value (`coding`). Resolved once per process by
  `raven.agent.profile.resolve_profile`; an unknown value warns and falls
  through to the next level rather than raising.
- Accepted values are exactly the prompt directory names under
  `raven/context_engine/segments/prompts/`, so there is no mapping table to keep
  in sync: today `coding` and `data`.
- The fallback chain when rendering is domain-family -> domain-default ->
  coding-default. A domain with only `default.txt` written still serves every
  model family; a domain with no directory degrades to the coding prompt instead
  of failing the run.
- `domain=data` additionally suppresses bootstrap-file injection (AGENTS.md and
  friends), which is a coding-repo convention with nothing to say to a data
  task.

### data-agent plugin config

Everything below lives under `plugins.config["data-agent"]` and is handed to the
tool factories verbatim.

| Key | Meaning |
| --- | --- |
| `sources` | Inline list of store dicts |
| `sources_file` | Path to a JSON list of the same dicts. Combinable with `sources`. Use this when each task has different stores — a harness materializes the file per task, where a static config cannot |
| `kernel_python` | Interpreter for the shared DuckDB/python kernel (default: raven's own) |
| `duckdb_extension_dir` | Pre-downloaded extensions, for offline/no-network runs |
| `semantic_map_model` | Model for the `semantic_map` extraction operator. Unset means `semantic_map` declines rather than guesses |

A source dict is `{"alias": ..., "kind": ..., "ref": ...}` plus optional
`read_only` (default `true`). `kind` must be one of `sqlite`, `duckdb`,
`postgres`, `mongo`; `ref` is a file path, a libpq DSN, or a mongo URI, and
`path` / `dsn` / `uri` are accepted as aliases for it. `alias` is the schema
name the model sees, e.g. `metadata.articles`.

The tools contributed are `sql`, `python`, `profile`, `distinct_values`,
`verify_join`, `semantic_map`, `resolve_entities`, `bootstrap_classifier`,
`grain_check`, `survey_sources`, `submit_answer`. Design notes:
[specs/data-agent-design.md](specs/data-agent-design.md).

## Under a benchmark harness (AgentEval)

The harness sets the domain through the env contract and the plugin through the
config passthrough:

```yaml
harness:
  domain: data
  run_profile: eval_answer
  raven_config_extra:
    plugins:
      enabled: ["data-agent"]
      config:
        data-agent:
          sources_file: /work/sources.json
```

The cross-repo contract is environment-only (`RAVEN_PROFILE`, `RAVEN_DELIVERY`,
`RAVEN_ACCEPTANCE`, `RAVEN_DOMAIN`) because raven's JSON config is
`extra='forbid'`: an older raven handed a config with newer keys refuses to
start, while it merely ignores unknown env.

## Verifying which form actually ran

Every run emits one audit line reporting the resolution **after** single-gate
env overrides, not what the preset claims:

```
raven-profile: v1 name=eval_answer source=env delivery=none acceptance=conversation domain=data gates=... ask_user=off
```

Grep for `raven-profile: v1` and read `domain=`. This is the feature-detection
target for harnesses — opt-in switches have already produced one silent-no-op
run whose conclusion was void.

## Known gap

The TUI (`raven` with no `-m`) does not resolve a run profile, so
`agents.defaults.domain` is ignored there and only `RAVEN_DOMAIN=data` takes
effect. `raven agent` honors all three levels.
