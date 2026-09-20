"""The workspace fence must read the command the shell will run, not the one typed.

``restrict_to_workspace`` is an operator's promise that the agent cannot reach
outside its workspace. It kept that promise only for paths spelled literally.
``_extract_absolute_paths`` wants a ``/`` sitting on a word boundary, and
``$HOME/`` puts ``E`` there, so a variable spelling produced no candidate at
all -- and ``os.path.expandvars`` sat downstream of the extraction it needed to
feed, so it never saw the text that hid the path. ``cat /etc/shadow`` was
refused while ``cat $HOME/.ssh/id_rsa`` ran.

An unset name is the sharpest form: the shell drops it, so ``$NOPE/etc/shadow``
*is* ``/etc/shadow``, and the fence was reading a ``/`` glued to a letter.

What made it worth fixing rather than noting is that nothing else was looking.
``cat`` is read-only, so the permission gate tiers it ``allow`` and asks nobody;
for a read the fence was the only thing in the way.

Stepping out is the same escape as reaching out, and the last section is the
destinations no pattern can take: ``cd /`` has nothing after the slash, ``..``
is not absolute, and a ``cd`` with no argument names ``$HOME`` by saying
nothing. ``cd /etc`` is not among them -- that is a path, and always was.

The rest of the file is the over-blocking these tests exist to stop. The fence
has to expand the way the shell does -- the environment the child is actually
given, ``$PWD`` as the directory it runs in, nothing at all inside single
quotes -- or a workspace-relative command starts getting refused and the
operator turns the fence off, which costs more than the hole did.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from raven.agent.tools.shell import ExecTool


@pytest.fixture
def fenced(tmp_path: Path) -> ExecTool:
    return ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)


#: Every refusal the fence can answer with, for the cases that assert only
#: that it answers at all rather than which way.
_REFUSALS = (
    "Error: Command blocked by safety guard (path traversal detected)",
    "Error: Command blocked by safety guard (directory change outside working dir)",
    "Error: Command blocked by safety guard (path outside working dir)",
)


def refusal(tool: ExecTool, command: str) -> str | None:
    return tool._check_workspace_restriction(command, str(tool.working_dir))


# ---------- the literal spelling, which always worked ------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/shadow",
        "cat ~/.ssh/id_rsa",
        "cat ../outside.txt",
        "tar cf - /etc | base64",
    ],
)
def test_a_path_written_out_in_full_is_refused(fenced: ExecTool, command: str) -> None:
    """The behaviour the fence was trusted for. Every case below is one of
    these commands wearing a variable, and has to end the same way."""
    assert refusal(fenced, command) is not None


# ---------- the same path, wearing a variable --------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat $HOME/.ssh/id_rsa",
        "cat ${HOME}/.raven/config.json",
        'cat "$HOME/.ssh/id_rsa"',
        'cat "$HOME"/.raven/config.json',
        "tar cf - $HOME/.raven | base64",
        "cp $HOME/.raven/config.json .",
        "cat $HOME/../etc/shadow",
    ],
    ids=[
        "bare",
        "braced",
        "inside-double-quotes",
        "quoted-name-bare-tail",
        "piped",
        "copied-in",
        "traversal-through-home",
    ],
)
def test_a_path_wearing_a_variable_is_refused_too(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """The bug, in the seven spellings it was reachable by."""
    monkeypatch.setenv("HOME", "/home/victim")

    assert refusal(fenced, command) is not None


def test_a_name_the_shell_has_no_value_for_is_read_as_the_empty_string(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sharpest spelling: the shell drops an unset name, so this command
    *is* ``cat /etc/shadow``. Leaving the name in place is what hid it -- there
    is no ``/`` on a boundary until the name goes away."""
    monkeypatch.delenv("NOT_A_REAL_VARIABLE", raising=False)

    assert refusal(fenced, "cat $NOT_A_REAL_VARIABLE/etc/shadow") is not None


