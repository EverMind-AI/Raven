# Changelog

All notable changes to Raven are documented here.

## v0.1.0 - Public Preview - 2026-06-30

Raven is now available as an Apache-2.0 open-source project.

This is the first public preview release: stable enough for developers to try,
inspect, and build around, while the CLI surface, plugin contracts, and runtime
internals may still evolve before v1.0.

### Highlights

- AI-native command line agent built for terminal-first workflows.
- Memory-first runtime with context assembly, routing, and session management.
- Proactive execution path for scheduled, event-driven, and background work.
- Native TUI and bridge packages aligned under the v0.1.0 release line.
- Skill and plugin foundations for extensible agent behavior.
- Provider routing and fallback paths for multiple model backends.
- Public repository hygiene: CI, commit linting, pre-commit checks, issue
  templates, security policy, contribution guide, and Apache-2.0 licensing.

### Installation

```bash
curl -fsSL http://raven.evermind.ai/install.sh | bash
```

### Release Status

- Version: `0.1.0`
- Tag: `v0.1.0`
- Stability: public preview
- License: Apache-2.0

### Notes

- Raven is not yet API stable.
- The public install endpoint and package distribution flow should be verified
  before publishing the GitHub Release.
- Future releases should use semantic versioning: patch releases for fixes,
  minor releases for new capabilities, and v1.0 once the public CLI and plugin
  contracts are stable.
