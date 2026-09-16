"""PROBE, removed before this branch merges: a wait the ceiling must still see.

The change under test subtracts runqueue wait from idle. That is only correct
if a test which genuinely waits keeps its idle: a deliberate sleep is not
runnable, so its runqueue wait must stay near zero while its idle stays near
the sleep. Without this control the subtraction could be erasing every wait,
including the production backoffs the ceiling exists to catch, and a green
shard would look like success.
"""

from __future__ import annotations

import time


def test_probe_a_real_wait_keeps_its_idle() -> None:
    time.sleep(4.0)
