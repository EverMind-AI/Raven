# Memory strategy on Raven

## Material and construction

Generate `memory.strategy` with MemoryBinding and a concrete MemoryStrategy. The binding inherits TaskBinding: its factory receives the mutable JSON checkpoint and host Task. All method arguments and returns require concrete JSON-serializable annotations. The host derives runtime checks from these annotations. The public protocol, binding classes and StepView source accompany this material in the generation request.

## Read and retention paths

Before each model call, `query(step)` may request recall; `context(result)` renders a string into this participant's replaceable system addendum. Both entries must be supplied together. None from query skips retrieval; None from context supplies no addendum. An empty concrete query remains a query.

After an iteration, `retain(step)` may produce a record for the strategy's retain method. None skips that observation. This path does not depend on the user_inbound or after_send paths. StepView contains a current transcript, history, question, phase and response. Tool contents retain native untrusted-data boundary markers; response is a proposal, not execution proof. Repeated recovery observations must not duplicate evidence.

## State, failure and verification

The task checkpoint is memory.json. Only successful typed operations persist it. Recall is read-only; retention may change it. A failed operation restores this mapping while preserving the live owner and its explicit delegates. Private state, external stores and effects of explicitly called dependencies are outside this rollback. Factory migrations must be explicit and preserve progress on reconstruction.

Native participant exceptions may be treated as no opinion. The adapter records failures; this path is not a mandatory security gate. Verify context in actual provider requests, retention against execution results, and persistence across turns and Harness installation. Constructor checks alone do not verify retrieval quality.


## Budgeted context composition

Set composition=true to enable the public compose and compact operations. Import ContextRequest and ContextView from experimental.curator.harness.strategies.memory; both methods must use these concrete annotations. This lane is exclusive with query/context addendum ownership; retain remains optional and independent.

The adapter wraps the native ContextEngine through its existing instance socket. It first receives native assembled messages, preserves system/developer messages and the current last message, and computes an input allowance from the native window minus output and tools reserves. compose selects or organizes this projection; if still over the host estimate, compact must reduce it. Required messages must survive unchanged and in order; tool calls and results must stay paired. Over-budget or invalid results fail explicitly. These operations are read-only with respect to the strategy checkpoint and do not silently overwrite raw history.

This supports assembly-time projection. The native MemoryModule retains its own mid-iteration shrink behavior; no module replacement is implied. Use an applicable probe to check projection behavior, budget fit and failure handling. Factory construction alone does not exercise these methods.
