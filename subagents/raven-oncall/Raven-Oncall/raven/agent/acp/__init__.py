"""The ACP wire codec, for this install's own ACP server.

Only ``protocol`` is vendored: the rest of mainline's ``agent.acp`` is the
*client* -- connection pool, elicitor, capability probes -- for calling other ACP
agents, and this install is the one being called.
"""
