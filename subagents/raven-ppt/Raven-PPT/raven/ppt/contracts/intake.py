"""What the user asked for, read out of the one line they said it in.

A deck task usually arrives as a sentence. Sometimes it carries everything --
"给投资人做一份 15 页中文路演,材料在 ./papers" states the language, the audience,
the length and where the sources are -- and sometimes it carries almost nothing.
Either way something has to read it, decide what is already answered, and go and
get the rest. That reading is a judgement about content against intent, so it is
a model's to make; this is where its answer is written down.

The split that matters is between *stated* and *inferred*. What the query actually
says is recorded and binds the deck -- the page budget is checked against the
built file, the language against what the pages say. What the query does not say
is a question for the user, never a guess: a budget nobody agreed to is worse
than no budget, because the deck then fails a check the user never set.

What comes out is a work list, not a verdict. "Are these materials enough?" looked
like the question until it was written down: on a first pass the answer is always
"go and get more", and a judgement with one possible answer is a step in a
process rather than a decision -- it costs a model call to say what was already
known. So this reads the task and says *what to get*, which is a judgement with
many answers, including an empty one when the sources already carry the deck. That
empty answer matters: figures here are extracted and never generated, so a deck
whose imagery came off the web has a surface on which a figure can be invented,
and filling that surface unasked would be the wrong default.

The plan is fingerprinted against the inputs it was made from, so it goes stale
when a document arrives and does not go stale because the deck was rebuilt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from raven.ppt.contracts.project import Project

SCHEMA = "raven.ppt.intake.v1"
INTAKE_FILE = "intake.json"


def intake_path(project: Project) -> Path:
    return project.state_dir / INTAKE_FILE


@dataclass(frozen=True)
class StatedBrief:
    """The brief fields the query states. `None` means the query did not say.

    Three-valued on purpose. "The query did not mention the language" and "the
    query asked for English" are different facts, and collapsing them into a
    default is how a deck ends up checked against a decision nobody made.
    """

    language: str | None = None
    audience: str | None = None
    pages_low: int | None = None
    pages_high: int | None = None
    forbidden: tuple[str, ...] = field(default_factory=tuple)
    """What the request ruled out: "不要用 icon", "别放对比表", "不许出现竞品名".

    A list rather than three-valued, because here the two states collapse honestly:
    "the request forbade nothing" and "the request said nothing about it" are the
    same fact, where a budget nobody stated and a budget of zero are not. It stays
    out of `missing` for the same reason -- there is no question to put to a user
    about what they did not forbid.
    """

    @classmethod
    def of(cls, mapping: object) -> StatedBrief:
        """Read the three fields out of a mapping, keeping "not said" distinct.

        One place, because both the model's reply and the recorded file are parsed
        into this and two spellings of "was this said?" would eventually disagree.
        """
        raw = mapping if isinstance(mapping, dict) else {}
        return cls(
            language=_maybe_str(raw.get("language")),
            audience=_maybe_str(raw.get("audience")),
            pages_low=_maybe_int(raw.get("pages_low")),
            pages_high=_maybe_int(raw.get("pages_high")),
            forbidden=_strings(raw.get("forbidden")),
        )

    @property
    def missing(self) -> tuple[str, ...]:
        absent = []
        if not (self.language or "").strip():
            absent.append("language")
        if not (self.audience or "").strip():
            absent.append("audience")
        if not self.pages_low or not self.pages_high:
            absent.append("pages")
        return tuple(absent)

    def as_dict(self) -> dict[str, object]:
        return {
            "language": self.language,
            "audience": self.audience,
            "pages_low": self.pages_low,
            "pages_high": self.pages_high,
            "forbidden": list(self.forbidden),
        }


@dataclass(frozen=True)
class Question:
    """Something only the user can answer."""

    question: str
    why: str = ""
    options: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {"question": self.question, "why": self.why, "options": list(self.options)}


@dataclass(frozen=True)
class Errand:
    """Something to go and get before the deck can be written.

    `how` names the tool, because an errand a reader cannot act on is a
    complaint: "we have no image of the architecture" is worth less than the same
    sentence ending in `web_search(kind="images")`.
    """

    what: str
    why: str = ""
    how: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"what": self.what, "why": self.why, "how": self.how}


@dataclass(frozen=True)
class IntakePlan:
    """One reading of the task, and the work it leaves."""

    topic: str
    stated: StatedBrief = field(default_factory=StatedBrief)
    materials_dir: str = ""
    """Where this deck's sources are, as read out of the request and then corrected
    to the directory that was actually ingested. Read by `ppt_fetch`, so material
    fetched later joins the material already there instead of starting a second
    pile only one of which the ingest can hold.
    """
    request: str = ""
    """The request this plan was read from, verbatim.

    Kept so a later call can tell a repeat from a revision. Recorded whatever the
    request turns out to be, unlike the copy `task_is_material` writes into the
    sources: that one is material the ingest reads, and making every request
    material would authorise an instruction's own numbers as facts.
    """
    task_is_material: bool = False
    """Whether the request itself carries the substance of the deck.

    A user who pastes their notes into the request has supplied a source, and
    before this it went nowhere: `materials_dir` was empty, nothing was ingested,
    and every number they had just given us came back unanchored. Set, the request
    text is written into the project and read like any other document.
    """
    template: str = ""
    """A `.pptx` the task pointed at, to build the deck inside."""
    questions: tuple[Question, ...] = field(default_factory=tuple)
    errands: tuple[Errand, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
    digest: str = ""
    """The state fingerprint this plan was made against; see `services.state`."""

    @property
    def outstanding(self) -> bool:
        return bool(self.questions or self.errands)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "topic": self.topic,
            "stated": self.stated.as_dict(),
            "materials_dir": self.materials_dir,
            "request": self.request,
            "task_is_material": self.task_is_material,
            "template": self.template,
            "questions": [question.as_dict() for question in self.questions],
            "errands": [errand.as_dict() for errand in self.errands],
            "notes": list(self.notes),
            "digest": self.digest,
        }


def write_plan(plan: IntakePlan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8")


def load_plan(path: Path) -> IntakePlan | None:
    """The recorded plan, or None when there is none or it cannot be read."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return IntakePlan(
            topic=str(raw.get("topic", "")),
            stated=StatedBrief.of(raw.get("stated")),
            materials_dir=str(raw.get("materials_dir") or ""),
            request=str(raw.get("request") or ""),
            task_is_material=bool(raw.get("task_is_material")),
            template=str(raw.get("template") or ""),
            questions=tuple(_question(entry) for entry in raw.get("questions") or () if isinstance(entry, dict)),
            errands=tuple(_errand(entry) for entry in raw.get("errands") or () if isinstance(entry, dict)),
            notes=tuple(str(note) for note in raw.get("notes") or ()),
            digest=str(raw.get("digest") or ""),
        )
    except (TypeError, ValueError):
        return None


