"""OpenShellExecutor against a stand-in control plane: spec building, lifecycle, exec, process bridging, cleanup."""

from __future__ import annotations

import asyncio
import gc
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from raven.sandbox import openshell_executor as ox
from raven.sandbox.interfaces import SandboxInitError
from raven.sandbox.network_policy import compile_network_policy, network_from_allow_net
from raven.sandbox.openshell_executor import OpenShellExecutor, sandbox_policy

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the OpenShell backend needs a Linux host")


def _legacy_policy(*, allow_net, **kw):
    """The policy an executor built with ``allow_net`` sends."""
    return sandbox_policy(network_policies=compile_network_policy(network_from_allow_net(allow_net)), **kw)


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
        self.denial_script: list = []
        self.denial_reads: list[float] = []
        self.merges: list[tuple[dict, list]] = []
        self.merge_error: Exception | None = None
        self.policy_loads = True
        self.waited: list[int] = []

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

    def merge_rules(self, name, add, remove):
        if self.merge_error:
            raise self.merge_error
        self.merges.append((dict(add), list(remove)))
        return len(self.merges) + 1

    def wait_policy(self, name, version, timeout):
        self.waited.append(version)
        return self.policy_loads

    def ocsf_lines(self, name, since):
        self.denial_reads.append(since)
        if not self.denial_script:
            return []
        item = self.denial_script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(since)
        now = time.time()
        return [line if isinstance(line, tuple) else (now, line) for line in item]

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
        policy = _legacy_policy(allow_net=False, mounts=[(str(tmp_path), "/workspace", False)], uid=1000)
        assert "network_policies" not in policy
        assert policy["filesystem"]["include_workdir"] is True
        assert "/workspace" in policy["filesystem"]["read_write"]
        assert {"/usr", "/lib", "/etc", "/proc"} <= set(policy["filesystem"]["read_only"])
        assert policy["landlock"] == {"compatibility": "best_effort"}

    def test_read_only_mounts_land_in_read_only(self, tmp_path):
        policy = _legacy_policy(
            allow_net=False,
            mounts=[(str(tmp_path), "/workspace", False), ("/h", "/agent-home", True)],
            uid=1000,
        )
        assert "/agent-home" in policy["filesystem"]["read_only"]
        assert "/agent-home" not in policy["filesystem"]["read_write"]

    def test_a_domain_list_becomes_one_l4_rule_per_domain(self):
        policy = _legacy_policy(allow_net=["pypi.org", "files.pythonhosted.org"], mounts=[], uid=1000)
        rules = policy["network_policies"]
        assert len(rules) == 2
        rule = next(r for r in rules.values() if r["endpoints"][0]["host"] == "pypi.org")
        assert rule["endpoints"] == [{"host": "pypi.org", "ports": [80, 443], "tls": "NETWORK_TLS_MODE_SKIP"}]
        assert rule["binaries"] == [{"path": "/**"}]

    def test_the_host_uid_is_the_workload_identity_when_it_is_not_root(self):
        policy = _legacy_policy(allow_net=False, mounts=[], uid=1234)
        assert policy["process"] == {"run_as_user": "1234", "run_as_group": "1234"}

    def test_a_root_host_leaves_the_identity_to_the_driver(self):
        assert "process" not in _legacy_policy(allow_net=False, mounts=[], uid=0)

    def test_a_host_without_a_uid_leaves_the_identity_to_the_driver(self):
        assert "process" not in _legacy_policy(allow_net=False, mounts=[], uid=None)

    def test_the_host_uid_is_none_where_the_platform_has_none(self, monkeypatch):
        monkeypatch.delattr(ox.os, "getuid", raising=False)
        monkeypatch.delattr(ox.os, "getgid", raising=False)
        assert ox._host_uid() is None
        assert ox._host_gid() is None

    def test_allow_net_true_opens_every_multi_label_host_on_every_port(self):
        rules = _legacy_policy(allow_net=True, mounts=[], uid=1000)["network_policies"]
        (rule,) = rules.values()
        wildcard = rule["endpoints"][0]
        assert wildcard == {"host": "**.*.*", "ports": list(range(1, 65536)), "tls": "NETWORK_TLS_MODE_SKIP"}
        assert rule["binaries"] == [{"path": "/**"}]

    def test_allow_net_true_adds_the_common_apex_domains_on_common_ports(self):
        (rule,) = _legacy_policy(allow_net=True, mounts=[], uid=1000)["network_policies"].values()
        apex = {e["host"]: e for e in rule["endpoints"][1:]}
        assert {"pypi.org", "github.com", "npmjs.org", "crates.io", "docker.io"} <= set(apex)
        assert all(len(host.split(".")) == 2 and "*" not in host for host in apex)
        assert apex["github.com"]["ports"] == [22, 80, 443, 8080, 8443, 9418]
        assert all(e["tls"] == "NETWORK_TLS_MODE_SKIP" for e in apex.values())

    def test_a_star_entry_keeps_the_open_default_and_adds_the_rest(self):
        policies = _legacy_policy(allow_net=["*", "example.com", "pypi.org"], mounts=[], uid=1000)["network_policies"]
        assert [e["host"] for e in policies["allow_open"]["endpoints"]][0] == "**.*.*"
        extra = [rule["endpoints"][0]["host"] for name, rule in policies.items() if name != "allow_open"]
        assert extra == ["example.com", "pypi.org"]

    def test_the_open_policy_parses_as_the_proto_and_fits_the_gateway_limit(self):
        pytest.importorskip("openshell")
        from google.protobuf.json_format import ParseDict
        from openshell._proto import sandbox_pb2

        extra = [f"apex{i}.example" for i in range(50)]
        policy = _legacy_policy(allow_net=["*", *extra], mounts=[("/h", "/workspace", False)], uid=1000)
        message = ParseDict(policy, sandbox_pb2.SandboxPolicy())
        assert message.network_policies["allow_open"].endpoints[0].host == "**.*.*"
        assert message.ByteSize() < 262144

    def test_an_executor_accepts_the_open_default(self, tmp_path):
        assert OpenShellExecutor(image="x", workspace=tmp_path, allow_net=True, control_plane=FakeControlPlane())


