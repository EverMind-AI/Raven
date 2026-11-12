"""The MCP configuration of an Agent Plugin package.

The rules under test are the ones the Agent Plugins specification states
for ``mcp.json``: the file is a closed object, its version has to match the
manifest's, a server entry that cannot be used never takes the others with
it, and the two placeholders are expanded in exactly the fields the format
names.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from raven.agent_plugins.mcp import (
    MCP_SCHEMA_URL,
    PACKAGE_SERVER_SEPARATOR,
    AgentPluginMcpConfig,
    AgentPluginMcpError,
    expand_placeholders,
    package_mcp_layer,
    package_mcp_report,
    package_mcp_servers,
    read_mcp_config,
    schema_version,
)
from raven.agent_plugins.package import load_package
from raven.mcp.client import open_mcp_transport

MANIFEST_SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
PACKAGE_NAME = "demo-mcp"


def _package(tmp_path: Path, *, mcp: dict | None = None, name: str = PACKAGE_NAME) -> Path:
    """A package root holding a manifest and (optionally) an MCP configuration."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps({"$schema": MANIFEST_SCHEMA_URL, "name": name, "version": "1.0.0"}),
        encoding="utf-8",
    )
    if mcp is not None:
        (root / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    return root


def _mcp(servers: dict, *, schema: str = MCP_SCHEMA_URL, extra: dict | None = None) -> dict:
    document = {"$schema": schema, "mcpServers": servers}
    if extra:
        document.update(extra)
    return document


def _stdio(**fields) -> dict:
    entry = {"type": "stdio", "command": "npx"}
    entry.update(fields)
    return entry


def _http(**fields) -> dict:
    entry = {"type": "streamable-http", "url": "https://example.invalid/mcp"}
    entry.update(fields)
    return entry


def test_plugin_data_is_spelled_through_a_resolved_data_root(tmp_path: Path) -> None:
    """The sandbox translator compares resolved roots; PLUGIN_DATA must match."""
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(command="npx")}))
    real = tmp_path / "data"
    real.mkdir()
    link = tmp_path / "data-link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("this platform cannot create a symlink")

    servers, notes = package_mcp_layer([root], allowed=[PACKAGE_NAME], plugin_data_root=link)

    assert notes == []
    server = servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]
    assert server.env["PLUGIN_DATA"] == str(real / PACKAGE_NAME)


def test_a_symlinked_package_data_dir_keeps_the_link_spelling(tmp_path: Path) -> None:
    """PLUGIN_DATA names the caller's directory, not its symlink target."""
    root = _package(tmp_path, mcp=_mcp({"local": _stdio()}))
    data_root = tmp_path / "data"
    data_root.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = data_root / PACKAGE_NAME
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("this platform cannot create a symlink")

    report = package_mcp_report(load_package(root), plugin_data=link)

    assert report.problems == ()
    server = report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]
    assert Path(server.env["PLUGIN_DATA"]) == link
    assert Path(server.env["PLUGIN_DATA"]) != target


def test_a_symlink_loop_in_a_package_command_is_a_server_problem(tmp_path: Path) -> None:
    """Python 3.12's ``Path.resolve`` raises ``RuntimeError`` on ELOOP, not ``OSError``."""
    root = _package(tmp_path, mcp=_mcp({"loop": _stdio(command="./loop")}))
    loop = root / "loop"
    try:
        loop.symlink_to("loop")
    except OSError:
        pytest.skip("this platform cannot create a symlink")
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert [problem.server for problem in report.problems] == ["loop"]


def test_a_data_dir_loop_does_not_crash_the_report(tmp_path: Path) -> None:
    """A remote server never reads the data directory, so its loop must not be fatal.

    Python 3.12's ``Path.resolve`` raises ``RuntimeError`` on ELOOP, not
    ``OSError``.
    """
    root = _package(tmp_path, mcp=_mcp({"remote": _http()}))
    data = tmp_path / "data"
    data.mkdir()
    loop = data / PACKAGE_NAME
    try:
        loop.symlink_to(PACKAGE_NAME)
    except OSError:
        pytest.skip("this platform cannot create a symlink")

    report = package_mcp_report(load_package(root), plugin_data=loop)

    assert report.disabled == ""
    assert list(report.servers) == [f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}remote"]


