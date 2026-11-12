# Permissions and security

Permission, authentication, and isolation answer different questions:
**may this action run**, **who is calling**, and **what can the process reach**.
Configure all three. Neither an authenticated A2A request nor a sandbox replaces
the Permission Gate.

## Decision order

The tool registry checks the Permission Gate before dispatching a tool:

1. Built-in rulings deny catastrophe-class commands, including recursive
   deletion of a root or home tree.
2. User rules in `permissions.tools` choose `deny`, `allow`, or `ask`.
3. An unmatched call uses its tool's default tier.
4. The current permission mode interprets the `ask` tier.

Read-only tools and recognized read-only local shell commands generally default
to allow. Mutations and unknown MCP tools generally default to ask. There are
explicit allow-default exceptions, such as `deliver_files`, which hands a file
to the requesting user. A user rule still overrides a default. Do not assume a
tool is denied merely because it is absent from the config.

## Modes

| `permissions.mode` | How an ask-tier call is handled |
| --- | --- |
| `ask` | Ask the interactive user |
| `smart` (default) | A model reviewer allows or escalates to the user |
| `full` | Run without asking |

All modes retain built-in denials and user deny rules. Smart review is not a
guarantee of safety: use explicit deny rules for prohibited operations. If the
reviewer fails or times out, the call escalates rather than being auto-approved.
If no approval responder is available, a still-asking call is refused.
`raven agent -m` never has one: after the reply it lists every refused call,
and exits with status 3 when any of them needed approval, so an unattended
driver can tell a run that skipped its changes from one that made them. It
also lists questions nobody could answer. Those do not change the exit
status, because the turn continued with its best judgment. Pass
`--permission-mode full` when a one-shot must mutate.

The global config supplies the starting mode. A session can override it
through session-scoped `config.set`; that mode is saved with the session.
Check the session's mode as well as the global value during troubleshooting.

## Configure tools and shell prefixes

Merge a fragment like this into your existing configuration:

```json
{
  "permissions": {
    "mode": "ask",
    "tools": {
      "exec": {
        "*": "ask",
        "git status *": "allow",
        "git diff *": "allow",
        "git push *": "deny"
      },
      "write_file": "ask",
      "edit_file": "ask",
      "a2a_send": "ask"
    }
  }
}
```

Only `exec` has the command-prefix table. Other entries name a tool and a tier;
this is not an arbitrary path-glob ACL. The example blocks an ordinary direct
`git push`, not every possible way a shell program could publish data.

Shell matching follows these rules:

- A specific pattern matches token prefixes (`git status *` also matches
  `git status`). Several matching **specific** patterns resolve strictest-wins:
  `deny > ask > allow`, independent of their order.
- `*` is used only when no specific pattern applies. Thus `* = ask` does not
  prevent a specific `git status * = allow` from working.
- Compound commands allow only when every segment allows; a denied segment
  denies the call. `git *` does not implicitly authorize `sudo git ...`.
- Command/process substitution, backticks, heredocs, or unconfined redirection
  may leave only the fallback rule applicable to `allow` and `ask` patterns.
  This is conservative parsing, not a proof about every program a command can
  launch.
- A `deny` pattern is asked about every command the string runs, before any of
  that: behind a wrapper (`sudo`, `env`, `bash -c`, `xargs`, `doas`, `watch`),
  inside a substitution, after a shell keyword, or under a path
  (`/usr/bin/curl`). So `curl * = deny` also refuses `bash -c "curl ..."` and
  `curl ... > /tmp/out`. It cannot see a command a program builds for itself
  (`python -c`, a script file, an alias).

Unlike OpenCode's ordered rules, Raven does **not** use last-match-wins. Do not
paste another product's permission schema or precedence into Raven. Avoid
`"*": "allow"` unless broad unattended execution is intentional.

## Approval scope

