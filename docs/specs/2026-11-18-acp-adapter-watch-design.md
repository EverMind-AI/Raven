# ACP adapter pin watch - design

Date: 2026-11-18
Status: designed
Base: `origin/main` at `c249c036c`.

## Goal

Tell the maintainers when an ACP adapter Raven pins has a newer release on
npm, without changing the pins itself.

The three shims are pinned to exact versions because the pin decides what
the agent offers: `codex-acp` builds its model menu from the codex build
it bundles, and `claude-agent-acp` carries the Claude Code CLI its SDK pin
names. Nothing watched npm, so a pin moved only when a person noticed,
and the Codex row kept a menu that stopped before gpt-6.2 until the
2026-11-18 bump.

## Design

- `scripts/check_acp_shim_pins.py` reads the three pins from the preset
  tables (`raven/agent/subagent/presets.py` and
  `raven/agent/subagent/acp_registry_presets.py`) and asks the npm
  registry for each package's `latest` tag. It prints a table and exits
  `1` when any pin is behind, `0` when every one is current, and `2` when
  npm cannot be read.
- `.github/workflows/acp-shim-watch.yml` runs the check daily, and on
  demand through `workflow_dispatch`. When the check reports a gap it
  creates or updates one open issue titled
  `chore(acp): adapter pins are behind npm`; when every pin is current it
  closes that issue with a comment.
- The issue is rolling: updated in place on each run, so one release
  produces one notification and the issue disappears when the pins move.
- No credentials are involved. The registry read is public and the issue
  write uses the workflow's own `GITHUB_TOKEN`.

## Non-goals

- No automatic bump and no pull request. A bump needs the handshake,
  model-menu and cross-version `session/load` measurements the preset
  docstring describes; those need a signed-in agent and have no place in
  CI. A bump opened from `GITHUB_TOKEN` would also start no CI run.
- The agent a shim bundles is not compared. The check reports the
  adapter package's own version; a newer adapter may still bundle an
  older codex, which the person bumping the pin measures.
- Pre-release tags are not read. `latest` is the only dist-tag asked
  for, so a `next` build is not a reminder.

## Decisions

- One issue rather than one per package: one release should not produce
  three notifications, and the current ones stay visible in the table.
- A registry failure fails the job and leaves any open issue alone, so a
  network problem never reads as "up to date".
- The check imports the preset tables rather than parsing the source
  text, so a pin that moves in the table moves the check with it and a
  test can hold the two together.

## Rollout

A scheduled workflow runs only from the default branch, so the first
check happens after this lands. GitHub disables a scheduled workflow
after 60 days without repository activity; a manual `workflow_dispatch`
run re-enables it.
