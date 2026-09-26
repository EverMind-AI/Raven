"""Resolve inspection through the same native pool and parent binding used by ACP dispatch."""


async def connection(backend, *, workspace, provider=None, model=None):
    binding = {}
    if model:
        binding["RAVEN_PARENT_MODEL"] = model
    if name := str(getattr(provider, "provider_name", "") or ""):
        binding["RAVEN_PARENT_PROVIDER"] = name
    if protocol := str(getattr(provider, "api_protocol", "") or ""):
        binding["RAVEN_PARENT_PROTOCOL"] = protocol
    return await backend.pool.acquire(
        name=backend.name,
        command=backend.command,
        cwd=backend.cwd or str(workspace),
        env=dict(backend.env),
        binding=binding or None,
        ready_timeout_s=max(1.0, backend.ready_timeout_ms / 1000),
    )