def test_a_data_dir_loop_does_not_take_the_layer_down(tmp_path: Path) -> None:
    """A package outside ``allowed`` is still parsed, so the loop reaches the layer."""
    root = _package(tmp_path, mcp=_mcp({"remote": _http()}))
    data = tmp_path / "data"
    data.mkdir()
    loop = data / PACKAGE_NAME
    try:
        loop.symlink_to(PACKAGE_NAME)
    except OSError:
        pytest.skip("this platform cannot create a symlink")

    servers, notes = package_mcp_layer([root], allowed=[], plugin_data_root=data)

    assert servers == {}
    assert notes == []


def test_an_unresolvable_data_root_does_not_raise(tmp_path: Path) -> None:
    """A data root the host cannot resolve falls back to the caller's spelling."""
    root = _package(tmp_path, mcp=_mcp({"remote": _http()}))
    loop = tmp_path / "data"
    try:
        loop.symlink_to("data")
    except OSError:
        pytest.skip("this platform cannot create a symlink")

    report = package_mcp_report(load_package(root), plugin_data=loop / PACKAGE_NAME)

    assert report.disabled == ""
    assert list(report.servers) == [f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}remote"]


def test_an_unresolvable_data_root_reports_every_package(tmp_path: Path) -> None:
    """The data root is shared, so an unusable one stops every package's server."""
    root = _package(tmp_path, mcp=_mcp({"remote": _http()}))
    loop = tmp_path / "data"
    try:
        loop.symlink_to("data")
    except OSError:
        pytest.skip("this platform cannot create a symlink")

    servers, problems, entries = package_mcp_servers(
        [load_package(root)], allowed=[PACKAGE_NAME], plugin_data_root=loop
    )

    assert servers == {}
    assert entries == []
    assert [problem.path for problem in problems] == [str(root)]
    assert "cannot be resolved" in problems[0].reason


def test_the_runtime_entry_reports_a_data_root_loop(tmp_path: Path, monkeypatch) -> None:
    """The runtime helper must reach the guard instead of creating the root first."""
    from types import SimpleNamespace

    from raven.core.runtime import _package_mcp_servers

    root = _package(tmp_path, mcp=_mcp({"remote": _http()}))
    loop = tmp_path / "data"
    try:
        loop.symlink_to("data")
    except OSError:
        pytest.skip("this platform cannot create a symlink")
    monkeypatch.setattr("raven.config.paths.package_data_root_path", lambda: loop)
    ec_config = SimpleNamespace(
        agent_plugins=SimpleNamespace(packages=[str(root)], mcp_packages=[PACKAGE_NAME], disabled=[])
    )

    servers, notes = _package_mcp_servers(ec_config)

    assert servers == {}
    assert len(notes) == 1
    assert "cannot be resolved" in notes[0]


def test_an_uncreatable_package_data_dir_reports_its_package(tmp_path: Path) -> None:
    """An allowed package whose data directory cannot be created is skipped, not fatal."""
    root = _package(tmp_path, mcp=_mcp({"remote": _http()}))
    data = tmp_path / "data"
    data.mkdir()
    loop = data / PACKAGE_NAME
    try:
        loop.symlink_to(PACKAGE_NAME)
    except OSError:
        pytest.skip("this platform cannot create a symlink")

    servers, problems, entries = package_mcp_servers(
        [load_package(root)], allowed=[PACKAGE_NAME], plugin_data_root=data
    )

    assert servers == {}
    assert entries == []
    assert [problem.path for problem in problems] == [str(root)]
    assert "cannot be created" in problems[0].reason


# ---------------------------------------------------------------------------
# read_mcp_config
# ---------------------------------------------------------------------------


