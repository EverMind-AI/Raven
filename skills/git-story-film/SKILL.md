---
name: git-story-film
description: Direct an animated film of about two minutes from a software repository's Git history. Select real commits, releases and reverts; develop the story, character and visual style; build an outline, cast sheet, storyboard and HTML review studio; then render a 4K/60fps MP4 after approval. Use for repository or product promos, launches, anniversaries, release stories, codebase timelines, character-led project histories, and requests such as "make a video about our project" or "show how this product grew." Not for UI screen recordings, slide decks, or commit-statistics charts.
---

# Git story film

The user gives you a GitHub link. You direct a film of about two minutes on
how that product grew up. The git history is the footage: it supplies the
order, the dates and the proof. Your job is to turn it into a story a newcomer
enjoys and learns from: what the product is, how it got here, what it can do
for them.

Directing means you make the calls and show them, and the user approves.
You own three things:

- **Data.** Which facts carry the film. Out of thousands of commits, pick
  the first commit, the release, the busiest day, the reverts and the hero
  feature, and put real hashes, dates and counts on screen. Everything shown
  is true and sourced; illustrative numbers are marked.
- **Story.** The protagonist, whose voice the captions are, the form the
  growth takes on screen, and the arc: setup (the first commit, the problem) → development
  (abilities arrive) → turning point (the revert, the rewrite, the busiest day) → resolution (where
  it landed, and what it means for the viewer).
- **Look.** The art style and how it changes: a look per act with a story
  reason, the palette, the protagonist's design, type and sound.

A mascot hatching and walking along the timeline is one form, not the only
one. The same growth can be staged as something growing in place, a machine
being built, game levels, a user's days, or a form the product suggests
(`references/forms.md`). Choose for each product.