class TestSandboxSpec:
    def test_the_spec_carries_image_mounts_limits_and_policy(self):
        pytest.importorskip("openshell")
        policy = _legacy_policy(allow_net=False, mounts=[("/h", "/workspace", False)], uid=1000)
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
        ex = _executor(tmp_path, cp, cpus=2, memory_mib=2048, create_timeout=77)
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

    async def test_an_unwritable_workspace_fails_start_and_removes_the_sandbox(self, tmp_path):
        cp = FakeControlPlane()
        cp.exec_script = lambda argv: FakeStream([("stdout", b"1000\nnot-writable\n"), ("exit", 0)])
        ex = _executor(tmp_path, cp)
        with pytest.raises(SandboxInitError, match=r"uid 1000\) cannot write"):
            await ex.start()
        await ox.drain_pending_deletes()
        assert cp.deleted == [cp.created[0]["name"]]

    async def test_a_root_host_says_why_the_sandbox_user_cannot_write(self, tmp_path, monkeypatch):
        cp = FakeControlPlane()
        cp.exec_script = lambda argv: FakeStream([("stdout", b"1000\nnot-writable\n"), ("exit", 0)])
        monkeypatch.setattr(ox, "_host_uid", lambda: 0)
        with pytest.raises(SandboxInitError) as info:
            await _executor(tmp_path, cp).start()
        assert "running as root" in str(info.value)
        assert "OpenShell refuses a root workload" in str(info.value)
        await ox.drain_pending_deletes()

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
        with pytest.raises(SandboxInitError, match="bind source path does not exist") as info:
            await _executor(Path("/tmp/ws"), cp).start()
        assert "PrivateTmp" in str(info.value)
        await ox.drain_pending_deletes()
        assert cp.deleted == [cp.created[0]["name"]]

    async def test_start_is_idempotent(self, tmp_path):
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        await ex.start()
        assert len(cp.created) == 1

    async def test_a_verify_probe_that_never_answers_is_cancelled_and_fails_start(self, tmp_path):
        cp = FakeControlPlane()
        silent = FakeStream(block=True)
        cp.exec_script = lambda argv: silent
        with pytest.raises(SandboxInitError, match="did not answer within 1s"):
            await _executor(tmp_path, cp, verify_timeout=1).start()
        assert silent.cancelled.is_set()
        await ox.drain_pending_deletes()
        assert cp.deleted == [cp.created[0]["name"]]

    async def test_an_unexpected_verify_answer_is_quoted(self, tmp_path):
        cp = FakeControlPlane()
        cp.exec_script = lambda argv: FakeStream([("stderr", b"sh: not found\n"), ("exit", 127)])
        with pytest.raises(SandboxInitError, match=r"verification failed \(exit_code=127.*sh: not found"):
            await _executor(tmp_path, cp).start()

    async def test_a_sandbox_init_error_from_the_plane_keeps_its_own_advice(self, tmp_path):
        cp = FakeControlPlane()
        cp.create_error = SandboxInitError("The OpenShell SDK is not installed")
        with pytest.raises(SandboxInitError) as info:
            await _executor(tmp_path, cp).start()
        assert str(info.value) == "The OpenShell SDK is not installed"

    async def test_without_a_control_plane_the_sdk_plane_is_built_for_the_gateway(self, tmp_path, monkeypatch):
        built: list[dict] = []
        cp = FakeControlPlane()

        def fake_plane(*, gateway, workspace):
            built.append({"gateway": gateway, "workspace": workspace})
            return cp

        monkeypatch.setattr(ox, "SDKControlPlane", fake_plane)
        ex = OpenShellExecutor(
            image="ubuntu:24.04", workspace=tmp_path, allow_net=False, gateway="dev", gateway_workspace="team"
        )
        await ex.start()
        await ex.exec("true")
        assert built == [{"gateway": "dev", "workspace": "team"}]
        assert len(cp.created) == 1


