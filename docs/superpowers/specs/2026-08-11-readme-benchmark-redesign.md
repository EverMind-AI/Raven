# README Benchmark Redesign

## Goal

Turn the README from a broad capability catalog into a concise product narrative that proves Raven's value early, then helps a new user install and understand the two newest flagship features: Deep Research and Tracing.

## Audience

- Developers evaluating agent harnesses
- Builders deciding whether Raven is credible enough to try
- Contributors looking for architecture and development links

## Information Architecture

1. Raven banner, positioning, and community links
2. Benchmark proof board
3. Three-command quick start
4. Deep Research
5. Tracing
6. Core systems: memory, context, proactivity, and skills/evolution
7. Supported providers and messaging gateways
8. Architecture, documentation, ecosystem, and contributing links

## Benchmark Proof Board

Create one wide image that consolidates the three supplied benchmark concepts:

- Better Results, Fewer Tokens
- Learns Best Among Peers
- Acts Earlier, Scores Higher

The board uses Raven's warm cream, dark brown, and gold palette, with high-contrast typography and explicit legends. It fixes spacing, capitalization, ambiguous zero glyphs, and chart labeling in the supplied drafts.

Every public claim must be traceable to an official benchmark, Raven property, or approved EverMind statement. The README text below the image links to the relevant methodology or leaderboard.

The final image is uploaded through GitHub User Content. No image, SVG, HTML, or generated asset is committed to the repository. Target download size is below 500 KB.

## Deep Research

Explain the user journey rather than only the integration:

- Enable with `raven deep-research enable`
- Choose deep or regular search per research-shaped query
- Run broad multi-source research through MiroThinker
- Receive the finished result directly, including citations
- Save a Markdown report in the workspace
- Support background completion and delivery when the gateway is available
- Make time and quota use explicit before the paid engine runs

## Tracing

Position Tracing as the way to inspect why Raven acted:

- Open the local dashboard with `raven tracing`
- Follow turn, model, tool, subagent, skill, context, and memory spans
- Inspect usage, cost, latency, errors, tool input, tool output, and artifacts
- Keep trace data local under the Raven state directory
- Allow tracing to be disabled without affecting the host workflow
- Guarantee that tracing failures do not break the agent loop

## Content Reduction

- Merge `What You Can Do in 2 Minutes` into Quick Start
- Merge `Why Raven` and `What Raven Is Built For`
- Remove the duplicate Deep Research update block
- Replace the full gateway table with a compact support line
- Merge `Useful Commands` and `Docs by Goal`
- Reduce Agent Templates to one paragraph
- Reduce status, developer workflow, and ecosystem tables to concise links
- Remove repeated back-to-top badges

## Verification

- Review every README claim against code, tests, or an official benchmark source
- Confirm image URL works without authentication
- Confirm downloaded image is below 500 KB and matches the uploaded source hash
- Run `git diff --check`
- Run `make check-large-files`
- Preview the rendered README on GitHub through the draft pull request

## Rollback

Revert the README-only commits. The externally hosted image can remain unreferenced without changing repository size.
