"""Read a scenario directory: the agency profile, its material files, traveller personas and the agency's checks."""

import json
import random
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .cards import Drawn, draw, split

SKILL_FILE = "SKILL.md"
BUNDLED = Path(__file__).resolve().parent / "scenarios"


class Criterion(BaseModel):
    """One check the agency applies to a round's conversations; a red line is one the owner never tolerates missing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    check: str = Field(min_length=1)
    severity: Literal["red_line", "standard"] = "standard"


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial: list[str] = Field(default_factory=list)
    criteria: list[Criterion] = Field(min_length=1)
    plans: dict[str, list[list[str]]] = Field(default_factory=dict)


@dataclass(frozen=True)
class Persona:
    """A drill card: `text` names its values in braces and `values` says how each is drawn (see `cards`)."""

    name: str
    text: str
    values: dict = field(default_factory=dict, compare=False)

    def play(self, rng: random.Random) -> Drawn:
        return draw(self.name, self.values, self.text, rng)


@dataclass(frozen=True)
class Scenario:
    """Materials are skill directories the agency owns; releasing one copies it into the employee's skill pool.

    `initial` is what the employee has before the first round under the staged plan; the agency hands over the rest.
    `plans` are named partitions of the materials into steps: step 0 at onboarding, step k with the review of round k.
    `onboarding` and `handover` are the owner's words around uploaded files, each listing them at `{files}`."""

    root: Path
    profile: str
    materials: dict[str, Path]
    personas: tuple[Persona, ...]
    criteria: tuple[Criterion, ...]
    initial: tuple[str, ...]
    onboarding: str = ""
    handover: str = ""
    plans: dict[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> "Scenario":
        root = Path(root).resolve()
        if not root.is_dir():
            root = BUNDLED / root.name
        profile = (root / "profile.md").read_text().strip()
        if not profile:
            raise ValueError(f"scenario profile is empty: {root}")
        materials = {path.parent.name: path.parent for path in sorted((root / "materials").glob(f"*/{SKILL_FILE}"))}
        personas = tuple(
            Persona(path.stem, body, values)
            for path in sorted((root / "personas").glob("*.md"))
            for values, body in [split(path.read_text())]
        )
        if not personas:
            raise ValueError(f"scenario has no personas: {root}")
        spec = _Spec.model_validate(json.loads((root / "scenario.json").read_text()))
        ids = [criterion.id for criterion in spec.criteria]
        if len(set(ids)) != len(ids):
            raise ValueError(f"criterion ids repeat: {sorted({i for i in ids if ids.count(i) > 1})}")
        unknown = set(spec.initial) - materials.keys()
        if unknown:
            raise ValueError(f"the initial release names materials the scenario does not have: {sorted(unknown)}")
        onboarding, handover = (
            (root / f"{name}.md").read_text().strip() if (root / f"{name}.md").is_file() else ""
            for name in ("onboarding", "handover")
        )
        plans = {}
        for name, steps in spec.plans.items():
            named = [material for step in steps for material in step]
            if not steps or not steps[0] or sorted(named) != sorted(set(named)) or set(named) != materials.keys():
                raise ValueError(f"plan {name} must give every material exactly once, starting at onboarding")
            plans[name] = tuple(tuple(step) for step in steps)
        return cls(
            root,
            profile,
            materials,
            personas,
            tuple(spec.criteria),
            tuple(spec.initial),
            onboarding,
            handover,
            plans,
        )

    def without(self, names) -> "Scenario":
        """The scenario as if the agency never had these materials: gone from its materials, opening set and plans."""
        names = set(names)
        unknown = names - self.materials.keys()
        if unknown:
            raise ValueError(f"the scenario has no materials named {sorted(unknown)}")
        return replace(
            self,
            materials={name: path for name, path in self.materials.items() if name not in names},
            initial=tuple(name for name in self.initial if name not in names),
            plans={
                plan: tuple(tuple(name for name in step if name not in names) for step in steps)
                for plan, steps in self.plans.items()
            },
        )

    def text(self, material: str) -> str:
        return (self.materials[material] / SKILL_FILE).read_text()

    def release(self, names, skills: Path) -> None:
        for name in names:
            shutil.copytree(self.materials[name], Path(skills) / name, dirs_exist_ok=True)

    def withdraw(self, skills: Path) -> None:
        """Remove every scenario-owned skill from the pool, so a run starts from the plan it declares."""
        for name in self.materials:
            shutil.rmtree(Path(skills) / name, ignore_errors=True)

    def document(self, material: str) -> str:
        """A material as the owner's own document: its text without the skill frontmatter."""
        text = self.text(material)
        if text.startswith("---"):
            text = text.split("---", 2)[2]
        return text.strip() + "\n"

    def upload(self, names, uploads: Path, *, shared: Path | None = None) -> list[str]:
        """Put materials in an uploads folder the way an owner uploads files, one folder per material.

        The folder holds the document as `<name>.md` and every file that ships beside it under its own name, so a
        document that says a template is "in this folder" stays true. With `shared` (the `uploads` folder of the
        working directory), the same folders are also put there and the returned paths are relative to the working
        directory: every drill works in its own copy of it, where those relative paths resolve, while an absolute
        path would name one drill's copy only. Otherwise they are relative to the uploads folder's parent.
        """
        places = [Path(uploads), *([Path(shared)] if shared is not None else [])]
        paths = []
        for place in places:
            written = []
            for name in names:
                folder = place / name
                shutil.rmtree(folder, ignore_errors=True)
                folder.mkdir(parents=True)
                (folder / f"{name}.md").write_text(self.document(name))
                written.append(folder / f"{name}.md")
                for extra in sorted(self.materials[name].iterdir()):
                    if extra.is_file() and extra.name != SKILL_FILE:
                        shutil.copyfile(extra, folder / extra.name)
                        written.append(folder / extra.name)
            paths = written
        base = Path(shared if shared is not None else uploads).parent
        return [path.relative_to(base).as_posix() for path in paths]

    def withdraw_uploads(self, *places: Path) -> None:
        for place in places:
            for name in self.materials:
                shutil.rmtree(Path(place) / name, ignore_errors=True)
