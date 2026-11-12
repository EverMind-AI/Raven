"""OpenShellExecutor against a stand-in control plane: spec building, lifecycle, exec, process bridging, cleanup."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from raven.sandbox import openshell_executor as ox
from raven.sandbox.interfaces import SandboxInitError
from raven.sandbox.openshell_executor import OpenShellExecutor, sandbox_policy


class FakeStream:
    """A finished exec: yields its scripted events, or blocks until cancelled."""

    def __init__(self, events=None, *, block: bool = False):
        self.events = list(events or [])
        self.block = block
        self.cancelled = threading.Event()
        self.written: list[bytes] = []
        self.input_closed = False

    def __iter__(self):
        yield from self.events
        if self.block:
            self.cancelled.wait(10)
            raise RuntimeError("cancelled")

    def cancel(self):
        self.cancelled.set()

    def write(self, data: bytes):
        self.written.append(data)

    def close_input(self):
        self.input_closed = True


class FakeControlPlane:
    def __init__(self):
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.execs: list[dict] = []
        self.interactives: list[dict] = []
        self.listed: list[tuple[str, dict]] = []
        self.create_error: Exception | None = None
        self.exec_script = lambda argv: FakeStream([("stdout", b"ok\n"), ("exit", 0)])
        self.interactive_stream: FakeStream | None = None

    def create(self, *, name, image, policy, driver_config, resources, labels):
        if self.create_error:
            raise self.create_error
        self.created.append(
            dict(name=name, image=image, policy=policy, driver_config=driver_config, resources=resources, labels=labels)
        )

    def wait_ready(self, name, timeout_seconds):
        self.ready_timeout = timeout_seconds

    def exec(self, name, argv, *, workdir, env, timeout_seconds):
        self.execs.append(dict(name=name, argv=argv, workdir=workdir, env=env, timeout=timeout_seconds))
        return self.exec_script(argv)

    def interactive(self, name, argv, *, workdir, env):
        self.interactives.append(dict(name=name, argv=argv, workdir=workdir, env=env))
        return self.interactive_stream

    def delete(self, name):
        self.deleted.append(name)

    def list_labeled(self, selector):
        self.selector = selector
        return self.listed


@pytest.fixture(autouse=True)
def _fresh_reaper(monkeypatch):
    monkeypatch.setattr(ox, "_reaped", False)


def _executor(tmp_path: Path, cp: FakeControlPlane, **kw) -> OpenShellExecutor:
    kw.setdefault("allow_net", False)
    return OpenShellExecutor(image="ubuntu:24.04", workspace=tmp_path, control_plane=cp, **kw)


class TestSandboxPolicy:
    def test_no_network_rules_when_network_is_off(self, tmp_path):
        policy = sandbox_policy(allow_net=False, mounts=[(str(tmp_path), "/workspace", False)], uid=1000)
        assert "network_policies" not in policy
        assert policy["filesystem"]["include_workdir"] is True
        assert "/workspace" in policy["filesystem"]["read_write"]
        assert {"/usr", "/lib", "/etc", "/proc"} <= set(policy["filesystem"]["read_only"])
        assert policy["landlock"] == {"compatibility": "best_effort"}

    def test_read_only_mounts_land_in_read_only(self, tmp_path):
        policy = sandbox_policy(
            allow_net=False,
            mounts=[(str(tmp_path), "/workspace", False), ("/h", "/agent-home", True)],
            uid=1000,
        )
        assert "/agent-home" in policy["filesystem"]["read_only"]
        assert "/agent-home" not in policy["filesystem"]["read_write"]

    def test_a_domain_list_becomes_one_l4_rule_per_domain(self):
        policy = sandbox_policy(allow_net=["pypi.org", "files.pythonhosted.org"], mounts=[], uid=1000)
        rules = policy["network_policies"]
        assert len(rules) == 2
        rule = next(r for r in rules.values() if r["endpoints"][0]["host"] == "pypi.org")
        assert rule["endpoints"] == [{"host": "pypi.org", "ports": [80, 443], "tls": "NETWORK_TLS_MODE_SKIP"}]
        assert rule["binaries"] == [{"path": "/**"}]

    def test_the_host_uid_is_the_workload_identity_when_it_is_not_root(self):
        policy = sandbox_policy(allow_net=False, mounts=[], uid=1234)
        assert policy["process"] == {"run_as_user": "1234", "run_as_group": "1234"}

    def test_a_root_host_leaves_the_identity_to_the_driver(self):
        assert "process" not in sandbox_policy(allow_net=False, mounts=[], uid=0)

    def test_allow_net_true_opens_every_multi_label_host_on_every_port(self):
        rules = sandbox_policy(allow_net=True, mounts=[], uid=1000)["network_policies"]
        (rule,) = rules.values()
        wildcard = rule["endpoints"][0]
        assert wildcard == {"host": "**.*.*", "ports": list(range(1, 65536)), "tls": "NETWORK_TLS_MODE_SKIP"}
        assert rule["binaries"] == [{"path": "/**"}]

    def test_allow_net_true_adds_the_common_apex_domains_on_common_ports(self):
        (rule,) = sandbox_policy(allow_net=True, mounts=[], uid=1000)["network_policies"].values()
        apex = {e["host"]: e for e in rule["endpoints"][1:]}
        assert {"pypi.org", "github.com", "npmjs.org", "crates.io", "docker.io"} <= set(apex)
        assert all(len(host.split(".")) == 2 and "*" not in host for host in apex)
        assert apex["github.com"]["ports"] == [22, 80, 443, 8080, 8443, 9418]
        assert all(e["tls"] == "NETWORK_TLS_MODE_SKIP" for e in apex.values())

    def test_a_star_entry_keeps_the_open_default_and_adds_the_rest(self):
        (rule,) = sandbox_policy(allow_net=["*", "example.com", "pypi.org"], mounts=[], uid=1000)[
            "network_policies"
        ].values()
        hosts = [e["host"] for e in rule["endpoints"]]
        assert hosts[0] == "**.*.*"
        assert hosts.count("example.com") == 1 and hosts.count("pypi.org") == 1

    def test_the_open_policy_parses_as_the_proto_and_fits_the_gateway_limit(self):
        pytest.importorskip("openshell")
        from google.protobuf.json_format import ParseDict
        from openshell._proto import sandbox_pb2

        extra = [f"apex{i}.example" for i in range(50)]
        policy = sandbox_policy(allow_net=["*", *extra], mounts=[("/h", "/workspace", False)], uid=1000)
        message = ParseDict(policy, sandbox_pb2.SandboxPolicy())
        assert message.network_policies["allow_open"].endpoints[0].host == "**.*.*"
        assert message.ByteSize() < 262144

    def test_an_executor_accepts_the_open_default(self, tmp_path):
        assert OpenShellExecutor(image="x", workspace=tmp_path, allow_net=True, control_plane=FakeControlPlane())


class TestSandboxSpec:
    def test_the_spec_carries_image_mounts_limits_and_policy(self):
        pytest.importorskip("openshell")
        policy = sandbox_policy(allow_net=False, mounts=[("/h", "/workspace", False)], uid=1000)
        spec = ox.build_sandbox_spec(
            image="img:1",
            policy=policy,
            driver_config={"docker": {"mounts": [{"type": "bind", "source": "/h", "target": "/workspace"}]}},
            resources={"limits": {"cpu": "2", "memory": "2048Mi"}},
        )
        assert spec.template.image == "img:1"
        assert spec.template.driver_config["docker"]["mounts"][0]["target"] == "/workspace"
        assert spec.template.resources["limits"]["memory"] == "2048Mi"
        assert "/workspace" in spec.policy.filesystem.read_write
        assert spec.policy.process.run_as_user == "1000"


class TestStart:
    async def test_creates_waits_and_verifies(self, tmp_path):
        cp = FakeControlPlane()
        owned: set[str] = set()
        ex = _executor(tmp_path, cp, owned_ids=owned, cpus=2, memory_mib=2048, create_timeout=77)
        await ex.start()
        (created,) = cp.created
        assert created["name"].startswith("rv") and len(created["name"]) <= 19
        assert created["image"] == "ubuntu:24.04"
        assert created["resources"] == {"limits": {"cpu": "2", "memory": "2048Mi"}}
        assert created["driver_config"] == {
            "docker": {
                "mounts": [{"type": "bind", "source": str(tmp_path), "target": "/workspace", "read_only": False}]
            }
        }
        assert set(created["labels"]) == {"raven.owner", "raven.pid"}
        assert cp.ready_timeout == 77
        assert cp.execs[0]["argv"][0:2] == ["sh", "-c"]
        assert owned == {created["name"]}

    async def test_an_unwritable_workspace_fails_start_and_removes_the_sandbox(self, tmp_path):
        cp = FakeControlPlane()
        cp.exec_script = lambda argv: FakeStream([("stdout", b"1000\nnot-writable\n"), ("exit", 0)])
        owned: set[str] = set()
        ex = _executor(tmp_path, cp, owned_ids=owned)
        with pytest.raises(SandboxInitError, match=r"uid 1000\) cannot write"):
            await ex.start()
        await ox.drain_pending_deletes()
        assert cp.deleted == [cp.created[0]["name"]]
        assert owned == set()

    async def test_an_unreachable_gateway_explains_what_to_check(self, tmp_path):
        cp = FakeControlPlane()
        cp.create_error = ConnectionError("connection refused")
        with pytest.raises(SandboxInitError, match="openshell status"):
            await _executor(tmp_path, cp).start()

    async def test_a_sandbox_that_fails_to_become_ready_reports_why(self, tmp_path):
        cp = FakeControlPlane()

        def not_ready(name, timeout_seconds):
            raise RuntimeError("sandbox entered error phase: bind source path does not exist: /tmp/ws")

        cp.wait_ready = not_ready
        owned: set[str] = set()
        with pytest.raises(SandboxInitError, match="bind source path does not exist") as info:
            await _executor(Path("/tmp/ws"), cp, owned_ids=owned).start()
        assert "PrivateTmp" in str(info.value)
        await ox.drain_pending_deletes()
        assert cp.deleted == [cp.created[0]["name"]] and owned == set()

    async def test_start_is_idempotent(self, tmp_path):
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        await ex.start()
        assert len(cp.created) == 1


class TestReaper:
    async def test_sandboxes_of_dead_processes_on_this_host_are_deleted_once(self, tmp_path, monkeypatch):
        cp = FakeControlPlane()
        cp.listed = [("rvdead", {"raven.pid": "999999"}), ("rvlive", {"raven.pid": "1"}), ("rvnolabel", {})]
        monkeypatch.setattr(ox, "_pid_alive", lambda pid: pid == 1)
        await _executor(tmp_path, cp).start()
        await _executor(tmp_path, cp).start()
        await ox.drain_pending_deletes()
        assert cp.selector.startswith("raven.owner=")
        assert cp.deleted.count("rvdead") == 1
        assert "rvlive" not in cp.deleted and "rvnolabel" not in cp.deleted


class TestExec:
    async def test_runs_through_sh_in_the_translated_cwd(self, tmp_path):
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        cp.exec_script = lambda argv: FakeStream([("stdout", b"hi\n"), ("stderr", b"warn\n"), ("exit", 3)])
        result = await ex.exec("echo hi", cwd=str(tmp_path / "sub"), env={"A": "1"}, timeout=9)
        assert (result.stdout, result.stderr, result.exit_code) == ("hi\n", "warn\n", 3)
        last = cp.execs[-1]
        assert last["argv"] == ["sh", "-c", "echo hi"]
        assert last["workdir"] == "/workspace/sub"
        assert last["env"] == {"A": "1"}
        assert last["timeout"] == 9

    async def test_a_timeout_cancels_the_stream_and_reports_minus_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ox, "_TIMEOUT_GRACE_SECONDS", 0.05)
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        blocking = FakeStream(block=True)
        cp.exec_script = lambda argv: blocking
        result = await ex.exec("sleep 100", timeout=0)
        assert result.exit_code == -1 and "timed out" in result.stderr
        assert blocking.cancelled.is_set()

    async def test_a_server_side_timeout_reads_like_the_other_executors(self, tmp_path):
        import time as _time

        class SlowExit(FakeStream):
            def __iter__(self):
                yield ("stdout", b"partial\n")
                _time.sleep(1.05)
                yield ("exit", 124)

        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        cp.exec_script = lambda argv: SlowExit()
        result = await ex.exec("sleep 100", timeout=1)
        assert (result.exit_code, result.stdout) == (-1, "partial\n")
        assert "timed out after 1s" in result.stderr

    async def test_a_quick_exit_124_is_the_command_own_status(self, tmp_path):
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        cp.exec_script = lambda argv: FakeStream([("exit", 124)])
        assert (await ex.exec("exit 124", timeout=30)).exit_code == 124

    async def test_cancelling_the_caller_cancels_the_stream(self, tmp_path):
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        blocking = FakeStream(block=True)
        cp.exec_script = lambda argv: blocking
        task = asyncio.create_task(ex.exec("sleep 100", timeout=60))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert blocking.cancelled.is_set()

    async def test_exec_before_start_starts_the_sandbox(self, tmp_path):
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.exec("true")
        assert len(cp.created) == 1


class TestStop:
    async def test_stop_deletes_in_the_background_and_releases_ownership(self, tmp_path):
        cp = FakeControlPlane()
        owned: set[str] = set()
        ex = _executor(tmp_path, cp, owned_ids=owned)
        await ex.start()
        name = cp.created[0]["name"]
        await ex.stop()
        await ox.drain_pending_deletes()
        assert cp.deleted == [name]
        assert owned == set()

    async def test_stop_without_start_and_a_second_stop_are_no_ops(self, tmp_path):
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.stop()
        await ex.start()
        await ex.stop()
        await ex.stop()
        await ox.drain_pending_deletes()
        assert len(cp.deleted) == 1

    async def test_a_failing_delete_is_logged_not_raised(self, tmp_path):
        cp = FakeControlPlane()

        def boom(name):
            raise RuntimeError("gateway gone")

        cp.delete = boom
        ex = _executor(tmp_path, cp)
        await ex.start()
        await ex.stop()
        await ox.drain_pending_deletes()


class TestStartProcess:
    async def test_bridges_json_rpc_over_the_interactive_stream(self, tmp_path):
        from mcp.shared.message import SessionMessage
        from mcp.types import JSONRPCRequest

        cp = FakeControlPlane()
        reply = {"jsonrpc": "2.0", "id": 1, "result": {}}
        cp.interactive_stream = FakeStream(
            [
                ("stderr", b"booting\n"),
                ("stdout", b"banner\n" + json.dumps(reply).encode()[:10]),
                ("stdout", json.dumps(reply).encode()[10:] + b"\n"),
                ("exit", 0),
            ]
        )
        (tmp_path / "pkg").mkdir()
        ex = _executor(tmp_path, cp)
        await ex.start()
        read, write = await ex.start_process(
            str(tmp_path / "pkg" / "server"),
            ["--root", str(tmp_path / "pkg")],
            env={"D": str(tmp_path)},
            cwd=str(tmp_path / "pkg"),
        )
        msg = await asyncio.wait_for(read.receive(), 2)
        assert isinstance(msg, SessionMessage) and msg.message.id == 1
        await write.send(SessionMessage(message=JSONRPCRequest(jsonrpc="2.0", id=7, method="ping")))
        await write.aclose()
        await asyncio.sleep(0.05)
        (launched,) = cp.interactives
        assert launched["argv"] == ["/workspace/pkg/server", "--root", "/workspace/pkg"]
        assert launched["workdir"] == "/workspace/pkg"
        assert launched["env"] == {"D": "/workspace"}
        assert json.loads(cp.interactive_stream.written[0])["method"] == "ping"
        assert cp.interactive_stream.input_closed
        await ex.stop()

    async def test_a_cwd_outside_every_mount_is_refused(self, tmp_path):
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        with pytest.raises(ValueError, match="not inside any volume"):
            await ex.start_process("srv", [], cwd="/elsewhere")

    def test_flags(self, tmp_path):
        ex = _executor(tmp_path, FakeControlPlane())
        assert ex.is_sandboxed and ex.supports_process_spawning