class TestReaper:
    async def test_sandboxes_of_dead_processes_on_this_host_are_deleted_once(self, tmp_path, monkeypatch):
        cp = FakeControlPlane()
        cp.listed = [("rvdead", {"raven.pid": "999999"}), ("rvlive", {"raven.pid": "1"}), ("rvnolabel", {})]
        monkeypatch.setattr(ox, "pid_alive", lambda pid: pid == 1)
        await _executor(tmp_path, cp).start()
        await _executor(tmp_path, cp).start()
        await ox.drain_pending_deletes()
        assert cp.selector.startswith("raven.owner=")
        assert cp.deleted.count("rvdead") == 1
        assert "rvlive" not in cp.deleted and "rvnolabel" not in cp.deleted

    async def test_a_gateway_that_cannot_list_does_not_block_the_start(self, tmp_path):
        cp = FakeControlPlane()

        def refuse(selector):
            raise RuntimeError("permission denied")

        cp.list_labeled = refuse
        await _executor(tmp_path, cp).start()
        assert len(cp.created) == 1 and cp.deleted == []

    async def test_a_gateway_that_cannot_list_is_asked_again_next_time(self, tmp_path, monkeypatch):
        """One blip must not switch the reaper off for the rest of the process."""
        cp = FakeControlPlane()
        calls: list[str] = []
        monkeypatch.setattr(ox, "pid_alive", lambda pid: False)

        def refuse(selector):
            calls.append(selector)
            if len(calls) == 1:
                raise RuntimeError("permission denied")
            return [("rvdead", {"raven.pid": "999999"})]

        cp.list_labeled = refuse
        await _executor(tmp_path, cp).start()
        await _executor(tmp_path, cp).start()
        await ox.drain_pending_deletes()
        assert len(calls) == 2
        assert cp.deleted == ["rvdead"]


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
        monkeypatch.setattr(ox, "_KILL_GRACE_SECONDS", 0.05)
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        blocking = FakeStream(block=True)
        cp.exec_script = lambda argv: blocking
        result = await ex.exec("sleep 100", timeout=0)
        assert result.exit_code == -1 and "timed out" in result.stderr
        assert blocking.cancelled.is_set()

    async def test_a_timed_out_command_does_not_outlive_its_budget(self, tmp_path, monkeypatch):
        """The local backstop is the command's own budget, not budget plus a
        long grace: the gateway enforces the same budget itself."""
        monkeypatch.setattr(ox, "_KILL_GRACE_SECONDS", 0.05)
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        cp.exec_script = lambda argv: FakeStream(block=True)
        started = time.monotonic()
        result = await ex.exec("sleep 100", timeout=1)
        elapsed = time.monotonic() - started
        assert result.exit_code == -1
        assert elapsed < 2, f"the command ran {elapsed:.1f}s past its 1s budget"

    async def test_a_timed_out_command_leaves_no_task_reporting_an_error(self, tmp_path):
        """A task that ends on an exception nobody reads is reported by asyncio,
        so the abandoned drain has to end on a result."""
        loop = asyncio.get_running_loop()
        reported: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        cp.exec_script = lambda argv: FakeStream(block=True)
        result = await ex.exec("sleep 100", timeout=0)
        assert result.exit_code == -1
        gc.collect()
        await asyncio.sleep(0.05)
        assert [context for context in reported if "never retrieved" in context.get("message", "")] == []

    async def test_a_broken_stream_reports_a_failed_command(self, tmp_path):
        """A stream that breaks mid-command is a failed command, not an
        exception thrown at the caller."""

        class Broken(FakeStream):
            def __iter__(self):
                yield ("stdout", b"partial\n")
                raise RuntimeError("stream reset")

        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        cp.exec_script = lambda argv: Broken()
        result = await ex.exec("echo hi", timeout=5)
        assert result.exit_code == -1

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

    async def test_a_sandbox_stopped_while_the_caller_waited_is_an_init_error(self, tmp_path, monkeypatch):
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)

        async def stopped_meanwhile():
            return None

        monkeypatch.setattr(ex, "start", stopped_meanwhile)
        with pytest.raises(SandboxInitError, match="stopped before the command could run"):
            await ex.exec("true")
        assert cp.execs == []


