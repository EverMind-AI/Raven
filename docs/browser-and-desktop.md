# Browser and desktop control

How the model operates a web page and a desktop, what it takes to turn each on,
and what has and has not been verified. Two independent lanes:

| Lane | Path | Ships with raven |
|---|---|---|
| Web pages | `browser_*` tools -> `raven.browser` driver -> the Chromium the Browser panel shows | yes (needs the `browser` extra) |
| Desktop apps | MCP client -> a desktop MCP server on the target computer -> the OS | catalog entry `macos-desktop` (macOS only for now) |

## 1. The shared browser

### What the model gets

Eight tools, registered by the main loop and by in-process sub-agents, withheld
from the schema while Playwright is not installed:

| Tool | Does | Default permission |
|---|---|---|
| `browser_navigate` | open a url, or back / forward / reload | allow |
| `browser_snapshot` | url, title, actionable elements with refs, visible text, console errors | allow |
| `browser_screenshot` | the page as an image block (JPEG, CSS-pixel coordinates) | allow |
| `browser_scroll` | wheel by dx/dy | allow |
| `browser_tabs` | list / new / activate / close | allow |
| `browser_click` | click a ref or an x/y point | **ask, once per site** |
| `browser_type` | fill a ref (or type into focus), optional Enter | **ask, once per site** |
| `browser_press` | a key or chord | **ask, once per site** |

Every acting call returns the page as it is afterwards (state + a compact
snapshot) so the next call can be aimed without a separate read. Refs are
re-numbered on every read; the tools say so.

The three acting tools carry the page's host into the call as `site` before the
permission gate reads it (`raven/permissions/builtin.py::session_keys`), so a
"for this session" approval of one click on `example.com` covers every later
click, type and key press on `example.com` in that conversation. To make them
free or forbidden, set `permissions.tools.browser_click: allow | deny` (and the
other two); `permissions.mode: full` skips the ask tier entirely.

### One browser, one tab per agent

The driver binds an **owner** to a tab on its first call: the sub-agent run in
flight when there is one, else the conversation. The owner takes the active tab
if nobody else holds it, otherwise a fresh tab. Its later calls land on that
tab whatever the panel is showing; each of its *acts* (not reads) brings the
tab to the front so the panel shows what the model is doing. Another owner's
tab is reported as `held` in `browser_tabs` and cannot be activated or closed
by the model. A binding lapses after 10 minutes idle (`OWNER_IDLE_S`) or when
its tab closes. A popup the owner's click opened becomes the owner's tab.

The reader's own hands (the panel's clicks, typing, tab switches, the address
bar) are calls with no owner. They are stamped as a **touch**, and the owner's
next readback carries `note: the user interacted with the browser since your
last action` until the owner acts again. The tools' descriptions tell the
model to hand a login / CAPTCHA / payment to the user through `ask_user`, then
read the page back with `browser_snapshot`.

### Enabling

```
uv sync --all-extras                 # source checkout: pulls playwright
uv run playwright install chromium   # the browser binary
```

On Linux add the system libraries Chromium links against, which the install
above does not bring: `uv run playwright install --with-deps chromium` (or
`playwright install-deps`). Without them Chromium is present and refuses to
start, naming a missing `libatk` / `libgtk` / `libnss3`.

On an installed raven: `<raven's python> -m playwright install chromium` (the
driver prints the exact line when Chromium is missing). Everything else is on
by default; turn it off with `tools.disabledTools: ["browser_navigate", ...]`.

To watch the model without opening the panel, set
`tools.browser.headfulOnAgentUse: true`: the model's first navigate (or new
tab) pops Chromium out as a real window on the desktop, the same relaunch the
panel's pop-out button does, with logins carried over in the persistent
profile. Off by default because a server has no desktop to pop into. Decided
once per process, so the reader can fold the window back into the panel
without the next call popping it out again; a change takes a restart.
Navigation policy (`raven/browser/policy.py`) refuses non-http(s) schemes and
link-local addresses; `RAVEN_BROWSER_BLOCK_PRIVATE=1` also refuses loopback and
private ranges.

