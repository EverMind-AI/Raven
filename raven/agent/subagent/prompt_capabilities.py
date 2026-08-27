"""What a sub-agent can do, and the file-reference gate that follows from it."""

from collections.abc import Iterable
from dataclasses import dataclass

from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_placeholders import Placeholder

# Placeholder kinds that hand the sub-agent a filesystem path. The content form
# to use instead comes from the placeholder itself (``Placeholder.content_form``),
# so a run-qualified reference keeps its qualifier in the advice.
PATH_KINDS = ("output_path", "input_path", "ref_path")


@dataclass(frozen=True)
class AgentCapabilities:
    """What one configured sub-agent can do, as the roster advertises it.

    Defaults are permissive so an agent missing from the map -- a test double, a
    backend built outside the config path -- is never rejected by these checks.
    An unknown ``subagent`` name is already the dispatcher's error to raise.
    """

    stateful: bool = True
    reads_local_files: bool = True
    injectable_skills: bool = True
    injectable_mcps: bool = True
    """Whether per-node ``skills`` / ``mcps`` can be pushed into this agent's
    session at all. Only an in-process raven loop has a skill menu raven controls;
    a cli or acp agent's is its own business, so a list aimed at one does nothing."""


def check_path_placeholders(
    placeholders: Iterable[Placeholder],
    subagent: str,
    *,
    reads_local_files: bool,
    subject: str = "this task",
    escape_hatch: str = "",
) -> None:
    """Reject path placeholders aimed at an agent that cannot read local files.

    Takes already-parsed placeholders rather than a raw template, so a grammar
    error is the caller's own parse step to raise -- on its own, in its own
    words -- and never something this gate catches and re-labels as a
    capability refusal.

    Args:
        placeholders (`Iterable[Placeholder]`):
            The template's parsed placeholders.
        subagent (`str`):
            The agent the template is aimed at, named in the error.
        reads_local_files (`bool`):
            Whether that agent can open a path on this filesystem.
        subject (`str`):
            How the message names the offending template, e.g. ``"node 'b'"``
            for a DAG node. Defaults to a phrasing that needs no such handle.
        escape_hatch (`str`):
            Extra text appended after the suggested content forms, for a fix
            besides switching forms -- e.g. a DAG moving the node to a
            different sub-agent. Empty by default.

    Raises:
        `DagValidationError`:
            When ``placeholders`` carries a ``_path`` form and
            ``reads_local_files`` is false.
    """
    if reads_local_files:
        return

    offenders = [f"{ph.raw} -> use {ph.content_form()}" for ph in placeholders if ph.kind in PATH_KINDS]
    if not offenders:
        return

    raise DagValidationError(
        f"{subject} passes local file paths to sub-agent '{subagent}', which the "
        f"roster tags [no-local-files]: it cannot open them, so the path would reach it as "
        f"meaningless text. Replace each with the content form ({'; '.join(offenders)})"
        f"{escape_hatch}."
    )
