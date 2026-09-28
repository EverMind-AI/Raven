# Understand the current task and Harness

## Objective

Identify the task outcome, relevant current behavior and the mechanism-level gap, if any. Establish enough factual context to choose a justified change.

## Inputs

Use task and worker facts, current_authored, orientation, available target knowledge, prior expectations, feedback and execution observations. When feedback carries `history`, it lists every earlier round's per-item results, the requirements raised with their strength, and the revision made after it. Read the existing code involved in a reported problem; the name of a strategy or component is not its behavior.

## Work

1. Extract the user's outcome and constraints. Separate a new task requirement, an evaluation of an earlier result and an ordinary request to continue.
2. Locate the behavior in the current implementation and host execution path. Distinguish an absent ability from an existing ability that was not exposed, used, correctly implemented or allowed to run.
3. Check what the evidence actually establishes. A disappointing answer alone may not identify the faulty mechanism; absence of an observed event does not by itself prove the mechanism is absent.
4. Identify the information needed to resolve material ambiguity. Read the relevant mechanism topic or current fact, and use the supplied native tools to discover implementation dependencies in the source snapshot. Check the input and state available at the relevant moment.
5. For each requirement, establish how it was handled before: use `history` and the current code to find which earlier revision addressed it, whether as information the model reads or in how the work is carried out, and whether the behavior failed again afterwards.
6. Determine whether a Harness change is warranted. Preserve an adequate implementation when the evidence supports continuing with it.

## Handoff

Carry the task interpretation, observed gap, relevant baseline behavior and material uncertainty into the plan's understanding. Selection and design use this same conclusion. This stage does not submit an additional object or execute the user's task.

## Completion check

Can the proposed problem be tied to a current behavior or an explicitly missing capability? Are its factual basis and any remaining uncertainty distinguishable? If not, obtain the missing information or report the specific unresolved gap.