def test_read_mcp_config_parses_a_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(_mcp({"local": _stdio()})), encoding="utf-8")

    config = read_mcp_config(path)

    assert isinstance(config, AgentPluginMcpConfig)
    assert config.schema_uri == MCP_SCHEMA_URL
    assert list(config.mcp_servers) == ["local"]


def test_read_mcp_config_accepts_an_empty_server_map(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(_mcp({})), encoding="utf-8")

    assert read_mcp_config(path).mcp_servers == {}


@pytest.mark.parametrize(
    "content",
    [
        "{ not json",
        "[]",
        json.dumps({"mcpServers": {}}),
        json.dumps({"$schema": MCP_SCHEMA_URL}),
        json.dumps(_mcp({}, extra={"unexpected": True})),
    ],
)
def test_read_mcp_config_refuses_an_unusable_file(tmp_path: Path, content: str) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(AgentPluginMcpError):
        read_mcp_config(path)


def test_read_mcp_config_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(AgentPluginMcpError):
        read_mcp_config(tmp_path / "absent.json")


# ---------------------------------------------------------------------------
# schema_version
# ---------------------------------------------------------------------------


def test_schema_version_reads_the_canonical_identifier() -> None:
    assert schema_version(MCP_SCHEMA_URL) == "1.0.0"
    assert schema_version(MANIFEST_SCHEMA_URL) == "1.0.0"


def test_schema_version_is_none_for_anything_else() -> None:
    assert schema_version("https://example.invalid/mcp.schema.json") is None
    assert schema_version("") is None


# ---------------------------------------------------------------------------
# package_mcp_report: the version rule
# ---------------------------------------------------------------------------


def test_report_accepts_a_configuration_of_the_manifest_version(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio()}))

    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.disabled == ""
    assert list(report.servers) == [f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]


def test_report_disables_mcp_when_the_version_differs(tmp_path: Path) -> None:
    root = _package(
        tmp_path,
        mcp=_mcp({"local": _stdio()}, schema="https://agent-plugins.org/schemas/2.0.0/mcp.schema.json"),
    )

    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert "version" in report.disabled
    assert "2.0.0" in report.disabled


def test_report_disables_mcp_for_a_non_canonical_schema(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio()}, schema="https://example.invalid/mcp.schema.json"))

    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert "canonical" in report.disabled


def test_report_of_a_package_without_mcp_is_empty(tmp_path: Path) -> None:
    root = _package(tmp_path)

    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert report.problems == ()
    assert report.disabled == ""


# ---------------------------------------------------------------------------
# stdio servers
# ---------------------------------------------------------------------------


def test_stdio_keeps_a_bare_command(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(command="npx", args=["-y", "server"])}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    server = report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]

    assert server.command == "npx"
    assert server.args == ["-y", "server"]


def test_stdio_refuses_a_shell_command_string(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(command="npx -y server")}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert "single executable token" in report.problems[0].reason


def test_stdio_anchors_a_plugin_relative_command_at_the_package_root(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(command="./bin/server")}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    server = report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]

    assert Path(server.command).is_absolute()
    assert Path(server.command) == (root.resolve() / "bin" / "server")


def test_stdio_refuses_a_plugin_relative_command_escaping_the_root(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(command="./../outside")}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert "outside" in report.problems[0].reason


def test_stdio_refuses_a_command_that_is_neither_bare_nor_plugin_relative(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(command="bin/server")}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert "plugin-relative" in report.problems[0].reason


def test_stdio_defaults_its_working_directory_to_the_package_root(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio()}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    server = report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]

    assert Path(server.cwd) == root.resolve()


@pytest.mark.parametrize(
    "cwd",
    ["${PLUGIN_ROOT}", "${PLUGIN_ROOT}/sub", "./sub", "${PLUGIN_DATA}", "${PLUGIN_DATA}/cache"],
)
def test_stdio_accepts_the_three_working_directory_forms(tmp_path: Path, cwd: str) -> None:
    data = tmp_path / "data"
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(cwd=cwd)}))
    report = package_mcp_report(load_package(root), plugin_data=data)

    server = report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]

    assert Path(server.cwd).is_absolute()
    assert report.problems == ()


