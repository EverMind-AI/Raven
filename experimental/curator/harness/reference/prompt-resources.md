# Prompt resources: authoring and consumption

## Declare and deliver

Import Prompt from experimental.curator.harness.prompts. Define a Pydantic input model in generated Python and load a template relative to that module:

```python
class Inputs(BaseModel):
    goal: str
    evidence: list[str]

GUIDANCE = Prompt.from_file(__file__, "prompts/guidance.md", Inputs)
```

Include the UTF-8 template in Artifact.files and its object reference, such as prompt_defs:GUIDANCE, in the prompt.resources target. This target is a list of object references, not a list of factories. The resource is installed with the same candidate as its consumers.

## Render typed inputs

Use GUIDANCE.render(Inputs(goal=goal, evidence=evidence)). Templates use $name or ${name}; $$ means a literal dollar. Values are checked against Inputs. Strings are inserted as text; other values become JSON. Expansion happens once, so dollar expressions inside evidence remain data. Unknown placeholders, missing fields, extra inputs and invalid types are errors. Empty text is a valid result.

Keep one template body and one input model. Do not repeat variable schemas in another manifest. Existing templates and declared input schemas are visible in current authored files and inspected prompt facts. Preserve all active consumers when revising a shared resource.

## Choose a real consumer

A strategy method can return rendered guidance, a context translator can render a strategy result, and a tool can return on-demand instructions. Skills remain native packages with their own discovery semantics. Prompt.render itself does not create a model call, choose system/user placement or enforce behavior. Declare and exercise the relevant host binding.

When a factory explicitly accepts the host's infer keyword, its async operations may use await infer(messages) for auxiliary text inference. It returns text; the strategy parses and validates its own semantic result. Consult the host inference contract for budgets and failure behavior. Do not call inference from synchronous rendering callbacks.

Loading a template proves its resource contract, not that the worker read or followed it. Check actual callback results, provider messages or tool results with an applicable probe.
