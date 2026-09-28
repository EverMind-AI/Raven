# Capability strategy: generation guide

Capability owns the mechanisms an agent can use and the knowledge needed to use them. Tools and skills are both relevant, although an executable tool and a readable skill have different consumers and effects.

## Supply and choice

`provide` constructs inert resources for installation. `select` chooses capabilities for a need; it does not execute tools or grant permissions. Resource types and selection types need not match. A valid empty selection, default delegation and an unavailable resource must remain distinct.

A tool's schema is its calling contract. A skill teaches knowledge or behavior only if its content is read; discovery alone is insufficient. Guidance is not executable enforcement. Resources can delegate to ordinary components or other explicitly supplied strategy instances, with one owner for shared state.

## Scope of changes

Generate resources and selection rules together when their behavior depends on each other. Preserve contracts used by other strategies. Construction must not start services or perform task actions. Use the host's actual resource and lifecycle contracts when binding the output. Supporting files, prompts, ordinary tools and skills remain independently selectable native targets where appropriate.
