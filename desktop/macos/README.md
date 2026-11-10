# Raven Desktop (macOS)

`~/Applications/Raven.app` is a small macOS window that shows the Raven web UI.
It exists because a browser tab is easy to lose among other tabs, and because
starting the gateway from a terminal every time gets old.

The app does not ship a second UI. On launch it runs `scripts/launch.sh`, which
makes sure `raven web` is running and prints the one-time authenticated URL;
the app loads that URL into a `WKWebView`. The `raven` CLI already installed on
the machine is the only runtime dependency.

```
Raven.app
  +-- Contents/Resources/scripts/launch.sh   starts/checks `raven web`
        `-- /auth#<nonce>
              `-- http://127.0.0.1:<port>/   the same page as `raven web`
```

## Build

```sh
cd desktop/macos
./build.sh
```

Needs macOS 12+, the Xcode Command Line Tools (`swiftc`, `sips`, `iconutil`)
and the `raven` CLI on `PATH` or at `~/.local/bin/raven`. The build compiles
the Swift source, renders `ui-web/src/assets/raven-mark.svg` into an `.icns`,
assembles the bundle and signs it ad hoc. Set `RAVEN_DESKTOP_APP` to build
somewhere other than `~/Applications/Raven.app`.

Build output goes into `desktop/macos/build/` and is ignored by git. Delete
`build/AppIcon.png` to force a re-render of the icon. On a Mac that has never
opened the app, Gatekeeper may require Right-click -> Open, or re-sign the
bundle with a Developer ID.

## What the launcher does

Finder launches apps with a short `PATH`, so the launcher adds `~/.local/bin`.
It resolves the `raven` CLI from `PATH` and reads its shebang to find the right
Python interpreter. The port comes from the gateway's own state file; nothing
is hard-coded. `BROWSER=/usr/bin/true` keeps `raven web` from opening a
browser tab, since this app is already the window.

If the gateway is not running, the launcher starts a resident one and waits for
its auth URL. If WebKit's content process dies, the app reconnects.
Cmd-Shift-R reconnects manually. Links that leave localhost go to the system
browser. Closing the window quits the app but leaves the gateway running.

## Layout

All of this lives under `desktop/macos/`. No existing upstream file is
modified, so syncing `main` from upstream cannot conflict with the desktop
work. The desktop branch is based on upstream `main` and can be rebased on it
whenever the gateway's auth behavior changes.

| File | What it does |
| --- | --- |
| `RavenDesktop.swift` | Window, menus, WKWebView, navigation and auth handling |
| `scripts/launch.sh` | Ensures a resident gateway and prints the auth URL |
| `build.sh` | Compiles, builds the icon, assembles and signs the bundle |
| `render_svg.swift` | Offscreen SVG to PNG, for cases where `qlmanage` is unavailable |

## Verification

The shell parts can be checked without the app:

```sh
cd desktop/macos
RAVEN_DESKTOP_APP=/tmp/Raven.app ./build.sh
/tmp/Raven.app/Contents/Resources/scripts/launch.sh   # prints an http://127.0.0.1:<port>/auth#... URL
open /tmp/Raven.app
```

The native window itself needs a Mac with a GUI, so it is not covered by CI.

## Known limits

Only macOS. The app is ad hoc signed, not notarized. It assumes the gateway
serves an `/auth#<nonce>` URL as `raven web` does today; if that contract
changes, `launch.sh` needs an update.
