"""Validate and resolve input placeholders shared by delegation surfaces."""

from collections.abc import Mapping
from typing import Any

from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_paths import check_confined, split_reference
from raven.agent.subagent.prompt_placeholders import Placeholder, iter_placeholders
from raven.security.trust import wrap_untrusted


async def resolve_file_placeholder(
    ph: Placeholder,
    inputs: Mapping[str, Any],
    *,
    backend: Any,
    cwd: str,
    runs_root: str | None,
    roots: tuple[str, ...] | None,
) -> str | None:
    """Resolve one placeholder, or ``None`` when it is not this layer's to resolve.

    ``None`` is the composition seam: the ``output`` forms, and an ``inputs``
    entry shaped ``{"node": ...}``, name a graph node, which only the DAG layer
    can resolve. A caller with no graph turns ``None`` into a correctable
    refusal.

    Args:
        ph (`Placeholder`):
            The placeholder to resolve.
        inputs (`Mapping[str, Any]`):
            The caller's inputs, keyed as the ``inputs.<key>`` forms name them.
        backend (`BackendBase`):
            Backend used to read referenced files.
        cwd (`str`):
            Directory that relative reference paths resolve against.
        runs_root (`str | None`):
            Root that a ``@runs/``-prefixed reference resolves against.
        roots (`tuple[str, ...] | None`):
            Absolute directories a file reference may resolve into.

    Returns:
        `str | None`:
            The replacement text, or ``None`` for a reference to a graph node.

    Raises:
        `DagValidationError`:
            On ``input`` form for an undefined or null key, ``input_path`` for
            a literal (non-file) input, a ``ref``/file path that escapes its
            root, or a ``_path`` form naming a file that does not exist.
    """
    if ph.kind in ("output", "output_path"):
        return None
    if ph.kind == "input":
        spec = inputs.get(ph.name)
        # ``file`` before ``node``: an entry carrying both is a file input to
        # every other pass over the spec, so it has to be one here too.
        if isinstance(spec, dict) and "file" in spec:
            check_confined(str(spec["file"]), what="input file", roots=roots)
            text = await read_text(backend, str(spec["file"]), cwd, runs_root, what=f"input '{ph.name}'")
            return wrap_untrusted(text, source="file")
        if isinstance(spec, dict) and "node" in spec:
            return None
        if isinstance(spec, dict):
            raise DagValidationError(
                f"input '{ph.name}' is an object naming neither a file nor a node -- "
                'give it a literal string, {"file": <path>}, or {"node": <id>}'
            )
        if ph.name not in inputs or inputs[ph.name] is None:
            raise DagValidationError(
                f"input '{ph.name}' is not defined -- add it to this task's inputs, or drop {ph.raw} from the prompt"
            )
        return str(spec)
    if ph.kind == "input_path":
        spec = inputs.get(ph.name)
        if isinstance(spec, dict) and "node" in spec:
            return None
        if not isinstance(spec, dict) or "file" not in spec:
            raise DagValidationError(f"input '{ph.name}' has no file path to reference")
        check_confined(str(spec["file"]), what="input", roots=roots)
        return await require_exists(backend, abspath(backend, str(spec["file"]), cwd, runs_root), ph.raw)
    if ph.kind == "ref":
        check_confined(ph.name, what="ref", roots=roots)
        return wrap_untrusted(await read_text(backend, ph.name, cwd, runs_root, what=ph.raw), source="file")
    # ph.kind == "ref_path"
    check_confined(ph.name, what="ref_path", roots=roots)
    return await require_exists(backend, abspath(backend, ph.name, cwd, runs_root), ph.raw)


async def render_template(
    template: str,
    inputs: Mapping[str, Any],
    *,
    backend: Any,
    cwd: str,
    runs_root: str | None,
    roots: tuple[str, ...] | None,
) -> str:
    """Render a template that may reference files and literal inputs only.

    The template is rebuilt in a single left-to-right pass over the placeholder
    spans, so a resolved value that itself contains ``{{ ... }}`` text is never
    re-scanned or re-substituted. Each distinct placeholder is resolved once and
    reused for duplicates.

    Args:
        template (`str`):
            The template to render.
        inputs (`Mapping[str, Any]`):
            The caller's inputs, keyed as the ``inputs.<key>`` forms name them.
        backend (`BackendBase`):
            Backend used to read referenced files.
        cwd (`str`):
            Directory that relative reference paths resolve against.
        runs_root (`str | None`):
            Root that a ``@runs/``-prefixed reference resolves against.
        roots (`tuple[str, ...] | None`):
            Absolute directories a file reference may resolve into.

    Returns:
        `str`:
            The fully substituted text.

    Raises:
        `DagValidationError`:
            When a placeholder cannot be resolved. This includes when it names a
            graph node (which has no meaning without a graph), or for any of the
            reasons that :func:`resolve_file_placeholder` raises.
    """
    check_input_contract(template, inputs)
    parts: list[str] = []
    last = 0
    cache: dict[str, str] = {}
    for start, end, ph in iter_placeholders(template):
        parts.append(template[last:start])
        if ph.raw not in cache:
            value = await resolve_file_placeholder(
                ph,
                inputs,
                backend=backend,
                cwd=cwd,
                runs_root=runs_root,
                roots=roots,
            )
            if value is None:
                raise node_form_refusal(ph.raw)
            cache[ph.raw] = value
        parts.append(cache[ph.raw])
        last = end
    parts.append(template[last:])
    return "".join(parts)