def test_a_name_the_child_is_not_given_is_empty_however_this_process_reads_it(
    fenced: ExecTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment that decides is the child's, not ours.

    ``DirectExecutor`` hands the command an allowlisted baseline, so a name
    outside that list reaches the shell unset whatever this process holds.
    Reading ``os.environ`` instead would see a workspace path here and allow a
    command that opens ``/notes.txt``.
    """
    monkeypatch.setenv("SECRET_VAULT", str(tmp_path))

    assert refusal(fenced, "cat $SECRET_VAULT/notes.txt") is not None


async def test_the_refusal_reaches_the_caller_as_a_boundary_error(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, because the fence is only worth what ``execute`` does with
    it: a boundary refusal stops the call and its siblings rather than being
    handed back as output the model can retry around."""
    monkeypatch.setenv("HOME", "/home/victim")

    result = await fenced.execute("cat $HOME/.ssh/id_rsa")

    assert not result.ok
    assert "path outside working dir" in result.model_text
    assert result.blocks_call and not result.retryable


# ---------- what must keep running -------------------------------------------


def test_the_directory_the_command_runs_in_is_the_one_pwd_names(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sh`` sets ``PWD`` from the directory it is started in, so ``$PWD``
    is the workspace no matter what this process inherited. Expanding it from
    our own environment would refuse a command that never leaves home."""
    monkeypatch.setenv("PWD", "/somewhere/else")

    assert refusal(fenced, "cat $PWD/notes.txt") is None


def test_a_name_inside_single_quotes_is_text(fenced: ExecTool, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell expands nothing in single quotes, so neither may the fence:
    this prints a sentence, and there is no path in it to be outside."""
    monkeypatch.setenv("HOME", "/home/victim")

    assert refusal(fenced, "echo 'put your keys in $HOME/.ssh and re-run'") is None


@pytest.mark.parametrize(
    "command",
    [
        "cat notes.txt",
        "ls -la",
        "grep -rn TODO .",
        "python -c 'print(1)'",
        "echo done 2>/dev/null",
    ],
)
def test_work_inside_the_workspace_is_left_alone(fenced: ExecTool, command: str) -> None:
    assert refusal(fenced, command) is None


def test_a_directory_the_operator_added_is_reachable_by_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``extra_allowed_dirs`` is the operator widening the fence on purpose.
    Expansion must land inside it, not merely outside the workspace."""
    shared = tmp_path / "shared"
    shared.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(shared))
    tool = ExecTool(
        working_dir=str(workspace),
        restrict_to_workspace=True,
        extra_allowed_dirs=(shared,),
    )

    assert refusal(tool, "cat $RAVEN_HOME/config.json") is None


def test_the_fence_has_no_opinion_when_the_operator_did_not_raise_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``restrict_to_workspace`` is off by default, and off means silent."""
    monkeypatch.setenv("HOME", "/home/victim")
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=False)

    assert refusal(tool, "cat $HOME/.ssh/id_rsa") is None


# ---------- a name with a fallback written beside it -------------------------


def test_a_fallback_the_shell_may_substitute_is_scanned_too(fenced: ExecTool, monkeypatch: pytest.MonkeyPatch) -> None:
    """``${NOPE:-/etc/shadow}`` is ``/etc/shadow`` whenever the name is unset,
    so the word after the operator is a path the command may open and has to be
    read as one. It is the same defect as a bare name, one spelling further on."""
    monkeypatch.delenv("NOT_A_REAL_VARIABLE", raising=False)

    assert refusal(fenced, "cat ${NOT_A_REAL_VARIABLE:-/etc/shadow}") is not None


@pytest.mark.parametrize(
    "command",
    [
        "echo ${#HOME}",
        "echo ${TERM%-256color}",
        "echo ${TERM##*-}",
    ],
    ids=["length", "strip-suffix", "strip-prefix"],
)
def test_a_brace_body_that_only_edits_a_value_is_left_alone(fenced: ExecTool, command: str) -> None:
    """``%``, ``#`` and ``${#...}`` reshape a value rather than offering a
    second word, so there is no path in them to find. Reading them as one
    would refuse ordinary shell."""
    assert refusal(fenced, command) is None


# ---------- walking out instead of reaching out ------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cd /; cat etc/shadow",
        "cd ..; cat outside.txt",
        "cd && cat .ssh/id_rsa",
        "(cd /; cat etc/shadow)",
        "cd -- /; cat etc/passwd",
        "cd -- ..; cat outside.txt",
        "cd -L -- /; cat etc/passwd",
        "cd --; cat .ssh/id_rsa",
    ],
    ids=[
        "root",
        "parent",
        "bare-cd-is-home",
        "subshell",
        "after-option-terminator",
        "parent-after-option-terminator",
        "flag-then-option-terminator",
        "option-terminator-alone-is-home",
    ],
)
def test_a_command_that_walks_out_first_is_refused(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """Reaching out and stepping out are the same escape.

    The scan reads paths, and these four name none it can see: bare ``/`` has
    nothing after it, ``..`` is not absolute, and a ``cd`` with no argument
    names ``$HOME`` by saying nothing. Every path after them is then relative
    to somewhere else, which is the whole of the trick.

    ``cd /etc`` is not here because it never worked -- ``/etc`` is a path, and
    the scan has always refused it.
    """
    monkeypatch.setenv("HOME", "/home/victim")

    assert refusal(fenced, command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "cd subdir && ls",
        "cd . && ls",
        "cd ./src; grep -rn TODO .",
        "cd -- subdir && ls",
        'echo "cd / is how you would leave"',
        "grep -rn cd notes.txt",
    ],
    ids=[
        "into-a-subdir",
        "into-itself",
        "relative-prefix",
        "subdir-after-option-terminator",
        "cd-as-text",
        "cd-as-an-argument",
    ],
)
def test_moving_around_inside_the_workspace_is_left_alone(fenced: ExecTool, command: str) -> None:
    """The fence is a boundary, not a ban on ``cd``. The last two are the ones
    a word-match would get wrong: neither is a directory change."""
    assert refusal(fenced, command) is None


