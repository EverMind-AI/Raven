# Raven VS Code Extension

Open the [Raven](https://github.com/EverMind-AI/Raven) TUI inside the VS Code
integrated terminal, mirroring the OpenCode extension model.

## Features

- Editor title button next to the active file tab plus the command palette
  entry **Raven: Open TUI**
- Launches `raven tui` in a dedicated integrated terminal named Raven; the
  terminal process is raven itself, so arguments pass through argv with no
  host-shell expansion
- Reuses the running terminal; recreates it after it closes
- Resolves the raven executable automatically: `raven.executablePath` or
  `RAVEN_BIN` (`~` expanded), then `which` / `where`, then a bash/zsh login
  shell probe (for GUI-launched VS Code that lacks the shell rc PATH), then
  `uv run raven`

## Requirements

- [Raven](https://github.com/EverMind-AI/Raven) installed (`raven` CLI on
  PATH), or set the `raven.executablePath` setting

## Settings

- `raven.executablePath`: absolute path to the raven executable (`~` is
  expanded). Empty by default; resolution falls back to PATH and uv.
- `raven.extraArgs`: extra arguments passed after `raven tui`.

## Development

```bash
npm install
npm run build
```

Then press F5 in VS Code to launch the extension development host, and run
**Raven: Open TUI** there.

## License

Apache-2.0
