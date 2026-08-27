# Raven-Design

Raven-Design is the host Raven's visual-artifact worker. The folder contains a
full Raven checkout plus the launcher and manifest needed to run it as one CLI
subagent. It works directly in the caller's workspace and returns the final
assistant answer committed by the inner session.

## Capability boundary

Delegate requests whose deliverable is visual: brand systems, icons, lettering,
illustration, marketing graphics, editorial layouts, presentations, data
visualization, technical diagrams, maps, UI systems, websites, product
interfaces, explainers, games, or polished visual frontends.

The host supplies the objective, source material, output format, and acceptance
criteria. Raven-Design owns the visual-domain decision, Skill reading, file
implementation, rendering, preview, and review. It does not spawn another agent
or send messages to external channels.

## Skill routing inside the worker

Every non-empty user turn runs the Visual Domain Selector before the main Agent:

1. The selector compares the complete `SKILL.md` bodies of the fixed 15-domain
   Raven-Design catalog against the current user query.
2. It returns at most two preferred and three alternative exact Skill ids.
3. The main Agent sees only those Skill names, descriptions, and priority groups.
4. The main Agent decides whether a procedure is useful and calls `read_skill`
   with the exact `builtin/<name>` id when it needs the body.
5. Public, workspace, EverOS, and Hub Skills continue through the general
   SkillForge route; the 15 domain Skills are excluded from that lane so their
   bodies are not injected twice.

The full selector catalog is request-local and is not written into the session.
A later user turn runs selection again from that turn's query.

## Context and long runs

The worker pins its context window to 262144 tokens. Same-turn Context
Compaction is enabled: after tool iterations push the request near the trigger,
the loop preserves the bootstrap, real user constraints, and recent complete
tool-call groups while replacing older work with a Compaction Summary. A
compaction resets Responses continuation state before the next model request.

The same launcher session id resumes the same inner Raven conversation and
workspace. Different session ids are isolated.

## Credentials

Copy `.env.example` to `.env` only when this worker needs credentials of its
own. With `DESIGN_API_KEY` blank, each launch copies the host Raven's provider
block, routing, selected model, and protocol. This is the normal setup for a
host using a private OpenAI-compatible Responses endpoint. A launch triggered
inside a host turn follows that turn's model and reasoning effort, including a
per-session model selected in WebUI.

Serper and Jina keys independently fall back to the host tool configuration.
The image tool accepts the host's declared `chat_modalities`,
`openrouter_images`, or OpenAI-compatible `images` protocol. The last uses
`/images/generations` for prompt-only generation and multipart `/images/edits`
for reference-image editing. A custom endpoint without an explicit supported
protocol is not inherited.

Rendered configs contain live keys and are created with mode 600 below the
state root, then deleted after the run. They never belong in this checkout.

## Build and register

From the host repository:

```bash
cd subagents/raven-design/Raven-Design
uv sync
cd ..
python3 install.py --dry-run
python3 install.py
```

The repository-wide `subagents/install.sh raven-design` performs the venv step.
Restart a running host Raven after registration so it reloads the roster.

## Manual worker smoke test

Run from the workspace whose files the worker should read and write:

```bash
python3 /path/to/subagents/raven-design/run.py \
  --task "Create an accessible single-page product launch visual and preview it" \
  --session design-smoke \
  --verbose
```

Manual execution proves the inner runtime. A full integration test must start
the host Raven, confirm `Raven-Design` appears in its subagent roster, and make
the host dispatch the request through `spawn`.
