# Browser and human collaboration

Raven's browser tools and WebUI Browser panel use the same Chromium in the
host process. You can see the page the agent is operating, complete a login
yourself, and let the agent continue from the resulting state.

This is browser automation, not unrestricted desktop control. Desktop apps
require a separate MCP integration and its own operating-system permissions.

## Enable the browser

For a source checkout:

```bash
uv sync --extra browser
uv run --extra browser playwright install chromium
```

Linux may also need Chromium's system libraries:

```bash
uv run --extra browser playwright install --with-deps chromium
```

The last command can install system dependencies; review it for your machine.
Managed installations have their own environment: use the command Raven
reports for that interpreter when the binary is missing. Run `raven doctor`,
then open [WebUI](webui.md). Installing the Python package alone does not
install a working browser binary.

The browser starts lazily on use. To open a native window on the host's desktop
when the agent first navigates, merge:

```json
{
  "tools": {
    "browser": {
      "headfulOnAgentUse": true
    }
  }
}
```

The default is false. This option needs a graphical desktop on the machine
running Raven; it does not open a window on an unrelated remote client.
Changes require a process restart. Popping the window out retains the browser
profile; closing a tab is not the same as clearing its login cookies.

## Try a read-only task

1. Open the Browser panel in the same host as your conversation.
2. Ask: “Open the documentation URL I supplied and summarize the installation
   prerequisites. Do not submit forms, download files, or change settings.”
3. Watch the selected tab and compare the answer with the actual page.
4. Ask for a screenshot or a fresh snapshot if the page changed before the
   agent continued.

`browser_snapshot` returns visible text and actionable element references.
Those references are regenerated on reads; a ref from an old snapshot should
not be treated as a stable selector.

## Hand a sensitive step to the human

For login, CAPTCHA, payment, or an unexpected consent page:

1. Have the agent stop and ask you before proceeding.
2. Perform the sensitive step yourself in the shared browser. Do not paste
   passwords, recovery codes, or tokens into the conversation.
3. Tell the agent it may continue, with the remaining allowed scope.
4. Have it take another snapshot before its next action.

User interactions are recorded as a touch; a later agent readback can report
that you interacted since its last action. This is coordination information,
not an automatic exclusive lock or a guaranteed pause of every active agent.

## Tabs and multiple agents

An owner is the active in-process sub-agent run, or otherwise the conversation.
It takes the current unowned tab or gets a fresh one. Subsequent calls stay
bound to that tab even if the panel displays another one.

An agent action brings its tab forward; a read alone does not. Another owner's
tab is marked held and cannot be activated or closed by that agent. Bindings
expire after ten idle minutes or when the tab closes. User panel interactions
are not restricted to a model's owner binding.

This shares one process's browser, not every browser in the deployment.
External ACP/CLI processes may have their own browser state. Do not promise
that an external Claude Code session, for example, uses the panel's Chromium.

## Permissions and network boundaries

| Model tools | Default policy |
| --- | --- |
| Navigate, snapshot, screenshot, scroll, tabs | Allow |
| Click, type, key press | Ask, with session grants grouped by site |

Check the effective [permission mode](permissions.md): `full` skips ordinary
ask-tier prompts. Site-scoped consent is broader than one button, and the
approval may show a ref and site rather than a human-readable element label.
Disable tools through `tools.disabledTools` or explicit permission rules when
they must not be available.

Browser navigation accepts HTTP(S) and its blank page, rejects dangerous
schemes and literal link-local targets. `RAVEN_BROWSER_BLOCK_PRIVATE=1` adds
private/loopback address checks. URL checks are not a network firewall or a
complete defense against DNS resolution, redirects, and all subresources.
Use network isolation for sensitive hosts.

Treat page content and screenshots as untrusted, potentially private data
sent to your configured model. Persistent browser profiles can contain
authenticated sessions. Use a dedicated account/profile and avoid sensitive
work in a broadly accessible Raven deployment.

## Troubleshooting and limits

| Symptom | Check |
| --- | --- |
| Browser tools absent | Browser extra, disabled tools, and which process/agent is serving the turn |
| Chromium fails to start | Browser binary, Linux libraries, and display availability for headful mode |
| Agent reads a different tab | Owner binding; the panel's active tab is not the agent's address |
| Snapshot refs no longer work | Read the current page and use new refs |
| No approval before an action | Effective mode, explicit allow rules, and previous site grant |
| Popup not reflected immediately | A script-created popup may be adopted on the next read |

The repository's real-browser tests cover local forms, tab ownership,
screenshots, and the loop/RPC path. They do not establish that every website's
authentication or anti-automation controls work. The driver lives in
`raven/browser/`; the repository's `docs/browser-and-desktop.md` records the
separate desktop integration and its verification limits.
