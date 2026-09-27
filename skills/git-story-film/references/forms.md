# Story forms: how the growth is staged

The subject never changes: a product growing up, told from its git history.
The form is how that growth is shown on screen. A mascot hatching and walking
along the timeline is one form. Pick the one that suits this product, or
invent one, and propose it in the outline with a sentence on why it fits.

## Contents
1. Choosing a form
2. Journey (has a reference implementation)
3. Growth
4. Building
5. Levels
6. Companion
7. Mixing and inventing forms

## 1. Choosing a form

| If the product... | Try |
|---|---|
| has a character, a mascot, or a clear "from nothing to here" arc | Journey |
| accumulates things: capabilities, integrations, content, users | Growth |
| is a system of parts: engine, plugins, agents, modules | Building |
| is for developers or players, or its history is a run of hard problems | Levels |
| lives in someone's day: an assistant, a consumer app, a daily tool | Companion |

Whatever the form, keep the mapping literal and consistent. One visual unit
per commit (or per day), one milestone per release, one setback per revert,
and real dates and counts. That mapping makes the film about *this*
repository and not a generic promo. Write it down in the outline as a small
table: git fact → what the viewer sees.

Every form needs the same things from the story: a protagonist who wants
something, setbacks (reverts, rewrites, the busiest day), and an ending that
lands on what the product does for the viewer now (`story.md` §2).

## 2. Journey

**Premise.** A traveller moves along a line that *is* the timeline; every date
is a stop on the way.

**Mapping.** x is time. Commits hang along the path (beads, one per commit,
real daily counts). Releases and big commits are seams where the material
changes. A revert is a fall.

**Camera grammar.** One continuous shot, and the traveller only moves right.
The camera follows a moving average of the traveller's x, so it can never pan
back, and there are no cuts. A fall climbs back in *further right*. This rule
is what turns the timeline into a physical journey and stops the film drifting
into a slideshow. Keep it for the whole film.

**Movement grows with the project.** Small hops, bigger hops, first take-off,
flight, leading a flock, the flock flying out of frame.

**Openings.** Hatching is one. Others: a sketch getting inked, waking up,
stepping out of a door, a paper boat set on a stream.

**Reference.** `examples/raven-story/` (128 s, fully built): route and hops
in `raven-route.js`, flight and the later acts in `raven-flight.js`, the page,
seams and writer in `raven-world.js`.

## 3. Growth

**Premise.** Something grows in place: a seed into a tree (a commit is a leaf,
a release a new branch, the busiest day a bloom), a village into a city (a
feature is a building), a blank page into a finished drawing, a garden through
the seasons.

**Mapping.** Size and density follow cumulative commits; the daily count sets
how much grows that day. A revert is a pruned branch or a storm; a rewrite is a
season change.

**Camera grammar.** Mostly locked off, with a slow pull-back as it grows. Push
in for a feature close-up, then back out. Time reads from light, weather, a
calendar or the season, not from the camera moving.

**Story engine.** Growth alone is not a story. Give it someone who tends it or
lives in it and wants something, and weather that gets in the way.

**Build.** Draw the organism as a pure function of t from `timeline.json`, so
every frame is deterministic. Grow each element over 0.3–0.6 s with a stagger,
or a busy day pops. Cache settled growth into a layer; thousands of leaves
redrawn every frame will drop frames.

## 4. Building

**Premise.** A machine, ship, house or robot gets assembled piece by piece.
Each feature is a part with a job; at the end the machine switches on and does
what the product does.

**Mapping.** Parts are features or modules (from `feat` commits or the
directory structure). Early commits are scaffolding and sketches. A revert
pulls a part back out; a refactor is rebuilt in blueprint.

**Camera grammar.** A workshop wide shot plus inserts: close-ups of each part
clicking in. Cuts are allowed (it is a construction montage), but keep one
screen direction for "progress" so the build still reads as forward motion.

**Watch for.** A parts list is not a story. Keep someone building it who hits
problems, and make the switch-on the payoff.

## 5. Levels

**Premise.** A side-scrolling game. Each release or milestone is a level,
bugs and issues are enemies, features are power-ups, a revert costs a life,
the busiest day is the boss.

**Mapping.** The level map is the timeline. The HUD shows the date, the
commit count as the score and the version as the level name.

**Camera grammar.** Side scroll locked to the player, as in a platformer.
Title cards between levels, where cuts are allowed.

**Look.** Pixel art suits it (`craft.md` §8), with a chiptune score.

**Watch for.** Game grammar invites invented numbers. Score, lives and level
names must come from real counts and tags.

## 6. Companion

**Premise.** A user's days. The product enters their life and grows up next
to them; each feature solves something in their day. The product can appear
as a small character (a kitten, a helper) that grows with it.

**Mapping.** The story's calendar is the commit calendar. Each scene is a day
with a real feature in it, and a date stamp or margin note carries the hash.

**Camera grammar.** Cuts between scenes are fine. Keep continuity devices (a
window, a desk, the same mug) so the passing time reads.

**Watch for.** It drifts into an advert. Keep the git history visible in every
scene, and keep the protagonist's want bigger than "use the product".

## 7. Mixing and inventing forms

Forms can change at an act boundary when the story gives a reason: a journey
for the solo early days, building when helpers or a team arrive, a pull-back
into growth for the ending overview. Announce the change the way the material
changes are announced: at a seam, on an important commit.

These forms are starting points. A product often suggests its own: a map being
drawn for a navigation tool, a recipe being cooked for a food app, letters
being exchanged for a chat product. Any form works if the mapping stays
literal and the arc is there.

Only the journey has a reference implementation. For another form, reuse the
engine (palettes, marks, cels, exposure, camera, materials), the film HTML,
the writer and the score as a skeleton, and write the world code fresh.
Expect the cast sheet and the 30-second storyboard to take longer than they
did for a journey: they are where a new form proves itself.
