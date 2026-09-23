# Skills, memory, and extensions

This is a usage guide. To implement an extension, continue to
[Building a Plugin](building-plugin.md) or [Building an Agent](building-agent.md).
To connect an existing product, use [Agent Integrations](agent-integrations.md).

An instruction, a tool, a remembered fact, and an agent are different kinds of
capability. Choose the smallest extension that provides what the task lacks.

## Capability map

| Component | Provides | Does not imply |
| --- | --- | --- |
| Skill | A discoverable procedure, possibly with scripts and reference files | A new runtime tool or unrestricted execution |
| SkillForge | Retrieval from local skills, memory-backed experience, and Skill Hub | Autonomous source-code changes |
| Memory Engine / EverOS | Persistence and recall across conversations | Every past message stays in the model context |
| MCP server | Tools, resources, or prompts exposed by an external service | An independent agent session |
| Plugin | Runtime contributions such as tools, hooks, services, or memory backends | Isolation from the host process |
| Specialized agent | Its own identity, configuration, tools, and session behaviour | Automatic access to all host tools or credentials |
| [Playbook](playbooks.md) | A named reusable procedure or graph | A skill indexed by SkillForge |
| [Evolver](evolver.md) | Separate benchmark-driven evaluation of harness changes | A production agent rewriting itself during a conversation |

See [Architecture](architecture.md) for assembly boundaries and
[DAG orchestration](orchestration.md) for coordinating specialized agents.

## Discover and manage skills

Start by inspecting what the current installation offers:

```bash
raven skill list
raven skill get --help
raven skill block --help
```

SkillForge uses three sources: local files, skills/cases recalled through the
configured memory backend, and remote Skill Hub candidates. Availability
depends on configuration and service connectivity; a catalog result is not
proof that a bundle is installed or its required tools exist.

`skillForge.discovery` defaults to `pull`: the model receives a small
name/description menu on eligible turns and uses `find_skill` to search and
`read_skill` to fetch a body. `push` instead runs the selection/injection
pipeline and places selected bodies in context. Pull avoids the push path's
per-turn rewriting and gating model calls; it does not mean all retrieval is
free or every turn receives the same menu.

Bundle downloads have a separate consent setting:

```json
{
  "skillForge": {
    "discovery": "pull",
    "autoInstall": "prompt"
  }
}
```

`autoInstall` defaults to `auto`; `prompt` requests interactive terminal
consent and behaves like `off` without a TTY. `off` skips the bundle download,
but does **not** block reading a vetted skill body. Use the blocklist when the
skill itself should be unavailable. Review scripts before execution; a catalog
safety score is not a security guarantee, and missing scores are not a hard
rejection in the current policy. Successful installs are audited under
`<workspace>/skills/hub/installs.jsonl`.

## Memory and learning boundaries

EverOS is the default memory-backend plugin, distributed separately from the
Raven core. It recalls user context and agent experience through the Memory
Engine. Use `raven plugins` to inspect the active backend, and configure memory
through onboarding and the deployment's plugin settings.

Keep three mechanisms separate:

1. Context assembly decides what the current model call sees; long histories
   may be archived or compacted.
2. Memory storage and recall retain selected knowledge beyond a conversation.
3. Experience extraction is a backend-specific capability, not the same thing
   as SkillForge retrieval. The current tree retains `skillForge.extraction`
   configuration and older descriptions of a local pipeline, but the live
   host wiring does not consume that setting. Do not assume enabling it
   starts local skill distillation or creates `.cache/skills.db`.

Feedback-driven skill versioning and retirement are not implemented merely
because configuration placeholders exist. Inspect your memory backend's
actual stored cases/skills and health before claiming it learned a procedure.
Harness self-evolution is performed
by the separate Evolver tool and evaluated against benchmarks; start with
[Evolver: usage and experiments](evolver.md), then use
[Design and implementation](self-evolution-map.md) for implementation details. Do not
describe retrieval or backend experience extraction as automatic
production-code self-modification.

## From a completed task to reusable knowledge

First decide what needs to survive:

| Information | Appropriate home | How to check it |
| --- | --- | --- |
| User preference or personal context | Memory backend's user track | Inspect stored memory and test recall in a new conversation |
| Agent experience or reusable case | Memory backend's agent track, when supported | Inspect the case/skill actually returned by that backend |
| Reviewed instructions | A local Skill | Read its body and verify required tools |
| Repeatable multi-step execution | [Playbook](playbooks.md) | Validate and test the stored graph |
| Source documents and passages | [Knowledge base](knowledge.md) | Verify indexing and retrieved source text |
| Evidence of a failed run | [Trajectory bundle](trajectory-debugging.md) | Replay it and retain the relevant artifacts |

