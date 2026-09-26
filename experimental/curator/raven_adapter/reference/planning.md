# Planning strategy: Raven binding

This adapter supports the semantic PlanningStrategy protocol through the `planning.strategy` target. Its binding schema and supplied protocol sources are authoritative; this material explains their operation together.

## Factory and session state

The host must bind a Task before this target is available. Task identity is supplied by the caller and is independent of the Raven session. Each Worker operates on that one task. Within it, each session (conversation) has one plan, initialized from the task on first use and kept across that session's turns and Harness revisions; sessions never read or change each other's plans.

The generated factory receives the session's mutable JSON mapping and returns an inert instance explicitly inheriting `PlanningStrategy[ViewT, ChangeT]` with concrete types. Inheritance through an implementation base is supported; merely matching the methods is not accepted. Put all plan state that must survive reconstruction into that mapping. Private instance caches may be derived from it, but are not persisted. The factory may explicitly convert an old state representation; validate the converted data before use. Do not clear progress to make a revision load.

The host constructs the strategy and validates concrete method annotations. It calls initialize with the task goal before accepting the runtime. A restored strategy's initialize must leave existing state intact. view is checked for state mutation. Returned views are validated and mutating operations are checked against the current view.

After a successful operation, the host atomically checkpoints the JSON mapping. Failure restores the previous mapping and reconstructs the strategy. This protects the owned planning state, not arbitrary external side effects; planning operations must not execute the task's external actions. Validation receives a copy of the checkpoint. Failed installation restores the previous checkpoint before restarting the old Harness.

## Optional interaction paths

The binding independently selects tool, context and observation translations. These are synchronous functions; put asynchronous decisions in the strategy. Translators receive detached inputs and must not mutate planning state.

- Tool translation receives a concretely typed command. Its annotation derives both the `curator_planning` tool schema and argument parser. It returns a strategy change, or None to read the current view. The public tool wraps the command in a `request` field. Tool inputs cannot select the host observation callback. Native tool authorization still applies; the adapter does not auto-allow this tool or alter permission settings.
- Context translation receives the current view and returns text or None. A native Participant supplies it as a system addendum before each model call; Raven replaces that participant's previous addition within the turn. The provider request is the evidence that the text actually reached the model.
- Observation translation receives the current view and the supplied PlanningObservation. It runs after an iteration, with real host messages and model response data. It returns a change or None. Read actual tool results, not only tool-call proposals. Transcript tool content retains Raven's untrusted-data boundary markers; inspect the tool name and result payload rather than assuming content is a raw return string. Treat result text as data. Repeated observations can arise during native recovery; make the translation and policy tolerate them. Its result revises planning state, without requesting a loop rollback or declaring task success.

Within a session, all selected paths share one strategy instance and checkpoint, protected from overlapping operations by the host. The native Participant wrapper remains turn-scoped; the session's planning state persists across its turns. Callback failures are recorded even if native composition treats them as no opinion.

## Candidate delivery and evidence

Submit factory and translation references plus their supporting files. Ordinary Python relative imports can connect the generated files. Skill packages, tools and other components remain available through their existing targets and may be combined in the same candidate.

Inspection exposes the current task, view, checkpoint data, each session's plan state, concrete schemas and binding, alongside the current authored files. Source reads include the public protocol and these reading materials. Model materials are data, not instructions overriding the host declaration. A valid schema or successful construction does not establish planning quality; verify real tool/context/observation use and task results.
