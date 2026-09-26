"""Drill cards with drawn values: each time a card is played, its trip facts are drawn afresh within the card's ranges.

A card is a persona file whose frontmatter declares `values`. The trip keys carry meaning the agency's checks rely on:
`address` (how the traveller is addressed), `origin`, `destination`, `start` (a `MM-DD` date), `nights`, `adults`
(seniors included), `children` and `seniors` (one age per person), `budget` (the party total) and `phone` (a list of
prefixes; the remaining eight digits are drawn). Any other key is a free value for the text. A value is fixed, a list
to choose from, or a `{from, to, step}` range (dates as `MM-DD`); `children` and `seniors` list one such value per
person. Strings may name earlier values in braces, such as `address: Mr {surname}`.

The text names values in braces; a date `start` also gives `start_month` and `start_day`, the trip gives `end`,
`end_month` and `end_day`, and the i-th child or senior is `child_i` or `senior_i`. A card without frontmatter plays
as written and has no trip facts.
"""

import random
from dataclasses import dataclass
from datetime import date, timedelta

import yaml

TRIP = ("address", "origin", "destination", "start", "nights", "adults", "children", "seniors", "budget", "phone")
PEOPLE = ("children", "seniors")
YEAR = 2026


@dataclass(frozen=True)
class Trip:
    """The facts a drill card holds this time: what the traveller will say when asked, and what a quote must match."""

    address: str
    origin: str
    destination: str
    start: tuple[int, int]
    end: tuple[int, int]
    nights: int
    adults: int
    children: tuple[int, ...]
    seniors: tuple[int, ...]
    budget: int
    phone: str

    @property
    def persons(self) -> int:
        return self.adults + len(self.children)

    def facts(self) -> dict:
        return {
            "address": self.address,
            "origin": self.origin,
            "destination": self.destination,
            "start": f"{self.start[0]:02d}-{self.start[1]:02d}",
            "end": f"{self.end[0]:02d}-{self.end[1]:02d}",
            "nights": self.nights,
            "adults": self.adults,
            "children": list(self.children),
            "seniors": list(self.seniors),
            "budget": self.budget,
            "phone": self.phone,
        }


@dataclass(frozen=True)
class Drawn:
    """One playing of a card: the persona text the traveller reads, and its trip facts (None for a fixed card)."""

    name: str
    text: str
    trip: Trip | None


def split(text: str) -> tuple[dict, str]:
    """A persona file's frontmatter and body; a file without frontmatter has no values."""
    if not text.startswith("---"):
        return {}, text.strip()
    _, head, body = text.split("---", 2)
    values = (yaml.safe_load(head) or {}).get("values") or {}
    if not isinstance(values, dict):
        raise ValueError("a drill card's values must be a mapping")
    return values, body.strip()


def _day(value) -> date:
    month, day = (int(part) for part in str(value).split("-"))
    return date(YEAR, month, day)


def _pick(spec, rng: random.Random, known: dict):
    if isinstance(spec, list):
        return _pick(rng.choice(spec), rng, known)
    if isinstance(spec, dict):
        low, high = spec["from"], spec["to"]
        if isinstance(low, str):
            first, last = _day(low), _day(high)
            return first + timedelta(days=rng.randint(0, (last - first).days))
        return rng.randrange(int(low), int(high) + 1, int(spec.get("step", 1)))
    if isinstance(spec, str):
        return spec.format_map(known)
    return spec


def draw(name: str, values: dict, body: str, rng: random.Random) -> Drawn:
    """Draw every value in declaration order, then fill the text; the trip is derived when the card declares one."""
    if not values:
        return Drawn(name, body, None)
    known: dict = {}
    for key, spec in values.items():
        if key in PEOPLE:
            known[key] = tuple(int(_pick(item, rng, known)) for item in spec or ())
            for number, age in enumerate(known[key], 1):
                known[f"{key[:-3] if key == 'children' else key[:-1]}_{number}"] = age
        elif key == "phone":
            digits = f"{rng.randrange(10**8):08d}"
            known[key] = f"{_pick(spec, rng, known)} {digits[:4]} {digits[4:]}"
        elif key == "start":
            known[key] = _day(spec) if isinstance(spec, str) else _pick(spec, rng, known)
        else:
            known[key] = _pick(spec, rng, known)
    trip = None
    if "start" in known:
        start, nights = known["start"], int(known["nights"])
        end = start + timedelta(days=nights)
        known.update(
            start=f"{start.month:02d}-{start.day:02d}",
            start_month=start.month,
            start_day=start.day,
            end=f"{end.month:02d}-{end.day:02d}",
            end_month=end.month,
            end_day=end.day,
        )
        missing = [key for key in TRIP if key not in known]
        if missing:
            raise ValueError(f"drill card {name} declares a trip without {missing}")
        trip = Trip(
            address=str(known["address"]),
            origin=str(known["origin"]),
            destination=str(known["destination"]),
            start=(start.month, start.day),
            end=(end.month, end.day),
            nights=nights,
            adults=int(known["adults"]),
            children=known["children"],
            seniors=known["seniors"],
            budget=int(known["budget"]),
            phone=str(known["phone"]),
        )
    return Drawn(name, body.format_map(known), trip)
