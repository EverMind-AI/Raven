# Evidence Bundle: one artefact a buyer can hand an auditor

Date: 2026-10-20. Status: design draft. Ship it as an open spec — donated, not a product.

## Problem

Raven has every part of an assurance bundle and nothing stitches them into one artefact.
A buyer cannot hand an auditor a Raven run, and a buyer who wants proof of how an AI
system behaved cannot get it from Raven at all.

Three findings shape this design, each read from a primary source on 2026-10-20.

**1. The parts are all here and the artefact is not.** Raven already records a
provenance stamp, an event-log export with lineage, a sealed-test record, a
human-oversight and decision-authority record, and a tool-chain SBOM with VEX. What is
missing is the stitching: a buyer has to know which of those to ask for, and in what
shape, and the answer is currently "it depends on who you ask".

**2. The buyer who pays asks for behaviour, not supply chain.** The published
underwriting criteria of the insurers writing cover for AI agents are behavioural and
runtime: a technical system description, a governance and oversight record, an incident
history, written scope limits, evaluation and red-team results, and performance data.
Supply-chain provenance — in-toto, SLSA, signed SBOM, VEX — appears in none of them.
The closest published dossier methodology, *Agent Certified* v2.0, in force 2026-04-24,
asks for a guardrail specification, a **dated** red-team report with remediation
evidence, monitoring and alert configuration, an incident playbook with **named
kill-switch access**, and an incident log, with evidence no more than **twelve months**
old. One underwriter states its own process as *technical due diligence by experts*.
So a bundle that carried only the build-time half would be answering a question its
buyer is not asking.

**3. Signed evidence is not the differentiator it looks like.** Across the governance,
evaluation and GRC vendors checked, none ships a third-party-verifiable attestation:
OneTrust, LangSmith, Braintrust, Arize, ModelOp, Patronus and Vanta's own
documentation carry zero in-toto, SLSA, sigstore, DSSE, Ed25519 or CycloneDX hits.
But three small vendors already ship the artefact: one at **$79 per month** with a
public offline verifier and an explicit unaffiliated-signer claim, one at **$3,000 to
$5,000 per month** with an Ed25519-signed public registry and an open-source verifier
that needs no account, and one shipping signed CycloneDX and SLSA provenance for the
customer's supply chain. The two heaviest price bands in the market sit on exactly the
artefact this spec describes. **Signed evidence is a commodity. Conformity-grade
evidence is not** — and both of those vendors say in their own words that a receipt
does not establish compliance.

There is also a window, and it is dated. The EU AI Act lists **zero** notified bodies
in NANDO, against 176 for the MDR and 538 for the MDD on the same endpoint, and the AI
Omnibus in force 2026-07-27 defers the Annex III high-risk obligations to
**2027-12-02**. The statutory demand for a third-party verifier does not exist for
roughly fourteen months. That is a window to be the verifier before the register fills,
not before the incumbents move.

## Goals

- **G1** One bundle, one directory, one manifest, openable by a third party with no
  Raven install and no network.
- **G2** Every part carries a **Bundle Stamp**: what it is, what produced it, from
  what inputs, when, and what it is worth.
- **G3** The bundle carries **both halves** — build-time evidence and run-time
  evidence — because the security reviewer asks for the first and the underwriter
  asks for the second, and the buyer is often both.
- **G4** The bundle is **open and vendor-neutral**: anyone may produce one, anyone may
  verify one, and the format is published rather than hosted. It is a spec, not a
  product.
- **G5** A verifier who is **not** the producer can check the bundle offline and can
  state precisely which checks passed.
- **G6** The bundle **declares its assurance rung** — `same_principal`, `third_party`,
  or `third_party + L3` — and the rung is a field, not an inference.
- **G7** **Evidence freshness** is a first-class field on every part, because an
  attestation that is two years old is not evidence of anything now.
- **G8** The bundle **never claims conformity**. It claims what was done, what was
  observed and what evidence exists. "A receipt alone does not establish compliance"
  is the correct shape of the claim, and the bundle's own language says so.
- **G9** The bundle **integrates with the registers that exist** — the NANDO Art. 35
  list and IAF CertSearch under ISO/IEC 42006 — rather than inventing a third.

