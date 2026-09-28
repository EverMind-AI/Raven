# Planning strategy: generation guide

Planning owns the meaning of the current task's plan. Its concrete implementation chooses a representation and typed revision requests. The public protocol supplied with this target is the authority for method signatures and behavior.

## Operations and ownership

`initialize` creates or resumes a task's plan. Repeated initialization preserves progress. `view` returns detached data without changing the plan. `revise` evaluates a typed request, applies any valid change and returns the resulting view. A request can legitimately leave the plan unchanged; invalid requests and failed dependencies remain errors.

The view and revision types belong to the concrete implementation. A checklist and a dependency graph can obey the same operations with different types and policies. Write concrete annotations on the implementation methods so consumers can derive and validate schemas.

A strategy may delegate to an existing component. Tools, skills, prompt content and other strategies can participate in its implementation. Keep one owner of the planning rules and state. A skill guides behavior when read; mandatory validation requires executable logic. A plan item marked complete does not establish that its task or tool actually succeeded.

## Generation and execution

Generate the strategy implementation, its concrete data types and necessary supporting resources. Runtime revisions update task data. Changing the representation, update rules or interaction paths changes the Harness and must pass the host's candidate validation and installation process.

Read the existing implementation, state format and host binding conditions before editing shared code. The host explains the actual triggers, checkpoint lifetime and result consumers. Missing state, an unsupported method and a valid empty plan have different meanings; do not conceal one as another.