### Verified

- `tests/test_browser_tools.py` (unit, no Chromium): admission, default tiers,
  site-keyed grants, owner identity, tab binding, popup follow, readback shape,
  image block.
- `tests/integration/test_browser_tools_real_web.py` (real Chromium, local
  server): a form filled and submitted through the tools; the screenshot as an
  image block; two owners in two tabs, neither able to take the other's; a read
  leaving the front tab alone, an act bringing the owner's tab forward; the
  reader's touch reported once; a `target=_blank` link landing the owner on the
  new tab; and a scripted model driving `AgentLoop`'s default tools with the
  permission gate asking once for the site, after which the panel's
  `browser.frame` RPC returns the page the model left.

### Not verified / limits

- Tested in one `raven serve`-shaped process (loop + RPC + Chromium). A
  sub-agent that runs as a *separate process* (the ACP / cli-agent lanes) has
  its own `Browser` singleton and would start its own Chromium on a throwaway
  profile; the browser tools are registered only for in-process
  (`raven-loop`) sub-agents.
- The panel was exercised at the RPC layer (`browser.state` / `browser.frame`),
  not by clicking in the web UI.
- A popup opened by a JavaScript handler (not a declared `target=_blank`) is
  adopted asynchronously: the click's readback may still describe the opener;
  the owner's next read lands on the popup.
- Approval prompts show the ref id and site, not the element's label.

## 2. Desktop control (macOS)

### Decision: reuse `macos-mcp`

Surveyed 2025-2026 open-source macOS desktop MCP servers (Peekaboo, cua-driver,
zavora computer-use-mcp, mediar macos-use, automation-mcp, AppleScript-only
servers). `macos-mcp` (CursorTouch/MacOS-MCP, MIT, PyPI) was chosen because it
is pure Python run by `uvx` with no build step, speaks the three transports
raven's MCP client already supports, returns screenshots as MCP image content
that `raven.agent.tools.media.blocks_from_mcp_content` already turns into
model image input, reads the accessibility tree into a numbered element list
with logical-point coordinates (Retina scaling handled on its side), and was
last released within the week of this note. Peekaboo is the stronger tool set
but needs a compiled Swift binary and Node; cua-driver requires its `.app`
daemon.

Nothing was written on the raven side beyond the catalog entry: the MCP
client, the tool registry, the permission gate (unknown MCP tools ask by
default) and the image path were already there.

### Tools the model gets (prefixed `mcp_<server>_`)

`Snapshot` (focused window, open apps, interactive elements `id|window|type|
name|(x,y)`, scrollable areas; `use_vision=true` adds an annotated PNG),
`Click`, `Type`, `Scroll`, `Move` (drag), `Shortcut`, `App` (launch / switch /
move / resize), `Desktop`, `Wait`, `Scrape`, `Shell` (shell or AppleScript),
`Notification`. The loop the model walks is observe (`Snapshot`) -> act
(`Click` / `Type` / `Shortcut` at the coordinates it read) -> observe again.

`Shell` runs arbitrary commands with the server's privileges. It is an MCP tool
and therefore on the ask tier by default; deny it outright with
`permissions.tools.mcp_macos-desktop_Shell: deny` where the agent should not
have it.

### Same machine

Install from the plugin market ("macOS Desktop Control") or add by hand:

```json
"tools": {
  "mcpServers": {
    "macos-desktop": {
      "type": "stdio",
      "command": "uvx",
      "args": ["macos-mcp==0.4.6", "serve", "--transport", "stdio"],
      "env": {"ANONYMIZED_TELEMETRY": "false"},
      "toolTimeout": 60
    }
  }
}
```

Permissions the server process needs, both under System Settings > Privacy &
Security:

