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

The installer puts uv, Node.js 22, Raven and its plugins in place, downloads the
browser runtime, installs or offers LibreOffice, and then finishes by opening
Raven in your browser. First-run setup happens on that page. The installer holds
the terminal while the page is up; press Ctrl-C to stop it, then start Raven
again with:

```bash
raven web
```

That keeps Raven running in the background and opens the page; `raven web --stop`
stops it. Prefer the terminal? `raven` runs the same first-run setup and opens
the TUI, and `raven onboard` stays the explicit way to reconfigure later. Set
`RAVEN_MINIMAL=1` to skip the browser and LibreOffice downloads, or
`RAVEN_NO_LAUNCH=1` to have the installer return without opening the page.

## Upgrade

Already running Raven? Upgrade in place -- configuration, sessions, and memory
are preserved:

```bash
raven web --stop
raven upgrade
raven web
```

`raven upgrade` installs the latest stable release together with the plugin
wheels it ships, so it never picks up a pre-release. The install runs in a
detached helper on every platform; wait for its `Raven upgraded` line before
starting Raven again. `raven upgrade --check` reports whether a newer release
exists without installing it. Editable source checkouts are never overwritten:
`raven upgrade` reports the checkout path and how far it is ahead of or behind
`origin/main`, and the remedy is `git pull && ./install.sh` in the checkout.
Rerunning the one-line installer also upgrades, and ends on the running page.

## Release Status

- Version: `__VERSION__`
- Tag: `__TAG__`
- Stability: <!-- TODO: e.g. public preview patch / public preview minor -->
- Assets: the `raven` wheel and source distribution, the three plugin wheels
  (`everos_memory`, `design_engine`, `ppt_engine`), the locked constraints file
  `raven-constraints.txt`, and the plugin list `raven-plugins.txt`

## Notes

- Raven is still pre-1.0; CLI surfaces, plugin contracts, and runtime internals may continue to evolve.
- PyPI publishing is not enabled yet; the supported public install path uses the GitHub Release wheel asset.
