# The director's steps and documents

You direct. Every call about data, story and look is yours to propose and the
user's to approve. The film gets built in seven steps. Each step ends in
something the user can look at, and you stop there until they approve.
Nothing is rendered to MP4 until the user has approved the film in the studio.

## Contents
0. Input: a GitHub link
1. Outline: story direction: what story, what it highlights
2. Character: expressions and actions: the cast sheet
3. Outline: the full film: the 2-minute outline
4. Storyboard: the first 30 seconds: the first storyboard page
5. Build the full film: the rest of the shots and the code
6. Studio preview: the film in the studio, not rendered
7. Final delivery: render, verify, report
8. Director's checks before each hand-off

## 0. Input: a GitHub link

Clone the repo outside the film folder with
`git clone --filter=blob:none <url>`, which keeps the full history and fetches
file contents on demand. Then work through `python3 scripts/git_timeline.py <repo>`,
the README's feature sections, the docs, the tags and releases (`gh release list`
if gh is set up), and the code wherever a claim needs checking (`story.md` §1,
§6). If you don't know the audience, the languages or where the film will be
shown, ask. The length is about 2 minutes unless the user says otherwise.

## 1. Outline: story direction

Start `OUTLINE.md` with a pitch the user can answer in a minute:

- **Logline.** One sentence: who, wants what, what happens, where it lands.
  ("A pencil raven hatches on a branch that is Raven's commit history and
  flies until it leads every agent it meets.")
- **Story.** The form and why it suits this product (`forms.md`). The
  protagonist, and whose voice the captions are. The arc in four lines, setup / development
  / turning point / resolution, each with the commits it rests on (`story.md` §2).
- **What it highlights.** The 3–5 things a viewer should remember
  afterwards, each tied to a real feature, and which one gets the most time.
- **Data.** The numbers the film stands on, each with its source (first
  commit, first release, total commits, the busiest day and its count), and the
  mapping from git facts to what the viewer sees (`forms.md` §1).
- **Look.** A line or two on the look family and how it changes.

In chat, tell the user the story and the highlights in a few lines and point to
the file for the rest. If a second story is a real contender, give it one line.

## 2. Character: expressions and actions

Design the protagonist in the chosen look, plus companions if the product has
helpers, and render a cast sheet (`cast-sheet.html`: a film page with one
frame, as in the example):

- 6–12 expressions the story needs (curious, happy, surprised, dizzy, proud...);
- the actions the story calls for as key poses, 3–4 per action: a walk or hop
  cycle, jump, fly, talk, point, carry;
- every look or material the protagonist appears in, at delivery size, with a
  1×/2×/3× check for small appearances.

See `craft.md` §1–2, and §8 for pixel art. Open the sheet for the user. Fix
the drawing here: it is what people most want to change, and every later frame
depends on it.

## 3. Outline: the full film

Expand `OUTLINE.md` into the whole film, about 2:00:

- a format line: length, aspect, languages, delivery
  (`2:00 · 16:9 · zh + en · 4K60 MP4 · social + site`);
- the acts, each with its look, where it changes and the story reason
  (`story.md` §5);
- the recurring devices: commit token, date display, feature card, keepsake
  (`story.md` §4);
- the beats, one line each: time range, what happens, the feature it shows, the
  commit. About 15–25 of them, adding up to about 2:00;
- the ending (`story.md` §2);
- claims to double-check (`story.md` §6) and open questions.

The raven film's `examples/raven-story/OUTLINE.md` is a real outline. It comes
from an older workflow, so it merges outline and shot list, and its camera rules
section is the journey form's camera grammar.

## 4. Storyboard: the first 30 seconds

Write shots for the opening 30 seconds only (through the first hand-over)
into `shots.json`. Draw one keyframe per shot with the real cast and world code
at a single pose, with caption and card in place (no animation yet), and render
the storyboard page with the stills:

```bash
python3 scripts/storyboard.py shots.json --frames storyboard/frames --out storyboard/storyboard.html \
  --ui en --title "<Film title> - Storyboard (first 30 seconds)"
```

Open it for the user. These 30 seconds set the tone, pacing, caption voice and
look for everything after, so this is the cheapest place to redirect the film.

Fields per shot:

| Field | What goes in it |
|---|---|
| `time` | start, `m:ss` |
| `dur` | seconds, only when the shot doesn't simply run to the next one |
| `key` | the moment that best shows the shot (`m:ss` or seconds); the studio draws its storyboard frame there. Default: the middle |
| `act` | act label, e.g. `Setup - Pencil`; consecutive shots with the same label form one act |
| `card` | on-screen card or title text (the product speaking) |
| `picture` | what is in frame and what happens, in one or two sentences |
| `camera` | shot size and camera move, within the form's grammar ("wide, drifting right", "close on the face", "pull back to the whole tree") |
| `lines` | the caption in each language: `{"English": "..."}` (add localized language labels for other cuts); `" / "` marks a line break |
| `sound` | the cue: a hop tick, a splash at the seam, a chord on the stamp |
| `note` | the commit, PR, tag or date the shot rests on; `connective` if none |
| `shot` | the still's file name inside `--frames` |

In a one-take journey, a "shot" is a stretch of the continuous move, named by
what the camera frames. The cut list is empty on purpose.

## 5. Build the full film

With the 30 seconds approved, write the remaining shots into `shots.json` and
animate the whole film.

- Keep `shots.json` as the source of truth, and re-render the storyboard page as
  shots are added. Rows without stills are fine.
- When a shot needs an expression or action the cast sheet lacks, add it to the
  cast and to the sheet, then show the user the additions with a line on where
  each is used. The user may ask for more at any point.
- Check your own work with the preview tools (`production.md` §2):
  `frames.mjs load`, the overview grid, strips across fast action, full-size
  stills of text-heavy shots.
- Do not render an MP4.

## 6. Studio preview

The studio is the film page itself. `engine/studio-ui.js`, loaded after
`core.js`, turns the page into a review desk:

- the film on top, with a scrub track as wide as the frame: acts as bands and
  shots as numbered cells. Drag anywhere to seek; hover to see the time and
  shot;
- play from any point, with the score in sync. The picture starts at once and
  the sound joins when its buffer is ready;
- keys: space play/pause, ←/→ one frame, shift+←/→ one second, `[` `]`
  previous/next shot, Home/End;
- below it, the storyboard: one frame per shot, drawn live from the film at the
  shot's `key`. Click one to jump there. The current shot is highlighted as
  the film plays.

Write the shot data next to the film HTML and open the film:

```bash
python3 scripts/storyboard.py shots.json --js shots.js --out storyboard/storyboard.html
open film.html
```

It works from `file://` with no server. Without `shots.js` the studio treats
each timeline scene as a shot. Shots past the film's current end show as "not
in the film yet", so the studio is also useful mid-build.

Ask the user to review the film in the studio, and iterate on their notes
(timing, drawing, captions, cast) until they approve. Still no MP4.

## 7. Final delivery

Only after the user approves in the studio, render and deliver (`SKILL.md`
step 7, `production.md` §3). Report duration, resolution, fps, file size,
"decode OK", which stills and strips you actually looked at, the score levels
you measured (and that you could not listen to them), and any claim you
softened or flagged. Then swap the storyboard page's keyframes for real frames
(`node frames.mjs film.html shots <key seconds>`) so the page stays the
reference for later edits.

## 8. Director's checks before each hand-off

- **Newcomer test.** After the film, would someone who has never heard of the
  product know what it does for them?
- **Arc.** Does each act teach one thing? Is the turning point the peak? Does the ending pay
  off something set up in the first 20 seconds?
- **Data.** Is every hash, date and count real and sourced? Are the
  illustrative ones marked?
- **Form.** Does the camera grammar hold for the whole film?
- **Look.** Does every look change sit on an important commit with a story
  reason?
- **Voice.** Does every caption say something new, in native copy, in every
  language?
