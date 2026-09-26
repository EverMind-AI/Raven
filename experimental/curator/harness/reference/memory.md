# Memory strategy: generation guide

Memory owns which information to retrieve, retain, correct or forget for the task. Its concrete query, context, record and receipt types carry the domain meaning. The supplied public protocol is authoritative for methods.

## Information and evidence

Retrieval returns detached relevant context, including provenance and uncertainty where they matter. It must not modify retained information. A missing result differs from a failed store. Retention evaluates the record and returns a receipt; neither a model statement nor the receipt establishes external task success.

Choose retention rules appropriate to this task. Deduplicate repeated evidence using stable identities. Distinguish correction, forgetting and empty information. Keep one state owner; retrieval, rendering and other strategy consumers do not maintain parallel copies.

## Construction and collaboration

Generate concrete async operations and explicitly constructed dependencies. A store, skill or another strategy can implement part of the behavior. Host evidence is translated into the public types; native callback names do not define memory semantics. The checkpoint contains durable data, not live Python objects. External stores need explicit lifecycle and consistency contracts.
