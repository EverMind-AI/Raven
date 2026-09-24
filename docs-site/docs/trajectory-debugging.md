# Trajectory debugging and replay

Turn a real failure into inspectable evidence and, when useful, a deterministic
regression test. Tracing records what ran; a trajectory groups the relevant
attempt, model/tool artifacts, conversation context, and outcome labels.
Use [Tracing and Instrumentation API](tracing-api.md) when adding instrumentation;
this page covers the operator's replay workflow.

## Capture the failing attempt

Tracing is enabled by default. `RAVEN_TRACING` or `tracing.enabled` can change
that; missing historical artifacts cannot be reconstructed by turning tracing
on after the failure. Use the same installation/trace store as the failing
run, including any `RAVEN_TRACING_DIR` override.

```bash
raven trajectory list
raven trajectory save ATTEMPT_ID
```

Replace `ATTEMPT_ID` with a listed id. A trace id can resolve to its containing
attempt, so saving it may collect more than one turn. For another agent's
workspace, use `save --workspace /path/to/agent/home` to locate its session
records.

`save` creates a self-contained bundle and pins the attempt against normal
purging. It does not sanitize the bundle. The manifest reports missing
artifacts/messages and whether session context was included. A bundle with
missing evidence is not necessarily replayable.

Keep raw bundles local and outside git. They can contain source files,
prompts, credentials, tool outputs, and private conversation history.

## Produce a reviewed report

```bash
raven trajectory report ATTEMPT_ID --config /path/to/agent/config.json
```

Use the config the traced agent actually used to seed known-secret redaction.
The command repacks current evidence, redacts a copy, and creates a local
shareable tarball after its review/confirmation flow. The original raw bundle
is retained. A successful automatic scan does not guarantee every private
business fact was removed.

For a structured bug package:

```bash
raven trajectory report-bug ATTEMPT_ID \
  --description "The tool result was omitted from the next model request" \
  --expected "The next request includes the tool result" \
  --actual "The next request has no tool result"
```

This creates a local record/package; it does not open a GitHub issue or upload
the report. Review flagged items and the package before sharing.
`--yes` is not sensitive-data risk consent. Do not routinely bypass findings
with `--accept-risk` or declare every token harmless.

## Replay the harness without repeating actions

```bash
raven trajectory replay ATTEMPT_ID --strict
raven trajectory replay ATTEMPT_ID --warn --json --out /tmp/raven-replay-report.json
```

An id here resolves an already saved bundle; alternatively pass its directory.
Replay supplies recorded model replies and recorded tool results to the live
harness in a temporary workspace. It does not dispatch real tool code or
re-evaluate the task with a fresh model. Replaying a recorded deployment does
not deploy again through its recorded tool calls.

| Mode/result | Meaning |
| --- | --- |
| `--strict` | Halt at the first request/recording divergence |
| `--warn`, CLI default | Record differences and continue feeding by order |
| Exit 0 | Reached the end; warn mode can still contain divergences |
| Exit 1 | Bad target or invocation |
| Exit 2 | Replay halted at a strict divergence or ran out of recording |

Read the divergence's call kind/index, field, expected value, and actual value.
“No divergence” means this harness reproduced the compared recording, not
that the original task was correct. After a fix, a specific divergence can
be exactly the evidence you want.

## Make a regression case

In a source checkout, scaffold from a saved bundle or reviewed report:

```bash
raven trajectory regression init /path/to/bundle --name tool_result_preserved
```

The command minimizes and redacts a cassette, checks residual findings, and
publishes a draft under `tests/trajectories/tool_result_preserved/` only after
its gates pass. It refuses an existing case. Complete both files before
considering the result ready:

- `case.yaml`: issue, owner, protected contract, and when re-recording is
  justified; review any residual findings with an explicit rationale.
- `expect.yaml`: whether faithful reproduction or a particular first divergence
  is expected, plus assertions on the live request.

For a fixture where the first model request now contains the corrected user
message, this **illustrative expectation** checks the change:

```yaml
mode: strict
divergence:
  kind: llm
  index: 0
  field: messages[1]
checks:
  - call: llm
    index: 0
    message: 1
    op: contains
    value: expected corrected text
```

Derive the index, field, and text from your replay report; do not paste this
expectation unchanged into an unrelated case. To expect faithful reproduction,
omit `divergence` and assert the behavior that must remain stable.

```bash
raven trajectory regression validate tests/trajectories/tool_result_preserved
raven trajectory regression validate --all
uv run pytest tests/test_trajectory_regressions.py -q
```

Static validation checks metadata, cassette completeness, residual review,
and size. Pytest performs replay assertions. Validation success is not a
substitute for actually running the case. Read `tests/trajectories/README.md`
before contributing a cassette; raw reports and report assets do not belong
in the repository.

## Know what replay does not test

The current comparison checks model/streaming path, message roles and
non-system contents, tool calls, and offered tool names. It deliberately does
not compare system-message content or complete tool-schema bodies.

Recorded media are not re-fed, tool display/abort/block metadata is not fully
reconstructed, and parallel interleavings are consumed sequentially. A changed
order appears as divergence rather than an intelligent rematch. Missing
pre-attempt history may also cause an early difference.

Use targeted unit tests for system-prompt assembly and real integration tests
for provider behavior, browser interactions, concurrency, and external side
effects. Recorded replies cannot prove that a new model would choose the same
actions. [Evolver](evolver.md) runs fresh benchmark evaluations; replay
diagnoses harness behavior against a recording.

## Troubleshooting and retention

| Symptom | Check |
| --- | --- |
| Attempt not listed | Correct trace root, tracing configuration, and retained logs |
| Missing session/artifacts | Agent workspace and bundle manifest completeness |
| Replay stops immediately | First divergence, missing input, or pre-attempt history |
| Warn mode exits successfully despite a bug | Inspect divergence list and write assertions |
| Regression validation fails after init | Fill draft metadata/expectations and review residuals |
| Trace storage keeps growing | Pinned corpus and retained bundles, not just log rotation |

`raven trajectory unpin ATTEMPT_ID` removes retention protection; it does not
delete exported copies or redact existing bundles. Treat evidence retention,
sharing, and deletion as separate decisions.

Implementation: `raven/trajectory/replay.py`, `regression.py`, `cassette.py`,
`redact.py`, and `raven/cli/trajectory_commands.py`.
