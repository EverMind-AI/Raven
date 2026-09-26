# Two-level Harness composition

## Scope and generation ownership

A composed deployment has a root Harness and existing child registrations. Both use the same public four-strategy protocols, target declarations, Python artifacts and local generation stages. A child runs a full Raven AgentLoop through native ACP. It is not a Charter-only implementation and cannot delegate to a third level.

The host supplies the child baselines and grants. The native playbook remains authoritative for node tasks, inputs, dependencies, outputs and child names. Runtime instance handles and ACP sessions remain native identities; a registration name is not a session ID. Do not infer node IDs from their spelling: playbook instantiation can give runtime nodes different IDs.

## Root-to-child handoff

The root's inspection includes `scope` and `composition`. Child fact sources describe actual assembled processes; preparation configuration is not execution proof. For each current, added or retired playbook node, the root explains its decision in `Plan.node_reasons`.

A nonempty `<playbook>/nodes/<node>/requirements.json` contains the shared `Requirement` array. Requirements state behavior, evidence, strength and acceptance, not prescribed code. They delegate implementation to the child Curator. Omitted files preserve existing requirements. Explicit removal withdraws management of that node. When the final managed use of a child is withdrawn, the host restores the original artifact supplied for that deployment, which may itself be a previously customized Harness.

For a child selected by nonempty node requirements, `assigned_nodes` includes all known uses of that registration in the proposed playbooks, even nodes without requirements. Those other nodes provide impact context, not additional modification requests. Their tasks and handoffs also participate in the child input identity, so changing a known use invalidates a previously generated result. This does not enumerate ad hoc calls or equate a registration with a runtime session.

The child independently understands, selects, designs and implements. Its sources include its own mechanisms, `execution.current`, and the root's materials under `parent.`. Read relevant supplied material bodies before choosing a mechanism. A capability gap or conflict between uses returns to the root with evidence; it does not authorize dropping requirements or installing only the successful half.

## Authoring and runtime constraints

Root capability selection may narrow ordinary tools and provide additional resources. The host preserves native playbook/delegation tool implementations and child registrations. Before a main decision it restores their native offered definitions, subject to native visibility and permission rules; it does not grant withheld execution permission. Disabling or replacing these tools is rejected. The leaf process uses Raven's native subagent role, which does not construct orchestration tools. Its experiment-owned registry refuses backend resolution and registration expansion, including direct chat and DAG resolution. Binding rejects generated orchestration tools. These constraints govern managed delegation; they are not an arbitrary-code sandbox.

Each child owns its strategy artifacts and home content. Root writes into managed child homes and conflicting child content writers are rejected. Do not use a second copy of the same SOP or state as a synchronization mechanism.

## Checks, activation and evidence

Local candidates use the normal syntax, contract and isolated assembly checks. The complete proposed set is then started on copied state; optional host probes exercise native dispatch and behavior. A construction check proves readiness, not task success. The active deployment changes only at the root's idle boundary. Old processes close before new versions use formal state; a startup failure restores the prior artifacts, owned content and strategy checkpoints.

ACP inspection observes the responding process and reports its actual revision and records. `child.execution` records attach child observations and available native instance associations to the root execution. Missing associations remain unknown. Analyst requirement `locations` describes where evidence was observed, not where Curator must repair it. Root analysis receives the whole feedback; children receive their located requirements, unlocated requirements and the original source materials.

Each local GenerationState owns its counters and retained investigation. Composition totals derive from all attempts, including replaced attempts. A budget pause preserves stage messages, queries, candidate and completed sibling results. Resume requires matching inputs. A root repair invalidates only child results whose baseline, requirements, relevant materials or feedback changed.

## Hosting facts to inspect

The child host uses the product's own configuration renderer, plugins and native ACP/RPC session pipeline. Product-owned model credentials stay pinned; products that inherit the parent model retain the native launch-time inheritance rule. Agent home, task workdir and runtime state directory are different resources. Native per-session mode, MCP and tool permissions still apply. In particular, a playbook's builtin-only skill injection is not equivalent to installing a skill in an ACP child's Harness.

The host-selected baseline, current actual Inspection and declared targets determine support. The same protocol does not prove that every product service is available: check actual plugin/service status, session conditions and execution evidence.

## Hosting integration boundary

The experimental host composes the existing ACP frame, method, permission, question and update components over an externally mounted RPC stack. Private inspection routing and lifecycle wiring live here, with no Raven source edits or global monkeypatches. The adapter reuses native request draining and method routing and depends on the current parent-binding keys used by the ACP pool; compatibility tests cover those version-sensitive seams.
