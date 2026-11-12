"""The gateway's OCSF denial lines, as recorded on a real OpenShell 0.1.8 gateway, parsed into denials."""

from __future__ import annotations

from raven.sandbox.denials import NetworkDenial, denial_line, parse_denials

TCP = "NET:OPEN [MED] DENIED /usr/bin/curl(0) -> example.com:443 [reason:transparent_tcp_policy_denied]"
DNS = "NET:REFUSE [MED] DENIED example.com [reason:policy_dns_ineligible]"


def test_a_tcp_denial_names_binary_host_port_and_reason():
    assert parse_denials([TCP]) == [
        NetworkDenial("/usr/bin/curl", "example.com", 443, "transparent_tcp_policy_denied"),
    ]


def test_a_dns_refusal_is_kept_only_without_a_tcp_line_for_the_host():
    assert parse_denials([DNS]) == [NetworkDenial("", "example.com", None, "policy_dns_ineligible")]
    assert parse_denials([DNS, TCP]) == [
        NetworkDenial("/usr/bin/curl", "example.com", 443, "transparent_tcp_policy_denied"),
    ]


def test_other_ocsf_lines_are_ignored():
    assert (
        parse_denials(
            [
                "SSH:OPEN [INFO] ALLOWED",
                "CONFIG:OBSERVATION [INFO] Policy DNS staged unapproved name example.com synthetic=198.18.0.2 "
                "for TCP policy review",
                "EVENT [INFO] ssh relay open (channel_id=63326a2b, target=unix:/run/openshell/ssh.sock)",
            ]
        )
        == []
    )


def test_repeated_denials_collapse_to_one():
    assert len(parse_denials([TCP, TCP])) == 1


def test_ipv6_and_bracketed_hosts_parse():
    line = "NET:OPEN [MED] DENIED /usr/bin/python3.12(812) -> [2606:4700::1]:443 [reason:transparent_tcp_policy_denied]"
    assert parse_denials([line]) == [
        NetworkDenial("/usr/bin/python3.12", "2606:4700::1", 443, "transparent_tcp_policy_denied"),
    ]


def test_first_seen_order_is_kept():
    other = TCP.replace("example.com", "example.org")
    assert [d.host for d in parse_denials([other, TCP])] == ["example.org", "example.com"]


def test_a_denial_reads_as_binary_arrow_target():
    assert denial_line(NetworkDenial("/usr/bin/curl", "example.com", 443, "x")) == "/usr/bin/curl -> example.com:443"
    assert denial_line(NetworkDenial("", "example.com", None, "x")) == "example.com"
