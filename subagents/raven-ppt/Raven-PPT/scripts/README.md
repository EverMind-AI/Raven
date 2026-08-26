# Raven operational scripts

Standalone CLIs and admin utilities. Not part of the Raven wheel —
these scripts are invoked directly from a checkout and don't ship to
end users.

## Layout

```
scripts/
├── README.md                     This file.
├── boxlite_cli.py                Direct CLI for the boxlite microVM library.
└── build_tabler_icons.py         Regenerate the packaged PPT icon data.
```

## When to use

| Script | Purpose | See |
|---|---|---|
| `boxlite_cli.py` | Manage boxlite OCI images + VMs (pull / ls / create / start / stop / rm / shell). Independent of Raven — works even when no agent is running, can inspect VMs owned by another boxlite home. | [`docs/sandbox/boxlite_cli.md`](../docs/sandbox/boxlite_cli.md) |
| `build_tabler_icons.py` | Rebuild `raven/ppt/services/assets/data/tabler_outline.json` from a tabler-icons checkout: chooses which of the upstream ~5100 outline SVGs ship, converts their paths to the absolute M/L/C/Z grammar the data file uses, and attaches each icon's upstream tags and category. Run by hand when the icon set changes; not part of CI, and never rewrites or drops an icon already in the file. | the module docstring |

`scripts/boxlite_cli.py` is **complementary** to the `raven sandbox`
sub-command group (in `raven/cli/sandbox_commands.py`):

- `raven sandbox …` — agent-runtime debug interface (Unix-socket
  connection to a SandboxDebugServer inside a running Raven process;
  only sees VMs owned by that process).
- `scripts/boxlite_cli.py …` — direct boxlite library CLI (manages
  images, creates / cleans up VMs, can target any boxlite home dir).

Use `raven sandbox` when you want to inspect / shell into the VMs
your live agent is currently using. Use `scripts/boxlite_cli.py` for
everything else (image management, post-mortem cleanup, cross-process
inspection, Raven-not-running scenarios).

## Where fixture generators live

Test / benchmark data generators are NOT in `scripts/` — they live
alongside the benchmark or test that consumes them. Examples:

- `benchmarks/skill_evals/fixtures/build_mock_library.py` — synthetic
  SQLite mass-library DB for `test_skill_forge_e2e.py` and
  `skill_evals/run_eval.py`.
