# Select the necessary authoring entries

## Objective

Choose the smallest coherent set of supported changes that addresses the understood problem. Select by intended behavior and actual effect, not by a target's name alone.

## Inputs

Use available_targets as the effective scope. Relate its effects, channels, phases, state conditions and required knowledge to the current implementation and evidence established during understanding.

## Work

1. Locate the responsible semantic behavior. When a supplied public strategy protocol fits that change, use it as the implementation contract. Native resource entries remain useful for capabilities, knowledge, configuration and execution support.
2. Identify the information/control path: who supplies input, who calls the operation, where its result is consumed and what effect follows. Match this path to the host's supported bindings.
3. Compare reuse, resource adjustment and new behavior. A requirement can be expressed in the Harness in two broad ways. As information the model reads, such as a prompt, a skill or a delegated agent's instructions: this states the knowledge most directly and leaves the model free to apply it with judgement, and whether it is applied is decided by the model each time. Or in how the work is carried out, such as state kept across turns, which steps may follow which, or a check before an action or result takes effect: the execution then carries the requirement whatever the model does in the moment, at the cost of more code and the risk of stopping legitimate work. The two can be combined. Decide from the evidence which expression each requirement needs. A tool definition and an executable, authorized tool are different things.
4. Include the dependencies needed for the mechanism to work together. A new strategy may need a tool, skill or state resource; a resource change does not automatically require replacing an entire strategy.
5. Check current reachability and authority. An entry available in the general host may be unavailable for this instance or phase. Every chosen target and field must remain inside the effective declaration.

## Handoff

Submit targets and understanding through the selection action. In understanding, state the task diagnosis, evidence, the reason for each choice and unresolved questions for design. Select enough to establish a plausible direction; detailed mechanism investigation belongs to the next stage. An empty targets list means retain the existing Harness. The later plan must cover exactly these targets; change selection explicitly if investigation reveals a different need.

## Completion check

For every choice, can you identify both the behavior it contributes and its viable path into execution? Are required dependencies included without unrelated changes? If a necessary path is unavailable, report that gap rather than selecting a nominal substitute with different semantics.
