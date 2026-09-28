# Action strategy: generation guide

Action owns decisions about proposed actions, observed outcomes and recovery. Its proposal, failure and decision types express the task's criteria, not the callback vocabulary of one host.

## Judgment and evidence

`assess` decides whether and how progress should continue; the proposal type must distinguish an intended action from an actual result. `recover` uses the same decision language when normal progress fails. A decision to retry does not undo prior external effects. Keep repeated observations and retry limits in mind.

Planning can explain remaining work, Memory can provide evidence and Capability can explain available mechanisms. Such collaboration uses explicit dependencies and their public methods; Action does not copy their state or become another executor. The generated factory may compose delegates. There is no automatic cross-target dependency injection.

## Applying a decision

The host maps the semantic decision to a supported execution effect. A terminal recovery point may support fewer effects than ordinary assessment. Unsupported decisions must remain errors, not silent acceptance. Missing evidence is not proof of success. Prompt guidance may help future decisions; mandatory judgment belongs in executable code.
