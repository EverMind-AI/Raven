"""Assembly root: the stacks that compose a running agent out of parts.

Each ``*_stack`` module is one assembly concern (plugins, hooks, ...) shared by
every entrance (cli / gateway / tui / rpc). Entrances call these builders and
own only their transport; the stacks own discovery, admission, and wiring
order. Nothing in here renders or transports — that stays at the surfaces.
"""