def _question(entry: dict) -> Question:
    return Question(
        question=str(entry.get("question", "")),
        why=str(entry.get("why") or ""),
        options=tuple(str(option) for option in entry.get("options") or ()),
    )


def _errand(entry: dict) -> Errand:
    return Errand(
        what=str(entry.get("what", "")),
        why=str(entry.get("why") or ""),
        how=str(entry.get("how") or ""),
    )


def _strings(value: object) -> tuple[str, ...]:
    """A list of non-empty lines, from whatever a reply or a file holds there.

    Blanks are dropped here rather than downstream: an empty prohibition becomes an
    empty bullet in the design brief, which reads as a rule nobody wrote down. One
    string is read as one item rather than discarded, because the field it parses is
    a prohibition -- a reply that answered "no icons" instead of ["no icons"] means
    the user forbade icons, and dropping it is the one wrong reading available.

    Anything in the list that is not a string is dropped for the same reason the
    blanks are. `str()` on it does not recover the intent: a reply holding
    `[{"what": "no icons"}]` came through as the literal text `{'what': 'no icons'}`
    and went into the design brief as a rule written in Python.
    """
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        return ()
    return tuple(text for text in (item.strip() for item in items if isinstance(item, str)) if text)


def _maybe_str(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _maybe_int(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