TCP_DENIAL = "NET:OPEN [MED] DENIED /usr/bin/curl(0) -> example.com:443 [reason:transparent_tcp_policy_denied]"
DNS_REFUSAL = "NET:REFUSE [MED] DENIED example.com [reason:policy_dns_ineligible]"
RELAY_CLOSED = "EVENT [INFO] ssh relay closed (channel_id=c1, target=unix:/run/openshell/ssh.sock)"


class TestDenials:
    @pytest.fixture(autouse=True)
    def _quick_polls(self, monkeypatch):
        monkeypatch.setattr(ox, "_DENIAL_POLL_SECONDS", 0.0)

    async def _started(self, tmp_path, exit_code=7, *, runs_for=0.0):
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()

        def script(argv):
            time.sleep(runs_for)
            return FakeStream([("exit", exit_code)])

        cp.exec_script = script
        return cp, ex

    async def test_a_command_carries_the_denials_logged_during_it(self, tmp_path):
        cp, ex = await self._started(tmp_path)
        cp.denial_script = [[TCP_DENIAL, RELAY_CLOSED]]
        result = await ex.exec("curl https://example.com")
        assert [(d.binary, d.host, d.port) for d in result.denials] == [("/usr/bin/curl", "example.com", 443)]

    async def test_a_failing_command_reads_until_its_relay_closed_line_arrives(self, tmp_path):
        cp, ex = await self._started(tmp_path)
        cp.denial_script = [[], [DNS_REFUSAL], [DNS_REFUSAL, TCP_DENIAL, RELAY_CLOSED]]
        result = await ex.exec("curl https://example.com")
        assert len(cp.denial_reads) == 3
        assert [(d.binary, d.host, d.port) for d in result.denials] == [("/usr/bin/curl", "example.com", 443)]

    async def test_a_relay_closed_before_the_command_ended_does_not_end_the_wait(self, tmp_path):
        cp, ex = await self._started(tmp_path, runs_for=0.6)
        cp.denial_script = [
            lambda since: [(since + 0.01, RELAY_CLOSED), DNS_REFUSAL],
            lambda since: [(since + 0.01, RELAY_CLOSED), DNS_REFUSAL, TCP_DENIAL, RELAY_CLOSED],
        ]
        result = await ex.exec("curl https://example.com")
        assert len(cp.denial_reads) == 2
        assert [d.port for d in result.denials] == [443]

    async def test_the_wait_for_the_relay_ends_at_its_deadline(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ox, "_DENIAL_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(ox, "_DENIAL_POLL_SECONDS", 0.01)
        cp, ex = await self._started(tmp_path)
        cp.denial_script = [[DNS_REFUSAL]] * 1000
        t0 = time.monotonic()
        result = await ex.exec("curl https://example.com")
        assert time.monotonic() - t0 < 1.0
        assert [(d.host, d.port) for d in result.denials] == [("example.com", None)]

    async def test_a_timed_out_command_reads_once_and_does_not_wait(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ox, "_DENIAL_WAIT_SECONDS", 5.0)
        cp, ex = await self._started(tmp_path, exit_code=-1)
        cp.denial_script = [[TCP_DENIAL]]
        t0 = time.monotonic()
        result = await ex.exec("sleep 60", timeout=1)
        assert len(cp.denial_reads) == 1 and time.monotonic() - t0 < 1.0
        assert [d.host for d in result.denials] == ["example.com"]

    async def test_a_failing_log_read_returns_the_result_without_denials(self, tmp_path):
        cp, ex = await self._started(tmp_path)
        cp.denial_script = [RuntimeError("gateway gone"), RuntimeError("gateway gone")]
        result = await ex.exec("curl https://example.com")
        assert (result.exit_code, result.denials) == (7, ())

    async def test_a_hung_log_read_is_abandoned_after_its_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ox, "_DENIAL_READ_TIMEOUT", 0.05)
        cp, ex = await self._started(tmp_path)
        cp.denial_script = [lambda since: time.sleep(1) or [TCP_DENIAL, RELAY_CLOSED]] * 2
        t0 = time.monotonic()
        result = await ex.exec("curl https://example.com")
        assert time.monotonic() - t0 < 0.8
        assert result.denials == ()

    async def test_denials_since_reads_the_sandbox_log_from_the_time_given(self, tmp_path):
        cp, ex = await self._started(tmp_path)
        cp.denial_script = [[TCP_DENIAL]]
        since = time.time() - 5
        assert [(d.host, d.port) for d in await ex.denials_since(since)] == [("example.com", 443)]
        assert cp.denial_reads == [since]

    async def test_denials_since_waits_for_a_connections_program_and_port_when_asked(self, tmp_path):
        cp, ex = await self._started(tmp_path)
        cp.denial_script = [[DNS_REFUSAL], [DNS_REFUSAL, TCP_DENIAL]]
        denials = await ex.denials_since(time.time(), wait=True)
        assert [(d.binary, d.port) for d in denials] == [("/usr/bin/curl", 443)]
        assert len(cp.denial_reads) == 2

    async def test_a_sandbox_that_is_not_running_has_no_denials(self, tmp_path):
        ex = _executor(tmp_path, FakeControlPlane())
        assert await ex.denials_since(time.time()) == []

    async def test_denials_outside_the_command_window_are_ignored(self, tmp_path):
        cp, ex = await self._started(tmp_path, exit_code=0)
        before = time.time()
        await ex.exec("true")
        assert abs(cp.denial_reads[0] - before) < 0.05

    async def test_a_successful_command_reads_once_and_does_not_wait(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ox, "_DENIAL_WAIT_SECONDS", 5.0)
        cp, ex = await self._started(tmp_path, exit_code=0)
        t0 = time.monotonic()
        result = await ex.exec("true")
        assert len(cp.denial_reads) == 1 and result.denials == ()
        assert time.monotonic() - t0 < 1.0


