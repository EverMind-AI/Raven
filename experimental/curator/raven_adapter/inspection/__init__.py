"""One inspection boundary for effective facts, source reads and host declarations."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ...harness import Declaration
from ...harness.view import Mechanism, validate
from ..baselines import Baseline
from .runtime import declaration_for, fingerprint, redact, unavailable_targets
from .sources import file_source, runtime_sources


@dataclass(frozen=True)
class Inspection:
    declaration: Declaration
    facts: dict
    sources: dict[str, dict]

    @property
    def mechanisms(self):
        return tuple(Mechanism.model_validate(item) for item in self.facts.get("mechanisms", ()))

    def read_source(self, name: str, offset: int = 0, length: int = 12000, find: str | None = None) -> dict:
        if name not in self.sources:
            raise ValueError(f"unknown source: {name}")
        if offset < 0 or length < 1 or length > 24000:
            raise ValueError("source reads require offset >= 0 and 1 <= length <= 24000")
        source = self.sources[name]
        path = Path(source["path"])
        content = path.read_bytes()
        if sha256(content).hexdigest() != source["digest"]:
            raise ValueError(f"source changed; inspect the current baseline again: {name}")
        text = "\n".join(content.decode().splitlines()[source["start"] - 1 : source["end"]])
        if find is not None:
            if not find or len(find) > 160:
                raise ValueError("source search requires 1 to 160 literal characters")
            matches = []
            position = text.find(find)
            while position >= 0 and len(matches) < 30:
                matches.append(
                    {
                        "offset": max(0, position - 120),
                        "line": source["start"] + text[:position].count("\n"),
                        "text": text[max(0, position - 120) : position + len(find) + 160],
                    }
                )
                position = text.find(find, position + len(find))
            return {"source": name, "matches": matches, "truncated": position >= 0}
        if offset > len(text):
            raise ValueError("source offset exceeds the registered region")
        end = min(len(text), offset + length)
        return {
            "source": name,
            "path": str(path),
            "text": text[offset:end],
            "first_line": source["start"] + text[:offset].count("\n"),
            "next_offset": end if end < len(text) else None,
        }

    @classmethod
    def restore(cls, data: dict, *, names=None, fields=None, phases=None) -> "Inspection":
        declaration = declaration_for(data["identity"], data["unavailable"], **data.get("grants", {}))
        declaration = declaration.restrict(
            names if names is not None else (target.name for target in declaration.targets),
            fields=fields,
            phases=phases,
        )
        facts = dict(data["facts"])
        facts["mechanisms"] = [
            item.model_copy(
                update={
                    "targets": tuple(name for name in item.targets if name in {t.name for t in declaration.targets})
                }
            ).model_dump(mode="json")
            for item in (Mechanism.model_validate(row) for row in facts.get("mechanisms", ()))
        ]
        inspection = cls(declaration, facts, data["sources"])
        validate(inspection.mechanisms, facts, inspection.sources, declaration)
        return inspection


__all__ = [
    "Baseline",
    "Inspection",
    "declaration_for",
    "fingerprint",
    "redact",
    "unavailable_targets",
    "file_source",
    "runtime_sources",
]
