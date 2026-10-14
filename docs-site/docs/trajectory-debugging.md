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

This creates a local record/package. It does not open a GitHub issue, and it
uploads the package only when `RAVEN_BUGREPORT_URL` names a Bug Report Receiver
(see [Send and receive bug reports](#send-and-receive-bug-reports)); the
receipt is then written into the record. Review flagged items and the package
before sharing. `--yes` is not sensitive-data risk consent. Do not routinely
bypass findings with `--accept-risk` or declare every token harmless.

The WebUI's **Report a problem** runs the same pipeline on the turns you pick
in a conversation, with the decisions made on a page instead of a prompt; see
[Export and report a problem](webui.md#export-and-report-a-problem).

### Remembered redaction decisions

Every keep or replace answer you give in the review is remembered on this
machine, in `redaction_history.json` under the Raven home (`~/.raven`, or
`RAVEN_HOME` when set). The next report replays it: a value you already decided
is listed as decided from your redaction history instead of being asked again,
and a run without a TTY no longer needs `--keep-findings` or `--redact-findings`
for those values (`--accept-risk` is still required). Only a hash of each value
and a masked sample are stored. Private-key items are always asked, and
decisions made by the blanket `--keep-findings` / `--redact-findings` flags are
never remembered.

```bash
raven trajectory report-bug ATTEMPT_ID -d "..." --review-all   # decide every item again
raven trajectory redaction-history list                        # what is remembered
raven trajectory redaction-history forget 3f9a1c2b7d4e          # drop one decision by id
raven trajectory redaction-history clear --yes                  # drop all of them
```

A report made in the WebUI follows a different rule on purpose: every
decision on its decisions page is remembered, including the default "keep all"
and the bulk buttons, with `web` as its source, because the page shows every
value it asks about.

Set `RAVEN_BUGREPORT_REDACTION_HISTORY=off` to neither replay nor record
decisions. A history file that cannot be read is backed up next to it before
the next save; until then every item is asked.

## Send and receive bug reports

### The receiver

A Bug Report Receiver is a small HTTP service that takes Bug Report Packages
in, checks them, stores them by day, and answers with a receipt. It does
nothing else: it authenticates nobody, and it serves no listing or download.
Who can reach it is decided by the network it listens on.

```bash
raven trajectory receiver --save-dir /srv/raven-bugreports
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--save-dir` | required | Directory that receives packages; created with mode 0700 |
| `--host` / `--port` | `127.0.0.1` / `18795` | Bind address; `--port 0` picks a free port and prints it |
| `--trusted-proxy CIDR` | none | A proxy whose `X-Forwarded-For` is believed (repeatable) |
| `--max-package-mib` / `--max-unpacked-mib` | 64 / 256 | Largest package, and largest decompressed volume inspected |
| `--max-daily-mib` / `--max-daily-count` | 2048 / 500 | What one UTC day may accept, in bytes and in packages |
| `--min-free-mib` | 1024 | Free disk space the receiver keeps |
| `--rate-burst` / `--rate-per-10min` | 10 / 30 | Uploads one address may make at once, and regains per ten minutes; `--rate-burst 0` disables rate limiting |
| `--concurrency` | 4 | Uploads handled at the same time; others get 503 |
| `--idle-timeout` / `--total-timeout` | 30 / 600 | Seconds without body data, and seconds for one upload in total |

Packages land as `<save-dir>/<YYYY-MM-DD>/<report-id>.tar.gz`, the original
bytes untouched, next to a `<report-id>.json` sidecar that holds the receipt,
the sender's address, and a triage digest of `bugreport.json` (reporter,
description, severity, completeness, redaction classification) so a reader can
sort packages without unpacking them. The digest keeps the first 1000
characters of each field and at most 16 path notices, and a sidecar counts
against the daily byte budget together with its package. The day is the UTC day the upload was
admitted on: a package that finishes after midnight stays in the day it
started, and its sidecar's `received_at` records when it actually landed. The
same package sent twice returns the receipt already stored; a different
package under the same report id is refused. Retention is the operator's job
-- the per-day directories make `find -mtime` cleanup trivial.

### Deployment

The receiver speaks plain HTTP and binds loopback by default. Anything beyond
this machine belongs behind a TLS-terminating proxy; never expose the port
itself to the public internet. An nginx location that works:

```nginx
location /v1/bugreports {
    proxy_pass http://127.0.0.1:18795;
    proxy_request_buffering off;
    client_max_body_size 64m;      # at least --max-package-mib
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Add `--trusted-proxy 127.0.0.0/8` (or the proxy's network) so rate limiting
and the sidecar's `client` address follow the real sender instead of the proxy;
without it every upload through the proxy shares one bucket. `GET /healthz`
answers `{"status": "ok"}` for probes. Run the receiver as an unprivileged
user and treat `--save-dir` as holding sensitive material: packages are
redacted for credentials, not anonymized.

### Telling clients where to send

`report-bug`, the browser's *Report a bug*, and `raven trajectory
upload-bug-report` all read one setting:

| `RAVEN_BUGREPORT_URL` | Effect |
| --- | --- |
| not set | the build's default (empty in this repository: packages stay local) |
| set to an empty value | uploading off, even in a build that ships a default |
| `https://ops.example.com/raven-bugreports` | upload there; a path prefix is fine, the client adds `/v1/bugreports`; a query, a fragment or credentials in the URL are refused |
| `http://127.0.0.1:18795` | plain HTTP is accepted on this machine only |
| `http://10.1.2.3:18795` | refused unless `RAVEN_BUGREPORT_ALLOW_HTTP=1` says plaintext on the network is acceptable |

A misconfigured URL stops `report-bug` before anything is collected. With a
URL in place the confirmation screen says `Will upload to ...`, the package is
sent as soon as it lands, and the receipt is written into the record. When
the upload fails the package is kept and the record says so; send it again
with:

```bash
raven trajectory upload-bug-report REPORT_ID
```

### What the receiver checks

Before a package is stored its size, gzip and tar framing, member names and
types, the manifest's hashes, and the `needs_review`/`risk_accepted`
invariant are all verified in a stream, without extracting anything. The
receiver does not scan content for secrets: redaction remains the sender's
responsibility, and what it reports on the confirmation screen is what ships.

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
`redact.py`, `package_check.py`, `receiver.py`, `upload.py`, and
`raven/cli/trajectory_commands.py`.
