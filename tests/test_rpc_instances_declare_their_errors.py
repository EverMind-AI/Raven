"""Every wire error these handlers raise is declared for its method.

`rpc-schema/openrpc.json` is the hand-maintained shared contract, and a method's
`errors` list is the half of it a client reads to know what a call can answer
with. Nothing ties that list to the handlers: `test_rpc_schema_match` compares
params and results field by field, and `test_error_codes_match_spec` pins the
code TABLE in `components.errors` -- but neither looks at which methods raise
which, so a handler can start emitting a typed error and every schema and
codegen check stays green. That is how `subagents.instance.set_mode` came to
raise `ConfigValidationError` twice against an `"errors": []` declaration, and
how `subagents.instance.create` nearly did the same.

Scope, stated because a guard that quietly misses is worse than one that says
where it stops: this reads the `raise` statements written DIRECTLY in
`raven/rpc/methods/instances.py`'s own handler bodies. An `RpcError` raised
inside a helper the handler calls is not seen here, and neither is one raised in
another module. What it does catch is the case that actually happened -- a typed
refusal added to a handler and not declared -- which is a change to the same
file the declaration lives beside.

Statically, not by calling: reaching the raise needs a manager, a registry and a
roster per branch, and a test that has to build all of them to notice a missing
line in a JSON file would be answering a different question.
"""

import ast
import json
from pathlib import Path

import raven.rpc.errors as rpc_errors

HANDLERS = Path(__file__).resolve().parents[1] / "raven" / "rpc" / "methods" / "instances.py"
SCHEMA = Path(__file__).resolve().parents[1] / "rpc-schema" / "openrpc.json"


# Which handler serves which wire method, read off the `dispatcher.register`
# calls in the module rather than restated here: a mapping written by hand is
# one more thing that can drift from the file it describes.
def _registered() -> dict[str, str]:
    tree = ast.parse(HANDLERS.read_text(encoding="utf-8"))
    inner: dict[str, str] = {}
    wired: dict[str, str] = {}
    for node in ast.walk(tree):
        # `async def _create(...): return await instances_create(...)`
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("_"):
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    if call.func.id.startswith("instances_"):
                        inner[node.name] = call.func.id
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[1], ast.Name)
        ):
            wired[str(node.args[0].value)] = node.args[1].id
    return {method: inner[shim] for method, shim in wired.items() if shim in inner}


def _rpc_error_names() -> set[str]:
    return {
        name
        for name in dir(rpc_errors)
        if isinstance(getattr(rpc_errors, name), type)
        and issubclass(getattr(rpc_errors, name), rpc_errors.RpcError)
        and getattr(rpc_errors, name) is not rpc_errors.RpcError
    }


def _raised_by(func_name: str) -> set[str]:
    tree = ast.parse(HANDLERS.read_text(encoding="utf-8"))
    typed = _rpc_error_names()
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == func_name:
            out = set()
            for raise_ in ast.walk(node):
                if isinstance(raise_, ast.Raise) and isinstance(raise_.exc, ast.Call):
                    callee = raise_.exc.func
                    if isinstance(callee, ast.Name) and callee.id in typed:
                        out.add(callee.id)
            return out
    raise AssertionError(f"{func_name} is not a top-level function in {HANDLERS.name}")


def _declared() -> dict[str, set[str]]:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return {m["name"]: {e["$ref"].rsplit("/", 1)[-1] for e in (m.get("errors") or [])} for m in schema["methods"]}


def test_the_handler_map_was_actually_found() -> None:
    """The control for every assertion below.

    Every check here is "raised minus declared is empty", which an empty map
    satisfies vacuously -- so a rename that breaks the AST walk would turn this
    whole file green while measuring nothing.
    """
    found = _registered()
    assert "subagents.instance.create" in found
    assert "subagents.instance.set_mode" in found
    assert len(found) >= 6


def test_every_typed_error_these_handlers_raise_is_declared() -> None:
    declared = _declared()
    missing: dict[str, set[str]] = {}
    for method, func in sorted(_registered().items()):
        gap = _raised_by(func) - declared.get(method, set())
        if gap:
            missing[method] = gap
    assert not missing, "raised but not declared in rpc-schema/openrpc.json:\n" + "\n".join(
        f"  {m}: {sorted(errs)}" for m, errs in sorted(missing.items())
    )
