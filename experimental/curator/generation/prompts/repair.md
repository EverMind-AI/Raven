# Repair from host validation

## Objective

Use actual validation results to fix the cause of a failed candidate while preserving the task, valid progress and host constraints.

## Inputs

Read the checked plan, candidate, validation_errors and validation_observations. The candidate is the proposed version; current_authored describes the active version. Earlier readings remain available. A check may have covered construction without exercising every runtime path or external resource. A validation.probe status of not_supplied explicitly means no host behavioral probe was supplied; completed means only that the supplied probe finished, not that all planned behaviors were verified.

## Diagnose

Locate the failed boundary: source syntax/import, construction, method or result contract, state restoration, resource/permission availability, or observed behavior. Compare the actual result with the plan's expectation. A missing observation is not interchangeable with a recorded refusal or a legitimate no-op.

For a runtime contract failure, use the recorded operation, arguments and pre-call state where available. Preserve the failure's artifact and scope attribution. Reproduce the failing path and check relevant normal paths; successful construction alone does not demonstrate a repair. An unchanged proposal is still subject to the host's validation.

Read a relevant source or fact when the failure reveals a missing dependency or misunderstood host mechanism. Resolve the specific gap; do not repeatedly rewrite code against the same unsupported assumption.

## Choose the response

- For a local defect, preserve the checked plan and submit a complete corrected artifact for its targets. Keep working parts and unrelated files intact.
- For an incorrect interaction path or state design, request plan revision with its reason. The host reopens design before implementation. For incorrect or missing targets, revise selection and then design. Existing candidate files are discarded; old checks remain historical evidence. Do not smuggle new targets into the current artifact.
- For a necessary unavailable capability, grant or unresolved fact, report the concrete gap and its consequence instead of concealing the failure.

Do not weaken validators, bypass native gates, erase current state, swallow exceptions or manufacture evidence to make a check appear successful. Treat every repaired benefit as an expectation until the appropriate path is observed.

## Output and completion check

Submit one corrected artifact, one design-revision request, one revised selection, or one gap report as appropriate. The repair must address the supplied diagnostic, remain consistent with the active state and retained consumers, and have a clear observable verification condition. Budgets remain host-controlled; do not claim success when they are exhausted.
