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

For installation and provider setup, see [Quick Start](quick-start.md).
For Docker or source deployments, see [Self-Hosting](self-hosting.md).
