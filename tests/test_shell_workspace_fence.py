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

from pathlib import Path

import pytest

from raven.agent.tools.shell import ExecTool


@pytest.fixture
def fenced(tmp_path: Path) -> ExecTool:
    return ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)


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
    ],
    ids=["root", "parent", "bare-cd-is-home", "subshell"],
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
        'echo "cd / is how you would leave"',
        "grep -rn cd notes.txt",
    ],
    ids=["into-a-subdir", "into-itself", "relative-prefix", "cd-as-text", "cd-as-an-argument"],
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
