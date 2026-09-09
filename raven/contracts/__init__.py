"""The papers: declared shapes the layers hold each other to.

Two promise tiers, tracked per module via ``__tier__`` and enforced by the
two-tier ledger guard (tests/test_contracts_two_tier_ledger.py):

- ``contract``     — frozen for every loop; every paper stamped so. The turn
  contract is spine's (``raven/spine/turn.py``), not a paper here.
- ``factory_loop`` — versioned with the factory loop; carries the marker
  "Versioned with the factory loop" in its docstring.

Modules here export declared members only (``__all__`` is the ledger row) and
import no machinery — a paper describes, it does not do.

The contract tier is versioned: ``CONTRACTS_VERSION`` moves whenever a
contract-tier paper's declared surface changes shape -- signatures, fields,
exports; never prose. The surface digest pinned beside the ledger guard
makes a silent shape change a red gate.
"""

CONTRACTS_VERSION = "13"
