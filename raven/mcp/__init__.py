"""Everything raven knows about the Model Context Protocol.

Split out of ``raven.agent.tools`` because MCP knowledge had accumulated in
four files there and leaked into consumers that reached for it -- ownership was
guessed from tool-name prefixes in one place, recomputed from a spelling in
another, and recorded twice. One package makes the boundary checkable: a module
outside it should need at most the naming rules and the manager's public
surface, never a name-parsing trick of its own.

Naming hazard worth stating once: the MCP SDK is the top-level package ``mcp``.
Inside this package ``import mcp`` still resolves to the SDK -- Python 3 has no
implicit relative imports -- but a reader can misread it, so SDK imports here
are written at the point of use with the class named (``from mcp import
ClientSession``) rather than as a bare module import.

Layout:

- :mod:`raven.mcp.naming`    -- ``(server, tool)`` to the name the model sees
- :mod:`raven.mcp.client`    -- one connection's transport, handshake and tool wrappers
- :mod:`raven.mcp.manager`   -- the per-server lifecycle for a running raven
- :mod:`raven.mcp.resources` -- the three global tools for reaching MCP resources
- :mod:`raven.mcp.prompts`   -- the two global tools for reaching MCP prompts
- :mod:`raven.mcp.oauth`     -- the browser flow and token storage the SDK delegates
- :mod:`raven.mcp.report`    -- what one reconcile did, for callers that must act on it

The resource and prompt tools are addressed by server *name*, not through a
wrapper per server, so they read the manager's ``session_of`` /
``servers_offering`` rather than holding a session of their own. Those two are
the package's second public seam, after the naming rules.
"""
