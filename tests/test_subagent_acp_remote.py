"""An acp sub-agent whose entry names a registered machine, end to end.

Nothing here needs a second computer. A stand-in ``ssh`` on PATH does what sshd
does with the one remote-command argument it is sent -- hands it to the user's
shell, in that user's home -- so the real launch line, the real login shell and
the real stub ACP server all run: only the network is missing. The stand-in can
also fail the way ssh fails, writing its own address-bearing words where the
launch line asked (``-E``) or, for a one-shot call, to stderr.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from raven.acp_client.capabilities import snapshot_fingerprint, verify_agent
from raven.acp_client.pool import close_pool
from raven.acp_client.protocol import AcpConnectionError
from raven.agent.subagent import probe
from raven.agent.subagent.backends import (
    agent_meta,
    build_third_party_backend,
    format_agent_listing,
    session_mcp_effective,
)
from raven.agent.subagent.backends import env as backend_env
from raven.agent.subagent.mcp_grant import GrantedServer, McpGrant
from raven.agent.subagent.probe_state import fingerprint
from raven.agent.subagent.registry import _row_for
from raven.config.schema import MCPServerConfig, ThirdPartyAcpSubagentConfig
from raven.config.update_subagents import reject_unsupported_acp_fields

_STUB = Path(__file__).with_name("acp_stub_server.py")
HOST = "203.0.113.7"
PORT = 58717

_FAKE_SSH = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, "calls"), "a") as f:
    f.write(json.dumps(args) + "\\n")
mode = open(os.path.join(here, "mode")).read().strip()
home = open(os.path.join(here, "home")).read().strip()
launch = "-T" in args  # a held session; the one-shot runner sends no -T
log = args[args.index("-E") + 1] if "-E" in args else None

def fail(said):
    if log:
        open(log, "a").write(said + "\\n")
    else:
        sys.stderr.write(said + "\\n")
    sys.exit(255)

if mode == "unreachable":
    fail("ssh: connect to host {host} port {port}: Operation timed out")
if mode == "launch_denied" and launch:
    fail("worker@{host}: Permission denied (publickey).")
if mode == "dir_fails" and not launch:
    fail("ssh: connect to host {host} port {port}: Connection refused")
command = args[args.index("--") + 1] if "--" in args else args[-1]
os.chdir(home)
os.execve("/bin/sh", ["/bin/sh", "-c", command], {{"HOME": home, "SHELL": "/bin/sh", "PATH": os.environ["PATH"]}})
"""


@pytest.fixture(autouse=True)
async def _no_pooled_connections():
    yield
    await close_pool()


