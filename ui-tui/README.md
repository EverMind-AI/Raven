# Raven TUI (ui-tui/)

Raven's native terminal UI: an Ink + React child process rendered straight to
the terminal. The Python parent (`raven tui`) spawns the bundled
`dist/entry.js` under Node and drives it over bidirectional JSON-RPC pipes;
the spawn, handshake and lifecycle mechanics are documented where they live,
in `raven/cli/tui_commands.py`, and the wire schema is
`rpc-schema/openrpc.json`. Domain terms for this tree are in
`ui-tui/CONTEXT.md`.

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

From the Raven repo root, with the project environment synced (`uv sync`):

```bash
raven tui --check     # smoke: boot subprocess then exit (exit code 0/1/2)
raven tui             # interactive: Ctrl+C to exit
raven tui --dev       # tsx watch mode via subprocess
```

## Attribution

Some scaffolding patterns (Node subprocess lifecycle, esbuild config, terminal
mode reset) reference hermes-agent (MIT, (c) 2025 Nous Research). See
`../NOTICES.md` and `../LICENSES/MIT-hermes-agent.txt` at repo root.
