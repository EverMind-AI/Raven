# Launch WebUI

Use Raven's WebUI to chat with agents, coordinate tasks, and manage your
workspace in the browser. Follow task progress, inspect files and outputs,
and browse memory and skills from the same interface.

```bash
raven web
```

This command opens the WebUI in your browser and starts Raven in the
background. Raven keeps running after you close the browser. To stop the
service, run `raven web --stop`.

## Trajectory view

Once a conversation has content, a trajectory toggle appears in the chat
header, to the left of the workspace panel button. The trajectory view lists
every recorded step of that conversation (inputs, model calls, tool calls,
replies) with its timing, opens the details of any step, and draws a
duration bar above the list.

## Export and report a problem

The three-dot menu in the top-right corner of the chat header (the desk
launcher sits just left of it) acts on the open conversation. It is disabled on
a new task that has no content yet.

**Export** packs the turns you pick into a `.tar.gz` that the browser
downloads. A turn brings the sub-agents it started with it; a turn that is
still running cannot be picked. You can rename the file before exporting.

!!! warning
    The export is the raw trajectory and is not redacted. It can contain
    keys, tokens and other secrets: keep it private.

**Report a problem** appears only when Raven has a Bug Report Receiver to send
to (`RAVEN_BUGREPORT_URL`, or the built-in default address). It is the web
version of `raven trajectory report-bug`: pick the turns, describe the problem,
decide the values the redaction scan was unsure about (this page is skipped
when the redaction history already decided them all), then upload and get a
receipt. Every decision on that page, including "keep all" and the bulk
buttons, is remembered in the redaction history with `web` as its source.
Closing the wizard before the upload starts discards the report being
prepared. Once an upload has started the report is saved on this machine: if
the upload fails, a failure marked as retryable offers a retry, and either way
the saved report can be sent later with
`raven trajectory upload-bug-report <id>`, using the report id the page shows.
See
[Trajectory debugging](trajectory-debugging.md#produce-a-reviewed-report) for
the pipeline behind it.

For installation and provider setup, see [Quick Start](quick-start.md).
For Docker or source deployments, see [Self-Hosting](self-hosting.md).