@pytest.mark.parametrize("cwd", ["/etc", "../outside", "sub"])
def test_stdio_refuses_a_working_directory_outside_the_named_forms(tmp_path: Path, cwd: str) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(cwd=cwd)}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert "working directory" in report.problems[0].reason


def test_stdio_refuses_a_working_directory_escaping_its_anchor(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(cwd="${PLUGIN_ROOT}/../outside")}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert "working directory" in report.problems[0].reason


@pytest.mark.parametrize("reserved", ["PLUGIN_ROOT", "PLUGIN_DATA"])
def test_stdio_refuses_a_reserved_environment_name(tmp_path: Path, reserved: str) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(env={reserved: "hijacked"})}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert reserved in report.problems[0].reason


def test_stdio_expands_placeholders_in_args_and_env(tmp_path: Path) -> None:
    root = _package(
        tmp_path,
        mcp=_mcp(
            {
                "local": _stdio(
                    args=["--data", "${PLUGIN_DATA}/db", "--root", "${PLUGIN_ROOT}"],
                    env={"CONFIG": "${PLUGIN_ROOT}/config.json"},
                )
            }
        ),
    )
    data = tmp_path / "data"
    report = package_mcp_report(load_package(root), plugin_data=data)

    server = report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]

    assert server.args == ["--data", f"{data}/db", "--root", str(root.resolve())]
    assert server.env["CONFIG"] == f"{root.resolve()}/config.json"


@pytest.mark.parametrize(
    "arg",
    [
        "../outside",
        "${PLUGIN_ROOT}/../../etc/passwd",
        "..",
        "x/..",
        "a/../b",
        "..\\outside",
        "D:..\\target.txt",
        "d:..\\target.txt",
        "D:..",
        "D:.. ",
        "--config=../secret",
        "-I../include",
        "--require=../x.js",
        "PATH=a:../b",
        "a;..",
    ],
)
def test_stdio_refuses_an_argument_carrying_a_dotdot_component(tmp_path: Path, arg: str) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(args=[arg])}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert "'..' path component" in report.problems[0].reason


@pytest.mark.parametrize("value", ["${PLUGIN_DATA}/../../secret", "D:..\\secret", "--config=../secret"])
def test_stdio_refuses_an_environment_value_carrying_a_dotdot_component(tmp_path: Path, value: str) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(env={"CONFIG": value})}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert "'..' path component" in report.problems[0].reason


@pytest.mark.parametrize("arg", ["a..b", "...", "....", "./bin/tool", "..-ish", "D:foo"])
def test_stdio_keeps_an_argument_without_a_dotdot_component(tmp_path: Path, arg: str) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(args=[arg])}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.problems == ()
    assert report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"].args == [arg]


def test_a_dotdot_environment_value_does_not_take_other_entries_down(tmp_path: Path) -> None:
    root = _package(
        tmp_path,
        mcp=_mcp(
            {
                "bad": _stdio(env={"CONFIG": "${PLUGIN_ROOT}/../../secret"}),
                "good": _stdio(command="npx"),
            }
        ),
    )
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert [problem.server for problem in report.problems] == ["bad"]
    assert list(report.servers) == [f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}good"]


def test_a_dotdot_data_root_does_not_refuse_a_plugin_data_value(tmp_path: Path) -> None:
    """RAVEN_HOME may spell the data root through '..'; the runtime resolves it first."""
    root = _package(
        tmp_path,
        mcp=_mcp({"local": _stdio(env={"CONFIG": "${PLUGIN_DATA}/db.sqlite"})}),
    )
    data_root = tmp_path / "data"
    data_root.mkdir()
    plugin_data = tmp_path / "data" / ".." / "data" / PACKAGE_NAME

    report = package_mcp_report(load_package(root), plugin_data=plugin_data)

    assert report.problems == ()
    server = report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]
    assert Path(server.env["PLUGIN_DATA"]) == data_root / PACKAGE_NAME
    assert Path(server.env["CONFIG"]) == data_root / PACKAGE_NAME / "db.sqlite"


