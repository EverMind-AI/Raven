"""Expand the selected contracts and parse native configuration, content and code artifacts."""

import json
import re
from importlib.util import find_spec

from pydantic import BaseModel, ConfigDict

from ...harness import Artifact, Candidate, Declaration, Plan
from ...harness.artifact import ArtifactPath, Selection
from ...harness.declaration import parse_as, schema_for
from ..context.collect import Context
from ..context.render import tool
from . import design

NAME = "submit_artifact"
STAGE_FILE = "stage_file"


class StagedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: ArtifactPath
    content: str


def stage_tool() -> dict:
    return tool(
        STAGE_FILE,
        "Write one complete supporting file of the authored package, such as a Python module, one file per call; "
        "keep each file to a few hundred lines and split a larger module so each call fits the output limit. "
        "Every staged file is added to the artifact you submit or check; a file you also put in the artifact's "
        "files replaces the staged copy. Staging a path again replaces it.",
        schema_for(StagedFile),
    )


def parse_staged(arguments) -> StagedFile:
    return parse_as(StagedFile, arguments)


def materials(context: Context, selection: Selection, plan: Plan) -> dict:
    """What implementation adds: the selection, its contracts and the checked plan."""
    return {**design.materials(context, selection), "plan": plan.model_dump(mode="json")}


def output() -> dict:
    """The artifact submission for the shared tool list; its schema is the general artifact shape."""
    return tool(
        NAME,
        "Submit native payloads and supporting files for exactly the planned targets.",
        schema_for(Artifact),
    )


_REFERENCE = re.compile(r"^([A-Za-z_][\w.]*):[A-Za-z_]\w*$")


def _references(value):
    if isinstance(value, str):
        if match := _REFERENCE.match(value):
            yield match.group(1)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


def missing_modules(values: dict, files) -> list[str]:
    """Modules that values reference as module:attribute but that no file provides and nothing installed supplies."""
    missing = []
    for module in dict.fromkeys(_references(values)):
        path = module.replace(".", "/")
        if f"{path}.py" in files or f"{path}/__init__.py" in files:
            continue
        try:
            installed = find_spec(module.split(".")[0]) is not None
        except (ImportError, ValueError):
            installed = False
        if not installed:
            missing.append(module)
    return missing


def parse(declaration: Declaration, plan: Plan, arguments: dict, staged=None, authored=()) -> Candidate:
    """`staged` files join the artifact's own files, which take precedence; `authored` names files already installed.

    A value referencing a module that neither these files nor an installed package provide is refused here, before
    it can cost a validation round."""
    if not isinstance(arguments, dict):
        return declaration.accept(plan, arguments)
    arguments = dict(arguments)
    for field in ("values", "files"):
        value = arguments.get(field)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                continue
            if isinstance(decoded, dict):
                arguments[field] = decoded
    if staged:
        files = arguments.get("files")
        arguments["files"] = {**staged, **(files if isinstance(files, dict) else {})}
    candidate = declaration.accept(plan, arguments)
    missing = missing_modules(dict(candidate.artifact.values), {*candidate.artifact.files, *authored})
    if missing:
        raise ValueError(
            f"values reference {', '.join(repr(m) for m in missing)}, but no such module is staged, in the "
            "artifact's files or already authored; stage each module's complete source with stage_file "
            f"(for example path '{missing[0].replace('.', '/')}.py') and submit again"
        )
    return candidate
