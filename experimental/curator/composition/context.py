"""Share identical source excerpts while preserving each Harness's actual source references."""


def merge_sources(sources, incoming, prefix):
    """Return the source names visible in this context; different excerpts never share an alias."""

    def identity(entry):
        return tuple(entry[key] for key in ("path", "digest", "start", "end"))

    held = {identity(entry): name for name, entry in sources.items()}
    aliases = {}
    for name, entry in incoming.items():
        key = identity(entry)
        if key not in held:
            alias = f"{prefix}.{name}"
            if alias in sources:
                raise ValueError(f"source alias collision: {alias}")
            sources[alias] = entry
            held[key] = alias
        aliases[name] = held[key]
    return aliases
