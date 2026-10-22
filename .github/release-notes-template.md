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

Windows PowerShell 5.1 (the version built into Windows) cannot follow that URL's
redirect and stops with `(308) Permanent Redirect`. If you see that error, use the
direct one instead:

```powershell
irm https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.ps1 | iex
```

The installer puts uv, Node.js 22, Raven and its plugins in place, downloads the
browser runtime and the word dictionary and PDF models the knowledge reader uses
(about 110 MB), installs or offers LibreOffice, and then finishes by opening
Raven in your browser. First-run setup happens on that page. The installer holds
the terminal while the page is up; press Ctrl-C to stop it, then start Raven
again with:

```bash
raven web
```

That keeps Raven running in the background and opens the page; `raven web --stop`
stops it. Bare `raven` does the same, and opens the TUI only where no browser
can be opened. Prefer the terminal? `raven tui` runs the same first-run setup and
opens the TUI, and `raven onboard` stays the explicit way to reconfigure later.
Set `RAVEN_MINIMAL=1` to skip the browser, model and LibreOffice downloads, or
`RAVEN_NO_LAUNCH=1` to have the installer return without opening the page. The
Windows installer does not download the dictionary and models; there, and after
`RAVEN_MINIMAL=1`, `raven resources` fetches them.

## Upgrade

Already running Raven? Upgrade in place -- configuration, sessions, and memory
are preserved. On a `raven web` install whose page is served by 0.2.3 or later,
the update row at the bottom of the sidebar does this for you, and the page
reloads itself, still signed in, once the new version is up. A page served by
0.2.2 or earlier refuses it; from the command line, stop the page first, then
upgrade:

```bash
raven web --stop
raven upgrade
```

On Linux and macOS `raven upgrade` runs the install in the foreground and
returns when it is done. On native Windows it hands the install to a separate
helper and returns at once; wait for the helper's completion message. Then
start Raven again:

```bash
raven web
```

`raven upgrade` installs the latest stable release together with the plugin
wheels it ships. It never picks up a pre-release unless this install has joined
the beta channel (a `~/.raven/beta.json` that only the beta installer writes).
`raven upgrade --check` reports whether a newer release exists without
installing it. Editable source checkouts are never overwritten: `raven upgrade`
reports the checkout path and how far it is ahead of or behind `origin/main`,
and the remedy is `git pull && ./install.sh` in the checkout. Rerunning the
one-line installer also upgrades, and ends on the running page.

## Release Status

- Version: `__VERSION__`
- Tag: `__TAG__`
- Stability: <!-- TODO: e.g. public preview patch / public preview minor -->
- Assets: the `raven` wheel and source distribution, four plugin wheels
  (`everos_memory`, `everos_cloud_memory`, `design_engine`, `ppt_engine`), the
  locked constraints file `raven-constraints.txt`, and the plugin list
  `raven-plugins.txt`, which installs every plugin wheel the release carries

## Notes

- Raven is still pre-1.0; CLI surfaces, plugin contracts, and runtime internals may continue to evolve.
- PyPI publishing is not enabled yet; the supported public install path uses the GitHub Release wheel asset.
