"""Locate an instance's latest stored state across its agent's former names."""

import stat
from pathlib import Path

from raven.config.agent_names import canonical_agent_name, other_names_of_agent
from raven.utils.paths import safe_path_segment

MESSAGES_FILENAME = "messages.json"


def stored_instance_path(root: Path, agent: str, filename: str, *, state_file: str | None = None) -> Path:
    """Use the latest write; current and recent names break equal timestamp ties.

    Saved messages outrank a chat directory that has no state yet. All aliases
    consider the same candidates so they cannot resume different conversations.
    """
    name = canonical_agent_name(agent)
    names = (name, *other_names_of_agent(name))
    chosen = root / safe_path_segment(name) / filename
    newest: tuple[bool, int] | None = None
    for candidate in names:
        path = root / safe_path_segment(candidate) / filename
        try:
            stored = path.stat()
        except OSError:
            continue
        if state_file is None and not stat.S_ISREG(stored.st_mode):
            continue
        stamp = stored.st_mtime_ns
        has_state = False
        if state_file is not None:
            try:
                saved = (path / state_file).stat()
            except OSError:
                pass
            else:
                if stat.S_ISREG(saved.st_mode):
                    stamp = saved.st_mtime_ns
                    has_state = True
        score = (has_state, stamp)
        if newest is None or score > newest:
            chosen, newest = path, score
    return chosen
