# Raven Desktop (macOS)

`~/Applications/Raven.app` is a small macOS window that shows the Raven web UI.
It exists because a browser tab is easy to lose among other tabs, and because
starting the gateway from a terminal every time gets old.

The app does not ship a second UI. On launch it runs `scripts/launch.sh`, which
asks the installed `raven` CLI for the one-time authenticated URL; the app loads
that URL into a `WKWebView`. The CLI is the only runtime dependency, and it owns
the whole gateway lifecycle: attaching to one that is already up, waiting for
one that is starting, and starting one when there is none.

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
It resolves the `raven` CLI from `PATH` (falling back to
`~/.local/bin/raven`) and runs one command:

```sh
raven web --print-url
```

That command attaches to a running gateway, waits for one that is starting, or
starts a resident one, then prints the one-time `/auth#<nonce>` URL on stdout.
Progress and errors go to stderr. The launcher never stops a gateway: an empty
probe can mean "still booting", and stopping it there would kill the launch it
was trying to join.

The app marks itself as `RavenShell` in the user agent and implements the
frontend's small message bridge: the page lifts the native splash with `ready`,
asks for a fresh sign-in link with `reauth` after a gateway restart, and reports
`theme`, `drag` and `zoom` for the hidden title bar. If WebKit's content process
dies, the app reconnects. Cmd-Shift-R reconnects manually. Links that leave
localhost go to the system browser. Closing the window quits the app but leaves
the gateway running.

## Layout

All of this lives under `desktop/macos/`. No existing upstream file is
modified, so syncing `main` from upstream cannot conflict with the desktop
work. The desktop branch is based on upstream `main` and can be rebased on it
whenever the gateway's auth behavior changes.

| File | What it does |
| --- | --- |
| `RavenDesktop.swift` | Window, menus, WKWebView, the `raven` message bridge and auth handling |
| `scripts/launch.sh` | Prints the auth URL through `raven web --print-url` |
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
The launcher's argument contract has a pytest (`tests/test_desktop_macos_launcher.py`)
that runs on every platform with a POSIX shell, and a macOS CI job typechecks
the Swift sources whenever `desktop/**` changes.

## Known limits

Only macOS. The app is ad hoc signed, not notarized, so Gatekeeper may require
Right-click -> Open on a machine that has never seen it. It assumes the gateway
serves an `/auth#<nonce>` URL as `raven web --print-url` does today; if that
contract changes, the launcher and the CLI flag need to change together.

The app itself stores no credentials, but WebKit keeps the gateway's HttpOnly
session cookie in its default data store, so the window stays signed in across
launches just as a browser tab does.

Finder launches an app with the login environment, not a shell profile. Provider
keys, proxy settings or a custom `RAVEN_HOME` exported from `.zshrc` do not
reach a gateway started this way; start the app from a terminal when those are
needed, or put the settings somewhere Raven reads at startup.
