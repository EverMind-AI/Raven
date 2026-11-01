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
header, to the left of the workspace panel button. The trajectory view draws
every recorded step of that conversation (inputs, model calls, tool calls,
replies) on one clock-time timeline. The header sums the session up -- its
span, agents, turns, model and tool calls, and errors, with a link to the
first error -- and an activity strip shows its calls across the whole
session, with idle stretches folded. Below, each agent is one row: a capsule
for each stretch it worked, and how long it was busy. Open an agent to see
its turns, and a turn to see its steps; hover a turn to see what its time
went on, and click a step to open its details in a card you can move by its
top bar and resize from its corner (or focus the corner and use the arrow
keys). Pick Fit, 3h, 1h or 15m to set the
window, drag across the activity strip to choose one, or drag its frame
to move it; hold Ctrl (Cmd on macOS) and scroll to zoom, or hold Shift and
scroll to move along the timeline. Sub-agents with nothing in the window fold
into one row before it and one after, while the main agent always keeps a row
of its own. Press Replay to watch the conversation unfold again from its first
step, in 15 seconds at most -- a shorter one plays in its own time, and idle
stretches pass quickly -- while the button counts down; anything that starts
meanwhile follows at double speed, and pressing it again stops the replay.
A step that failed carries a red mark,
and so do the turn and the agent that hold it. While the trajectory view is
open the message box is hidden; switch back to the chat to send a message.
An approval or a question the conversation raises still shows over the
trajectory.

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
When the wizard opens it checks that the receiver answers. If it does not, the
wizard says why, and the report starts only when you choose **Continue
anyway**; **Check again** asks once more, for instance after the receiver is
back. Going on without it is safe: if the upload then fails, the report is kept
on this machine as described below.
Closing the wizard before the upload starts discards the report being
prepared. Once an upload has started the report is saved on this machine: if
the upload fails, a failure marked as retryable offers a retry, and either way
the saved report can be sent later with
`raven trajectory upload-bug-report <id>`, using the report id the page shows.
See
[Trajectory debugging](trajectory-debugging.md#produce-a-reviewed-report) for
the pipeline behind it.

## Import a conversation

**Import conversation**, in the left menu below New task and Connectors, opens
a package from another machine (or another Raven home) for reading. Pick the
file in the browser and it is uploaded to Raven, so this works whether you open
the WebUI locally or remotely. A file can be up to 100 MiB, and one import runs
at a time: starting another while one is in progress asks you to try again when
it finishes.

Three kinds of package can be imported: an export from the three-dot menu, a
bug report package (from **Report a problem** or
`raven trajectory report-bug`), and a trajectory report from
`raven trajectory report`. Any other file is refused with a message saying so.
A damaged or unsafe package (a path that leaves the package, a link, more data
unpacked than the limit allows) is refused and leaves nothing behind, and so is
one whose conversation record is too large to open (over 32 MiB or 50,000
entries). A package made by a newer Raven asks you to update first.

Imported packages are kept under `imports/` in the Raven home and stay there
across restarts. They are never added to the workspace's sessions. Importing
the same package again says it was already imported under its current title,
and offers **Open existing** or **Import anyway**.

Imported conversations are listed under **Imported**, below Recent; the group
is hidden while there are none. An export is titled after its original
conversation (or its first user input when that had no title), a bug report
package after the first line of its description (or its report id), and a
trajectory report after its original conversation (or its own id). Hovering a
row shows the file it came from. Hovering or right-clicking a row offers only
**Rename** and **Delete**; deleting also removes the copy kept on this machine.

An imported conversation is read-only. It reads like any other conversation,
and the trajectory view works as usual, including the full model input of a
long conversation. What differs: there is no input box, the title cannot be
edited in the chat header (rename it from the rail), replies have no Branch,
and the three-dot menu and the working-directory tag are not shown. Things a
package does not carry (a sub-agent's own transcript, attachments and delivered
files, task-graph runs, generated images) are shown as unavailable rather than
as errors. The `[REDACTED:...]` placeholders of a bug report package are shown
as they are.

An export is not redacted, so importing someone else's export gives you its raw
contents: keep it as private as they should have.

For installation and provider setup, see [Quick Start](quick-start.md).
For Docker or source deployments, see [Self-Hosting](self-hosting.md).
