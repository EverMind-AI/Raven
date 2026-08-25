"""What the design pass is told, with the measured numbers formatted in.

The numbers are parameters, not prose. In the predecessor the body floor was
stated in six places -- two briefs, a tool ask, and three sections of a skill
document -- with the measurement that actually enforces it in a seventh. Six
copies of a number is six chances for five of them to be wrong, and the one that
matters is the one nobody reads. Here the gate's constants are formatted in, so
the brief cannot disagree with what will be measured.
"""

from __future__ import annotations

PAGE_BRIEF = """Make this slide a better designed page, by rewriting the block of
code that draws it. You get the page as it renders, the deck's shared setup as
read-only context, and that block. Judge it as a reader will see it, projected.

What the page says stays: no invented facts, no numbers, names or citations that
were not there, nothing dropped that the page needs to make its point -- and no
invented emphasis: the rows of an outline or agenda are peers, and highlighting
one claims a "you are here" this deck never states. How it is arranged, grouped,
weighted, sized and decorated is yours.

A reader should see the page's structure before reading a word of it: what is
dominant, what is subordinate to it, where one component ends and the next
begins. Two failures, and they are opposites. A page where five things carry the
same weight has argued nothing -- one thing leads, by size or weight or position,
and the rest answer to it. A page whose components run into each other cannot be
read at all: two blocks side by side need a visible boundary, and the way to give
one is a surface tint under a group, or a gap that is plainly wider than the gaps
inside each group, or a hairline. Equal gaps everywhere is exactly what "I cannot
tell the sections apart" looks like.

So a panel behind something is a choice, not the default: give a block its own
surface when it is genuinely a peer of the blocks beside it, or genuinely an
aside. Wrapping every block on a page in an identical panel reads as a dashboard
and flattens the hierarchy you were asked for -- differentiate the groups instead
of tinting all of them the same.

Two constructions that read as broken in a render, worth a moment before you
draw them: a picture laid flush against the edges of the panel behind it keeps
its own outline, which fights the panel's wherever the two disagree -- inset it
instead; and a divider laid across a table at a declared row boundary strikes
through the row's text once the renderer grows the rows -- tie rules to what
will not move.

One measured limit, and it is measured rather than refused -- what it costs is that
the page comes back to you with the number on it: nothing on a page goes under
{min_pt}pt, and the size most of the page runs at stays at or above {body_pt}pt. If
what you want on the page will not fit above that, the page has too much on it:
merge, split, or hand something to the page next door. Shrinking the type to make
room is the one move that is off the table.

Two things about the machinery, not about design. The shared setup is read-only --
call anything it defines, change none of it, because every page uses it and
another pass owns it; a defect that lives in there goes in your notes. And the
build refuses a filled colour bar that carries *nothing* -- a strip welded to a
card edge, a bar of colour across a page with no copy on it -- so drawing one
costs you the round. A plane with copy on it is not that: a tinted surface under
a group, wherever it sits on the page, is the grouping this brief just asked for.

Reply as JSON and nothing else:

{{"slide": <int>,
 "verdict": "ok" | "edited",
 "notes": ["what you changed and why, briefly"],
 "block": "<the complete replacement for this page's block>"}}

Each note names what on this page changed and what that fixed. "Improved the
design", "made it clearer" and "restyled the page" name nothing and are worth
less than no note at all, because the next round reads them.

The block must be complete, runnable python at the same indentation as the one you
were given. Use `"verdict": "ok"` and omit `block` if the page is already as good
as you can make it."""


DECK_BRIEF = """Make this deck read as one designed thing, by rewriting the shared
setup its pages are drawn from. You get every page at once as a contact sheet, and
that setup.

You are looking for what only shows up across pages: the same role set at
different sizes on different pages, type too small for a room, a page header that
carries no identity, cards and rules that differ page to page, a scale with no
step in it. And the opposite of that last one, which is just as visible from
here and easier to miss: one construction used for everything, so that every
page is the same grid of panels and no page argues anything. A deck of card
grids reads as a dashboard, not a talk.

The setup is where a deck's boundaries come from, so define them there rather
than leaving each page to invent one: one gap between groups and a smaller one
inside a group, one surface tint for a grouped region, one rule weight. Pages
that each pick their own spacing are pages a reader cannot see the structure of,
and that is the defect this pass is best placed to fix.

Change the setup, not the pages -- their code is not yours to edit here, and a
page pass comes after you to apply what you decide. So say what you decided:
whatever a page has to do to match the deck you are setting up goes in
`vocabulary`, in short imperative lines, and each page gets handed those.

The palette and the font family stay exactly as they are. They may be the user's
choice rather than the deck's, and this pass is not where that gets revisited.
Every name the setup defines has to keep existing, whatever you change about what
it produces -- the pages call them, and all of them fail at once if one goes.

The scale you set is measured after the build, so it binds the pages: nothing
anywhere on a page may sit under {min_pt}pt, and the size a page mostly runs at has
to stay at or above {body_pt}pt. A step you define below that is a step every page
fails on.

Reply as JSON and nothing else:

{{"verdict": "ok" | "edited",
 "notes": ["what you changed and why, briefly"],
 "vocabulary": ["what every page should now do, one line each"],
 "prelude": "<the complete replacement for the shared setup>"}}

Use `"verdict": "ok"` with no `prelude` if the setup is already right for the deck
you can see."""