def test_stdio_carries_the_two_reserved_variables(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio()}))
    data = tmp_path / "data"
    report = package_mcp_report(load_package(root), plugin_data=data)

    server = report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]

    assert server.env["PLUGIN_ROOT"] == str(root.resolve())
    assert server.env["PLUGIN_DATA"] == str(data)


def test_composed_servers_mark_themselves_package_sourced(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(command="npx"), "remote": _http()}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    local = report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]
    remote = report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}remote"]

    assert local.source == "package"
    assert remote.source == "package"


# ---------------------------------------------------------------------------
# remote servers
# ---------------------------------------------------------------------------


def test_http_maps_streamable_http_to_the_raven_transport(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"remote": _http()}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    server = report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}remote"]

    assert server.type == "streamableHttp"
    assert server.url == "https://example.invalid/mcp"


def test_http_maps_sse_to_the_raven_transport(tmp_path: Path) -> None:
    root = _package(
        tmp_path,
        mcp=_mcp({"remote": {"type": "sse", "url": "https://example.invalid/sse"}}),
    )
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}remote"].type == "sse"


@pytest.mark.parametrize(
    "url",
    [
        "relative/path",
        "ftp://example.invalid/mcp",
        "https://user:secret@example.invalid/mcp",
        "https://example.invalid/mcp#fragment",
    ],
)
def test_http_refuses_a_url_the_format_excludes(tmp_path: Path, url: str) -> None:
    root = _package(tmp_path, mcp=_mcp({"remote": _http(url=url)}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert report.problems


@pytest.mark.parametrize("url", ["http://localhost:9000/mcp", "http://127.0.0.1:9000/mcp"])
def test_http_allows_plain_http_on_the_loopback(tmp_path: Path, url: str) -> None:
    root = _package(tmp_path, mcp=_mcp({"remote": _http(url=url)}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.problems == ()
    assert report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}remote"].url == url


def test_http_refuses_plain_http_on_a_public_host(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"remote": _http(url="http://example.invalid/mcp")}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert report.problems


def test_http_refuses_a_header_spelled_twice_under_different_casing(tmp_path: Path) -> None:
    root = _package(
        tmp_path,
        mcp=_mcp({"remote": _http(headers={"X-Tenant": "a", "x-tenant": "b"})}),
    )
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert "twice" in report.problems[0].reason


def test_http_refuses_a_header_value_carrying_a_line_break(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"remote": _http(headers={"X-Tenant": "a\r\nX-Evil: b"})}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert report.servers == {}
    assert "line break" in report.problems[0].reason


def test_http_does_not_expand_placeholders(tmp_path: Path) -> None:
    url = "https://example.invalid/${PLUGIN_DATA}/mcp"
    root = _package(tmp_path, mcp=_mcp({"remote": _http(url=url, headers={"X-Path": "${PLUGIN_ROOT}"})}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    server = report.servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}remote"]

    assert server.url == url
    assert server.headers == {"X-Path": "${PLUGIN_ROOT}"}


# ---------------------------------------------------------------------------
# entry-level failure isolation
# ---------------------------------------------------------------------------


def test_one_bad_entry_does_not_take_the_others_down(tmp_path: Path) -> None:
    root = _package(
        tmp_path,
        mcp=_mcp(
            {
                "good": _stdio(),
                "bad": _stdio(command="npx -y server"),
                "remote": _http(),
                "unknown-type": {"type": "carrier-pigeon"},
            }
        ),
    )
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert set(report.servers) == {
        f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}good",
        f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}remote",
    }
    assert {problem.server for problem in report.problems} == {"bad", "unknown-type"}


def test_a_server_entry_that_is_not_an_object_is_refused_alone(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"good": _stdio(), "bad": "npx"}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert list(report.servers) == [f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}good"]
    assert report.problems[0].server == "bad"


def test_a_server_entry_carrying_a_field_of_another_variant_is_refused(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"good": _stdio(), "bad": _stdio(url="https://example.invalid")}))
    report = package_mcp_report(load_package(root), plugin_data=tmp_path / "data")

    assert list(report.servers) == [f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}good"]
    assert report.problems[0].server == "bad"


