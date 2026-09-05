# Evidence governance templates

Use these as compact interfaces. The normative meanings live in
`skill-design/governance/CONTRACT.md`. A domain may add fields or stronger gates, but must not
merge the axes or change these field meanings. Use stable IDs and evidence-root-relative paths;
never store credentials or private reviewer details.

Keep the artifact header orthogonal:

```yaml
artifact_id: artifact-...
artifact_ref: path-or-resource-ref
build_hash: sha256:...
work_status: not_started | active | waiting | blocked | complete
artifact_lifecycle: draft | candidate | superseded | withdrawn | archived
canonical_record_ref: null
release_record_ref: null
authority_graph_ref: evidence/authority-graph.yaml
tool_evidence_refs: []
domain_gate_refs: []
promotion_record_refs: []
```

A new hash does not inherit old gates, review, external coverage, or promotions. Canonical and
release values come from their registries; Gallery selection is not a registry record.

## Authority graph

```yaml
authority_graph_id: authority-...
artifact_id: artifact-...
build_hash: sha256:...
nodes:
  - authority_id: source-master
    concern: named-concern
    role: authoritative
    owner_ref: source/native-master
    native_format: named-native-format
    tool_id: tool-profile-id
    version_or_hash: ...
  - authority_id: derived-view
    concern: rendered-view
    role: derived
    owner_ref: output/rendered-file
    native_format: named-output-format
    tool_id: renderer-profile-id
    version_or_hash: sha256:...
edges:
  - from: source-master
    to: derived-view
    transform: build-command-or-operation-id
    invocation_evidence_ref: evidence/build.json
    result_hash: sha256:...
```

Checks:

- Every applicable concern has exactly one authoritative owner; an owner may be a versioned
  native bundle.
- Multiple native masters are valid only for different concerns.
- Derived files, exports, indexes, screenshots, and Viewer caches do not own upstream concerns.
- Every derived node has a reproducible edge or disclosed human handoff; reject hidden cycles.

## Tool-use evidence

Create one record for every selected, considered, unavailable, handed-off, or rejected tool.

```yaml
tool_use_id: tool-use-...
tool_profile_id: registry-profile-id
task_capability: capability-being-filled
status: used | unavailable | human_handoff | considered | rejected
selection_reason: ...
alternatives_considered: []
dependency:
  package_or_product: ...
  installation_ref: ...
  resolved_version: ...
  license: ...
  license_evidence_ref: ...
invocation:
  kind: import | cli | api | gui
  entry: ...
  evidence_ref: ...
editable_master:
  authority_id: ...
  owner_ref: ...
  native_format: ...
rebuild_or_export:
  operation: ...
  evidence_ref: ...
  output_refs: []
  output_hashes: []
consumer_or_pixel_evidence_refs: []
known_limitations: []
```

Only `used` requires all five groups: dependency/version/license, real invocation, editable
master, rebuild/export, and current pixels or consumer proof. If a GUI or commercial product is
not actually available, use `unavailable` or `human_handoff`; planned future use is not use.

Before replacing a mature capability, add:

```yaml
custom_capability:
  required_capability: ...
  candidate_tool_profiles: []
  capability_probe:
    method: ...
    inputs: []
    evidence_refs: []
    result: pass | fail | unavailable | inconclusive
  gap:
    missing_behavior: ...
    why_it_blocks_the_contract: ...
  minimal_custom_boundary:
    owns: []
    must_not_reimplement: []
    authority_concerns: []
  coherence_evidence:
    data_refs: []
    interaction_refs: []
    visual_refs: []
    rebuild_or_export_refs: []
```

Only a probe that executes the required capability and returns `fail` proves a capability gap.
`unavailable` proves an availability gap and returns to another tool or `human_handoff`; it does
not authorize a custom substitute.

## Domain gate and review evidence

```yaml
gate_id: domain.gate-name
artifact_id: artifact-...
build_hash: sha256:...
status: pending | passed | failed | blocked | not_applicable
trigger: ...
inputs: []
actions: []
evidence_refs: []
claim_ceiling: []
failure_return: domain-state-or-contract-step
not_applicable_reason: null
```

`not_applicable` requires an accepted scope reason; unavailable, missing, and untested are not
not-applicable.

Review is an event, not an artifact state:

```yaml
review_id: review-...
artifact_id: artifact-...
build_hash: sha256:...
reviewer_id: stable-non-secret-id
reviewer_role: ...
relationship_to_author: self | independent_internal | independent_external
method: ...
claim_refs: []
result: pass | partial | fail | blocked
findings_refs: []
evidence_refs: []
reviewed_at: ...
```

Independent external coverage adds and requires:

```yaml
coverage_id: coverage-...
claim_refs: []
artifact_id: artifact-...
build_hash: sha256:...
reviewer_id: stable-non-secret-id
reviewer_role: ...
independence: independent_from_author
method: ...
result: pass | partial | fail | blocked
evidence_refs: []
reviewed_at: ...
```

Assurance values are `UNREVIEWED`, `SELF_REVIEW_ONLY`, `INDEPENDENT_CLAIM_REVIEWED`,
`USER_VALIDATED`, and `FIELD_VALIDATED`. Store assurance per claim. Never replace coverage with
`external: true` or use `SELF_REVIEW_ONLY` as a lifecycle, work, release, or gate status.

## Scoped promotion record

```yaml
promotion_id: promotion-...
record_status: active | superseded | withdrawn
artifact_id: artifact-...
build_hash: sha256:...
scope:
  domain: ...
  subtype: ...
  artifact_parts: []
  consumers: []
  contexts: []
identity: whole | component | protocol | diagnostic | negative
pool: none | internal-positive | externally-validated
positive_for: []
not_evidence_for: []
excluded_claims: []
claim_ceiling: []
domain_gate_refs: []
authority_graph_ref: evidence/authority-graph.yaml
tool_evidence_refs: []
review_refs: []
assurance:
  - claim_refs: []
    level: UNREVIEWED | SELF_REVIEW_ONLY | INDEPENDENT_CLAIM_REVIEWED | USER_VALIDATED | FIELD_VALIDATED
    review_refs: []
external_coverage_refs: []
known_limitations: []
created_at: ...
supersedes: []
```

Promotion requires the exact evaluated build, passing applicable gates, complete authority/tool
evidence, and visible exclusions. `externally-validated` additionally requires passing,
independent, role-qualified, hash-matching coverage for every promoted positive claim.
Promotion changes neither lifecycle, canonical, release, nor Gallery selection. A changed build,
wider scope, or stronger claim requires a new record.

## Failure return

Return to the first assumption disproved by evidence and preserve unrelated valid work:

| Failure | Return to | Preserve |
| --- | --- | --- |
| Wrong domain, consumer, or claim | routing and scope | inventory and observations |
| Capability fails or tool is unavailable | tool selection or `human_handoff` | accepted requirements and probe |
| Tool is named but not evidenced as used | real invocation/tool record | implementation independent of that claim |
| Authority owner conflicts or is missing | authority graph | valid native masters |
| Domain behavior fails | owning domain gate | unrelated passed gates |
| Render differs from authority | transform/render step | authoritative master |
| Review is stale | review current hash | current build and non-review evidence |
| One promotion loses coverage | that scoped record | independent promotion records |
| Release/canonical is wrong | owning registry | artifact and promotion history |

Do not repair a governance label by changing the artifact unless the artifact is the failed
assumption.
