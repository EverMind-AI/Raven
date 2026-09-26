# Action strategy on Raven

## Construction and semantic decisions

Generate `action.strategy` with ActionBinding. Its TaskBinding factory receives the owned JSON checkpoint and host Task. The concrete async assess and recover methods must share one concrete decision return type. The public strategy does not return a native verdict unless that is an intentional concrete representation; separate synchronous translations normally convert semantic decisions to host effects.

## Pre-decision guidance

The optional guidance translator receives StepView and returns the concrete assessment input type or None. A request calls async guide on the strategy, returning str or None. This operation may render a declared Prompt resource. The native system_addendum consumer replaces prior guidance before the next model call. Selecting guidance requires an implemented method; an inherited unsupported method is rejected during construction.

## Assessment path

The paired proposal and decision entries run through native review at execute_tools and after_iteration. Proposal None skips assessment. The translator receives detached StepView; distinguish unexecuted tool proposals from real transcript results. The decision translation must produce the supplied ReviewResult or None. Native composition decides accept, bounded resample or end. reason is diagnostic; inject puts corrections into subsequent model input. A rollback cannot undo tools that already ran.

## Recovery path

The paired failure and reply entries run through native salvage at answerless. Both can return None to defer. The strategy still produces its semantic decision; reply converts a supported terminal choice into text. It must reject choices that require an unavailable restart. Native synthesis or a scheduled rerun can bypass this path. This is not a general recovery scheduler.

## Persistence and failure

Successful typed decisions persist action.json; invalid arguments or results restore its owned mapping. Other owners' state and external side effects are not part of that transaction. The factory may explicitly migrate restored state; private attributes are reconstructed at generation installation, not rolled back after each operation. Participant failures may be swallowed by native composition and must not be interpreted as fail-closed enforcement. The adapter records them. Mandatory per-tool blocking uses the existing ToolGate target.

Verify real provider retries, injected correction, refused or executed tool calls, final replies, and retained state. A decision record alone is not proof that native composition applied that decision.

## Reading the plan

A planning strategy owns the conversation's stage, gates and collected facts; other components enforce against it instead of keeping their own copy. The action strategy factory, a participant entry point (review, salvage, advise) and a plugin component factory (tool gate, hook, tool) may declare a keyword-only `plan` parameter; the host passes a `PlanReader`. Call it when judging, not when constructing: it returns a detached copy of the plan planning last produced for the conversation now running, or None before planning has run for it or when no planning strategy is bound, and a component must treat None as "no plan yet" rather than as permission. The plan covers what planning observed up to the last completed iteration; the output under judgment is in the component's own arguments. A gate that refuses a call should say which stage or condition is unmet, so the model can do the missing step instead of retrying.