## Non-goals

- **Not a product.** No hosted store, no bundle service, no verifier network, no price.
  The paid programme is a separate item and lives on the hosted siblings.
- **Not an accreditation registry.** NANDO's Art. 35 list and IAF CertSearch under
  ISO/IEC 42006 already exist and are statutory-grade. A register this spec defines
  would be a directory of directories.
- **Not an attestation of correctness.** The bundle attests to process and to evidence.
  It does not say the outputs were right, and it must not be readable as if it did.
- **Not a replacement for the sealed test, the design index or the trajectory bundle.**
  It points at them.
- **Not a new date format, a new signature scheme or a new transparency log.** The
  parts use what already exists: in-toto statements, DSSE envelopes, sigstore bundles
  and Rekor for build-time; the event log and the decision-authority record for
  run-time.
- **Not a jurisdiction opinion.** The dossier templates state what was done in each
  jurisdiction's own terms. They do not say what is lawful there.

## Terms

**Evidence Bundle** (added to `CONTEXT.md` with this change). A portable directory that
attests to how an AI system was built and how it behaved, together with the records
needed to check both, and a manifest naming every part.
_Avoid_: "Trajectory Bundle" (`raven/trajectory/bundle.py`, `CONTEXT.md`) — that packs
one Attempt's run for a reader who will replay it; an Evidence Bundle packs assurance
records for a third party who never sees the run.

**Bundle Stamp.** The per-part record that says what the part is, what produced it,
from what inputs, when, and what it is worth. It is the artefact-level counterpart of
**Provenance** (`CONTEXT.md`), which answers the same five questions for one field of a
Record.

**Build-time evidence.** The records that describe how a system was constructed: the
tool-chain SBOM and VEX, the source and build provenance, the dependency attestations.

**Run-time evidence.** The records that describe how a system behaved while it ran: the
event log with lineage, the guardrail configuration, the human-oversight and
decision-authority record, the red-team report and its remediation, the incident log
and the incident playbook.

**Evidence freshness.** The age of the oldest record a bundle's claims rest on. A bundle
that claims a guardrail is in force is only saying so about the configuration as it
stood on a date, and freshness is what makes that claim readable.

**Assurance rung.** Which of `same_principal`, `third_party` or `third_party + L3` a
bundle is. A rung is a property of the **bundle**, declared by the producer, and it is
checked by the verifier rather than assumed.
_Avoid_: "ladder" unqualified; `CONTEXT.md` reserves ladders for the Model-Error
Ladder, the No-Progress Ladder and the Session Tier.

**Independent verifier.** The party that is neither the system operator nor the
producer of the evidence. `third_party` and `third_party + L3` differ in whether the
verifier is independent of the **operator** or also independent of the **producer**.

## The bundle

A directory. Openable with a file browser, checkable with a script, and small enough
to email. No archive format is mandated; a directory is what a reviewer can open.

```
bundle/
  manifest.json          the index, the rung, the freshness, and the checks
  stamps/                one Bundle Stamp per part
  build/                 build-time evidence
  runtime/               run-time evidence
  judgements/            the sealed-test record and the paired 2σ result
  dossiers/              the jurisdiction dossier templates, filled
  README.md              a human-readable summary a reviewer reads first
```

### manifest.json

The index. It is the only part a verifier must read to know what it is holding.

| Field | What it carries |
|---|---|
| `schema` | `"raven_evidence_bundle"`, and the version |
| `subject` | what the bundle is about: the system, its version, the deployment |
| `producer` | who produced the bundle, and their role |
| `assurance_rung` | `same_principal` / `third_party` / `third_party + L3` |
| `verifier` | for the two upper rungs: who checked it, and their independence claim |
| `issued_at` | when the bundle was produced |
| `freshness` | the oldest record the bundle's claims rest on, and the age in days |
| `parts` | every part: path, kind, stamp reference, freshness, media type, digest |
| `checks` | what the producer ran, and what a verifier can re-run offline |
| `limitations` | what the bundle does **not** cover, in the producer's own words |
| `registers` | the registers this bundle maps into: NANDO Art. 35, IAF CertSearch |

