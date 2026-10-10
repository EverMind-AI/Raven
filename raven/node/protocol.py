"""The wire between a Raven and the Raven node it starts on a registered machine.

JSON-RPC 2.0, one object per line on the node's stdin and stdout: the framing
ACP uses, so the host drives a node with the client it drives an ACP agent with
(:class:`raven.acp_client.client.AcpClient`). The node is started over ssh and
lives exactly as long as that ssh does; it opens no port.

Every ``fs/*`` call carries ``roots``, the directories it may touch. The host
decides them; the node checks them again, because only the machine can resolve
its own symlinks.
"""

from __future__ import annotations

#: Bumped when a method or a field changes meaning. A node and a host that
#: disagree refuse each other at ``node/hello`` instead of misreading a reply.
PROTOCOL = 1

HELLO = "node/hello"
READ = "fs/read"
LIST = "fs/list"
FIND = "fs/find"
GREP = "fs/grep"

#: Third-party packages a node cannot run without, beside Raven's own code: the
#: only ones its file tools import (measured 2026-10-10), pinned to the host's
#: versions.
REQUIREMENTS = ("loguru", "pillow")

#: Installed when the machine can get it, not required. ``ripgrep-bin`` puts the
#: same ``rg`` beside the node's Python that the host has beside its own, so a
#: search reads the same on both machines; without it ``grep`` runs Raven's own
#: Python search, as it does on a host with no ``rg`` (measured 2026-10-10: one
#: registered machine's package mirror did not carry it).
SEARCH_REQUIREMENT = "ripgrep-bin"

#: Where a node looks for a package its own index does not carry.
PUBLIC_INDEX = "https://pypi.org/simple"

__all__ = ["FIND", "GREP", "HELLO", "LIST", "PROTOCOL", "PUBLIC_INDEX", "READ", "REQUIREMENTS", "SEARCH_REQUIREMENT"]