class Machine:
    """The stand-in machine: its home, its ssh, and what was asked of it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = root / "box-home"
        self.home.mkdir()
        self.bin = root / "fakebin"
        self.bin.mkdir()
        ssh = self.bin / "ssh"
        ssh.write_text(_FAKE_SSH.format(python=sys.executable, host=HOST, port=PORT), encoding="utf-8")
        ssh.chmod(0o755)
        (self.bin / "home").write_text(str(self.home), encoding="utf-8")
        self.mode("ok")
        registry = root / "connections.json"
        registry.write_text(
            json.dumps(
                {
                    "connections": [
                        {
                            "id": "box",
                            "display_name": "Lab box",
                            "host": HOST,
                            "port": PORT,
                            "user": "worker",
                            "key": "/keys/id_test",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("RAVEN_CONNECTIONS", str(registry))
        path = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"
        # Both ways ssh is started: the one-shot runner inherits this process's
        # PATH, and an agent's launch takes the captured login-shell one.
        monkeypatch.setenv("PATH", path)
        monkeypatch.setattr(backend_env, "_LOGIN_ENV", {**os.environ, "PATH": path})
        monkeypatch.setattr("raven.config.paths.get_logs_dir", lambda: root / "logs")

    def mode(self, mode: str) -> None:
        (self.bin / "mode").write_text(mode, encoding="utf-8")

    def calls(self) -> list[list[str]]:
        path = self.bin / "calls"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    return Machine(tmp_path, monkeypatch)


def remote_config(*, mode: str = "ok", command: str | None = None, **kw: Any) -> ThirdPartyAcpSubagentConfig:
    return ThirdPartyAcpSubagentConfig(
        name=kw.pop("name", "stub@box"),
        command=command or f"{sys.executable} {_STUB}",
        env={"ACP_STUB_MODE": mode},
        machine="box",
        remote_cwd=kw.pop("remote_cwd", "~/raven-work"),
        ready_timeout_ms=kw.pop("ready_timeout_ms", 15000),
        **kw,
    )


# ---- a dispatch ----------------------------------------------------------------


async def test_a_dispatch_runs_on_the_machine_in_a_directory_made_there_and_says_where(box, tmp_path):
    backend = build_third_party_backend(remote_config())
    local = tmp_path / "local-workspace"

    reply = await backend.run("hi", task_id="t1", workspace=local, executor=None, instance="reviewer")

    there = box.home / "raven-work" / "reviewer"
    assert there.is_dir(), "the session directory is made on the machine"
    assert "[raven] Ran on machine 'Lab box' (box), in " in reply
    assert str(there.resolve()) in reply or str(there) in reply
    assert "not on this computer" in reply
    assert not local.exists(), "the caller's workspace is never touched"
    for argv in box.calls():
        assert str(local) not in " ".join(argv), "this computer's workspace path never reaches the machine"


async def test_the_launch_line_carries_the_entry_s_env_and_keeps_ssh_s_words_off_stderr(box, tmp_path):
    backend = build_third_party_backend(remote_config())

    await backend.run(
        "hi",
        task_id="t1",
        workspace=tmp_path,
        executor=None,
        model="parent-model",
    )

    (launch,) = [argv for argv in box.calls() if "-T" in argv]
    remote = launch[launch.index("--") + 1]
    assert "ACP_STUB_MODE=ok" in remote and "RAVEN_SUBAGENT=1" in remote
    assert "RAVEN_PARENT_MODEL" not in remote, "the parent binding is this computer's process env"
    assert "-E" in launch, "ssh's own messages go to their log"


async def test_two_handles_on_one_machine_work_in_two_directories(box, tmp_path):
    backend = build_third_party_backend(remote_config())

    await backend.run("one", task_id="t1", workspace=tmp_path, executor=None, instance="a")
    await backend.run("two", task_id="t2", workspace=tmp_path, executor=None, instance="b")

    assert (box.home / "raven-work" / "a").is_dir()
    assert (box.home / "raven-work" / "b").is_dir()


async def test_an_unreachable_machine_fails_the_dispatch_in_one_sentence(box, tmp_path):
    box.mode("unreachable")
    backend = build_third_party_backend(remote_config())

    with pytest.raises(AcpConnectionError) as caught:
        await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)

    said = str(caught.value)
    assert "machine 'Lab box' (box) did not answer within the connection timeout" in said
    assert HOST not in said and str(PORT) not in said


async def test_a_launch_ssh_refuses_is_read_from_its_own_log_not_its_stderr(box, tmp_path):
    box.mode("launch_denied")
    backend = build_third_party_backend(remote_config())

    with pytest.raises(AcpConnectionError) as caught:
        await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)

    said = str(caught.value)
    assert "refused the login" in said
    assert HOST not in said
    assert caught.value.returncode == 255


async def test_an_agent_not_installed_on_the_machine_says_so(box, tmp_path):
    backend = build_third_party_backend(remote_config(command="no-such-agent-for-raven-tests --acp"))

    with pytest.raises(AcpConnectionError, match="command was not found on machine 'Lab box'"):
        await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)


async def test_an_unregistered_machine_fails_before_anything_starts(box, tmp_path):
    backend = build_third_party_backend(remote_config().model_copy(update={"machine": "elsewhere"}))

    with pytest.raises(AcpConnectionError, match="machine 'elsewhere' is not registered"):
        await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)
    assert box.calls() == []


async def test_two_parent_models_share_one_ssh_rather_than_opening_one_each(box, tmp_path):
    # The parent binding is this computer's process env, which for a remote
    # agent is ssh's: carried, each parent model would open an ssh of its own.
    backend = build_third_party_backend(remote_config())

    await backend.run("one", task_id="t1", workspace=tmp_path, executor=None, model="parent-a")
    await backend.run("two", task_id="t2", workspace=tmp_path, executor=None, model="parent-b")

    assert len([argv for argv in box.calls() if "-T" in argv]) == 1


async def test_this_computer_s_files_are_not_accounted_for_a_remote_turn(box, tmp_path, monkeypatch):
    from raven.acp_client import acp_agent

    built: list[dict[str, Any]] = []
    real = acp_agent._TurnCollector.__init__

    def spy(self, *args, **kwargs):
        built.append(kwargs)
        real(self, *args, **kwargs)

    monkeypatch.setattr(acp_agent._TurnCollector, "__init__", spy)
    backend = build_third_party_backend(remote_config())

    await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)

    assert built and built[0]["track_files"] is False and built[0]["workspace"] is None


async def test_this_computer_s_attachments_are_not_linked_to_a_remote_agent(box, tmp_path):
    from raven.spine.message import Media

    deck = tmp_path / "house style.pptx"
    deck.write_bytes(b"PK")
    backend = build_third_party_backend(remote_config(mode="echo_blocks"))

    reply = await backend.run(
        "use my template",
        task_id="t1",
        workspace=tmp_path,
        executor=None,
        media=(Media(path=str(deck), mime="application/octet-stream", kind="file"),),
    )

    assert json.loads(reply.split("\n\n[raven]")[0]) == [{"type": "text", "text": "use my template"}]


def test_a_launch_starts_from_an_empty_ssh_log(box):
    from raven.acp_client import remote

    log = remote.ssh_log_path("stub@box")
    log.write_text("worker@host: Permission denied (publickey).\n", encoding="utf-8")

    launched = remote.prepare_launch("stub@box", "box", "agent", remote_cwd=None, env={})

    assert launched.log == log and log.read_text(encoding="utf-8") == ""
    assert launched.root == "~/raven-work"
    assert "command" not in repr(launched), "the launch line carries the address and never prints"


async def test_a_connection_that_ends_mid_turn_is_reported_as_it_ended(box, tmp_path):
    # Past ssh: the agent itself exits during the turn, so there is no ssh
    # failure to classify and the connection's own report stands.
    backend = build_third_party_backend(remote_config(mode="elicits_then_dies"))

    with pytest.raises(AcpConnectionError, match="connection ended"):
        await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)


async def test_granted_mcp_servers_are_withheld_on_the_wire_and_named_in_the_reply(box, tmp_path):
    backend = build_third_party_backend(remote_config(mode="echo_blocks"))
    grant = McpGrant(granted=(GrantedServer("pg", "stdio", MCPServerConfig(command="/bin/true")),))

    reply = await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, mcp_grant=grant)

    assert "MCP servers 'pg' were not lent to acp agent 'stub@box'" in reply
    assert "Ran on machine 'Lab box' (box)" in reply


# ---- what it is not given ------------------------------------------------------


def test_mcp_is_not_lent_to_an_agent_on_a_machine_and_the_reply_says_why():
    cfg = remote_config()
    backend = build_third_party_backend(cfg)
    grant = McpGrant(granted=(GrantedServer("pg", "stdio", MCPServerConfig(command="/bin/true")),))

    assert backend._session_mcp_refused
    note = backend._mcp_note(grant)
    assert "were not lent" in note and "runs on machine 'box'" in note
    assert session_mcp_effective(cfg) is False
    assert _row_for(cfg).injectable.mcps is False


def test_the_roster_names_the_machine_and_hands_it_no_paths():
    cfg = remote_config()
    meta = agent_meta(cfg)

    assert meta.machine == "box"
    assert meta.reads_local_files is False
    listing = format_agent_listing([meta])
    assert "on machine box" in listing and "no-local-files" in listing
    assert HOST not in listing
    assert _row_for(cfg).caps.machine == "box"
    assert _row_for(cfg).meta().machine == "box"


def test_a_local_row_is_listed_as_before():
    cfg = ThirdPartyAcpSubagentConfig(name="here", command="qwen --acp")

    meta = agent_meta(cfg)
    assert meta.machine == "" and meta.reads_local_files is True
    assert "on machine" not in format_agent_listing([meta])


def test_raven_s_keys_are_not_lent_to_a_machine_row():
    loaded = ThirdPartyAcpSubagentConfig(name="x", command="pi", machine="box", lend_keys=["openrouter"])
    assert loaded.lend_keys == [], "dropped on load, with a warning"

    with pytest.raises(ValueError, match="runs on machine 'box'"):
        reject_unsupported_acp_fields(
            [{"kind": "acp", "name": "x", "command": "pi", "machine": "box", "lendKeys": ["openrouter"]}]
        )


def test_moving_a_row_to_a_machine_is_a_new_launch_and_a_local_row_s_digest_is_untouched():
    here = ThirdPartyAcpSubagentConfig(name="a", command="qwen --acp")
    there = here.model_copy(update={"machine": "box"})
    elsewhere_root = there.model_copy(update={"remote_cwd": "/srv/agents"})

    assert len({snapshot_fingerprint(c) for c in (here, there, elsewhere_root)}) == 3
    assert len({fingerprint(c) for c in (here, there, elsewhere_root)}) == 3
    # A row that names no machine digests exactly as it did before the fields
    # existed, so no recorded measurement is discarded by the upgrade.
    assert snapshot_fingerprint(here) == snapshot_fingerprint(here.model_copy(update={"remote_cwd": None}))


# ---- checks -------------------------------------------------------------------------


async def test_the_free_probe_does_not_reach_the_machine(box):
    result = await probe.probe_one(remote_config(), source="config")

    assert result.status == "attention"
    assert "runs on machine 'Lab box' (box)" in result.detail
    assert "run a test" in result.detail
    assert box.calls() == [], "a listing never opens an ssh connection"


async def test_the_free_probe_names_a_machine_that_is_gone(box):
    result = await probe.probe_one(remote_config().model_copy(update={"machine": "elsewhere"}), source="config")

    assert result.status == "attention"
    assert "machine 'elsewhere' is not registered" in result.detail


async def test_verify_handshakes_on_the_machine_in_the_one_check_directory(box):
    snapshot = await verify_agent(remote_config())

    assert snapshot.status == "ready", snapshot.detail
    assert (box.home / "raven-work" / "raven-check").is_dir()


async def test_verify_of_an_unreachable_machine_is_attention_never_missing(box):
    box.mode("unreachable")

    snapshot = await verify_agent(remote_config())

    assert snapshot.status == "attention", "missing would offer an installer that runs on this computer"
    assert "did not answer within the connection timeout" in snapshot.detail
    assert HOST not in snapshot.detail


async def test_verify_of_a_launch_ssh_refuses_says_so(box):
    box.mode("launch_denied")

    snapshot = await verify_agent(remote_config())

    assert snapshot.status == "attention"
    assert "refused the login" in snapshot.detail
    assert HOST not in snapshot.detail


async def test_verify_of_an_agent_that_dies_on_its_machine_is_attention_in_its_own_words(box):
    # Measured 2026-10-10: a registered machine with a full disk, where the
    # launch line's own mkdir fails before the agent starts.
    snapshot = await verify_agent(remote_config(remote_cwd="/proc/raven-cannot-make-this"))

    assert snapshot.status == "attention", "missing would offer an installer that runs on this computer"
    assert "handshake failed" in snapshot.detail


async def test_verify_of_an_unregistered_machine_names_it(box):
    snapshot = await verify_agent(remote_config().model_copy(update={"machine": "elsewhere"}))

    assert snapshot.status == "attention"
    assert "machine 'elsewhere' is not registered" in snapshot.detail
    assert box.calls() == []


async def test_verify_that_cannot_make_its_directory_says_so(box):
    box.mode("dir_fails")

    snapshot = await verify_agent(remote_config())

    assert snapshot.status == "attention"
    assert "could not be reached" in snapshot.detail and HOST not in snapshot.detail


async def test_verify_with_no_ssh_here_is_attention_with_the_reason(box, monkeypatch):
    # A PATH holding no ssh at all, so nothing falls through to the real one.
    (box.bin / "ssh").unlink()
    monkeypatch.setattr(backend_env, "_LOGIN_ENV", {**os.environ, "PATH": str(box.bin)})

    snapshot = await verify_agent(remote_config())

    assert snapshot.status == "attention"
    assert "ssh" in snapshot.detail


async def test_the_free_probe_reads_the_recorded_verdict_until_the_entry_changes(box):
    cfg = remote_config()
    await probe.record_capabilities(cfg)

    recorded = await probe.probe_one(cfg, source="config")
    moved = await probe.probe_one(cfg.model_copy(update={"remote_cwd": "/srv/elsewhere"}), source="config")

    assert recorded.status == "ready", recorded.detail
    assert moved.status == "attention" and "launch config changed" in moved.detail
    assert [argv for argv in box.calls() if "-T" in argv], "only the recording reached the machine"


def test_a_launch_whose_ssh_log_cannot_be_emptied_still_starts(box):
    from raven.acp_client import remote

    log = remote.ssh_log_path("stub@box")
    log.mkdir()

    launched = remote.prepare_launch("stub@box", "box", "agent", remote_cwd=None, env={})

    assert remote.failure(launched, RuntimeError("no exit code")) is None


async def test_a_ping_answers_from_the_machine_and_leaves_one_directory(box):
    result = await probe.ping_agent(remote_config())

    assert result.ok, result.detail
    made = sorted(p.name for p in (box.home / "raven-work").iterdir())
    assert made == ["raven-check"], "every check reuses one directory instead of one per press"


async def test_a_failed_ping_on_a_machine_offers_no_fix_to_run_here(box):
    box.mode("launch_denied")

    result = await probe.ping_agent(remote_config())

    assert result.ok is False
    assert result.remedy is None
    assert "on machine 'box'" in result.detail
    assert HOST not in result.detail


# ---- the announce ------------------------------------------------------------------


async def test_the_announce_does_not_point_at_this_computer_s_workspace(box):
    from raven.agent.subagent.manager import SubagentManager

    class _Provider:
        def get_default_model(self) -> str:
            return "m"

    mgr = SubagentManager(provider=_Provider(), workspace=Path("/tmp"), agents=[remote_config()])
    submitted: list[Any] = []
    mgr.set_submit(submitted.append)

    origin = {"channel": "web", "chat_id": "d", "session_key": "web:s1", "agent": "stub@box", "workspace": "/tmp"}
    await mgr._announce_result("t1", "Look", "look", "done", origin, "ok")

    text = submitted[0].text
    assert "Working directory: on machine 'box'" in text
    assert "Working directory: /tmp" not in text