| Choice | Scope |
| --- | --- |
| Allow once | The pending action |
| Allow for this session | Still-asking action keys in that session |
| Don't ask again | A validated, user-confirmed `exec` prefix persisted in config |
| Deny | Refuse the action; normally the turn can continue |
| Deny and stop | Refuse and end the current turn |

On a WebUI approval card, Esc means Deny and leaves the turn running so the
agent can respond to the refusal. A later Esc follows the page's normal close
order and stops the turn when no other layer is open.

For shell commands, session grants include the command segment, machine, and
working directory. Built-in file writers key by path and working directory;
browser acting tools share a per-site key. Other tools generally key the exact
call. Session grants live in memory; they are not the persisted global prefix
rules. Neither grant overrides a deny. Expired or denied actions are not asked
again unchanged in the same turn.

## Network

What a sandboxed command may reach belongs to the same permission set, under
`permissions.network`. Every sandbox Raven starts reads it: the main agent's,
a sub-agent's, a DAG node's, and a sandboxed MCP server's. A change to it
reaches a running sandbox before that sandbox's next command.

```json
{
  "permissions": {
    "network": {
      "default": "open",
      "rules": [
        { "host": "git.internal.example.com", "ports": [443] },
        { "host": "pastebin.com", "tier": "deny" }
      ]
    }
  }
}
```

| Key | Meaning |
| --- | --- |
| `default` | `open` (the default): every host name of three or more labels on every port, plus common package, code-hosting and model-provider names on common ports. `closed`: only what a rule allows. |
| `rules[].host` | A host name. `*.example.com` matches one label, `**.example.com` any number. |
| `rules[].ports` | Ports. Left empty, an allow rule takes 22, 80, 443, 8080, 8443 and 9418, and a deny rule refuses every port. |
| `rules[].binary` | The program, by its absolute path in the sandbox, such as `/usr/bin/curl`. Left empty, any program. |
| `rules[].tier` | `allow` (the default) or `deny`. |

A config that still sets `tools.sandbox.allowNet` has it moved here on its
first load, with a notice: `true` is the open default, `false` is closed, a
host list allows those hosts on ports 80 and 443 with the default closed, and
a list with `"*"` keeps the open default and adds the other hosts.

### When a connection is blocked

A sandbox refuses a connection no rule allows while the command is running.
Raven reads the refusal from the gateway and settles it the way it settles a
tool call:

- A target one of your deny rules names is refused without asking.
- In `ask` mode you are asked, with the program, the address and the command.
- In `smart` mode the reviewer is asked first. It allows a target the command
  plainly needs, such as a package index for an install or the repository
  host for a clone, and hands anything else to you.
- In `full` mode the target is opened for the session.
- With nobody to ask, as in a scheduled job or `raven agent -m`, the target
  stays blocked, the tool result says so, and `-m` exits with status 3.

An address given as an IP, or an internal name such as `localhost` or
`metadata.google.internal`, is never opened by the mode or by the reviewer:
only you can allow it. After an approval Raven does not run the command again
by itself; the agent is told it may. When several agents of one conversation
hit the same address at once, you are asked once.

| Choice | Where it opens the address |
| --- | --- |
| Allow once | The sandbox that was refused, until it stops |
| Allow for this session | Every sandbox of the conversation, until Raven restarts |
| A rule in `permissions.network` | Every sandbox, from then on |

The main agent's sandbox is shared by every conversation the gateway serves,
so what is allowed in it reaches all of them until Raven restarts. The
approval card says so when it applies.

A sandboxed MCP server is handled the same way, for the connections refused
while one of its tools ran. The card names "a process in the sandbox", since
the gateway does not say which process made a connection.

### Limits

- Under the `open` default a deny rule takes a two-label host such as
  `pastebin.com` out of the open set, but cannot block a host of three or more
  labels: OpenShell cannot carve an exception out of the rule that opens them.
  Such a rule still stops Raven from opening the host, and Raven warns when it
  loads one. Use `default: closed` when a host must be unreachable.
