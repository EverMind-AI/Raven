# Design one coherent change

## Objective

Turn the selected behavior and bindings into a concrete mechanism that implementation and host validation can assess.

## Inputs

Use selection, the current code and bindings, selected_contracts, host facts and history. Read the expanded contracts and query missing mechanisms before designing; initial selection is not proof that a path works. Reuse existing components whose behavior and state are compatible with the proposal.

## Work

For each proposed change, connect the caller or trigger to the semantic operation, available input, state access, result and actual consumer. State the expected difference from the current behavior and what must remain intact.

Specify the cooperation that matters for implementation: representation and concrete types, construction dependencies, one owner of shared rules/state, initialization, subsequent updates and state handoff when replacing code. Keep task state distinct from a turn-scoped wrapper. Private implementation details need not become new public methods or output fields.

When a requirement is expressed in how the work is carried out, specify what the mechanism reads to decide, what it stops or redirects, what the model is told so it can continue, and how the normal path still completes.

Check interaction conditions. A tool request and observed execution evidence may have different authority. A callback can only use information available at its phase. Returned guidance, requested control and the host's actual action have distinct meanings. Account for composition order, permitted budgets and failure behavior from the relevant contracts.

Define verification in terms of observations: what call or input should occur, what state or result should change, and what evidence would contradict the proposal. Include meaningful empty, repeated, rejected or failed operations when they affect the mechanism. A schema check alone cannot validate a behavioral claim.

## Output

Submit one plan using the provided plan-submission action. Use understanding for the factual basis, diagnosis and any remaining nonblocking uncertainty. The person who gave the feedback reads it as your answer, so write it in the language of the task and its feedback and in their terms: what you understood and what the assistant will do differently, without target, strategy, file, tool or module names, which belong in design and changes. Use design for the concrete invocation path, semantic operations, essential types, explicit dependencies, state ownership and failure behavior. This must guide implementation without leaving the core mechanism to be invented there. For each changes entry, connect reason, expected behavior and verification to its target. Cover exactly the current selection. Use state descriptions for resources whose ownership or lifetime the mechanism depends on. If the chosen entries are wrong or incomplete, revise selection first; the host will reopen design with the corresponding contracts. Report a gap if a critical unknown cannot be resolved.

## Completion check

Could an implementer follow the proposal without inventing an invocation path, essential type, state owner or permission? Can the host check the intended behavior? Resolve missing dependencies before submission; keep unverified benefits explicitly framed as expectations.
