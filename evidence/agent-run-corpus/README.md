# The signed-records corpus

Real agent runs, attested, verifiable offline. This is the empirical half of the
Phase-0 artefact: a standards proposal for agent attestation is stronger with a
corpus of real runs behind it than without one, and the corpus is what shows the
attestation shape working on runs that actually happened.

## What is in here

| | |
|---|---|
| `manifest.json` | the index: the entries, the **mix of run kinds**, the predicate type, the signing key, and the corpus's own known gaps |
| `entries/*.envelope.json` | one signed DSSE envelope per run |

Each envelope carries an in-toto statement whose **subject is the run's own digest**. A verifier
recomputes that digest from the run record and finds the same value, which is what ties the
signature to *this* run rather than to a description of it.

## What these 107 runs are, and are not

| | |
|---|---|
| **100** | `run.kind: tool-using` — the agent called a tool, and the tool chain is in the predicate |
| **7** | `run.kind: transport-only` — the run called no tool, so what it establishes is that the transport worked |

The 100 tool-using runs were collected on **2026-10-20** by driving Raven's own ACP agent
(`raven acp`) with tasks that cannot be answered without a tool call — six `web_fetch` tasks
against small, stable public pages, and `read_file` tasks against a fixture file in the run's
own workspace. The collection script is `scripts/collect_agent_runs.py`; it keeps only runs that
produced a tool call, because a run that called nothing is the other kind of evidence.

The 7 transport-only runs are the ones this machine already had (2026-09-29). They are kept,
not deleted: the mix is the corpus's history, and `manifest.json` states it in `run_kinds` so
that a reader can tell what they are holding without opening every entry.

**What the tool-using runs do not establish.** The tasks are narrow and mechanical on purpose —
read a page, report one fact from it. They show the attestation shape working on runs that
really happened, with a real tool call in the chain. They are **not** a sample of agent work,
they are not representative of it, and the predicate says so in its own words in every entry:
what it establishes is that the run happened, that these frames produced it, and that the tool
chain is the one the run called. **Nothing about the quality of the run, the correctness of its
output, or any regulatory position.**

**The hundred is a number, not a claim.** The Phase-0 artefact asks for at least 100 real agent
runs; the corpus now holds 100 tool-using ones, which meets the count. It does not meet a
representativeness bar, and nothing here says it does. The manifest says so in `known_gaps`.

## The signing key, and what is missing from it

The signatures are **local Ed25519**, verifiable offline with the public key in the manifest, and
**not anchored in a transparency log**. Sigstore keyless signing is unavailable to this build: it
needs an OIDC identity and a Fulcio/Rekor round trip, and the plan already records that as a
blocker. The gap is stated in every entry's `known_gaps` and in the manifest's, rather than left
for a reader to notice.

The private key is **not in this directory**. It lives outside the repository, and a corpus whose
signing key is published is not a signature.

## Building and verifying

```bash
# collect real, tool-using runs (needs a working model route and the web tools)
python3 scripts/collect_agent_runs.py --runs <dir> --count 100

# build from a directory of run records
python3 scripts/evidence_corpus.py build --runs <dir> --out evidence/agent-run-corpus --key <keyfile>

# verify offline: every signature, and every subject digest recomputed from the run record
python3 scripts/evidence_corpus.py verify --corpus evidence/agent-run-corpus --runs <dir>
```

The verifier reports three states and never conflates them:

- **passed** — the signature verified, and the subject digest matched the run record;
- **failed** — a signature did not verify, or the run record now digests to something else;
- **not run** — the run record was not on hand, so the digest was not recomputed.

A check that could not run is reported as **not run**, never as a pass. A corpus that reports a
check it did not perform is worse than no corpus, because it moves the burden of noticing onto
every reader.

## What an entry does not claim

It claims that a run happened, that these frames are the ones that produced it, and that the tool
chain recorded is the one the run called. It claims nothing about the quality of the run, the
correctness of its output, or any regulatory position. The `what_this_establishes` field in every
predicate says so, and it is the same rule the evidence bundle spec states: a part that
establishes nothing says so.