class TestLiveNetwork:
    async def _started(self, tmp_path, policies):
        cp = FakeControlPlane()
        state = {"policies": policies}
        ex = OpenShellExecutor(
            image="i", workspace=tmp_path, control_plane=cp, network_source=lambda: state["policies"]
        )
        await ex.start()
        return cp, ex, state

    async def test_a_changed_source_is_merged_before_the_next_command(self, tmp_path):
        cp, ex, state = await self._started(tmp_path, {"allow_open": {"name": "allow_open"}})
        state["policies"] = {"allow_open": {"name": "allow_open"}, "raven_rule_a": {"name": "raven_rule_a"}}
        await ex.exec("true")
        assert cp.merges == [({"raven_rule_a": {"name": "raven_rule_a"}}, [])]
        assert cp.waited == [2]

    async def test_an_unchanged_source_sends_nothing(self, tmp_path):
        cp, ex, _ = await self._started(tmp_path, {"allow_open": {"name": "allow_open"}})
        await ex.exec("true")
        await ex.exec("true")
        assert cp.merges == []

    async def test_a_rule_the_source_dropped_is_removed(self, tmp_path):
        cp, ex, state = await self._started(tmp_path, {"allow_open": {"name": "allow_open"}})
        state["policies"] = {}
        assert await ex.sync_network() is True
        assert cp.merges == [({}, ["allow_open"])]

    async def test_a_rule_whose_body_changed_is_removed_and_added_in_one_merge(self, tmp_path):
        # OpenShell merges an add_rule into a rule of the same name, so a
        # narrowed rule sent as an add alone would keep what it dropped.
        wide = {"name": "allow_open", "endpoints": [{"host": "pypi.org"}, {"host": "github.com"}]}
        narrow = {"name": "allow_open", "endpoints": [{"host": "github.com"}]}
        cp, ex, state = await self._started(tmp_path, {"allow_open": wide})
        state["policies"] = {"allow_open": narrow}
        await ex.exec("true")
        assert cp.merges == [({"allow_open": narrow}, ["allow_open"])]

    async def test_a_failed_merge_is_retried_on_the_next_command(self, tmp_path):
        cp, ex, state = await self._started(tmp_path, {})
        state["policies"] = {"raven_rule_a": {"name": "raven_rule_a"}}
        cp.merge_error = RuntimeError("gateway gone")
        await ex.exec("true")
        cp.merge_error = None
        await ex.exec("true")
        assert cp.merges == [({"raven_rule_a": {"name": "raven_rule_a"}}, [])]

    async def test_a_once_grant_applies_to_this_sandbox_and_survives_the_next_sync(self, tmp_path):
        from raven.sandbox.network_policy import NetworkGrant

        cp, ex, state = await self._started(tmp_path, {})
        assert await ex.grant_network([NetworkGrant("/usr/bin/curl", "example.com", 443)]) is True
        ((added, removed),) = cp.merges
        ((name, rule),) = added.items()
        assert name.startswith("raven_grant_") and rule["binaries"] == [{"path": "/usr/bin/curl"}]
        state["policies"] = {"raven_rule_a": {"name": "raven_rule_a"}}
        await ex.exec("true")
        assert cp.merges[-1] == ({"raven_rule_a": {"name": "raven_rule_a"}}, [])

    async def test_granting_to_a_stopped_executor_is_a_no_op(self, tmp_path):
        from raven.sandbox.network_policy import NetworkGrant

        cp, ex, _ = await self._started(tmp_path, {})
        await ex.stop()
        await ox.drain_pending_deletes()
        assert await ex.grant_network([NetworkGrant("/usr/bin/curl", "example.com", 443)]) is False
        assert await ex.sync_network() is False
        assert cp.merges == []

    async def test_a_policy_that_never_loads_reports_false_after_the_wait(self, tmp_path):
        from raven.sandbox.network_policy import NetworkGrant

        cp, ex, _ = await self._started(tmp_path, {})
        cp.policy_loads = False
        assert await ex.grant_network([NetworkGrant("/usr/bin/curl", "example.com", 443)]) is False


