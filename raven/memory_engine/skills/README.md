# Raven Built-in Skills

This directory holds skills shipped with Raven. Each skill is a directory containing a `SKILL.md` file with YAML frontmatter (name, description, metadata) and Markdown instructions for the agent.

Frontmatter follows YAML scalar and comment rules. For example, `always: on`
is true, and a nonzero numeric `always` value is true. Quote descriptions that
contain a whitespace-prefixed `#`, or YAML treats the rest as a comment;
`description: "Handles issue #42 and C# code"` keeps the whole description.
Likewise, `inject: description # digest` selects description-only injection.
Malformed or unsupported frontmatter uses the legacy line fallback, and
non-string `name` and `description` values retain their source spelling.
The registry and SKILL importers use the same parser and legacy line fallback.
Their metadata and Markdown body use the same complete three-dash delimiter lines;
trailing spaces on those lines are accepted, while longer horizontal rules are not headers.
Memory-note importers preserve a malformed leading block as text instead of removing
rules written there. YAML composition rejects aliases
and stops at 32 nested nodes; blocks over 16,384 characters use the line fallback
without invoking YAML. These limits bound parser work without promising a fixed
time per skill.

## Available Skills

| Skill | Description |
|-------|-------------|
| `weather` | Get current weather and forecasts (wttr.in + Open-Meteo, no API key) |
| `subagent-dag-orchestration` | Orchestrate multi-sub-agent work as one `run_subagent_dag` graph |
| `raven-self-config` | Read and change Raven's own settings with `raven_config`, and apply them |

## Notes
User-defined skills can be placed under `<workspace>/skills/` or any directory listed in `skill_forge.local_dirs`.