`limitations` is not a courtesy field. A bundle whose limitations are empty reads as
a bundle with no gaps, which is the one thing a bundle can never be.

### The Bundle Stamp

One per part, in `stamps/`. Five questions, the same five **Provenance** answers for a
field:

| Question | Field |
|---|---|
| What is it? | `kind` — the part type from the registry below |
| What produced it? | `producer` — the process, version and invocation that produced it |
| From what inputs? | `inputs` — the digests of the inputs, so the part is re-derivable |
| When? | `produced_at` — an instant, not a date |
| What is it worth? | `worth` — `evidential`: what the part does and does not establish |

`worth` is where a bundle earns trust, and it is where the house rule applies hardest:
a part that establishes nothing must say so, and a price, a premium or a conformity
claim that was not published is **absent, never zero**.

### The parts

Build-time and run-time, and the judgement.

**Build-time.** The tool-chain **SBOM with VEX**, so a reviewer can say which
components are known-affected and which are not; the **source and build provenance**,
carried as an in-toto statement in a DSSE envelope, signed and logged to a
transparency log; the dependency attestations, one per dependency, each naming its own
source and timestamp.

**Run-time.** This is the half the plan's earlier framing left out, and it is the half
the buyer asks for.

| Part | What it establishes | From the research |
|---|---|---|
| Event log with lineage | what the system did, and what caused what | the machine-generated traffic that makes telemetry overload the live cost problem |
| Decision-authority record | which decisions a human made, which were delegated, and what authority was in force | *human-oversight record*; also "decision authority" is what an insurer's own record of who could stop the system looks like |
| Guardrail configuration | the controls in force, **as they stood on a date** | the guardrail specification, and the kill-switch access |
| Red-team report and remediation | what was attacked, what was found, what was fixed, and when | a **dated** red-team report with remediation evidence |
| Monitoring and alert configuration | what would have been noticed, and by whom | the monitoring and alert configuration |
| Incident log and playbook | what happened, and what the response was | the incident log, and the incident playbook with named kill-switch access |
| Performance data | what the system did against the standard it was assessed against | the underwriters' "performance data" |

**The judgements.** The sealed-test record, the **paired 2σ** result, and the
human-oversight record's scope. A bundle without a judgement part is evidence without
a conclusion; the conclusion is the part a reviewer reads.

**The dossiers.** Per-jurisdiction templates, filled, each in that jurisdiction's own
terms. The PRC network-log record, the EU cross-border transfer record, and a slot
for the GB 45438-2025 metadata injector that stays **empty until the standard is
obtained** — an empty slot with a stated reason is the correct content, and a guess is
not.

## The assurance rung

A field, and the honest part of the spec.

| Rung | Who produced the evidence | What a verifier can check | What it cannot |
|---|---|---|---|
| `same_principal` | the system operator | that the parts are internally consistent and the stamps line up | anything about the operator's claims, which are self-issued |
| `third_party` | the operator, verified by a party independent of the operator | the same, plus that a party other than the operator checked it | that the party is independent of the **producer** of any part |
| `third_party + L3` | as above, with verifier independence from the producer as well | that, plus the verifier's own accreditation and scope | conformity with any standard, unless a standard says so and a body says so |

**L3 is not a flag.** It is a claim about the verifier's independence and
accreditation, and it is checkable against the registers that exist: the verifier's
entry in NANDO's Art. 35 list for the AI Act, or accreditation under ISO/IEC 42006
recorded in IAF CertSearch, which is where ISO itself points a buyer. **The bundle
points at those registers; it does not replace them.** A bundle that ships its own
verifier register is a directory of directories.

## What a verifier can check, offline

The list is short on purpose, and it is the spec's acceptance criterion.

1. The manifest parses, and every part it names is present.
2. Every part's digest matches the one in its stamp.
3. Every stamp's `inputs` resolve to parts that are present, or are declared missing.
4. Every part's freshness is within the bundle's stated limit.
5. Every part that claims a signature carries a signature that verifies against the
   key or the transparency log entry the stamp names.
6. The judgement part's paired result is present and the pairing is stated.
7. The `limitations` field is non-empty.
8. The rung declared is one of the three, and the verifier's independence claim is
   present for the two upper ones.

