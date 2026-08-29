"""The owner's machine registry: which machines exist, and how one is written down.

Only the registry lives here. The campaign machinery (declare, submit, ledger,
wakes) is the on-call agent's own domain and stays in its vendored tree; what
the host needs is to name the machines and to add one while the owner is in the
conversation.
"""