The engine in `engine/` (MIT, adapted from alesha-pro/tools hand-drawn-canvas-animation,
https://github.com/alesha-pro/tools/tree/main/skills/hand-drawn-canvas-animation; keep its LICENSE with
it) draws and animates, and `engine/studio-ui.js` turns
any film page into the review studio. `examples/raven-story/` is a finished
journey film (128 s, made for EverMind's Raven, with Chinese files in `zh/`).
Reuse their code; do not start from a blank page.

The instructions and default examples use English. Keep dedicated Chinese film
assets in `zh/` and Chinese UI labels as localization data; translating those
into English would remove the Chinese cut. Choose the output language for the
audience, and use `--ui zh` only for a Chinese storyboard interface.

## Setup

Clone the repo outside the film folder (`git clone --filter=blob:none <url>`)
and research it before step 1: run `python3 scripts/git_timeline.py <repo>`,
read the README feature list and docs, and note the first commit, public
release, busiest day, reverts, hero feature, built-in agents/modules and real
integrations (`references/story.md` §1, §6). Ask about audience, languages and
where the film will be shown if you don't know.

Set up the film folder, also outside the product repo
(`references/production.md` §1):
- copy `engine/*`, `scripts/render-hq.mjs` and `scripts/frames.mjs`;
- run `npm i` and `sh scripts/fetch_fonts.sh <dir>`;
- run `git init` with `node_modules/` and `out/` ignored.

For a journey, start from the example. For a Chinese film take
`raven-cast.js`, `raven-world.js`, `raven-route.js` and `raven-flight.js` from
`examples/raven-story/zh/`, and the rest (`raven.html`, `raven-score.js`,
`cast-sheet.html`) from `examples/raven-story/`; for English take everything
from the top level. For another form, keep the example's film HTML, writer and
score as the skeleton and write the world code fresh. Every film HTML loads
`studio-ui.js` right after `core.js`. The bundled example links to
`../../engine/`; when copying it into a flat film folder with the engine,
change those script paths to the local filenames.

Captions carry the voice and the score is synthesized; there is no voiceover.
If the user asks for voiceover, say so and offer to leave timed caption
gaps for a recorded track.

## The director's workflow

Seven steps, in this order. Each ends with something the user can look at, and
you stop for their approval before the next. Story, character and pacing are
cheap to change on paper and expensive after a 4K render. Templates, shot
fields and checks are in `references/directing.md`.

1. **Outline: story direction.** Tell the user what story you'll tell and what it
   highlights. Start `OUTLINE.md` with a logline, the form and why it fits, the
   protagonist and caption voice, the four-part story arc in four lines, the 3–5
   things the film will make the viewer remember (and which gets the most
   time), the numbers it stands on, and the look in a line or two.
2. **Character: expressions and actions.** Design the protagonist (and companions) in the
   chosen look. Render `cast-sheet.html` with 6–12 expressions, key poses for
   each action the story needs (walk or hop cycle, jump, fly, talk, point,
   carry), every material they appear in, and a 1×/2×/3× size check
   (`references/craft.md` §1–2, §8).
3. **Outline: the full film.** Expand `OUTLINE.md` to the whole film, about 2:00: acts
   and looks with reasons, recurring devices, one line per beat (time, what
   happens, feature, commit), the ending, and claims to double-check.
4. **Storyboard: the first 30 seconds.** Write the shots for the first 30 seconds into
   `shots.json`, draw one keyframe per shot with the real cast and world code,
   and open the storyboard page:
   `python3 scripts/storyboard.py shots.json --frames storyboard/frames --out storyboard/storyboard.html --ui en`.
   These 30 seconds fix the tone, pacing, voice and look for the rest.
5. **Build the full film.** Write the rest of the shots and animate the whole film.
   When a shot needs a new expression or action, add it to the cast sheet and
   show the user; they may ask for more at any time. Check your own work with
   the preview tools (`references/production.md` §2). Don't render.
6. **Studio preview.** Write the shot data and open the film page, which
   `studio-ui.js` turns into the studio:
   `python3 scripts/storyboard.py shots.json --js shots.js --out storyboard/storyboard.html`, then
   open the film HTML. The film is on top with a scrub track (acts as bands,
   shots as cells, drag anywhere, play from any point with the score in sync)
   and the storyboard below, one frame per shot drawn live from the film;
   click a frame to jump there. Iterate on the user's notes until they
   approve. Still no MP4.
7. **Final delivery.** Only after approval in the studio, deliver with
   `node render-hq.mjs film.html --width 3840 --fps 60`, one language at a
   time (a 1080p24 file first, `--width 1920 --fps 24 --no-cover`, only if the
   user wants a quick file to share). By default the closing frame is also
   written as frame 0, so players and feeds show it as the cover, and it
   flashes for one frame before the film starts. Keep it for social uploads;
   pass `--no-cover` for anything played inline or looped. Verify with ffprobe
   and a full decode, and report duration, size, resolution and what you
   actually looked at. Then swap the storyboard's keyframes for real frames
   (`node frames.mjs film.html shots <seconds>`).

Other languages live on a branch. Translate as native copy, not literally;
localize references (WeChat → Slack, only if the product has Slack); and
re-check every text-heavy frame in the studio after translating, because
longer lines collide.

## Things that matter more than they look

- **No render before the studio.** The user approves the film in the studio;
  the MP4 comes after. A 4K render is the most expensive place to find a
  problem.
- **Story before spectacle.** Each act teaches the viewer one thing, the turning point is the
  peak, and the ending pays off something set up in the first 20 seconds.
- **The form's grammar holds for the whole film.** A journey never cuts and
  only moves right; growth stays in place and pulls back; levels cut only
  between levels. Break the grammar once and the film turns into a slideshow
  (`references/forms.md`).
- **Literal mapping.** One token per commit, one milestone per release, one
  setback per revert, with real daily counts. That is what makes it this
  repo's story.
- **Two clocks.** Position every output frame; poses on ones/twos. Mixing
  them up makes the protagonist stutter at 60 fps (`references/craft.md` §3).
- **Fonts must be licensed for video.** Never ship a system font into a
  published film; bundle OFL faces (`references/text-and-fonts.md`).
- **Truthful beats.** Real hashes and dates; mark illustrative numbers; don't
  let a development tool read as runtime magic; only list integrations that
  exist.
- **Native copy in every language**: spaces between Chinese and Latin text,
  official product casing, and a caption that says something new each time.
- **One render at a time.** Parallel 4K renders crash Chrome workers.

## Reading map

| When | Read |
|---|---|
| Each step's document, shot fields, the studio, checks before a hand-off | `references/directing.md` |
| Choosing how the growth is staged (journey, growth, building, levels, companion...) | `references/forms.md` |
| Evidence, the setup, development, turning point and resolution arc, beats, devices, looks per act, fact checks | `references/story.md` |
| Drawing the protagonist, poses, motion, props, seams, companions, pixel art | `references/craft.md` |
| Fonts, captions, translation, spacing, text layout checks | `references/text-and-fonts.md` |
| Folder layout, preview commands, 4K delivery, sound, pitfalls | `references/production.md` |
| How the pieces fit in real code (a journey) | `examples/raven-story/` (start at `raven.html`) |
| A text-only storyboard reference with bilingual captions; view live frames in the film studio | `examples/raven-story/storyboard/storyboard.html` |
| Engine API (palettes, marks, cels, exposure, camera) | comments in `engine/core.js`, `engine/cels.js`, `engine/studio.js` |

## Scripts

| Script | Does |
|---|---|
| `scripts/git_timeline.py <repo>` | daily counts, totals, busiest days, feat/perf commits with PRs, reverts, tags → `timeline.json` |
| `scripts/fetch_fonts.sh <dir> [zh] [en]` | downloads OFL fonts with licences (zh: wenkai/xiaolai/kuaile; en: comicneue/patrickhand/shortstack/architectsdaughter) |
| `scripts/frames.mjs film.html load|shots|score` | page errors and frame count; stills at given seconds; score rendered with per-second levels |
| `scripts/storyboard.py shots.json [--frames dir] [--embed] [--js shots.js] [--ui zh] [--length s]` | the storyboard page: a storyline strip (acts, durations, time ticks), then the shots grouped by act, text-only or with stills (`--embed` makes one sendable file). `--js` also writes the shot data the studio reads |
| `engine/studio-ui.js` | loaded after `core.js`, makes the film page the studio: scrub track with acts and shots, playback from any point with the score, keys, live storyboard frames; renders (`?bare`) never build it |
| `scripts/render-hq.mjs film.html [--width 1920/3840] [--fps 24/60] [--no-cover] [--param k=v]` | streaming parallel render straight into ffmpeg, score muxed, closing frame as cover unless `--no-cover`; writes `out/<name>-<height>p<fps>.mp4` |
| `engine/render.mjs` | preview modes at 12/24 fps only: `--grid N`, `--only i,j`, `--strip start,count` (frame indices) |

Requirements: Node 22+, Google Chrome, ffmpeg (Homebrew path may need
exporting), Python 3.8+.
