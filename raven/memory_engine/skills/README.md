# Raven Built-in Skills

This directory holds skills shipped with Raven. Each skill is a directory containing a `SKILL.md` file with YAML frontmatter (name, description, metadata) and Markdown instructions for the agent.

## Available Skills

| Skill | Description |
|-------|-------------|
| `weather` | Get current weather and forecasts (wttr.in + Open-Meteo, no API key) |
| `subagent-dag-orchestration` | Orchestrate multi-sub-agent work as one `run_subagent_dag` graph |
| `raven-self-config` | Read and change Raven's own settings with `raven_config`, and apply them |

## Notes
User-defined skills can be placed under `<workspace>/skills/` or any directory listed in `skill_forge.local_dirs`.