class TestStop:
    async def test_stop_deletes_in_the_background(self, tmp_path):
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.start()
        name = cp.created[0]["name"]
        await ex.stop()
        await ox.drain_pending_deletes()
        assert cp.deleted == [name]

    async def test_stop_without_start_and_a_second_stop_are_no_ops(self, tmp_path):
        cp = FakeControlPlane()
        ex = _executor(tmp_path, cp)
        await ex.stop()
        await ex.start()
        await ex.stop()
        await ex.stop()
        await ox.drain_pending_deletes()
        assert len(cp.deleted) == 1

    async def test_draining_waits_only_for_this_loop_s_deletes(self, monkeypatch):
        other = asyncio.new_event_loop()
        try:
            stranded = other.create_future()
            monkeypatch.setattr(ox, "_pending_deletes", {stranded})
            await asyncio.wait_for(ox.drain_pending_deletes(), 1)
        finally:
            other.close()

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

    async def test_a_broken_stream_still_delivers_the_unterminated_last_message(self, tmp_path, caplog):
        from mcp.shared.message import SessionMessage

        reply = {"jsonrpc": "2.0", "id": 2, "result": {}}

        class Broken(FakeStream):
            def __iter__(self):
                yield ("stdout", b"\n   \n" + json.dumps(reply).encode())
                raise ConnectionResetError("stream reset")

        cp = FakeControlPlane()
        cp.interactive_stream = Broken()
        ex = _executor(tmp_path, cp)
        await ex.start()
        read, write = await ex.start_process("srv", [])
        msg = await asyncio.wait_for(read.receive(), 2)
        assert isinstance(msg, SessionMessage) and msg.message.id == 2
        assert any("stream reset" in r.getMessage() for r in caplog.records)
        await write.aclose()
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


class _FakeCall:
    def __init__(self, events):
        self.events = list(events)
        self.cancelled = False

    def __iter__(self):
        return iter(self.events)

    def cancel(self):
        self.cancelled = True


