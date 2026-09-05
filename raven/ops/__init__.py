"""The owner's machine registry, lifted from the on-call agent's checkout.

Only the registry lives here. The campaign machinery (declare, submit, ledger,
wakes) is the on-call agent's own domain and stays in its vendored tree; what
the host needs is to know which machines exist and to write one down while the
owner is in the conversation -- reading a registry through a subprocess into
another install's binary was the crutch this lift removes.
"""
