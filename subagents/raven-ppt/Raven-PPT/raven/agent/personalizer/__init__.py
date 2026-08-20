"""Personalizer — PAHF 4-step personalization flow.

Implementation lives in ``personalizer.py``.

External callers should keep using:

    from raven.agent.personalizer import Personalizer
"""

from raven.agent.personalizer.personalizer import Personalizer

__all__ = ["Personalizer"]