class _FakeStub:
    """The raw gRPC stub surface SDKControlPlane reaches past the SDK wrappers."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.conditions: list[dict] = []
        self.get_error: Exception | None = None
        self.events: list = []
        self.log_lines: list[tuple[str, str, float]] = []
        self.active_versions: list[int] = []
        self.revision_status = 0

    def GetSandbox(self, request, timeout):
        from openshell._proto import openshell_pb2

        self.calls.append(("GetSandbox", request, timeout))
        if self.get_error:
            raise self.get_error
        response = openshell_pb2.SandboxResponse()
        for condition in self.conditions:
            response.sandbox.status.conditions.add(**condition)
        return response

    def UpdateConfig(self, request, timeout):
        from openshell._proto import openshell_pb2

        self.calls.append(("UpdateConfig", request, timeout))
        return openshell_pb2.UpdateConfigResponse(version=7)

    def GetSandboxPolicyStatus(self, request, timeout):
        from openshell._proto import openshell_pb2

        self.calls.append(("GetSandboxPolicyStatus", request, timeout))
        response = openshell_pb2.GetSandboxPolicyStatusResponse(active_version=self.active_versions.pop(0))
        response.revision.status = self.revision_status
        return response

    def GetSandboxLogs(self, request, timeout):
        from openshell._proto import openshell_pb2

        self.calls.append(("GetSandboxLogs", request, timeout))
        response = openshell_pb2.GetSandboxLogsResponse()
        for target, message, at in self.log_lines:
            line = response.logs.add(target=target, message=message)
            line.event_time.FromMicroseconds(int(at * 1_000_000))
        return response

    def ExecSandbox(self, request, timeout):
        self.calls.append(("ExecSandbox", request, timeout))
        self.call = _FakeCall(self.events)
        return self.call

    def ExecSandboxInteractive(self, requests):
        self.requests = requests
        self.call = _FakeCall(self.events)
        return self.call


class _FakeSDKClient:
    def __init__(self):
        self._stub = _FakeStub()
        self.calls: list[tuple] = []
        self.ready_error: Exception | None = None
        self.refs: list = []

    def create(self, **kw):
        self.calls.append(("create", kw))

    def wait_ready(self, name, **kw):
        self.calls.append(("wait_ready", name, kw))
        if self.ready_error:
            raise self.ready_error

    def delete(self, name, **kw):
        self.calls.append(("delete", name, kw))

    def list_all(self, **kw):
        self.calls.append(("list_all", kw))
        return self.refs


def _event(kind: str, value):
    from openshell._proto import openshell_pb2

    if kind == "exit":
        return openshell_pb2.ExecSandboxEvent(exit=openshell_pb2.ExecSandboxExit(exit_code=value))
    body = {"stdout": openshell_pb2.ExecSandboxStdout, "stderr": openshell_pb2.ExecSandboxStderr}[kind]
    return openshell_pb2.ExecSandboxEvent(**{kind: body(data=value)})


def _next_within(requests, seconds: float = 2.0):
    """``next()`` on the request iterator, failing instead of hanging when it blocks."""
    box: dict = {}

    def run():
        try:
            box["value"] = next(requests)
        except BaseException as exc:
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "the request iterator blocked"
    if "error" in box:
        raise box["error"]
    return box["value"]


@pytest.fixture
def sdk(monkeypatch):
    openshell = pytest.importorskip("openshell")
    client = _FakeSDKClient()
    clusters: list = []

    def from_active_cluster(*, cluster=None, **_kw):
        clusters.append(cluster)
        return client

    monkeypatch.setattr(openshell.SandboxClient, "from_active_cluster", staticmethod(from_active_cluster))
    plane = ox.SDKControlPlane(gateway="dev", workspace="team")
    assert clusters == ["dev"]
    return plane, client


class TestSDKControlPlane:
    def test_exec_sends_one_request_and_decodes_the_events(self, sdk):
        plane, client = sdk
        client._stub.events = [_event("stdout", b"hi"), _event("stderr", b"warn"), _event("exit", 3)]
        stream = plane.exec("rvx", ["sh", "-c", "echo hi"], workdir="/workspace", env={"A": "1"}, timeout_seconds=9)
        assert list(stream) == [("stdout", b"hi"), ("stderr", b"warn"), ("exit", 3)]
        ((kind, request, deadline),) = client._stub.calls
        assert kind == "ExecSandbox"
        assert request.workspace_scope.workspace == "team" and request.sandbox == "rvx"
        assert list(request.command) == ["sh", "-c", "echo hi"] and request.workdir == "/workspace"
        assert dict(request.environment) == {"A": "1"} and request.no_login_shell
        assert request.execution_timeout.seconds == 9
        assert deadline == 9 + ox._TIMEOUT_GRACE_SECONDS
        stream.cancel()
        assert client._stub.call.cancelled

    def test_exec_without_a_timeout_sets_no_deadline(self, sdk):
        plane, client = sdk
        plane.exec("rvx", ["true"], workdir="/", env={}, timeout_seconds=None)
        ((_kind, request, deadline),) = client._stub.calls
        assert not request.HasField("execution_timeout") and deadline is None

    def test_interactive_sends_the_start_request_then_stdin_until_closed(self, sdk):
        plane, client = sdk
        stream = plane.interactive("rvx", ["/workspace/srv"], workdir="/workspace", env={"K": "v"})
        requests = client._stub.requests
        first = _next_within(requests)
        assert first.WhichOneof("payload") == "start"
        assert first.start.sandbox == "rvx" and list(first.start.command) == ["/workspace/srv"]
        assert not first.start.HasField("execution_timeout")
        stream.write(b'{"id":1}\n')
        assert _next_within(requests).stdin == b'{"id":1}\n'
        stream.close_input()
        with pytest.raises(StopIteration):
            _next_within(requests)

    def test_cancelling_an_interactive_stream_ends_its_input(self, sdk):
        plane, client = sdk
        stream = plane.interactive("rvx", ["srv"], workdir="/", env={})
        requests = client._stub.requests
        _next_within(requests)
        stream.cancel()
        assert client._stub.call.cancelled
        with pytest.raises(StopIteration):
            _next_within(requests)

    def test_a_sandbox_that_is_not_ready_names_the_gateway_conditions(self, sdk):
        plane, client = sdk
        client.ready_error = TimeoutError("timed out waiting for rvx")
        client._stub.conditions = [
            {"type": "Ready", "status": "False", "reason": "DependenciesNotReady", "message": "bind source missing"},
            {"type": "Scheduled", "status": "True", "reason": "Ok", "message": "placed"},
            {"type": "Policy", "status": "False", "reason": "Pending", "message": ""},
        ]
        with pytest.raises(RuntimeError) as info:
            plane.wait_ready("rvx", 30)
        assert str(info.value) == "timed out waiting for rvx: DependenciesNotReady: bind source missing"
        assert client.calls == [("wait_ready", "rvx", {"workspace": "team", "timeout_seconds": 30})]
        ((_kind, request, _timeout),) = client._stub.calls
        assert request.name == "rvx" and request.workspace_scope.workspace == "team"

    def test_a_failed_condition_lookup_keeps_the_sdk_message(self, sdk):
        plane, client = sdk
        client.ready_error = TimeoutError("timed out waiting for rvx")
        client._stub.get_error = ConnectionError("unavailable")
        with pytest.raises(RuntimeError, match=r"^timed out waiting for rvx$"):
            plane.wait_ready("rvx", 30)

    def test_create_delete_and_list_stay_in_the_workspace(self, sdk):
        from types import SimpleNamespace

        plane, client = sdk
        plane.create(
            name="rvx",
            image="ubuntu:24.04",
            policy=_legacy_policy(allow_net=False, mounts=[], uid=0),
            driver_config={"docker": {"mounts": []}},
            resources={"limits": {"cpu": "1", "memory": "512Mi"}},
            labels={"raven.owner": "abc"},
        )
        ((kind, created),) = client.calls
        assert kind == "create" and created["workspace"] == "team" and created["name"] == "rvx"
        assert created["labels"] == {"raven.owner": "abc"} and created["spec"].template.image == "ubuntu:24.04"
        plane.delete("rvx")
        assert client.calls[-1] == ("delete", "rvx", {"workspace": "team", "allow_missing": True})
        client.refs = [SimpleNamespace(name="rv1", labels={"raven.pid": "3"})]
        assert plane.list_labeled("raven.owner=abc") == [("rv1", {"raven.pid": "3"})]
        assert client.calls[-1] == ("list_all", {"workspace": "team", "label_selector": "raven.owner=abc"})

    def test_ocsf_lines_keeps_the_sandbox_log_since_the_start_with_event_times(self, sdk):
        plane, client = sdk
        client._stub.log_lines = [
            ("ocsf", TCP_DENIAL, 1_700_000_001.5),
            ("openshell_sandbox", "supervisor started", 1_700_000_001.0),
        ]
        lines = plane.ocsf_lines("rvx", 1_700_000_000.25)
        assert lines == [(1_700_000_001.5, TCP_DENIAL)]
        ((kind, request, _timeout),) = client._stub.calls
        assert kind == "GetSandboxLogs"
        assert request.sandbox == "rvx" and request.workspace_scope.workspace == "team"
        assert request.since_time.seconds == 1_700_000_000 and request.since_time.nanos == 250_000_000

    def test_merge_rules_removes_then_adds_named_rules_in_one_update(self, sdk):
        plane, client = sdk
        rule = {
            "name": "raven_grant_x",
            "endpoints": [{"host": "example.com", "ports": [443], "tls": "NETWORK_TLS_MODE_SKIP"}],
            "binaries": [{"path": "/usr/bin/curl"}],
        }
        assert plane.merge_rules("rvx", {"raven_grant_x": rule}, ["allow_open"]) == 7
        ((kind, request, _timeout),) = client._stub.calls
        assert kind == "UpdateConfig" and request.sandbox == "rvx" and request.workspace_scope.workspace == "team"
        remove, add = request.merge_operations
        assert add.add_rule.rule_name == "raven_grant_x"
        assert add.add_rule.rule.endpoints[0].host == "example.com" and list(add.add_rule.rule.endpoints[0].ports) == [
            443
        ]
        assert add.add_rule.rule.binaries[0].path == "/usr/bin/curl"
        assert remove.remove_rule.rule_name == "allow_open"

    def test_wait_policy_polls_until_the_version_is_active(self, sdk, monkeypatch):
        monkeypatch.setattr(ox, "_POLICY_POLL_SECONDS", 0.01)
        plane, client = sdk
        client._stub.active_versions = [5, 6, 7]
        assert plane.wait_policy("rvx", 7, timeout=2) is True
        assert len(client._stub.calls) == 3

    def test_wait_policy_gives_up_on_a_failed_revision(self, sdk, monkeypatch):
        from openshell._proto import openshell_pb2

        monkeypatch.setattr(ox, "_POLICY_POLL_SECONDS", 0.01)
        plane, client = sdk
        client._stub.active_versions = [5] * 10
        client._stub.revision_status = openshell_pb2.POLICY_STATUS_FAILED
        assert plane.wait_policy("rvx", 7, timeout=2) is False
        assert len(client._stub.calls) == 1

    def test_a_missing_sdk_is_an_init_error_that_names_the_extra(self, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "openshell", None)
        with pytest.raises(SandboxInitError, match="sandbox extra"):
            ox.SDKControlPlane(gateway=None, workspace="default")