INTAKE_BRIEF = """Read a deck task and say what it takes to do it. You are the
first thing that sees it, and nothing after you gets to ask what the user meant.

You get the task in the user's own words, a listing of the workspace, and whatever
is already on disk for this deck.

Separate what the task *says* from what you would *guess*. Anything it says goes
in `stated` and binds the deck: the page budget is checked against the built file
and the language against what the pages say. Anything it does not say is a
question, never a guess -- a budget nobody agreed to is worse than no budget,
because the deck then fails a check the user never set. "15 分钟" is not a page
count. "给投资人" is an audience. A task written in Chinese is not a task asking
for a Chinese deck, though it usually is; if that is your only evidence, ask.

`errands` is what to go and get. Be specific enough to act on: name the search
terms or the URL, not the topic. Two things worth getting right about it.

An empty list is a real answer. The figures in this deck are extracted from the
sources and never generated, so imagery pulled off the web is the one surface on
which a figure can be invented -- if the sources already carry what the deck has
to show, ask for nothing. Conversely, a task with no sources at all and no images
cannot be built from nothing, and saying so is the most useful thing you can do.

And an errand is for material, not for work: "write the results section" is not an
errand, "the paper's Table 2, which the task refers to and the materials do not
contain" is.

`task_is_material` is for the request that *is* the source: notes, an outline with
its numbers in it, a paragraph of findings. Set it and the request text is written
into the deck as a document and read like any other, which is what makes the
numbers in it checkable. Leave it false when the request only says what to make --
"做一份关于 LLM 的介绍" carries no material, and recording it as one would put the
instruction in the deck's evidence.

`questions` is only for what the user alone can answer: a decision, a preference,
a missing input they hold. Not what you could read out of the materials, and not
what you could search for. Keep them few and answerable -- offer options where the
answer is a choice.

Do not ask about the language, the audience or the page count. Those three are
added for you whenever `stated` leaves them null, in one canonical wording, so a
question of your own about any of them reaches the user as a second version of a
question they are already being asked.

Reply as JSON and nothing else:

{"topic": "<one line: what this deck is about>",
 "stated": {"language": <string or null>, "audience": <string or null>,
            "pages_low": <int or null>, "pages_high": <int or null>},
 "materials_dir": "<path the task named, relative to the workspace, or \"\">",
 "task_is_material": <true when the request itself carries the substance of the deck>,
 "template": "<path to a .pptx the task said to build inside, or \"\">",
 "questions": [{"question": "...", "why": "...", "options": ["...", "..."]}],
 "errands": [{"what": "...", "why": "...", "how": "..."}],
 "notes": ["an instruction from the task that no field above carries"]}

`notes` carries instructions, not observations. "开头要有一页讲动机" and "不要用
蓝色" belong there, because they bind nobody else and they are handed to whoever
writes the deck. "The task did not specify the language" does not: that is what
the null in `stated` already says, and a note repeating it is read by every later
step as something the user asked for. Three at most, in the user's own words.

One bound on `errands`, for the same reason. They are for material this deck's
subject needs -- the paper it is about, the figure it refers to, a picture of the
thing it describes. Not for building a case around the subject: a market-size
report nobody asked for is scope you invented, and its numbers reach the deck as
facts with a source the user never chose."""


HOUSE_STYLE = """

One more thing about this deck: it is built inside a template the user supplied.
Its palette, its type and the furniture its pages carry are their house style
rather than this deck's, and they are no more yours to revisit than the palette
is on a deck without one -- a page that comes back in your colours instead of
theirs has lost the thing they asked for. What is yours here is what it always
is: how a page is arranged, grouped, weighted and sized within that style."""

# The measured half of that, when there is one. The generic paragraph above kept a
# pass from repainting a deck; it does not stop one from nudging the page title half
# an inch off the row the template puts it on, on one page out of twelve. These are
# the same numbers the page's author was given, so the two cannot drift apart.
HOUSE_NUMBERS = """

Measured off that template, and binding on every page you touch:
{lines}
The pages that are the template's own -- its cover, contents, divider and closing --
are cloned rather than drawn, so their arrangement is not yours either."""


def intake_brief() -> str:
    """The intake pass's brief. A function for symmetry with the two below, which
    format measured constants in; this one has none to format."""
    return INTAKE_BRIEF


def page_brief(*, body_pt: float, min_pt: float, house_style: bool = False, house: str = "") -> str:
    brief = PAGE_BRIEF.format(body_pt=_trim(body_pt), min_pt=_trim(min_pt))
    return brief + _house(house_style, house)


def deck_brief(*, body_pt: float, min_pt: float, house_style: bool = False, house: str = "") -> str:
    brief = DECK_BRIEF.format(body_pt=_trim(body_pt), min_pt=_trim(min_pt))
    return brief + _house(house_style, house)


def _house(house_style: bool, house: str) -> str:
    """The template addendum: the paragraph always, the numbers when they were measured."""
    if not house_style:
        return ""
    return HOUSE_STYLE + (HOUSE_NUMBERS.format(lines=house.rstrip()) if house.strip() else "")


def _trim(value: float) -> str:
    """`14` rather than `14.0`, `10.8` unchanged -- the brief is read by a reader."""
    return f"{value:g}"
