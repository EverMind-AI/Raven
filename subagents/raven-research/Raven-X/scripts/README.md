# Raven operational scripts

Standalone CLIs and admin utilities. Not part of the Raven wheel —
these scripts are invoked directly from a checkout and don't ship to
end users.

## Layout

```
scripts/
├── README.md                     This file.
├── boxlite_cli.py                Direct CLI for the boxlite microVM library.
├── everos_dr_smoke.py            Live raven-agent DR run -> deferred capture -> flush acceptance smoke.
├── everos_flush_batch.py         Post-batch flush of deferred-capture sessions recorded in workspace sidecars.
├── everos_memory_roundtrip.py    Real user-track store->extract->recall check against a live everos root.
├── everos_preflight.py           Pre-batch EverOS health / capability / defer-honored gate.
└── everos_skill_extract.py       Real agent-track weather-skill extraction + recall check.
```

## When to use

| Script | Purpose | See |
|---|---|---|
| `boxlite_cli.py` | Manage boxlite OCI images + VMs (pull / ls / create / start / stop / rm / shell). Independent of Raven — works even when no agent is running, can inspect VMs owned by another boxlite home. | [`docs/sandbox/boxlite_cli.md`](../docs/sandbox/boxlite_cli.md) |
| `everos_preflight.py` | Run before launching a batch that writes to EverOS: `/health` + capability check, v2/v1 mount probe, and a behavioral probe that a deferred `/add` really is buffer-only (released tags <= 1.2.3 silently ignore `defer_extraction` and run the boundary LLM in-request — version strings cannot catch this). Never starts or stops the service. Exit 0 = safe to launch. | — |
| `everos_flush_batch.py` | Run after the batch: read the `.everos_sessions.jsonl` sidecars the HTTP backend wrote into each workspace and `/flush` every recorded (session, app, project), then optionally wait for the cascade backlog to drain. Client budget via `--flush-timeout-s`; keep it above the server's 360 s `session_lock_timeout_seconds` (e.g. 420) to receive the server's own error envelope instead of disconnecting first. | — |
| `everos_dr_smoke.py` | Acceptance smoke for the whole deferred write path: launch a real `raven agent -m` DR run (production assembly chain: config -> plugin -> AgentLoop store dispatch), then assert sidecar, buffer contents, in-window extraction silence, and the post-hoc flush -> episode/case artifacts. Needs a base config with a working provider key and a live EverOS; costs one real DR task. Run once per environment or write-path change, not per batch. | — |
| `everos_memory_roundtrip.py` | Drive the embedded EverOS backend with a live Raven config against the real everos memory root: store a demo corpus, drain extraction, recall, and report where the user-track `user.md` landed. Verifies the `user_id`/`agent_id` wiring end-to-end. Needs network + a working everos LLM/embedding runtime; run from a normal (non-sandboxed) shell. | — |
| `everos_skill_extract.py` | Agent-track counterpart: store several verify-before-report weather tool trajectories, let everos distil a reusable `agent_skill`, and recall it via `EverosBackend.recall(agent_id=...)`. Reports the on-disk `agents/<agent_id>/skills/<skill>/SKILL.md`. Forces `EVEROS_MEMORIZE__MODE=agent`. Same runtime/network requirements. | — |

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
