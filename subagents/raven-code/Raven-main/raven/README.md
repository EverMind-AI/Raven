# Raven Runtime Package

`raven/` contains the Python runtime for the Raven command line agent. The
package is intentionally split by responsibility so CLI commands, provider
adapters, memory, routing, channels, and proactive execution can evolve without
coupling everything through one agent loop.

## Key Areas

- `cli/` - Typer command surface and terminal workflows.
- `agent/` and `spine/` - turn execution, scheduling, and message delivery.
- `context_engine/` and `routing/` - context assembly and model/tool routing.
- `memory_engine/` - memory backend contracts and consolidation paths.
- `providers/` - model provider adapters and fallback behavior.
- `channels/` - gateway/channel integrations.
- `plugin/` and `skill_hub/` - plugin discovery and skill retrieval.
- `sandbox/` and `security/` - execution isolation and trust boundaries.
- `templates/` - workspace templates materialized by Raven.

Runtime code should not import from `benchmarks/`, `demos/`, or `tests/`.