# ---------------------------------------------------------------------------
# expand_placeholders
# ---------------------------------------------------------------------------


def test_expand_placeholders_replaces_both_names() -> None:
    assert expand_placeholders("${PLUGIN_ROOT}/a:${PLUGIN_DATA}/b", plugin_root="/r", plugin_data="/d") == "/r/a:/d/b"


def test_expand_placeholders_leaves_other_text_alone() -> None:
    assert expand_placeholders("${HOME}/x ${PLUGIN_ROOT_DIR}", plugin_root="/r", plugin_data="/d") == (
        "${HOME}/x ${PLUGIN_ROOT_DIR}"
    )


def test_expand_placeholders_does_not_rescan_what_it_introduced() -> None:
    assert expand_placeholders("${PLUGIN_ROOT}/x", plugin_root="${PLUGIN_DATA}", plugin_data="/d") == (
        "${PLUGIN_DATA}/x"
    )


# ---------------------------------------------------------------------------
# package_mcp_layer: which packages run
# ---------------------------------------------------------------------------


def test_layer_only_runs_the_allowed_packages(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio()}))

    servers, notes = package_mcp_layer([root], allowed=[], plugin_data_root=tmp_path / "data")

    assert servers == {}
    assert notes == []


def test_layer_composes_the_allowed_package_and_creates_its_data_directory(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio()}))
    data_root = tmp_path / "data"

    servers, notes = package_mcp_layer([root], allowed=[PACKAGE_NAME], plugin_data_root=data_root)

    assert list(servers) == [f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]
    assert notes == []
    assert (data_root / PACKAGE_NAME).is_dir()


def test_layer_reports_a_broken_configuration_without_running_it(tmp_path: Path) -> None:
    root = _package(tmp_path, mcp=_mcp({"local": _stdio()}, schema="https://example.invalid/schema.json"))

    servers, notes = package_mcp_layer([root], allowed=[PACKAGE_NAME], plugin_data_root=tmp_path / "data")

    assert servers == {}
    assert notes


def test_a_host_server_name_is_never_replaced_by_a_package(tmp_path: Path) -> None:
    """Host names are unrestricted, so a composed name may already be taken."""
    root = _package(tmp_path, mcp=_mcp({"search": _http(url="https://pkg.invalid/mcp")}), name="acme")
    taken = f"acme{PACKAGE_SERVER_SEPARATOR}search"

    servers, notes = package_mcp_layer(
        [root],
        allowed=["acme"],
        plugin_data_root=tmp_path / "data",
        reserved_names=[taken],
    )

    assert servers == {}
    assert any("already configured" in note for note in notes)


async def test_a_composed_package_server_probes_outside_its_root(monkeypatch, tmp_path: Path) -> None:
    """The composer's marker reaches the transport: probe outside, launch inside.

    The two halves are unit-tested apart; this drives the real chain from the
    package file to the transport, so a marker dropped between them is caught.
    """
    root = _package(tmp_path, mcp=_mcp({"local": _stdio(command="npx")}))
    servers, problems, entries = package_mcp_servers(
        [load_package(root)], allowed=[PACKAGE_NAME], plugin_data_root=tmp_path / "data"
    )
    assert problems == [] and entries == []
    server = servers[f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}local"]
    assert server.source == "package"

    seen: dict = {}

    async def _fake_launch(argv, env, cwd=None, **kwargs):
        seen["probe_cwd"] = cwd
        return list(argv)

    @asynccontextmanager
    async def _fake_stdio(params, errlog=None):
        seen["launch_cwd"] = params.cwd
        yield ("read", "write")

    monkeypatch.setattr("raven.utils.mirrors.package_launch_async", _fake_launch)
    monkeypatch.setattr("mcp.client.stdio.stdio_client", _fake_stdio)

    async with open_mcp_transport(server, "stdio"):
        pass

    assert seen["probe_cwd"] is None
    assert Path(seen["launch_cwd"]).resolve() == root.resolve()
