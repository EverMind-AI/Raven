# Strategy behavior, prompt resources and model calls

## What the public operations express

The four Strategy protocols define collaboration operations. They do not prescribe one algorithm, one prompt shape or one model invocation per method. Deterministic code can implement an operation; an explicitly supplied model client can help implement it. An async annotation alone does not supply that dependency.

The current public operations form a runnable minimum, not an exhaustive description of all possible Harness behavior. Memory covers recall and retention. Planning covers plan initialization, viewing and revision. Capability covers inert resource construction and selection. Action covers assessment and recovery, plus optional pre-decision guidance. Memory can also compose and compact message projections when the composition binding is enabled. Separate operations should be added only when an independent caller needs their semantics, rather than turning every implementation detail into a public method.

## Three different prompt paths

1. **Guidance for the worker model.** A strategy returns task information or a decision; a host translation renders it into a supported model-input path. Planning and Memory context renderers and Capability's selection renderer produce replaceable addenda. Action's native resample translation uses inject to carry a correction; reason alone is diagnostic. These paths use the worker's next normal model call, not a second model call inside the strategy.
2. **Agent-requested guidance or operation.** The worker can read an installed skill or invoke a registered tool. Skill bodies and tool descriptions teach use; a tool may return instructions or structured state. Planning has a direct generated tool binding. Capability can supply ordinary tools and skill packages. Memory and Action currently have no automatic tool wrapper that exposes their active bound instances.
3. **Inference inside a strategy.** Summarizing evidence, evaluating a proposal or revising a plan may need an auxiliary model call. The concrete strategy should receive a host-granted model callable/client explicitly, own the prompt and typed result handling, and leave translation callbacks synchronous. A semantic strategy factory can explicitly accept the optional keyword-only infer dependency. The host supplies a bounded async infer(messages) callable returning text; consult its supplied contract. Deterministic strategies need not accept it. Do not create credentials or infer permissions from async. Native plugin factories have their own supplied context contract; consult that contract rather than assuming it applies to a Strategy factory.

## Prompt organization

Use the supplied Prompt type and declare its module:symbol object reference in prompt.resources. Its UTF-8 template lives in Artifact.files and loads relative to the declaring module's __file__. Prompt.render validates the declared Pydantic input model and substitutes named variables once. Separate stable instructions and output requirements from task data, retrieved evidence and current state. Fill variables at the point where their values are known. Keep input provenance and the expected output contract explicit.

Do not add a mandatory prompt or model field to every public strategy. A static rendering function, a skill and an auxiliary model's system prompt have different consumers. The strategy owns task-specific reasoning rules; the adapter owns native timing and message placement. Register each resource once and make its consumers explicit.

## Current integration boundaries

The existing semantic bindings do not cover every possible interaction: Memory composition covers context assembly and budget-triggered compaction of that projection, but does not replace the native mid-iteration shrink path. Capability has no general runtime installation operation. Action guidance supplies a replaceable pre-decision addendum; mandatory tool blocking still requires native gates. Native context, advice, tool, skill and gate targets remain selectable, but their existence does not make those missing public operations implicitly available.

Strategies can compose explicitly constructed delegates. The adapter does not automatically inject other active strategy instances. Do not duplicate another strategy's checkpoint or rebuild an independent owner and describe it as shared state. Check host support before proposing such a mechanism; report a concrete integration gap if it cannot be assembled.
