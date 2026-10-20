# A draft `agent-decision` predicate for in-toto

**Status:** a proposal draft, prepared for the proposer to put in front of the in-toto
maintainers. **Nothing has been posted to in-toto, and no issue has been claimed.** The
nearest target is in-toto issue **#554**, an RFC for an agent-decision predicate, open and
unowned since 2026-05-19.

**Why this is being written down.** The MCP route needs a Working Group, a sponsor and a
co-author, and a `2026-12-31` deadline. The in-toto route needs **none of those**: a
predicate type is a schema and a reference implementation, and the reference implementation
already exists in this repository. The cost of the in-toto route is that the artefact is
a document rather than a standard — and the benefit is that it can be done **now**, by
one person, with no permission.

**What this document is for.** It is the thing a maintainer reads to decide whether the
idea is worth a spec PR. It states the shape, the constraints the shape has to satisfy, and
the questions the maintainers would have to answer.

---

## 1. The gap

in-toto has a Statement format and a small set of predicate types. The predicate types
that exist describe **builds** (`https://slsa.dev/provenance/v1`) and **vulnerabilities**
(`https://in-toto.io/attestation/vuln`). There is **no predicate type for an autonomous
agent's decision**: what the agent did, on whose authority, under which controls, against
which evidence, and when.

This is not a gap in the abstract. It is the gap the **buyers** of AI assurance have:
the underwriters who write cover for AI agents ask for a *technical system description*, a
*governance and oversight record*, an *incident history*, *written scope limits*,
*evaluation and red-team results* and *performance data*. Not one of their published
criteria mentions supply-chain provenance. The closest published dossier methodology
(*Agent Certified* v2.0) asks for a guardrail specification, a **dated** red-team report
with remediation evidence, monitoring and alert configuration, an incident playbook with
**named kill-switch access**, and an incident log, with evidence no more than **twelve
months** old.

So the artefact that is missing is **runtime and behavioural**, and it is missing from
every body of evidence work — including this project's own, which had been building a
build-time bundle.

## 2. The statement

