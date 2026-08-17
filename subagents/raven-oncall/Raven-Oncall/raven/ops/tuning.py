"""Adaptive tuning loop: let a proposer drive a campaign round by round.

Each round asks the proposer for the next batch of trials given the results so
far, runs them on the campaign's backend, and repeats until the proposer stops
(returns []), no new trials are added, or the round cap is hit. The proposer may
be mechanical (a grid) or LLM-driven; either way scoring stays deterministic in
the trial, so the reward remains verifiable.
"""

from __future__ import annotations

from raven.ops.campaign import Campaign
from raven.ops.ledger import JobRecord
from raven.ops.proposer import Proposer


async def tune(campaign: Campaign, proposer: Proposer, *, max_rounds: int = 8) -> JobRecord | None:
    for round_index in range(max_rounds):
        batch = await proposer.propose(campaign.history(), round_index)
        if not batch:
            break
        if campaign.add_trials(batch) == 0:
            break
        await campaign.run()
    return campaign.best()