def test_a_directory_change_into_an_allowed_extra_is_left_alone(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = ExecTool(
        working_dir=str(workspace),
        restrict_to_workspace=True,
        extra_allowed_dirs=(shared,),
    )

    assert refusal(tool, f"cd {shared} && ls") is None


def test_quoting_the_lexer_cannot_close_does_not_become_a_crash(fenced: ExecTool) -> None:
    """An invariant rather than a new behaviour: the directory check tokenises,
    and the fence is on the path of every command, so an unbalanced quote has
    to leave it answering rather than raising. The permission gate reads the
    same text through the same lexer and is what refuses it."""
    assert refusal(fenced, "cat 'unterminated") is None


# ---------- the program a nested shell is handed ------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'cat $HOME/.ssh/id_rsa'",
        "bash -c 'cat $HOME/.ssh/id_rsa'",
        "/bin/sh -c 'cat $HOME/.ssh/id_rsa'",
        "sh -c 'cd /; cat etc/shadow'",
        "sh -c \"sh -c 'cat \\$HOME/.ssh/id_rsa'\"",
    ],
    ids=["sh", "bash", "by-path", "walks-out-inside", "two-deep"],
)
def test_the_program_handed_to_a_nested_shell_is_scanned_in_that_shell(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """Single quotes suppress expansion in the shell that reads them, and the
    fence is right to leave them alone -- but their contents are the *inner*
    shell's program, and the name expands there. Reading only the outer shell
    left every payload unscanned while the outer quoting looked handled.
    """
    monkeypatch.setenv("HOME", "/home/victim")

    assert refusal(fenced, command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'cat notes.txt'",
        "bash -c 'cd src && ls'",
        "sh -c 'echo building now'",
        "sh -c 'cat $PWD/notes.txt'",
    ],
    ids=["relative-read", "walks-inside", "names-no-path", "pwd-inside"],
)
def test_a_nested_payload_that_stays_inside_is_left_alone(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """The payload is scanned, not refused for being a payload."""
    monkeypatch.setenv("HOME", "/home/victim")

    assert refusal(fenced, command) is None


# ---------- a brace body that moves the path ---------------------------------


@pytest.mark.parametrize(
    "command",
    [
        'cat "${PWD%/*}/outside.txt"',
        "cat ${PWD%/*}/outside.txt",
        "cat ${PWD%%/*}/etc/shadow",
        "cat ${HOME#/home}/outside.txt",
    ],
    ids=["quoted", "bare", "longest-suffix", "shortest-prefix"],
)
def test_a_brace_body_that_shortens_a_path_is_resolved_not_ignored(
    fenced: ExecTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """Removing a suffix walks a path UP, which is how it leaves the root.

    These were read as inert text beside an in-root value, so the scan saw the
    workspace and never the parent the shell would open. A trim is not a
    decoration on the value; it is the value.
    """
    monkeypatch.setenv("HOME", "/home/victim")

    assert refusal(fenced, command) is not None


def test_a_trim_that_matches_nothing_leaves_the_value_alone(fenced: ExecTool) -> None:
    """The other half of resolving them: a pattern with no match must not
    become a refusal, or every trim would be refused and nothing was resolved."""
    assert refusal(fenced, "cat ${PWD%/}/notes.txt") is None


def test_a_longest_prefix_trim_yields_a_basename_and_stays_inside(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sharper half. ``${HOME##*/}`` is ``victim`` -- a bare name, so the
    command reads inside the workspace and must run. Before the trim was
    resolved this was refused, because the unresolved body left the whole of
    ``$HOME`` standing beside it for the scan to find. Getting this one right
    is what separates resolving a trim from refusing everything shaped like one.
    """
    monkeypatch.setenv("HOME", "/home/victim")

    assert refusal(fenced, "cat ${HOME##*/}/notes.txt") is None


@pytest.mark.parametrize(
    "command",
    [
        "cat ${PWD/workspace/elsewhere}/x",
        "cat ${PWD^^}/x",
        "cat ${PWD,,}/x",
        "cat ${!HOME}/x",
        "cat ${PWD:1}/x",
    ],
    ids=["substitution", "upper", "lower", "indirection", "offset"],
)
def test_a_brace_body_the_fence_cannot_resolve_is_refused(fenced: ExecTool, command: str) -> None:
    """The posture. Each of these can hand the shell a path this cannot
    compute, so allowing them makes the fence's promise depend on which
    spellings happened to be implemented. Refusing is visible and arguable;
    the alternative is silent and was the whole defect.
    """
    error = refusal(fenced, command)

    assert error is not None
    assert "unsupported shell expansion" in error


@pytest.mark.parametrize(
    "command",
    ["echo ${#HOME}", "echo ${#PWD} chars", "cat $((1 + 1))/notes.txt"],
    ids=["length", "length-in-a-sentence", "arithmetic"],
)
def test_a_construct_that_yields_a_number_is_left_alone(fenced: ExecTool, command: str) -> None:
    """The exceptions to the refusal, and the reason they are exceptions: a
    count and an arithmetic result are numbers, and a number cannot name an
    absolute path. Pinned so neither is later swept into the refusal for
    looking unresolved -- they are resolved, to something harmless."""
    assert refusal(fenced, command) is None


# ---------- the spelling cmd.exe actually runs --------------------------------


def test_a_percent_name_is_expanded_where_cmd_would_expand_it(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``DirectExecutor`` runs the platform shell, so on Windows the ordinary
    spelling of this defect is ``%USERPROFILE%\\.ssh\\id_rsa`` rather than
    ``$HOME/...``. The name pattern knew only the POSIX form, and the Windows
    path pattern wants a drive prefix the unexpanded text does not have, so
    both halves of the scan looked straight past it.
    """
    monkeypatch.setattr(ExecTool, "_WINDOWS_SHELL", True)
    monkeypatch.setenv("USERPROFILE", "/home/victim")

    assert refusal(fenced, "type %USERPROFILE%/.ssh/id_rsa") is not None


def test_a_percent_name_is_read_case_insensitively_as_cmd_reads_it(
    fenced: ExecTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cmd.exe`` resolves an environment name without regard to case, so an
    exact-key lookup leaves the ordinary spelling standing and the path scan
    sees no drive prefix. Asserted on the text rather than the verdict, because
    a Windows path is not absolute to a POSIX ``Path`` and the verdict would
    turn on the host running the suite."""
    monkeypatch.setattr(ExecTool, "_WINDOWS_SHELL", True)
    monkeypatch.setenv("USERPROFILE", r"C:\Users\victim")
    env = fenced._child_env(tmp_path)

    assert fenced._as_the_shell_reads_it(r"type %UserProfile%\.ssh\id_rsa", env) == (
        r"type C:\Users\victim\.ssh\id_rsa"
    )


def test_a_percent_name_cmd_does_not_have_is_left_standing(
    fenced: ExecTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cmd.exe`` leaves an undefined name as written, so this pass does too."""
    monkeypatch.setattr(ExecTool, "_WINDOWS_SHELL", True)
    monkeypatch.delenv("NOT_A_REAL_VARIABLE", raising=False)
    env = fenced._child_env(tmp_path)

    assert fenced._as_the_shell_reads_it("type %NOT_A_REAL_VARIABLE%/x", env) == "type %NOT_A_REAL_VARIABLE%/x"


@pytest.mark.parametrize(
    "command",
    ["echo '%HOME%/notes'", "echo %HOME%/notes", "date +%Y%m%d", "printf '%s%s' a b"],
    ids=["quoted", "bare", "date-format", "printf-format"],
)
def test_no_percent_is_expanded_where_the_shell_is_not_cmd(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """The regression this pass introduced. `%VAR%` means nothing to `sh`,
    which prints these literally -- but HOME, PWD, USER and TMPDIR are all on
    the executor's allowlist on POSIX too, so running the pass unconditionally
    refused text that names no path at all. It is cmd.exe syntax, and it is
    read only where cmd.exe is the shell."""
    monkeypatch.setenv("HOME", "/tmp/outside-home")

    assert refusal(fenced, command) is None


# ---------- the branches a reader would otherwise have to take on trust -------


def test_a_brace_group_that_walks_out_is_refused(fenced: ExecTool) -> None:
    """The sibling of the subshell case. Both arrive with the bracket already
    removed, which is why neither needs unwrapping here."""
    assert refusal(fenced, "{ cd /; cat etc/shadow; }") is not None


@pytest.mark.parametrize("command", ["cd -", 'cd ""'], ids=["oldpwd", "empty-operand"])
def test_a_destination_this_cannot_name_is_not_guessed_at(fenced: ExecTool, command: str) -> None:
    """``$OLDPWD`` can only hold a directory an earlier ``cd`` already passed,
    and an empty operand moves nowhere. Refusing either would refuse a command
    that cannot leave."""
    assert refusal(fenced, command) is None


def test_a_destination_that_cannot_be_resolved_at_all_is_refused(fenced: ExecTool) -> None:
    """``~nosuchuser`` has no home to resolve to, so the fence cannot say where
    the command lands. Under the same rule as an unresolved expansion, not
    knowing is a refusal rather than a pass."""
    assert refusal(fenced, "cd ~nosuchuserxyz") is not None


def test_a_nesting_deeper_than_the_cap_still_answers(fenced: ExecTool, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap is there so a crafted command cannot drive the scan down
    forever. What it must not do is fail to answer."""
    monkeypatch.setenv("HOME", "/home/victim")
    command = "cat $HOME/.ssh/id_rsa"
    for _ in range(6):
        command = f"sh -c {shlex.quote(command)}"

    assert refusal(fenced, command) in (None, *_REFUSALS)


# ---------- a wrapper in front of the thing that runs -------------------------


@pytest.mark.parametrize(
    "command",
    [
        "command sh -c 'cat $HOME/.ssh/id_rsa'",
        "env sh -c 'cat $HOME/.ssh/id_rsa'",
        "env FOO=bar sh -c 'cat $HOME/.ssh/id_rsa'",
        "sudo sh -c 'cat $HOME/.ssh/id_rsa'",
        "FOO=bar sh -c 'cat $HOME/.ssh/id_rsa'",
        "command cd /; cat etc/shadow",
    ],
    ids=["command", "env", "env-with-assignment", "sudo", "bare-assignment", "wrapped-cd"],
)
def test_a_wrapper_does_not_hide_what_it_wraps(fenced: ExecTool, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    """The payload search read the segment as written, while the permission
    policy unwraps `env`, `sudo`, `command` and leading assignments first. Half
    the machinery was reused and half was not, so a wrapper in front of the
    nested shell put its program back out of view."""
    monkeypatch.setenv("HOME", "/home/victim")

    assert refusal(fenced, command) is not None


def test_a_wrapper_in_front_of_inside_work_is_still_left_alone(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/home/victim")

    assert refusal(fenced, "env sh -c 'cat notes.txt'") is None


# ---------- a parameter inside a brace body ----------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat ${UNSET:-$HOME/secret}",
        "cat ${UNSET:-${HOME}/secret}",
        "cat ${PWD%$PWD}/etc/passwd",
        "cat ${UNSET:-$NOPE/etc/shadow}",
    ],
    ids=["fallback-word", "fallback-braced", "trim-pattern", "fallback-unset-name"],
)
def test_a_parameter_inside_a_brace_body_is_expanded_too(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """The body is emitted after the walk has passed that position, so what it
    inserted was never itself read. The shell expands it, which made this the
    one spelling that the resolved-or-refused contract claimed to cover and did
    not: neither expanded nor refused, just handed through."""
    monkeypatch.setenv("HOME", "/home/victim")
    monkeypatch.delenv("UNSET", raising=False)
    monkeypatch.delenv("NOPE", raising=False)

    assert refusal(fenced, command) is not None


@pytest.mark.parametrize(
    "command",
    ["cat ${UNSET:-notes.txt}", "cat ${UNSET:-$PWD/notes.txt}"],
    ids=["relative-fallback", "pwd-fallback"],
)
def test_a_brace_body_that_resolves_inside_still_runs(
    fenced: ExecTool, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """Resolving the body, not refusing everything with one in it: the second
    case only stays allowed if `$PWD` inside the fallback is actually expanded."""
    monkeypatch.delenv("UNSET", raising=False)

    assert refusal(fenced, command) is None


def test_a_brace_this_cannot_parse_is_refused(fenced: ExecTool) -> None:
    """Nesting runs past the first `}`, which this does not parse. Under the
    contract an expansion it cannot resolve is a refusal, not a pass."""
    error = refusal(fenced, "cat ${UNSET:-${X:-/etc/shadow}}")

    assert error is not None and "unsupported shell expansion" in error


# ---------- where a second cd starts from ------------------------------------


@pytest.mark.parametrize(
    "command",
    ["cd subdir && cd .. && ls", "cd a && cd b && cd .. && cd .. && ls"],
    ids=["up-once", "down-two-up-two"],
)
def test_a_cd_that_returns_to_the_workspace_still_runs(fenced: ExecTool, command: str) -> None:
    """Each `cd` was resolved from the directory the command started in, so a
    second one was read as if the first had not happened and a walk back was
    refused for leaving. The sequence ends where it began.

    Spelled with `&&` rather than `;`: the semicolon versions of these were
    here first and were wrong, because a `cd` into a directory that does not
    exist fails and the shell carries on from where it stood, so the `cd ..`
    after it leaves the workspace. `&&` is what makes the walk knowable -- it
    runs its right side only when the left one returned zero. Those spellings
    now sit under ``test_a_cd_that_may_not_have_run_leaves_the_walk_where_it_was``.

    Spelled as repeated `cd ..` rather than `cd ../..` on purpose: a literal
    `../` anywhere is refused by the traversal rule before any of this runs, so
    the shorter spelling would pass for a reason that has nothing to do with
    where the walk starts from."""
    assert refusal(fenced, command) is None


@pytest.mark.parametrize(
    "command",
    [
        "cd subdir; cd ..; cd ..; ls",
        "cd a; cd b; cd ..; cd ..; cd ..; cat etc/shadow",
        "cd subdir && cd .. && cd .. && ls",
        "cd a && cd b && cd .. && cd .. && cd .. && cat etc/shadow",
    ],
    ids=["one-past", "two-past", "one-past-chained", "two-past-chained"],
)
def test_a_cd_sequence_that_ends_outside_is_still_refused(fenced: ExecTool, command: str) -> None:
    assert refusal(fenced, command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "cd subdir; cd ..; ls",
        "false && cd subdir; cd ..; ls",
        "cd subdir || cd ..; ls",
        "cd subdir | cat; cd ..; ls",
        "cd subdir & cd ..; ls",
    ],
    ids=["semicolon", "skipped-by-and", "or", "pipe", "background"],
)
def test_a_cd_that_may_not_have_run_leaves_the_walk_where_it_was(fenced: ExecTool, command: str) -> None:
    """Carrying a destination forward claims the `cd` ran and succeeded.

    Only `&&` proves that: it is the one separator whose right side runs
    *because* the left side returned zero. After every other one the shell may
    still be standing where it started -- `;` continues from there when the
    `cd` fails, `||` runs its right side only when it failed, and `|`, `&` and
    a bracket each put the `cd` in a subshell whose directory dies with it.
    The walk then reads the following `cd ..` as a return to the workspace
    while the real shell takes it one level above.

    None of these directories exists, which is the point: `cd subdir` fails
    here exactly as it would on an operator's machine.
    """
    assert refusal(fenced, command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "cd sub && cd ..; ls",
        "cd sub && cd .. && ls | head",
        "cd a && cd b && cd .. && cd ..; ls",
        "cd sub && cd .. ; cd sub && cd .. ; ls",
    ],
    ids=["trailing-semicolon", "trailing-pipe", "two-deep-then-semicolon", "twice-over"],
)
def test_a_proven_walk_survives_a_separator_later_in_the_command(fenced: ExecTool, command: str) -> None:
    """A separator that cannot prove one `cd` must not unprove an earlier one.

    Each `cd` here is chained to the one before it with `&&`, so every step of
    the walk is known: the chain stops at the first failure, and nothing after
    a failed `cd` runs. What follows the walk -- a `;`, a pipe, a second
    chain -- says nothing about where the walk ended. Reading the whole
    command as unproven because of a separator standing somewhere else refuses
    a command that cannot leave the workspace in either branch.
    """
    assert refusal(fenced, command) is None


def test_a_subshell_makes_the_walk_strict_again(fenced: ExecTool) -> None:
    """A `cd` inside a subshell does not outlive it, and the splitter has
    already dropped the bracket that said so. Carrying the directory forward
    would read this as returning to the workspace when the shell is one level
    above it, so where the structure cannot be seen the stricter reading holds
    -- each `cd` from the starting directory, as before.
    """
    assert refusal(fenced, "(cd subdir); cd ..; ls") is not None