The envelope is the ordinary in-toto Statement; only the predicate is new.

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    { "name": "agent-run:<session-id>", "digest": { "sha256": "<64 hex>" } }
  ],
  "predicateType": "https://evermind.ai/attestation/agent-decision/v0.1",
  "predicate": {
    "version": "0.1",
    "agent": { "name": "<agent>", "version": "<version>" },
    "run": {
      "kind": "tool-using | transport-only",
      "agent": { "name": "<agent>", "version": "<version>" },
      "started_at": "<instant>",
      "ended_at": "<instant>"
    },
    "decisions": [
      {
        "action": "tool_call",
        "tool": "<tool name>",
        "arguments_digest": { "sha256": "<64 hex>" },
        "outcome": "succeeded"
      }
    ],
    "tool_chain": {
      "tools": [ "<tool name>" ],
      "digest": { "sha256": "<64 hex>" }
    },
    "authority": {
      "principal": "who the agent acted for",
      "delegated": true,
      "scope": "what it was allowed to do",
      "kill_switch": "who can stop it"
    },
    "evidence": {
      "event_log": { "uri": "<uri>", "digest": { "sha256": "<64 hex>" } },
      "guardrails": [ { "id": "<guardrail>", "in_force_since": "<date>" } ],
      "red_team": { "uri": "<uri>", "date": "<date>" },
      "incidents": [ { "id": "<id>", "at": "<instant>", "resolution": "<text>" } ]
    },
    "what_this_establishes": "<the sentence, in the predicate, that says what the statement is worth>",
    "known_gaps": [ "<what it does not cover>" ],
    "observed_at": "<instant>",
    "provenance": {
      "source_url": "<uri>",
      "retrieved_at": "<instant>",
      "confidence": 0.9
    }
  }
}
```

**The unit is a run, not a decision.** A run can make many decisions, and the evidence a
buyer wants (the event log, the guardrails, the incident history) is about the run. So one
statement covers one run, `decisions` is a list inside it, and the subject is the run's
own digest. A decision-level predicate is a plausible v0.2, and the questions in §6 are
where it should be settled rather than guessed.

**`what_this_establishes` and `known_gaps` are part of the predicate, not comments on it.**
They are the two fields that make C5 and C7 mechanical rather than a matter of taste:
the first says what the statement is worth, in the statement; the second says what it does
not cover, in the statement. A predicate format that cannot carry a limitation will have
limitations read into it that it does not have.

**The subject is the run's own digest.** A verifier recomputes the digest from the run
record and finds the same value. That is what ties the signature to *this* run rather
than to a description of it, and it is why the subject is a digest of the record and not
of the predicate.

**`provenance` is the brief schema.** `{value, source_url, retrieved_at, confidence}` is
the canonical brief schema this project already uses for competitive research, and it is
the same shape the `Provenance` record in this repository carries. One vocabulary, used
in both places.

## 3. The constraints the predicate has to satisfy

These are not wishes. Each one comes from a check that exists, or from a buyer that was
read directly, and each one is falsifiable.

### C1 — A third party can verify it without the producer's cooperation

The Statement is signed, the payload is a DSSE envelope, and the subject digest is
recomputable. A verifier needs the envelope, the run record and the public key — not
the producer's server, and not the producer's permission.

### C2 — It expresses behaviour, not supply chain

The predicate's subject is an **agent run**, not an artifact build. The evidence block
points at behaviour: an event log, the guardrails in force, the red-team result, the
incidents. If the only evidence a predicate can carry is a source digest, it cannot be
used by the people who are actually asking the question.

### C3 — It carries authority

`decision` says what happened; `authority` says **on whose authority, within what scope,
and who can stop it**. An insurer's first question is not "what did the agent do" but
"who was responsible for it, and what were they allowed to do". A predicate that cannot
answer that is a log, not an attestation.

### C4 — It is dated, and freshness is a first-class field

`observed_at` and `retrieved_at` are not metadata; they are the difference between
evidence and a stale artefact. The evidence a buyer will accept has a shelf life — the
closest published methodology uses **twelve months** — and the predicate has to be able
to say how old it is without a second document.

### C5 — It never claims conformity

The predicate says **what was done, what was observed and what evidence exists**. It does
not say the system is compliant, and the vocabulary must not allow a reader to infer
that. Two vendors that ship signed evidence today both say, in their own words, that a
receipt does not establish compliance. A predicate type that let a reader conclude
compliance would be adopted and then disbelieved.

### C6 — An estimate is an estimate

The mechanical audit this project uses on its own research reports **fails a numeric
column that carries an estimate marker** — `≈`, `~`, `est.`, `estimated`, `approx`, and
the Chinese equivalents. An attestation that quietly contains an estimate has to fail the
same way. Confidence is a **field**; an estimate is not.

### C7 — A check that could not be run is not a pass

The audit reports three states: **passed**, **failed**, and **not run**. A verifier that
cannot reach the event log, or cannot check a guardrail, must say **not checked**. A
predicate format that can only express "present" cannot carry that distinction, and a
verifier built on it will report silence as a pass.

### C8 — Anyone may produce one

The format is a schema and a reference implementation, not a service. The point of a
predicate type is that a second party can produce one the first party did not.

## 4. The signer model

The envelope is ordinary DSSE, and the predicate is indifferent to how it was signed:

| How | Key | What a verifier needs |
| --- | --- | --- |
| Keyless | Fulcio certificate | the certificate, and Rekor's entry for the signature |
| Key-based | the signer's public key | the public key, published where the verifier can find it |
| HSM / corporate | the signer's key id | the key id resolvable to a key the verifier trusts |

**`signatures[].keyid` must resolve to an identity the verifier can check**, and the
resolution mechanism is the part that needs the maintainers' input: a DNS record, a
transparency-log entry, or a key in the keyid itself. A predicate type that leaves this
open will be implemented inconsistently, and the inconsistency will be the reason it is
not trusted.

**The four assurance rungs are a property of the signature, not of the predicate.** The
same predicate is valid at every rung; what changes is who signed it and how the verifier
establishes that:

| Rung | Who signs | What the verifier can establish |
| ---- | --------- | ------------------------------ |
| `same_principal` | the operator | that the parts are internally consistent |
| `third_party` | a party independent of the operator | the same, plus that someone other than the operator checked it |
| `third_party + L3` | a party independent of the operator **and** of the producer, with a published accreditation | that, plus the accreditation |

Writing the rung into the predicate would make the predicate a claim about the signer,
which is the wrong layer. The envelope is the right layer, and the predicate should stay
silent about it.

## 5. What v0.1 deliberately does not carry

| Not in v0.1 | Why |
| ----------- | --- |
| Model weights, training data, evaluation scores | Those are **evidence**, and the predicate **points at** them. Putting the numbers in the predicate makes them assertions instead of citations, and a stale assertion is worse than a pointer |
| A legal or regulatory position | C5. The predicate states facts about a run, not a law's satisfaction |
| The human's identity in the clear | The `authority` block names the **principal and scope**; a name is a fact about a decision, and the predicate should not become a place to assert an identity it cannot verify |
| The order's commercial terms | Not an assurance fact |

## 6. The questions the maintainers would have to answer

1. **Predicate type or separate spec?** A predicate type is cheaper and fits the Statement
   envelope. But a decision attestation has a lifecycle — the run, the authority, the
   evidence — and some maintainers will argue it deserves its own spec with its own
   subject conventions. The honest answer from the field: the run's **event log** is
   already the natural subject, and the predicate should say what the *decision* was and
   point at the log.
2. **What is the subject?** This proposal uses `agent-run:<session-id>` with the digest of
   the **run record**. The alternative is the digest of the **event log** itself, which
   is more stable — the session id is a label, the log is the thing. A maintainer who
   prefers the log should be listened to.
3. **Does `authority` belong in the predicate?** It could be a separate attestation the
   predicate references. The argument for keeping it here is C3: the buyer asks about
   authority **in the same breath** as the decision, and a pointer would make them join
   two documents.
4. **What is the retention story?** The predicate points at evidence that will age out
   — a log, a red-team report. Does the predicate need a statement about how long the
   evidence is expected to live, or is that out of scope?
5. **Is `confidence` a field, or a claim?** This proposal puts it in `provenance`, where
   it means "how much this reading is worth" rather than "how sure the agent was".

## 7. The evidence base

A reference implementation exists in this repository, on the branch
`feat/evidence_records_corpus`, and it does the following:

- reads a real agent run and emits **one in-toto statement per run**, in a DSSE envelope,
  with the run's own digest as the subject;
- signs it with an **Ed25519** key, so it is verifiable offline against a published
  public key with no account and no transparency log;
- **verifies** it offline: the signature, the subject digest recomputed from the run
  record, and the manifest;
- reports **three states** — passed, failed, **not run** — and never reports a check it
  did not perform as a pass;
- **refuses** a SKU or a run whose identifiers are placeholders, rather than sending
  anything on the strength of a TODO.

**What the implementation emits today, and what v0.1 proposes.** The reference
implementation emits the **run** half of the predicate: `run`, `tool_chain`,
`what_this_establishes` and `known_gaps`, with the run's own digest as the subject. It
does **not** yet emit `decisions`, `authority` or `evidence`, and the seven runs it has
would carry an **empty** `decisions` list — they called no tool at all. **So the gap
between the implementation and this proposal is named rather than glossed:** the
`decisions`, `authority` and `evidence` blocks are the part of the predicate that is
**proposed and not yet implemented**, and the part that is implemented is the part that
the seven runs can honestly support.

**What it is not.** The corpus holds **seven** real runs and **every one is a transport
check with no tool call at all**, so what it demonstrates is the **machinery** — the
statement shape, the signing, the offline verification, the not-checked state — and not a
corpus of tool-using agent runs. A hundred real runs need a machine that has run the
agent with tools.

**The honest claim to the maintainers:** the schema and the verifier exist and work
against real runs; the corpus is small and of the wrong kind; and the proposal is
better for having been tried against real data than for being proposed on paper.

## 8. What proposing it would involve

1. A comment on in-toto issue #554 stating the shape above, with this document attached.
2. A spec PR adding the predicate type, the JSON schema, and the field semantics, with
   C1–C8 as the acceptance list.
3. A reference implementation, already written, offered as a second PR.

**All three are the founder's to make.** Nothing in this repository has been posted
anywhere, no issue has been claimed, and no account has been used.
