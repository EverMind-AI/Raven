"""The address vocabulary: what a host string denotes, whatever the policy."""

from __future__ import annotations

import ipaddress

import pytest

from raven.security import hosts


@pytest.mark.parametrize(
    "spelling,address",
    [
        ("2852039166", "169.254.169.254"),
        ("0xA9FEA9FE", "169.254.169.254"),
        ("0251.0376.0251.0376", "169.254.169.254"),
        ("127.1", "127.0.0.1"),
        ("169.254.169.254.", "169.254.169.254"),
    ],
)
def test_legacy_ipv4_reads_the_browser_spellings(spelling: str, address: str) -> None:
    assert hosts.legacy_ipv4(spelling) == ipaddress.IPv4Address(address)
    assert hosts.is_legacy_ip_form(spelling)


@pytest.mark.parametrize(
    "spelling", ["example.com", "localhost", "\u0661\u0662\u0663.example", "1.2.3.4.5", "300.1.1.1"]
)
def test_a_name_is_not_a_legacy_address(spelling: str) -> None:
    assert hosts.legacy_ipv4(spelling) is None


def test_a_canonical_address_is_not_legacy() -> None:
    assert not hosts.is_legacy_ip_form("169.254.169.254")
    assert hosts.as_ip("[::1]") == ipaddress.ip_address("::1")


def test_fullwidth_stops_map_onto_the_address() -> None:
    assert hosts.mapped_host("169\uff0e254\uff0e169\uff0e254") == "169.254.169.254"


@pytest.mark.parametrize(
    "spelling",
    [
        "169.254.169.254",
        "127.0.0.1",
        "10.1.2.3",
        "100.64.0.1",
        "fec0::1",
        "::ffff:169.254.169.254",
        "::169.254.169.254",
        "64:ff9b::a9fe:a9fe",
    ],
)
def test_not_public_sees_through_every_spelling(spelling: str) -> None:
    assert hosts.not_public(ipaddress.ip_address(spelling))


@pytest.mark.parametrize("spelling", ["93.184.216.34", "2606:4700::1111", "64:ff9b::808:808"])
def test_a_public_destination_stays_public_in_translation(spelling: str) -> None:
    assert not hosts.not_public(ipaddress.ip_address(spelling))


def test_the_two_questions_about_localhost() -> None:
    assert hosts.is_local_name("localhost") and hosts.names_this_machine("localhost")
    assert not hosts.is_local_name("evil.localhost") and hosts.names_this_machine("evil.localhost")
