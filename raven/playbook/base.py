"""The one model base every playbook contract shares.

Its own module because the contracts form a chain -- a role row is a delegate
row with the round's fields on it -- and a base living in any one of them would
make that chain a circle. Nothing else belongs here: this is a base class, not
a place to put things that have no home.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

__all__ = ["CamelBase"]


class CamelBase(BaseModel):
    """Wire shape is camelCase; python stays snake_case.

    ``extra="forbid"`` is load-bearing rather than tidy: a playbook file is a
    distribution unit, so a field nobody reads is a field whose author believed
    something that is not true, and saying so at load is the only moment that
    belief is cheap to correct.
    """

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)
