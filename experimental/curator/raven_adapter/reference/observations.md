# Runtime observations and their consumers

## Authority and representation

The selected contract includes the actual source definitions and conversion functions for its observations. Use those definitions for field structure. An `Any` annotation on a host field does not mean arbitrary API JSON is supplied. Required protocol definitions are shared across targets; current tools, components, state and permissions still come from Inspection.

| Input | Actual representation | Meaning |
|---|---|---|
| StepView.response | Detached native LLMResponse, or None before a response exists | The model proposal, not a receipt that its tools ran |
| LLMResponse.tool_calls | ToolCallRequest objects | Read name and arguments directly from each native request; arguments are already parsed |
| StepView.transcript / history | Sequences of message dictionaries | Stored messages use the native message serialization; assistant tool calls have the wire structure produced by openai_tool_call |
| PlanningObservation.response | JSON-compatible projection of the native response through plain | Read dictionary fields; the projection preserves native field names, not the API wire layout |
| PlanningObservation.messages | This turn's transcript through the completed iteration | Match actual tool results to call IDs; a proposed call alone proves no effect |
| Offered tools | Definition dictionaries, including their function schema | Describe model-visible proposals; neither registration nor exposure grants execution permission |

Do not reuse a wire-message parser on the native response, or an object-attribute parser on PlanningObservation's JSON. Decode an embedded wire arguments string only on a path that actually supplies one. Do not convert an unfamiliar representation to an empty success. Valid None, an empty tool batch, and an unreadable input have different meanings.

## Timing and evidence

Before a model decision, response can be None. At execute_tools, response contains the proposed batch; its results do not yet exist. At after_iteration, inspect the transcript for what actually ran: rejection, skipped calls and failures are not successful execution. Native recovery may repeat observations. Use phase and call identities, not the presence of response text, to decide what evidence is available.

A tool request may itself ask a native routing tool to invoke another operation. Match the actual registered tool's contract; do not assume every desired effect appears as a top-level tool name. Read its implementation when the distinction matters. Historical results retain native untrusted-data boundaries.

## Ownership and consumption

Observations are detached and read-only. Their mutation does not update the loop. Strategy state lives in its supplied checkpoint: Memory is task-scoped; Planning, Action and Capability are conversation-scoped. A PlanReader returns the last rendered plan for the current conversation, not the effect of a still-pending proposal. Refer to the selected binding for the permitted state changes and synchronous/asynchronous method forms.

Translations produce the concrete semantic input the strategy declares. They may intentionally filter irrelevant observations or return None where their binding permits it. Such filtering must follow the design; successful type validation cannot prove that relevant information was preserved.

Return values have different consumers: Planning revisions update plan state, Memory results contribute context or retention, Capability selection narrows offered definitions, and Action decisions enter native review composition. None and empty outputs follow each binding's contract. Review resampling is bounded and cannot undo executed tools; mandatory per-tool refusal uses the native ToolGate path. Consult the selected role's reference for its exact consumer.

## Verification

For each affected path, establish the input representation, decision, consumer and observable effect. Exercise relevant normal, absent and failure inputs with native types. For a claimed control, test both the condition that permits the action and the condition that refuses it, then check actual execution, not only the decision object. For memory or planning changes, check retained state and repeated observations as appropriate.

Host validation may only construct the candidate. A validation.probe record says whether the caller supplied a behavioral probe and whether that probe completed; completion proves only the assertions that probe performed. It does not certify every Plan.verification claim. Missing behavioral coverage remains unverified, not a successful behavioral test.
