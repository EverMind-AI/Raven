# Turning a repository into a story

The film is for people who have never used the project. Its job is to answer
"what can this do for me?", told as the project growing up. The git history
supplies the order and the proof; the user's product pitch supplies the point.
How the growth is staged (a journey, something growing, a machine being
built...) is the form, covered in `forms.md`. This file covers what every form
needs.

## Contents
1. Gather evidence (data)
2. The arc: setup, development, turning point and resolution
3. Choosing beats
4. Recurring devices
5. Acts and looks
6. Claims you must not overstate

## 1. Gather evidence (data)

Run `scripts/git_timeline.py <repo>` and read the README's feature sections
and docs site. Collect:

- first commit, public launch/first release, latest release, busiest day;
- `feat`/`perf` commits and PR numbers as candidate beats;
- reverts: a revert makes a good early setback;
- the product's own vocabulary (e.g. "The Harness of Harnesses", agent names).

Cite real hashes and PR numbers on screen. Never invent a commit, a
contributor count or a benchmark number. If you show an illustrative score
(A 66, B 81), say so in the storyboard notes.

Author counts are unreliable: one person often appears under several names.
Prefer "1,686 commits" over "39 hands".

Selecting is the director's main job with data. Two minutes holds about 20
beats out of thousands of commits. Write down what you left out and why, so
the user can overrule it.

## 2. The arc: setup, development, turning point and resolution

A timeline is not a story until something is at stake. Shape the history into
four movements, and name the commits each one rests on:

| | What it does | Typical git material |
|---|---|---|
| Setup | who the protagonist is, the problem, the first line | first commit, init, the README's first promise |
| Development | abilities arrive one by one; each shows the viewer a use | `feat` commits, the first release, integrations |
| Turning point | the pressure peaks: something breaks, gets rebuilt or changes direction | reverts, rewrites, the busiest day, a pivot, the hero feature's hard part |
| Resolution | where it has landed and what it means for the viewer now | latest release, the overview, the closing card |

Early commits make better comedy than features (hatching, a stranger's note,
a revert as a fall); use them in the setup. Give the turning point the most screen time after the
hero feature. The ending should pay off something set up in the first 20
seconds (a keepsake dropped, a first line becoming the signature).

The raven film ended in three moves, and most forms can adapt them:
1. Pull back over the whole history: the commit tokens in their real daily
   counts.
2. A signature written by the protagonist (e.g. "Made by Raven, / made for
   you.").
3. A closing card: product name, tagline, the protagonist peeking over the
   name and blinking, and a hand-drawn search box that types the repo URL
   beside the official GitHub mark. This frame is also the video's cover
   (`production.md` §3).

## 3. Choosing beats

Aim for ~20 beats in two minutes (4–6 s each, longer for the hero feature).
For each beat decide: the feature card text, what the viewer sees, the line
the protagonist says, and the commit it rests on.

Prefer features a newcomer can picture. From the raven film:

1. The orchestration / "one request becomes a task graph" idea, shown as a
   real request split among helpers ("research three rivals, write a scraper,
   watch it overnight, make a deck"). Threading one request through several
   beats makes the built-in agents understandable.
2. Each built-in agent gets its own small visual payoff (sources, "12 tests
   passed ✓", a night shift ending at dawn, three slides).
3. Third-party agents joining: named birds with name tags. Use only names the
   project actually supports (check presets/docs).
4. The hero feature (here: self-evolution) gets the most time and a process
   the viewer can follow: gates, candidates, scores, "kept", "next round".
5. Memory, skills on demand, import, reminders, playbooks, upgrades without
   restart: one visual gag each.

## 4. Recurring devices

Every form needs a few devices that carry the data and hold the film
together. Find this form's equivalent of each:

| Device | Job | In the raven journey |
|---|---|---|
| The time axis | where "now" is | the branch, drawn solid up to today and faint dashed beyond; new segments are drawn as the raven arrives |
| Commit token | one unit per commit, real daily counts | shiny beads hung under their day (ravens hoard shiny things) |
| Margin notes | the proof | handwritten hash / PR number with an arrow, pinned to the page so they scroll away |
| Feature card (top right) | the product speaking | big: feature name; small: what it does for the viewer; tiny: date and running commit count; holds until the next feature so it can be read |
| A keepsake | set up early, paid off at a milestone | the eggshell hat, dropped at v0.1.0 |

In other forms the token might be a leaf, a brick, a coin or a stamp in a
diary. Keep one token per commit either way.

Captions (top left) are the protagonist speaking, in the first person unless
the outline chose a narrator. The card is the product speaking. Keep them
different.

## 5. Acts and looks

A look changes at an important commit (a seam in a journey, a season in
growth, a new level) and needs a story reason. The raven film:

| Act | Material | Reason |
|---|---|---|
| Draft days | pencil | the first commits are a sketch |
| Going public | ink | open source is permanent; the wash inks the mascot |
| The hero feature | riso print | many proof prints, pick the best |
| Working with others | screen print | flat colours tell the helpers apart |
| Rebuilding internals | blueprint | skeleton and structure |
| Everything together | mixed | all materials in one sky |

The look family can be something else entirely: pixel art for a levels film
(`craft.md` §8), cut paper, chalk on a blackboard, a watercolour diary. The
rule stays the same: every change sits on a commit and has a reason the viewer
could say out loud.

## 6. Claims you must not overstate

Read the docs for each feature you show. Typical traps:

- A development-time tool (e.g. a benchmark-driven evolver) is not the product
  "rewriting itself while you chat". "I can swap out my own feathers" is fine;
  implying runtime self-modification is not.
- Only list integrations and channels the code actually has (check the channel
  adapters before saying "find me in Slack").
- Illustrative numbers must be marked as illustrative in the storyboard.

Flag these to the user in your summary rather than silently softening them.