- The design engine's renderer runs Raven's own rendering job, not a command
  an agent wrote, and stays offline. It has no network setting of its own.
- An approved address takes about 10 seconds to reach a running sandbox with
  the gateway's default delivery. See [Sandbox](sandbox.md#41-network-policy)
  for the gateway setting that makes it about a second.

## Delegation is a separate trust boundary

Raven's gate governs its own registry calls, not every command an external agent
might execute inside its process.

- **Inbound ACP:** Raven can request permission from its editor/host through
  `session/request_permission`.
- **Outbound ACP:** Raven's unattended client automatically prefers the
  child's `allow_always` or `allow_once` option. Enabling this agent is not a
  promise of human review for each internal operation. Your refusals do
  travel: a request whose shell command matches your `deny` rules or
  `tools.exec.extraDenyPatterns` is answered with the child's reject option.
  Raven's own agents (Raven-Code and its siblings) run under your whole
  permission set, the mode and the ask, allow, deny and network rules, and,
  where your sandbox is OpenShell on Linux or macOS, in the same kind of
  sandbox. A third-party agent is held only for what it asks about, and a
  rule you add reaches an already running Raven agent when it is next
  launched.
- **CLI and other backends:** inspect their command, environment, working
  directory, and native permission policy. Host permission rules are not
  automatically inherited across every backend.
- **A2A:** the shared bearer token admits a remote operator. The host's tool
  gate still applies, but the current inbound A2A turn has no tool-approval
  responder. Its question broker is not a permission UI.
- **DAG:** `confirm: true` requests approval for the declared graph, not for
  every action discovered later. Without an ask channel, the current implementation
  logs a notice and proceeds without confirmation; this flag is not a fail-closed
  security control. Skill/MCP selection depends on backend capability;
  a downgrade notice means it is not a reliable enforcement boundary there.

Prefer narrow credentials, an isolated account or container, and explicitly
partitioned working files when allowing unattended child agents.

## Sandbox, files, and untrusted content

Sandboxing controls execution placement. An OpenShell sandbox mounts the real
workspace read-write; deleting inside that mount deletes real files. The gate
therefore does not auto-approve a command simply because a sandbox exists.
Third-party backends may not use Raven's executor. See [Sandbox](sandbox.md).

Working-directory protections, DAG reference confinement, channel sender
allowlists, and network restrictions are additional controls with different
scopes. Prompt-injection labels mark tool results and sub-agent output as
untrusted data; they do not make model judgement infallible. Check credentials
and network reach separately from prompt instructions.

ACP redacts recognized secrets in outgoing arguments and permission
descriptions, and in the errors Raven itself composes. Content that comes back
from another agent -- a sub-agent's answer, a Mirasim turn, an A2A peer's reply
-- is kept as that agent sent it, on the policy that the agent producing it
decides what may be redacted; a credential such a reply echoes is therefore
left in place. A2A turn failures expose a fixed message and retain the detailed
exception in local logs. Neither mechanism guarantees every log or
transcript is safe to publish. Keep secrets out of task prompts where possible,
and review/redact diagnostic exports before sharing.

## Diagnose a refused or stalled action

| Symptom | Check |
| --- | --- |
| `full` still refuses a command | Built-in or user deny; changing mode does not override it |
| Smart mode asks unexpectedly | Reviewer escalation, timeout, or unavailable provider |
| Headless task refuses a mutation | No approval responder; configure narrowly before launching |
| A compound command asks | Every segment and redirection must be covered |
| A previous grant no longer applies | Different directory, machine, site, session, or process restart |
| A child acted without asking | Outbound ACP/native child policy, not just the host gate |

Inspect `permission.decision`, `permission.source`, and smart reviewer fields
on the tool-call trace span. Implementation entry points are
`raven/permissions/gate.py`, `raven/permissions/rules.py`,
`raven/permissions/session.py`, and `raven/acp_client/permissions.py`.
