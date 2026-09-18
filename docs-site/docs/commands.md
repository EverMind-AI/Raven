# Command Reference

| Command | Purpose |
| --- | --- |
| `raven` or `raven tui` | Launch the terminal UI |
| `raven web` | Open the WebUI and keep Raven running in the background |
| `raven web --stop` | Stop the background WebUI service |
| `raven agent -m "..."` | Run a one-shot task |
| `raven onboard` | Configure providers, sandboxing, channels, memory, web tool keys, sub-agents, and import |
| `raven status` | Show configuration and runtime status |
| `raven doctor` | Diagnose provider and environment problems |
| `raven --version` | Show the installed Raven version |
| `raven upgrade --check` / `raven upgrade` | Check for updates or upgrade a managed installation |
| `raven agents new <name>` | Create a specialized agent from Raven's modular templates |
| `raven acp` | Serve Raven as an ACP agent over stdio |
| `raven sessions` | Create, list, fork, export, or delete sessions; resolve session keys with `resume` |
| `raven playbook` | Create, validate, manage, and run reusable agent workflows |
| `raven provider` | Configure providers and endpoints, authenticate, test connectivity, and select the active model |
| `raven channels` | List, configure, authenticate, enable, or disable messaging channels |
| `raven gateway` | Run messaging gateways |
| `raven gateway status` / `raven gateway reload` / `raven gateway stop` | Inspect, reload configuration, or gracefully stop a running gateway |
| `raven serve` | Run the headless WebSocket RPC service, serving the WebUI when available |
| `raven skill` | Browse SkillForge skills, inspect their contents, block or unblock skills, and remove installed bundles |
| `raven plugins` | List installed plugins and the active memory backend |
| `raven plugin auth <server>` | Authenticate or refresh OAuth access for an MCP server |
| `raven mcp bridge <socket-path>` | Bridge a subagent's MCP connection over stdio to a host-managed server |
| `raven import` | Preview and import data from other AI tools, inspect progress, or stop an import |
| `raven deep-research` | Configure, inspect, or reset the MiroThinker research integration |
| `raven cron` | Create, inspect, run, enable, disable, or delete scheduled jobs |
| `raven sentinel` | Configure proactivity and inspect attention, routines, decisions, and nudges |
| `raven ops connection` | Register local or remote machines, list them, and check connectivity |
| `raven sandbox` | List sandbox VMs, run commands, or open a shell; requires `sandbox.debug=true` |
| `raven tracing` | Open the local trace dashboard |
| `raven tracing compact` | Fold duplicate trace artifacts to reclaim disk space |
| `raven trajectory` | Save, replay, redact, label, and preserve execution trajectories for debugging |

Run `raven --help` or `raven <command> --help` for the complete CLI surface.