EverOS exposes user and agent recall separately: user memories supply personal
context; agent cases/skills can supply SkillForge candidates. Keep the intended
`userId` and `agentId` stable across the conversations you want to connect.
A different agent identity or memory backend is not automatically the same
memory collection.

Try a non-sensitive preference such as “I prefer a short checklist before
instructions.” Complete the turn, inspect the memory view/backend if available,
then open a new conversation under the same identity and ask a related
question. Verify the recorded memory and retrieved context, not only the
model's claim that it remembers. Repeating the preference in the test prompt
would not demonstrate recall.

For reusable work, complete and verify a small task first, then inspect whether
the backend recorded a useful case or skill. If it did not, explicitly author
and review a local Skill or [create a Playbook](playbooks.md#inspect-and-create).
Do not treat every successful answer as an automatically installed procedure.

Host memory writes are queued off-turn, with bounded queues and retries.
An answer finishing does not mean backend indexing finished; outages, queue
limits, and shutdown can leave data unindexed or in an uncertain in-flight
state. Memory persistence is not a backup of the entire transcript.

## Context pressure is not long-term learning

Curator selects history for the next context window. Its fast path avoids a
model call below pressure thresholds; its slow path can spend bounded model
calls producing a plan, with deterministic fallback when no valid plan arrives.
Archived messages can be retained verbatim on disk and retrieved later, but
are not necessarily present in every prompt.

In-turn **Compaction** is different: it can prune older tool-result bodies and
summarize the transcript head while retaining recent messages. It defaults to
off and does not write long-term memory notes. To opt in:

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "enabled": true
      }
    }
  }
}
```

Merge rather than replace the rest of your agent configuration. Compaction
can use the turn's model and incur charges; it does not enlarge the provider's
actual context limit. Curator and compaction are not substitutes for an
explicit task brief or a source file that the agent can re-read.

## Diagnose missing recall or skills

| Symptom | Check |
| --- | --- |
| New conversation lacks a preference | Memory backend health, indexing completion, identity, and recall relevance |
| A skill is listed but unused | Whether its body was fetched, required tools, blocklist, and selected agent |
| No skill appeared after a task | Actual backend extraction support; `skillForge.extraction` alone does not activate a host pipeline |
| Long task lost a detail | Archived versus in-window history, compaction, and a retrievable source |
| Unexpected model cost | Backend extraction, embedding, Curator slow path, and skill rewrite/gating where enabled |

Memory views may expose private conversations and inferred preferences.
Backend-specific deletion and retention controls must be checked separately;
deleting a host session or blocking a skill is not proof that all backend
records were deleted.

## Extend tools or build an agent

Use MCP when a capability already exists behind a service interface. Configure
the server, verify its tool list and authentication, and then test a read-only
operation before permitting mutations. `raven plugin auth <server>` handles
configured MCP OAuth; it is not an installer for arbitrary plugins.

Use a Raven plugin for in-process tools, hooks, services, or memory backends.
Its declarative `raven-plugin.toml` names contributions; code runs in the host
trust domain. `raven plugins` lists installed contributions. A plugin Tool Gate
and the platform Permission Gate are distinct contracts.

Use `raven agents new <name>` to scaffold a specialized agent when it needs a
separate identity and execution profile. Read `raven agents new --help` first;
the resulting folder includes its own README. In a source checkout,
`agents/BUILDING.md` documents the launcher, configuration, plugin factories,
and discovery. Keep runtime state outside the definition folder and distinguish
the agent's state root, ACP home, and the user's working directory.

## Operate the complete workflow

| Task | Starting point | Verify before relying on it |
| --- | --- | --- |
| Reuse expertise | `raven skill list` | Body, required tools, download consent |
| Retain knowledge | `raven plugins` and memory configuration | Backend health and intended user/agent identity |
| Connect a tool service | MCP configuration and `raven plugin auth --help` | Credential scope and actual available tools |
| Coordinate agents | A DAG or playbook | Roster readiness, references, shared limits |
| Run scheduled work | `raven cron --help` | Schedule, recipient, permissions, resident host |
| Enable proactive work | [Proactive Reminders and Follow-ups](proactivity.md) | Opt-in configuration, interruption policy, cost |
| Connect messaging | `raven channels --help` | Sender allowlist, credentials, delivery |
| Diagnose a run | `raven doctor`, tracing, trajectory tools | Redacted evidence, not just a successful handshake |

For recurring or unattended work, review [Permissions and security](permissions.md)
and [Self-Hosting](self-hosting.md) first. Model context, memory, downloaded
skills, child processes, and network peers each introduce their own trust and
cost boundaries.