**A verifier that cannot run a check says so.** "Not checked" and "passed" are
different states, and a bundle that conflates them is worth less than no bundle.

## What the buyers asked for, and where it lands

The research read the published criteria of the market this bundle addresses. This
table is the spec's reason for existing, and the coverage is the acceptance criterion
for the layout above.

| What the buyer asks for | The part that answers it |
|---|---|
| A technical system description | the subject, the README, and the decision-authority record |
| A governance and oversight record | the decision-authority record and the incident playbook |
| An incident history | the incident log |
| Written scope limits | the manifest's `subject`, and the guardrail configuration |
| Evaluation and red-team results | the red-team report and its remediation, with a date |
| Performance data | the performance part of the run-time set |
| A guardrail specification | the guardrail configuration, dated |
| Monitoring and alert configuration | the monitoring and alert configuration |
| Named kill-switch access | the incident playbook, naming who holds it |
| Evidence no older than a stated limit | `freshness`, on every part |
| Supply-chain provenance, for the security reviewer | the build-time half |

## What this spec deliberately does not do

- It does not name a verifier network, a hosted store or a price. Those are the paid
  programme, and they belong on the hosted siblings, not in a donated spec.
- It does not claim that a bundle makes a system compliant, safe or correct.
- It does not invent a signature scheme, a transparency log, a register or a
  certificate.
- It does not assert a price. No carrier in the research published a premium, a rate
  card or an appetite table, and a bundle that quoted one would be quoting nothing.

## Dependencies

- **The design index.** `docs/design-index.json` and its builder are not on `main`
  yet; they arrive with the design-index work, which is a separate branch. **This
  document needs an index entry when that lands**, and the index's own check for a new
  design document will hold this file to that.
- **The sealed test and the decision-authority record** are the two Raven parts this
  spec leans on hardest. The spec states what they must contribute; it does not
  restate what they are.

## Open questions for the founder

1. **Which rung does Raven's own bundle claim?** Raven can produce `same_principal`
   today. Whether a Raven run is ever worth shipping as `third_party` depends on
   whether an independent verifier wants to exist at all, and that is a business
   decision rather than a format one.
2. **How long is the freshness limit?** Agent Certified uses twelve months. A shorter
   window suits a fast-moving system; a longer one suits a slow one. The bundle
   states its limit rather than assuming one, but the default is a decision.
3. **Which parts are in scope for the first release?** The run-time half is where the
   buyer is, and it is also where Raven's own record is thinnest.
4. **Does the PRC and EU dossier come from Raven or from the host?** The templates
   belong here; the data belongs to whoever operates the system.

## Appendix: what was read, and when

Every fact in this spec is from a primary source read on 2026-10-20.

| Fact | Source |
|---|---|
| NANDO lists the AI Act as active legislation with zero notifications; MDR 176, MDD 538 | the European Commission's NANDO search API |
| AI Omnibus in force 2026-07-27; Annex III deferred to 2027-12-02, Annex I to 2028-08-02 | the Commission's regulatory-framework page and its AI Omnibus announcement |
| Underwriting criteria are behavioural: technical description, governance record, incident history, scope limits, evaluation results, performance data | the insurers' own published process and factsheets |
| Agent Certified v2.0's seven weighted dimensions, its evidence list, and the twelve-month limit | the published methodology |
| No incumbent governance, evaluation or GRC vendor ships a signed attestation | each vendor's own documentation, checked for in-toto, SLSA, sigstore, DSSE, Ed25519 and CycloneDX |
| A signed-attestation vendor at $79 per month with a public offline verifier and an unaffiliated-signer claim | that vendor's own documentation and pricing page |
| A signed model registry at $3,000 to $5,000 per month with Ed25519 signatures and a public verifier | that vendor's own pricing page and registry |
| Two vendors state that a receipt does not establish compliance | their own pages |
| No carrier publishes a premium, rate card or appetite table | the carriers' own material |
| ISO/IEC 42001 CHF 225, 42005 CHF 181, 42006 CHF 159 | ISO's own catalogue |
| Signed model attestations already exist in the adjacent market | the Sigstore model-transparency project's own repository and documentation |