def check_inputs_referenced(template: str, inputs: Mapping[str, Any], *, prefix: str = "") -> None:
    """Refuse a declared input key no placeholder in ``template`` references.

    Material reaches a sub-agent only where a placeholder puts it -- nothing is
    prepended, and nothing is added around a substituted value -- so a declared
    key with no placeholder has no route in at all. Left to render, it would
    simply not arrive, and the run would still read as finished, which is the
    failure the parameter exists to prevent.

    Both delegation surfaces call this, from different places for the same
    reason. The renderer calls it before its first read, so a spawn is refused
    before a file is opened. The graph validator calls it while checking the node,
    so a graph is refused whole with nothing dispatched -- the renderer alone
    would be too late there, and it has its own interpolation loop that never
    reaches this module's.

    Args:
        template (`str`):
            The prompt template whose placeholders name the inputs in use.
        inputs (`Mapping[str, Any]`):
            The declared inputs, keyed as ``inputs.<key>`` names them.
        prefix (`str`):
            Prepended to the message, for a caller that can say which node.

    Raises:
        `DagValidationError`:
            When any declared key is referenced by no placeholder.
    """
    referenced = {ph.name for _, _, ph in iter_placeholders(template) if ph.kind in ("input", "input_path")}
    if stranded := sorted(key for key in inputs if key not in referenced):
        named = ", ".join(f"'{key}'" for key in stranded)
        plural = "s" if len(stranded) > 1 else ""
        verb = "are" if len(stranded) > 1 else "is"
        raise DagValidationError(
            f"{prefix}input{plural} {named} {verb} declared but never referenced -- material reaches "
            "the sub-agent only where a placeholder puts it, so add {{ inputs.<key> }} to the "
            "template where it belongs, or drop the entry"
        )


def check_input_contract(
    template: str,
    inputs: Mapping[str, Any],
    *,
    prefix: str = "",
    require_references: bool = True,
) -> None:
    """Validate input value shapes and the placeholders that consume them."""
    for key, spec in inputs.items():
        if isinstance(spec, str):
            continue
        if spec is None:
            raise DagValidationError(
                f"{prefix}input '{key}' is not defined -- add it to this task's inputs, "
                "or drop its placeholder from the prompt"
            )
        if not isinstance(spec, dict):
            raise DagValidationError(
                f'{prefix}input \'{key}\' must be a literal string, {{"file": <path>}}, or {{"node": <id>}}'
            )
        forms = set(spec) & {"file", "node"}
        if not forms:
            raise DagValidationError(
                f"{prefix}input '{key}' is an object naming neither a file nor a node -- "
                'give it a literal string, {"file": <path>}, or {"node": <id>}'
            )
        if "node" in forms and (extra := set(spec) - {"node"}):
            raise DagValidationError(
                f"{prefix}input '{key}' mixes a node reference with {sorted(extra)}; a node input takes "
                "only 'node' -- an id already names one node per conversation, so there is nothing more to qualify"
            )
        if len(forms) != 1 or len(spec) != 1:
            raise DagValidationError(
                f"{prefix}input '{key}' must contain exactly one of 'file' or 'node', with no other fields"
            )
        form = next(iter(forms))
        target = spec[form]
        if not isinstance(target, str) or not target.strip():
            raise DagValidationError(f"{prefix}input '{key}' {form!r} must be a non-empty string")

    if require_references:
        check_inputs_referenced(template, inputs, prefix=prefix)
    for _, _, ph in iter_placeholders(template):
        if ph.kind in ("input", "input_path") and ph.name not in inputs:
            raise DagValidationError(
                f"{prefix}input '{ph.name}' is not defined -- add it to this task's inputs, "
                f"or drop {ph.raw} from the prompt"
            )
        if ph.kind == "input_path":
            spec = inputs.get(ph.name)
            if not isinstance(spec, dict) or not ({"file", "node"} & set(spec)):
                raise DagValidationError(
                    f"{prefix}input '{ph.name}' is not a file or node input, so '.path' cannot be referenced"
                )


