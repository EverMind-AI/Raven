"""My-Agent's engine: the agent's harness as an installable wheel.

The manifest (``raven-plugin.toml``) lives inside this package because the
host reads it with ``importlib.resources`` -- a manifest beside the package
would install fine and never be found.
"""
