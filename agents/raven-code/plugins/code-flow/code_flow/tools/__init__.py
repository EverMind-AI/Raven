"""Raven-Code's own tool face, contributed by the code-flow plugin.

The product ships these instead of taking the host's: the evaluation fork
aligned its file tools with the conventions models are trained on
(``file_path`` / ``old_string``, a ``glob`` for pathname patterns, a
``todo`` checklist) and taught them the conduct their descriptions
carry. Trunk's built-ins kept the older spelling, and nothing here changes
them -- the four other products on this engine keep serving the host's own.

Where the two names collide, the contribution wins by riding the same name:
plugin tools register last, and a same-name registration replaces the
built-in instance, so the model is only ever shown this one. Where the names
differ (``glob`` against trunk's ``find``) the launcher withholds trunk's
name while this face is being served.
"""

#: Host tool names this face supersedes under a DIFFERENT name, mapped to the
#: name that replaces them. A shared name needs no entry: registering over it
#: IS the replacement. A different name does, because both would otherwise be
#: served at once -- two tools for one job, and a model choosing between them.
#:
#: The launcher reads this table and withholds the keys while the face is
#: served, so adding a tool is one line here rather than an edit in run.py,
#: and turning the face off hands the host's own names straight back.
SUPERSEDED_HOST_TOOLS: dict[str, str] = {"find": "glob"}

__all__ = ["SUPERSEDED_HOST_TOOLS"]