def needs_a_graph(ph: Placeholder, inputs: Mapping[str, Any]) -> bool:
    """Whether resolving this placeholder needs a graph the caller may not have.

    Both shapes count: a placeholder naming a node outright, and an
    ``inputs.<key>`` entry whose value names one. A caller with no graph has to
    refuse either before it says anything else about them -- a capability gate
    speaking first sends the model to a form this surface also refuses, which
    costs a turn and arrives as advice that does not work.

    Args:
        ph (`Placeholder`):
            The placeholder to judge.
        inputs (`Mapping[str, Any]`):
            The call's inputs, for the ``inputs.<key>`` shapes.

    Returns:
        `bool`:
            True when only a graph could resolve it.
    """
    if ph.kind in ("output", "output_path"):
        return True
    if ph.kind not in ("input", "input_path"):
        return False
    spec = inputs.get(ph.name)
    return isinstance(spec, dict) and "node" in spec


def node_form_refusal(raw: str) -> DagValidationError:
    """The refusal a graph-only placeholder gets where there is no graph."""
    return DagValidationError(
        f"{raw} names another task's output, which only run_subagent_dag can "
        "resolve. Reference the file instead: {{ ref:<path> }} for its contents."
    )


def abspath(backend: Any, path: str, cwd: str, runs_root: str | None) -> str:
    """Resolve a checked reference path against the root its prefix names.

    Args:
        backend (`BackendBase`):
            Backend supplying the environment's path semantics.
        path (`str`):
            A reference path that already passed :func:`check_confined`.
        cwd (`str`):
            Root for an unprefixed path.
        runs_root (`str | None`):
            Root for a ``@runs/``-prefixed path.

    Returns:
        `str`:
            The absolute path in the backend environment.

    Raises:
        `DagValidationError`:
            When a ``@runs/`` reference is used where no run history root was
            supplied -- falling back to ``cwd`` would silently resolve it to a
            path in the workdir that means something else entirely.
    """
    root, relative = split_reference(path)
    if root != "runs":
        return backend.abspath(relative, cwd=cwd)
    if runs_root is None:
        raise DagValidationError(
            f"'{path}' refers to this session's DAG run history, which is not available here",
        )
    return backend.abspath(relative, cwd=runs_root)


async def require_exists(backend: Any, resolved: str, what: str) -> str:
    """Return ``resolved``, refusing a reference to a file that is not there.

    Both forms need this, for different reasons, and they used to report the
    same mistake in two different shapes. A ``_path`` form hands over a path
    instead of the bytes, so nothing in this process would otherwise open the
    file at all -- a mistyped name reaches the sub-agent as a plausible path
    and comes back as a confident answer about a file it could not read. A
    contents form does open it, but a bare ``FileNotFoundError`` names neither
    the placeholder that asked nor what the author should fix.

    Args:
        backend (`BackendBase`):
            Backend used for the existence check.
        resolved (`str`):
            The absolute path the reference resolved to.
        what (`str`):
            How the reference was written, named in the error.

    Returns:
        `str`:
            ``resolved``, unchanged.

    Raises:
        `DagValidationError`:
            When no file exists at ``resolved``.
    """
    if not await backend.file_exists(resolved):
        raise DagValidationError(f"{what} points at '{resolved}', which is not a file it can read")
    return resolved


async def read_text(
    backend: Any,
    path: str,
    cwd: str,
    runs_root: str | None = None,
    *,
    what: str | None = None,
) -> str:
    """Read a backend file as UTF-8 text, resolving against its root.

    Args:
        backend (`BackendBase`):
            Backend used for the read.
        path (`str`):
            A relative or absolute path in the backend environment.
        cwd (`str`):
            Directory a relative ``path`` resolves against.
        runs_root (`str | None`):
            Root for a ``@runs/``-prefixed ``path``.
        what (`str | None`):
            How the reference was written. Given, a missing file is reported
            against it rather than as a bare ``FileNotFoundError``.

    Returns:
        `str`:
            The decoded file contents, unfenced: what kind of reference
            asked for them is what decides the fence, and only the caller
            knows that.

    Raises:
        `DagValidationError`:
            When ``what`` is given and no file exists at the resolved path.
    """
    resolved = abspath(backend, path, cwd, runs_root)
    if what is not None:
        await require_exists(backend, resolved, what)
    try:
        data = await backend.read_file(resolved)
    except OSError as exc:
        # Shaped rather than raised: this whole layer's contract is that a
        # correctable mistake in the call comes back as advice, and an
        # unreadable file is one -- a permission, a vanished file, a device that
        # is not a file at all. Raising here escapes the tool as an unshaped
        # traceback with a host path in it.
        raise DagValidationError(f"{what or resolved} could not be read: {exc.strerror or exc}") from exc
    text = data.decode("utf-8", errors="replace")
    return text
