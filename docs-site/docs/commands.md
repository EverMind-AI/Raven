# Command Reference

Use this reference to find the command for a task. Each command's help output
lists its subcommands and options.

| Command | Purpose |
| --- | --- |
| `raven` or `raven tui` | Launch the terminal UI |
| `raven web` | Open the WebUI and keep Raven running in the background |
| `raven web --stop` | Stop the background WebUI service |
| `raven agent -m "..."` | Run a single task from the command line |
| `raven onboard` | Set up model providers, sandboxing, messaging channels, memory, web tool credentials, subagents, and data import |
| `raven status` | Show configuration and runtime status |
| `raven doctor` | Diagnose provider and environment problems |
| `raven --version` | Show the installed Raven version |
| `raven upgrade --check` / `raven upgrade` | Check for updates or upgrade a managed installation |
| `raven agents new <name>` | Create a specialized agent from Raven's modular templates |
| `raven acp` | Serve Raven as an ACP agent over stdio |
| `raven a2a enable` / `raven a2a disable` | Enable or disable the gateway-mounted A2A face; restart the host afterward |
| `raven a2a serve` | Start a standalone A2A HTTP listener (loopback port 8710 by default) |
| `raven sessions` | Create, list, fork, export, or delete sessions; resolve session keys with `resume` |
| `raven playbook` | Create, validate, manage, and run reusable agent workflows |
| `raven provider` | Configure providers and endpoints, authenticate, test connectivity, and select the active model |
| `raven channels` | List, configure, authenticate, enable, or disable messaging channels |
| `raven gateway` | Start the gateway and its configured services |
| `raven gateway status` / `raven gateway reload` / `raven gateway stop` | Inspect, reload configuration, or gracefully stop a running gateway |
| `raven serve` | Start the WebSocket RPC service and serve the WebUI when its build is available |
| `raven skill` | Browse SkillForge skills, inspect their contents, block or unblock skills, and remove installed bundles |
| `raven plugins` | List installed plugins and the active memory backend |
| `raven plugin auth <server>` | Authenticate or refresh OAuth access for an MCP server |
| `raven mcp bridge <socket-path>` | Bridge a subagent's MCP connection over stdio to a host-managed server |
| `raven import` | Preview and import data from other AI tools, inspect progress, or stop an import |
| `raven deep-research` | Configure, inspect, or reset the MiroThinker research integration |
| `raven cron` | Create, inspect, run, enable, disable, or delete scheduled jobs |
| `raven sentinel` | Configure proactivity and inspect attention, routines, decisions, and nudges |
| `raven ops connection` | Register and list machines; `add` probes by default, while `doctor` validates registry entries |
| `raven sandbox` | List sandbox VMs, run commands, or open a shell; requires `tools.sandbox.debug.enabled=true` |
| `raven tracing` | Open the local trace dashboard |
| `raven tracing compact` | Consolidate duplicate trace artifacts to free disk space |
| `raven trajectory` | Save, replay, redact, label, and preserve execution trajectories for debugging |

Run `raven --help` for the command list, or `raven <command> --help` for details
on a specific command. From a source checkout, prefix commands with `uv run`.

## Find documentation by task

| Task | Read |
| --- | --- |
| Choose a conversation or one-shot workflow | [Using Raven](using-raven.md) |
| Configure shipped or external agents | [Agent Integrations](agent-integrations.md) |
| Continue or steer a specialist task | [Working with sub-agents](agent-collaboration.md) |
| Share browser work with the agent | [Browser collaboration](browser-collaboration.md) |
| Store and search documents | [Knowledge bases](knowledge.md) |
| Run and watch long tasks | [Long-running work (Oncall)](oncall.md) |
| Configure a messaging bot | [Channels and Messaging](channels.md) |
| Integrate an editor or remote agent | [Agent protocols](agent-protocols.md) |
| Run parallel tasks or inspect graph history | [DAG orchestration](orchestration.md) |
| Save and run a reusable workflow | [Playbooks](playbooks.md) |
| Evaluate harness changes against a benchmark | [Evolver: usage and experiments](evolver.md) |
| Configure approvals and unattended access | [Permissions and security](permissions.md) |
| Add knowledge, tools, or an agent | [Skills and extensions](skills-and-extensions.md) |
| Deploy the resident host | [Self-Hosting](self-hosting.md) |
| Create an agent or plugin | [Building an Agent](building-agent.md), [Building a Plugin](building-plugin.md) |
| Capture, redact, and replay a failure | [Trajectory debugging and replay](trajectory-debugging.md) |

Use site search to find a topic and `--help` to check command syntax.
For optional protocol features, consult the capabilities reported during
the handshake.

From the repository root, search the documentation and implementation together:

```bash
rg -n 'ACP|A2A|run_subagent_dag' docs-site/docs raven tests
```

`run_subagent_dag`, `dag_status`, `cancel_dag`, and `a2a_send` are model tools,
not shell subcommands. Use their guides for arguments and behaviour.
