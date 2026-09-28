# Capability strategy on Raven

## Resources and lifetime

Generate `capability.strategy` with CapabilityBinding. The TaskBinding factory receives the owned JSON mapping and host Task. `provide()` returns CapabilityResources: native Tool objects and skill-package file text. It runs once during generation assembly and must be inert and must not mutate retained state. Invalid objects, duplicate or colliding tool names, escaping file paths and undiscoverable skills are rejected.

Tools are admitted through Raven's actual Tool contract and registered before turns. Native permissions and gates remain in force. Skills are staged under a private generation directory and added to native local skill sources. They do not overwrite agent-home files. A normal SKILL.md package is required. Native skill activation, requirement checks, summary/body retrieval and always injection remain native behavior. Verify the actual skill path and actual model input; a declared file does not prove consumption.

## Runtime selection

The optional `need(offered, step)` translation calls the strategy's typed async select operation before model input. It requires at least one consumer: `expose(selection)` for tool exposure or `context(selection)` for usage knowledge. Raven asks select_tools before system_addendum; the turn wrapper shares that selection with both consumers. If no tool-list callback ran, context obtains the selection from the current StepView tools. The next model request obtains a new selection. Expose returns unique names from the offered definitions, an empty list to expose none, or None to keep the native offer. It cannot manufacture schemas or grant execution authority. This binding narrows tool exposure; native select_tools remains available when a task needs its broader behavior. Skills remain discoverable resources under native selection, rather than tool names in this list. The optional context renderer can supply usage guidance from the same semantic choice. Its direct text is a generated addendum, distinct from native skill discovery and body retrieval.

## State and evidence

capability.json retains selection state per session, across that session's turns and revisions. Tool-private attributes and skill files are not this checkpoint. The caller owns native execution; tools needing durable state must use an explicitly owned store rather than privately modifying the strategy's checkpoint. Resources are reconstructed on installation. Failed selection restores the JSON mapping without replacing the live strategy or its installed resources; private mutations and external effects are not rolled back. Native runtime and service lifecycles are not extended by this target; services use their existing target.

Inspect reports installed resources and operation schemas. Records show provide, selection and resource installation; native execution evidence shows actual tool calls and model inputs. Admission, selection and successful task execution are different facts.
