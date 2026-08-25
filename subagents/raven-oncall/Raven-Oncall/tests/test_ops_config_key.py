"""A config with no entries still needs a name.

Measured 2026-08-12 on the OpenFOAM leg. The loop edited the case on the box, so
by submit time it had no parameters left to pass and sent ``configs=[{}]``. That
is a legitimate round -- the change was already in the case -- but the key built
from it was the empty string, and the empty string is not a name:

  * ``jobs/{idem_key}`` collapsed from ``jobs/<trial>/`` to ``jobs/`` itself, so
    the case, the pid file and the log landed in the jobs root;
  * the ledger record was filed under ``""``;
  * the backend handle read ``ops-``.

Only one job ran, so nothing showed. A second empty-config job would have reused
the same directory and the same ledger key and overwritten the first, silently.

The fix keeps idempotency intact rather than making each empty config unique:
two empty configs ARE the same config, and an idempotency key that agreed on
that was never the problem. The problem is that it agreed on a name that no path
and no ledger can hold.
"""

from __future__ import annotations

import pytest

from raven.ops.backend import JobSpec
from raven.ops.proposer import config_key


def test_an_empty_config_gets_a_usable_name() -> None:
    key = config_key({})
    assert key, "the empty string is not a name a path or a ledger key can hold"
    assert key.strip() == key and "/" not in key


def test_two_empty_configs_still_share_one_key() -> None:
    """Idempotency is the point of the key: the same config must not start a
    second job. Making empty configs unique would fix the path and break that."""
    assert config_key({}) == config_key({})


def test_a_config_with_entries_is_unchanged() -> None:
    """Existing campaigns' trial names must not shift under them -- a renamed key
    reads to the backend as a job that was never submitted."""
    assert config_key({"nuWater": 1e-06}) == "nuWater1em06"
    assert config_key({"k1": 1.5, "b": 0.75}) == "b0p75_k11p5"


def test_the_empty_name_cannot_collide_with_a_real_config() -> None:
    """Whatever stands in for "no parameters" must not be reachable from a config
    that does have parameters, or two different rounds would share a job."""
    assert config_key({}) != config_key({"default": ""})


def test_a_spec_with_no_key_is_refused_at_the_door() -> None:
    """Second line, for the paths config_key does not run through: world_state
    rebuilds a spec from disk, and a stored key that came back empty would
    collapse the same way. A spec that cannot name its job is not a spec.
    """
    with pytest.raises(ValueError, match="idem_key"):
        JobSpec({"nuWater": 1e-06}, idem_key="")
    with pytest.raises(ValueError, match="idem_key"):
        JobSpec({}, idem_key="   ")


def test_a_spec_with_a_key_still_builds() -> None:
    spec = JobSpec({"nuWater": 1e-06}, idem_key=config_key({"nuWater": 1e-06}))
    assert spec.idem_key == "nuWater1em06"
