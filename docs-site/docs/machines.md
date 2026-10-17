# Working on registered machines

Run a sub-agent on another computer -- a GPU box, a lab workstation, a
server in the office -- from the Raven on your own. You register the machine
once, put Raven's node on it, and name that machine in an agent entry. From
then on the agent is dispatched there, works in its own directory there, and
its files stay there; your conversation reads them by address and fetches one
when you ask. If the connection drops, or you stop Raven, the agent keeps
working and the result is picked up when you are back.

This page is the full walk-through. [Working with Sub-agents](agent-collaboration.md)
covers delegating in general; [Agent Integrations](agent-integrations.md) covers
installing the agents themselves.

## What you need

- A machine reached over ssh with a key, as the user the agent will run as.
  Raven never types a password: the login has to work with the key alone.
- Linux or macOS on that machine, with a POSIX shell for that user. The node
  uses unix sockets and process groups, so a Windows machine cannot be a
  destination. (Raven itself may run on Windows; see
  [Limits and defaults](#limits-and-defaults).)
- `uv` on the machine (on the PATH, or where its installer puts it), **or**
  `curl` or `wget` so the installer can fetch one. The installer builds the
  node with `uv` and stops without it, whatever Python the machine has. A
  Python 3.12 or newer there is welcome but not required: with one, the node's
  virtualenv is made from it; without one, `uv` brings a Python 3.12 of its
  own, which takes more space.
- The agent you want to run installed and signed in on that machine, for that
  user: an ACP agent such as Claude Code, Codex or Qwen Code; a CLI agent; or an
  OpenAI-compatible endpoint that answers on that machine's network.

## 1. Register the machine

```bash
raven ops connection add
```

The command asks one thing at a time: what you call the machine, whether it is
reached over ssh or is this computer, a short id for it, then the address,
port, username and private-key path. It connects before writing anything;
a machine that does not answer is not recorded. What the machine can say about
itself -- cores, memory, GPUs -- is read off it, not typed. The last questions
are for on-call jobs: what is installed on it (any short answer), the budget
unit and jobs at once (Enter takes the defaults), and the directories that hold
your work, which may be left blank. Directories you do list are also where a
file can be read without a question later (see [section 6](#6-read-edit-and-fetch-by-address)).

For a script, pass every answer as a flag:

```bash
raven ops connection add --id gpu1 --name "GPU box 1" \
  --host <address> --port 22 --user alice --key ~/.ssh/id_ed25519 \
  --non-interactive
```

What gets recorded, and where:

- The row lands in `connections.json`: the file `RAVEN_CONNECTIONS` points at,
  else the one beside your config, else the one in the Raven home (`~/.raven`).
  [Oncall](oncall.md#prepare-the-agent-and-machine) describes the registry in
  more detail.
- The **path** of the private key is stored, never the key itself.
- The machine's host key is accepted on the first connection and kept in
  `~/.ssh/known_hosts`. A machine that later presents a different key is
  refused until you check it yourself.
- A managed `Host <id>` block is written to `~/.ssh/config`, so `scp` and
  `rsync` on this computer can address the machine by its id.

`raven ops connection list` shows the machines. `raven ops connection doctor`
lists what is wrong with each entry, blocking or advisory, and exits non-zero
only when no entry is usable: a broken entry beside a usable one still exits
zero, so read its output rather than its exit code.

## 2. Install the node

```bash
raven ops connection install-node gpu1
```

The node is Raven's own code in a small virtualenv on the machine. It is what
starts agents there and answers file calls. The command looks at the machine
once, prints what it would do, and asks before doing it (`--yes` skips the
question). A successful run ends with two lines:

```text
Installed the Raven node <version>-<digest> in /home/alice/.raven-node (62 MB).
The node answers: Python 3.12.3, code <digest>.
```

What lands on the machine:

| Item | Where |
| --- | --- |
| The node | `<node_dir>/<version>-<digest>/`, with `node_dir` from the registry row, `~/.raven-node` by default |
| Its runtime state | `run/` (the socket), `tasks/<task>/` (each agent's record, owner-only) and `log/` under `node_dir` |
| `uv`, when the machine had none | `<node_dir>/.uv`, version 0.13.0, fetched with the machine's `curl` or `wget` |
| A Python 3.12, when the machine had none | beside the node, kept by `uv` |

Nothing else changes: no system package, no shell rc file, no environment
variable, no service, no open port. The node's packages come from the index
pip is configured with on the machine, else from PyPI. Space needed, as the
installer checks it:

| Part | About |
| --- | --- |
| The node | 60 MB |
| A Python 3.12 brought by `uv` | 80 MB more |
| `uv` itself, when fetched | 25 MB more |

`--dir <path>` keeps the nodes somewhere other than `~/.raven-node`; the path
is recorded in the registry row, so later commands find it. A machine that
already has this Raven's node is left as it is.

**Upgrading.** The node runs exactly the code of the Raven that installed it,
checked by a digest on every connection. After you update Raven on this
computer, run `install-node` again: the new node lands beside the old one, and
`--prune` removes the nodes of other versions from the directory (the cached
`uv` and Python stay). Until you do, a dispatch to that machine fails with
*runs different code from this Raven; update it with `raven ops connection
install-node gpu1`*.

**The daemon.** Nothing is started by the install. The first time Raven uses
the machine, its ssh session starts the node's daemon, which outlives the
session; the daemon leaves on its own after 10 minutes with no connection and
no agent running. After the machine reboots, the next use starts it again.

## 3. Name the machine in an agent entry

Take the entry you already have for the agent under `subagents.agents` in
your config -- the one the sub-agent page or `raven_config` added -- and add
`machine` to it. The value is the registry id. `remoteCwd` is where the agent works on that machine; it
defaults to `~/raven-work`, with `~` being the registered user's home there.
Each instance or task works in its own directory under it,
`<remoteCwd>/<instance or task id>`.

### ACP agents

```json
{
  "name": "Claude Code on gpu1",
  "preset": "claude_code",
  "kind": "acp",
  "enabled": true,
  "command": "<the agent's ACP command, as in a local entry>",
  "machine": "gpu1",
  "remoteCwd": "/data/alice/projects"
}
```

The node starts the entry's `command` on the machine, in the registered user's
login environment (the profile and rc chain of a `bash` or `zsh` login; a user
of another shell gets the environment ssh started the node with). The entry's
`env` is laid over it and reaches the agent over the link, never on a command
line. `cwd` names a directory on this computer and is not read for an entry on
a machine.

### CLI agents

```json
{
  "name": "my-cli on gpu1",
  "kind": "cli",
  "enabled": true,
  "command": "my-agent --prompt-file {prompt_file}",
  "machine": "gpu1"
}
```

The same command runs on the machine, split by that machine's POSIX shell
rules. The prompt reaches it the three ways it does locally: `{prompt}` as one
argument, `{prompt_file}` as a file written into the run's directory there,
or on stdin, which the node closes after the prompt. Output lines come back as
they are written, blank lines included. A cancel or a timeout ends the whole
process tree on the machine, background commands included.

### OpenAI-compatible endpoints

```json
{
  "name": "vLLM on gpu1",
  "kind": "openai",
  "enabled": true,
  "baseUrl": "http://127.0.0.1:8000/v1",
  "model": "my-model",
  "machine": "gpu1"
}
```

For an endpoint that answers only on the machine, or only on its network. Each
turn opens an ssh tunnel to the machine, sends the request through it to
`baseUrl` as the machine resolves it, and closes the tunnel. `baseUrl` has to
be `http`: the tunnel ends on this computer's loopback, which an `https`
certificate would not name. There is no `remoteCwd` for this kind -- an
endpoint has no working directory -- and one written here is dropped with a
warning.

### Built-in agents

A built-in agent is Raven's own loop in this process, where its model keys and
tools are. It has no process to start elsewhere: a `machine` on a built-in row
is dropped with a warning at load, and the row runs here.

## 4. Or add one from a conversation

Ask Raven in a chat, for example *"add the Codex preset on machine gpu1"*. The
main agent lists the usable registered machines and calls `subagents.add`
with the preset, the machine and, for an ACP or CLI preset, an optional
`remote_cwd`:

```json
{"preset": "codex", "machine": "gpu1", "remote_cwd": "/data/alice/projects"}
```

The entry is named `<preset>@<machine>` unless you give a name, so one preset
can be added once per machine. Before anything is stored, the add is proved
the way a local add is: one real test prompt, run on that machine, through
that agent -- so the agent has to be installed and signed in there already.
The add is refused up front when the preset is a built-in agent, when an
OpenAI preset is given a `remote_cwd`, or when the machine's entry cannot be
used.

The sub-agent page's **Add** does not take a machine yet; use the chat or the
config file for that.

## 5. What happens when you dispatch

Dispatch as usual: ask the host to delegate, or chat with the agent's instance.
Three things differ from a local agent.

**Attachments are named, not copied.** A file that comes with the task is
named to the agent by its address on this computer, with a note that nothing
was copied. The agent uses it where the machine can read it -- the same path
on a disk both mount, a copy already there -- or says in its reply that it
could not be found on the machine, and goes on.

**MCP servers are not lent.** The host's MCP servers are reached through a
socket on this computer, which an agent elsewhere cannot open. A granted
server is noted in the reply as not lent. Lending them across the link is
coming, in pull request #947.

**Its files stay on the machine.** Nothing is brought back when the turn
ends. The reply closes with a line that says where it worked:

```text
[raven] Ran on machine 'GPU box 1' (gpu1), in alice@203.0.113.7:/data/alice/projects/task-3f9a;
the files it wrote are there, not on this computer -- read_file, list_dir, find,
grep, write_file and edit_file take that path as it is, and deliver_files fetches one by it.
```

The record of the dispatch names the directory the same way: *Working
directory: on machine 'gpu1', /data/alice/projects/task-3f9a (made there when
the run started)*.

### How a path on a machine is written

A path on a machine is written the way `scp` writes one, with the user,
address and port from the registry. That is the one spelling a reply, a
record or a tool result uses, and the one the tools read back:

| Form | Example | When |
| --- | --- | --- |
| `user@host:/abs/path` | `alice@203.0.113.7:/data/alice/projects/task-3f9a/report.md` | port 22 |
| `scp://user@host:port//abs/path` | `scp://alice@203.0.113.7:2222//data/alice/projects/task-3f9a/report.md` | any other port; note the double slash before an absolute path |
| `<id>:/abs/path` | `gpu1:/data/alice/projects/task-3f9a/report.md` | what you may type yourself: the registry id works in place of the address |

A path after the colon that does not start with `/` is relative to the user's
home on the machine. A file on this computer whose name holds a colon is
written as `./a:b` or as an absolute path. A spelling that names no registered
machine is refused with a sentence that says so, before any ssh.

## 6. Read, edit and fetch by address

`read_file`, `list_dir`, `find`, `grep`, `write_file` and `edit_file` take a
path on a machine as readily as a local one. The call runs on the machine,
through its node, and only the answer travels: a read of 50 lines moves those
50 lines. Line numbers, limits and footers are the same as for a local file.

- **Reads ask no question** under the directories Raven's agents work in on
  that machine: `~/raven-work`, the `remoteCwd` of every entry on that
  machine, and the absolute `paths` listed on its registry row. A read
  elsewhere asks, as a read outside the workspace does here.
- **Writes always ask**, as they do here, and a grant is for the one file.
  A whole-file write checks that the file is still what this conversation last
  read; a file changed in between is left alone, with *changed after it was
  read; nothing was written. Read it again before writing.*
- The node confines every call to the directory the check allowed, resolving
  the machine's own symlinks.

`deliver_files` fetches one file by its address, so you can download it from
the page. The file lands under the conversation's workspace at
`pulled/<machine>/<key>/<name>`, the key being a digest of its path there, so
two paths never land on each other. Only a file under the directories the
agents work in is fetched, and only one of 20 MiB or less; a bigger file, or
one elsewhere, is read on the machine instead. `pulled/` is not cleaned up;
clear it when you no longer need what is in it.

In `exec`, an `ssh` typed to a registered machine's address is refused with a
pointer to the registered route; `scp` and `rsync` to it are fine, and can
address it by id thanks to the alias written at registration.

## 7. Disconnects and restarts

Raven keeps one ssh link per machine and watches it with a heartbeat: a ping
every 5 seconds, and 15 seconds without an answer means the link is gone. The
agents are not: they run under the machine's daemon, which keeps every frame
they write and every line of their stderr. Raven tries the link again on a
backoff of 2, 4, 8, then 16 seconds for as long as any agent is waiting, and a
link that comes back attaches each agent by its task id, from the frame and
the stderr line Raven last read. The turn goes on as if nothing had happened;
a cancel sent while the link was down is delivered when it is back.

Stopping Raven does the same on purpose. An agent running on a machine is left
to the daemon; what Raven keeps is a record under
`<raven home>/machines/runs/`. The next start reads those records, attaches
each run from where it was, reads what it wrote on stderr during that turn
too (a command's whole stderr: the reason it failed, or the session id a CLI
names only there), and announces the
result into the conversation that asked, as a finished turn. Keep the
machine's registration and the agent entry in place for that. `/restart` and a
Raven that was killed are covered the same way.

What is not recoverable: a machine that rebooted, or a daemon that was ended
by hand, no longer knows the task. The turn is then announced as an error
(*no longer runs 'agent' (task …)*), and nothing is left running there.

A link that has carried no call and run no agent for 10 minutes is closed
the next time Raven uses another machine; there is no timer, so the last link
in use stays open until then. The daemon does keep a timer: it leaves after
10 minutes with no connection and no agent. Neither is an error; the next use
starts them again.

## Limits and defaults

| What | Value |
| --- | --- |
| Agents running at once on one machine | 8; a ninth launch is refused with *already runs 8 agents; wait for one to finish* |
| A finished task's record on the machine | kept 7 days |
| Heartbeat / link lost after | 5 s / 15 s |
| Reconnect backoff | 2, 4, 8, 16 s |
| Idle link closed | after 10 min without a call or an agent, at the next use of another machine; no timer |
| Daemon leaves | after 10 min with no connection and no agent; a timer |
| ssh connect timeout | 15 s |
| A file call's answer | within 120 s, or the call fails |
| A file fetched by `deliver_files` | 20 MiB at most |
| Default working directory on the machine | `~/raven-work` |
| Default node directory | `~/.raven-node` |
| Ways to send the prompt to a CLI agent there | `{prompt}`, `{prompt_file}`, stdin |

The registry row's `concurrency` and `budget_unit` govern on-call jobs, not
these agents.

**Windows.** The machine has to be Linux or macOS. Raven itself may run on
Windows: there, a drive-relative path such as `C:file` is read as a local path
and never as a machine named `C`, and a fetched file whose name holds a
character Windows reserves lands with that character written as a percent
code.

## Troubleshooting

Every failure is one sentence that names the machine by its id and display
name, never by its address. ssh's own messages, which do name the address, go
to an owner-only log under the Raven home (`logs/ssh/`).

| The sentence says | It means | Do this |
| --- | --- | --- |
| *is not registered; add it with `raven ops connection add`* | No row has that id | Check the id in the entry against `raven ops connection list` |
| *is registered, but its entry cannot be used; run `raven ops connection doctor`* | The row is incomplete or its id is listed twice | Run `doctor` and fix what it names |
| *is this computer; remove `machine` from the agent's entry* | The row is a `local` transport | Drop `machine`; the agent runs here |
| *refused the login; check the key it is registered with* | ssh's key login failed | Try `ssh -i <key> user@host` by hand; re-add if the key changed |
| *presented a host key that differs from the one on record* | The machine's host key changed | Check the machine, then clear its `known_hosts` line |
| *could not be reached* / *did not answer within the connection timeout* | Network or address | Check address, port and whether sshd is up |
| *is not installed on machine …; install it with `raven ops connection install-node …`* | No node there | Run the install |
| *runs different code from this Raven; update it with …* | The node is from another Raven version | Run `install-node` again |
| *has no uv, and no curl or wget to fetch one with* | The installer needs `uv`, and the machine has neither it nor a way to fetch it; a Python there does not change this | Install `uv` on the machine (the sentence names its installer), or `curl` or `wget`, then retry |
| *the agent's command was not found on machine …* | The agent is not installed there for that user | Install and sign in on the machine |
| *already runs 8 agents; wait for one to finish* | The machine is at its limit | Wait, or use another machine |
| *did not answer a heartbeat within 15s* | The link dropped | Nothing; Raven reconnects. The agent keeps running |
| *no longer runs 'agent' (task …)* | The daemon lost the task (reboot, killed) | Dispatch again |
| *changed after it was read; nothing was written* | Someone changed the file since this conversation read it | Read it again, then write |
| *is over the 20.0 MB limit for a file brought back* | Too large for `deliver_files` | Read it on the machine, or copy it with `scp` |
| *an endpoint reached through the machine has to be http* | `baseUrl` is `https` | Use the `http` address the machine reaches it at |
| *this command reaches … over raw ssh* | A typed `ssh` to a registered address in `exec` | Use `exec(machine=…)`, or the file tools |

## Security

- The private key stays on this computer; the registry holds its path. The
  ssh command line built from the row is held in memory and written to no
  config, log or model-visible text.
- The user, address and port do reach a model, because they name every file on
  the machine (the `scp` spelling above). The key and the ssh line never do.
- The node opens no network port. It listens on a socket under its own
  directory, owned by the registered user, and only that user's ssh session
  reaches it. Each task's record there is readable by that user alone.
- The node runs file calls with Raven's own tools, fenced to the directory the
  permission check allowed. It holds no conversation and calls no model;
  nothing of your model keys goes to the machine.
- An agent on a machine is that machine's install, signed in there. Raven
  lends it no key of its own: `lendKeys` on an entry that names a machine
  lends nothing. The entry's `env` is the one thing of yours it receives, over
  the link.
