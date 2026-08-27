<!-- TODO: one-line summary of this release -->

## Highlights

<!-- TODO: user-facing changes, one bullet each -->

## Install

New install on Linux, macOS, or WSL2:

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
```

New install on native Windows, in PowerShell:

```powershell
irm https://raven.evermind.ai/install.ps1 | iex
```

Windows PowerShell 5.1 (the version built into Windows) rejects that URL with
`Permanent Redirect`; use the direct one instead:

```powershell
irm https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.ps1 | iex
```

Open a new terminal, then run:

```bash
raven
```

That sets you up on first run and then opens the TUI. `raven onboard` stays
the explicit way to reconfigure later.

## Upgrade

Already running Raven? Upgrade in place -- configuration, sessions, and memory
are preserved:

```bash
raven upgrade
```

`raven upgrade` installs the latest stable release, so it does not pick up a
pre-release; rerun the installer above for that. Editable source checkouts are
never overwritten -- pull the checkout and rerun its development setup. On
native Windows the upgrade finishes in an external helper; wait for its
completion message before running Raven again.

## Release Status

- Version: `__VERSION__`
- Tag: `__TAG__`
- Stability: <!-- TODO: e.g. public preview patch / public preview minor -->
- Assets: wheel and source distribution attached to this release

## Notes

- Raven is still pre-1.0; CLI surfaces, plugin contracts, and runtime internals may continue to evolve.
- PyPI publishing is not enabled yet; the supported public install path uses the GitHub Release wheel asset.
