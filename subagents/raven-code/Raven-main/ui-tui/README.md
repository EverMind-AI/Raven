# Raven TUI (ui-tui/)

Raven's native TUI subprocess. Spawned by Python `raven tui` command.
Renders Ink + React directly to the terminal.

## Status: bootstrap (L2-α `tui-bootstrap`)

Currently only renders a "Hello Raven TUI · Ctrl+C to exit" smoke screen.
Real business UI arrives in subsequent L2s (`tui-fork-hermes-import` pulls
hermes ui-tui wholesale; `tui-ipc-bridge` connects Python business layer;
case1/case3 集 adapt and test). See `../docs/openspec/changes/tui-bootstrap/`.

## Development

```bash
cd ui-tui
npm install              # one-time
npm run dev              # tsx watch (no build)
npm run build            # bundle to dist/entry.js
npm run test             # vitest
npm run type-check       # tsc strict
npm run lint             # eslint
```

From the Raven repo root, after `uv pip install -e .`:

```bash
raven tui --check     # smoke: boot subprocess then exit (exit code 0/1/2)
raven tui             # interactive: Ctrl+C to exit
raven tui --dev       # tsx watch mode via subprocess
```

## Architecture

Python parent → `subprocess.Popen([node, dist/entry.js])` → Ink/React in child.
Signal handling (SIGINT/SIGTERM/SIGHUP) forwarded parent → child with 5s
escalation to SIGKILL. Child exit code is propagated as parent exit code.

No IPC in this L2; bidirectional JSON-RPC arrives in `tui-ipc-bridge` L2.

## Attribution

Some scaffolding patterns (Node subprocess lifecycle, esbuild config, terminal
mode reset) reference hermes-agent (MIT, © 2025 Nous Research). See
`../NOTICES.md` and `../LICENSES/MIT-hermes-agent.txt` at repo root.