- **Accessibility** -- the element list, clicks and typing. macOS shows its own
  "would like to control this computer" dialog the first time the server
  starts; approving it registers the *interpreter binary* `uvx` resolved
  (here `/opt/anaconda3/bin/python3.13`), which is why the version is pinned:
  a different resolution is a different binary and asks again. Do not add the
  interpreter by hand through the "+" picker.
- **Screen Recording** -- the pixels of other apps' windows. Without it
  `Snapshot(use_vision=true)` still returns an image, but it is the wallpaper
  with element boxes drawn where the windows are. This grant goes to the
  *responsible app*: the terminal or desktop app that launched `raven serve`.
  `macos-mcp`'s own check (`osascript`) does not detect this; the reliable
  probe is `Quartz.CGPreflightScreenCaptureAccess()` from the server's
  interpreter.

`uvx` must be on the PATH of the process that runs raven. A launchd service or
a desktop app does not read `~/.zshrc`; give it the full path
(`~/.local/bin/uvx`) in `command` or set `tools.exec.pathAppend`.

### Raven on a server, the desktop elsewhere

The server runs on the computer being operated and raven reaches it over
HTTP. `macos-mcp` ships the pieces:

```
# on the Mac, once
uvx macos-mcp==0.4.6 auth            # mints an auth key (optionally TLS certs)
uvx macos-mcp==0.4.6 install         # launchd agent, streamable-http on 127.0.0.1:8000
```

Expose the port to the raven host over something private (an SSH tunnel,
Tailscale) and point raven at it:

```json
"macos-desktop": {
  "type": "streamableHttp",
  "url": "http://127.0.0.1:8000/mcp",
  "headers": {"Authorization": "Bearer <the key>"},
  "toolTimeout": 60
}
```

`--allow-insecure-remote` exists and should not be used: a desktop-control
endpoint without a key is remote control of the machine for anyone who can
reach the port.

### Verified (2026-09-16, macOS 15, Apple Silicon)

- Through raven's own MCP client, stdio: handshake, 12 tools registered under
  `mcp_macos-desktop_*`, `Snapshot(use_vision=true)` returning text + a PNG
  image block (1429x929 after the server's 1080p cap).
- A repeatable real-app flow through the client: `App launch Calculator`,
  `App switch`, `Snapshot`, read the keypad from the element list, `Click`
  2 + 3 =, `Shortcut command+c`, `Shell pbpaste` -> `5`. Coordinates from the
  element list landed on the right keys (Retina scale handled).
- streamable-http with `--auth-key`: tools and image block arrive; a wrong
  bearer is refused (401).

### Not verified / limits

- **Screen Recording was not granted on the test machine**, so every
  screenshot in the runs above shows the wallpaper with correctly placed
  element boxes rather than window pixels. Everything else (element list,
  clicks, typing, result readback) was verified; pixel content is the one
  step that awaits the grant.
- A model was not put in the loop for the desktop lane; the flow above was
  scripted against the tools. The browser lane's scripted-model test is the
  template for adding one.
- The `Snapshot` element list is per *focused* app plus system UI: an app that
  is running but not frontmost contributes no elements. `App switch` first.
- App names are localized (`Calculator` launches, `计算器` is what `switch` and
  the element list call it on a Chinese system); the model has to read the
  name from `Snapshot` rather than assume.
- Text that is not an interactive element (Calculator's display) is not in the
  element list; read it from the screenshot, `Scrape`, or the clipboard.
- `macos-mcp` returns its text content as a JSON-encoded list of strings; the
  registry hands that string to the model as-is.
- Windows and Linux: no executor chosen. The same MCP-client path applies;
  candidates are `zavora-ai/computer-use-mcp` (cross-platform) and
  `mediar-ai/terminator` (Windows).
- No pause / take-over protocol on the desktop lane beyond what the tools
  give: the human owns the same mouse and keyboard, and the model's next
  `Snapshot` sees whatever they did.
