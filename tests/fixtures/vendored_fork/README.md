# vendored_fork -- the frozen A side, as fixtures

Byte snapshots taken from `subagents/raven-research` at the record commit of
upstream `a903a424` (dr@3.5), immediately before the vendored tree was retired
from the working copy. They keep the parity guards in `tests/` honest without
carrying the tree:

- `run.py`, `config.json`, `subagent.json` -- the launcher trio the manifest pins.
- `fetch_gate.py` -- `Raven-X/raven/agent/fetch_gate.py`, the member-ledger record.
- `research_parity_probe.json` -- the output of the parity probe run inside the
  fork checkout (each mode's effective flow through the fork's own schema and
  `build_session_modes`, a deliberately broken baseline, and the fork's
  UNIVERSAL/INert keys). A frozen tree's measurement is a constant; the probe
  source lives in the pre-retirement history of
  `tests/test_agents_research_flow_parity.py`, and the tree itself in the git
  history of `subagents/`.

Do not edit these files: they are records, and every guard that reads them
treats a difference as drift on the twin's side, not on this one.
