# Production: project layout, rendering, delivery

## Contents
1. Project layout and git
2. Preview loop
3. Streaming 4K delivery
4. Sound
5. Pitfalls seen in practice
6. Samples, teasers and short cuts

## 1. Project layout and git

Work in a folder **outside** the product repository (a repo's own rules often
forbid HTML/video assets). Copy the engine and example in, then `npm i`.

```
<film>/
  core.js studio-ui.js studio.js cels.js materials.js   engine (MIT, keep LICENSE file)
  render.mjs package.json                  preview renderer (+ puppeteer-core)
  render-hq.mjs frames.mjs                 from this skill's scripts/
  fonts/                                   OFL faces + licence files + README
  OUTLINE.md       outline: story direction, then the whole film
  cast-sheet.html  character: expressions and actions
  shots.json       shot list       storyboard/  storyboard page + stills
  shots.js         shot data for the studio (storyboard.py --js)
  <name>-cast.js   the protagonist          <name>-world.js  page, marks, writer
  <name>-*.js      the acts (the raven: -route.js act I, -flight.js acts II+ and ending)
  <name>-score.js  synthesized score        <name>.html      the film; opens as the studio
  out/             renders (gitignored)
```

The film HTML loads `studio-ui.js` right after `core.js`, so opening it in a
browser gives the studio (`directing.md` §6). The render scripts load it with
`?bare`, which skips the player entirely.

`.gitignore`: `node_modules/`, `out/`, `.DS_Store`. Commit when the user asks;
keep languages on branches (e.g. `main` = Chinese, `feat/english_version`)
and merge fixes from `main` into the language branch.

`defineFilm` in the bundled `core.js` accepts fps 12, 24 or 60 (60 was added
for delivery; drawings stay on 24/12). The film reads `?fps=` from the URL
(the example accepts 12/24/60 and falls back to 24), so the same file previews
at 24 and delivers at 60. `render-hq.mjs --fps N` sets that parameter;
`engine/render.mjs` (previews) only knows 12 and 24 and never passes `?fps=`,
so do all preview work at 24.

Name collisions: the engine defines globals (`S`, `W`, `H`, `CX`, `CY`, `key`,
`arc`, `hash`, `spring`...). A film-level `const S = ...` throws at load and
the renderer waits forever. Use distinctive names (`ST`, `T2`).

## 2. Preview loop

From the film folder:

```bash
node frames.mjs film.html load                         # errors? frame count?
node render.mjs film.html --grid 60 --width 960 --out out/preview   # overview sheet
node render.mjs film.html --only 96,1300 --out out/preview           # full-size stills
node render.mjs film.html --strip 618,24 --width 1280 --out out/preview  # consecutive frames
node frames.mjs film.html shots 12.5,44,96             # stills at seconds
node frames.mjs film.html score                        # audio levels per second
```

Look at the overview after every structural change, full-size stills after
every text or layout change, strips around fast action and every hand-over.
A still that is blank except the background means something threw or a
drawing budget was blown; check with `frames.mjs load`.

These are for your own checks. The user reviews in two places: the storyboard
page (`scripts/storyboard.py`, `directing.md` §4) before the animation, and
the studio (`directing.md` §6) once the film is animated. No render happens
until the user approves the film in the studio.

## 3. Streaming 4K delivery

```bash
node render-hq.mjs film.html --width 3840 --fps 60 --workers 4 --out out
```

Parallel headless Chrome workers each render a stretch of frames and pipe PNGs
straight into ffmpeg (no frames on disk: 4K×7,000 PNGs would be >100 GB).
Segments are joined losslessly, the score is muxed in. The closing frame is
also written once at the start, so players and feeds use it as the cover; the
audio is delayed by that frame. `--no-cover` disables it; `--param k=v` passes
URL parameters (e.g. a font trial).

Throughput on a 12-core Mac: ~25 frames/s at 4K, so a 2-minute 60 fps film is
~5–6 minutes. Verify every delivery:

```bash
ffprobe -v error -show_entries format=duration,size:stream=width,height,r_frame_rate,nb_frames -of compact out/x.mp4
ffmpeg -v error -i out/x.mp4 -f null - && echo decode OK
```

Report duration, resolution, fps, size and "decode OK". A decode pass is not
a viewing: say what you actually looked at.

ffmpeg may be installed but not on the non-interactive PATH; export
`/opt/homebrew/bin` before calling it.

## 4. Sound

The score is Web Audio on the same timeline (`<name>-score.js`), rendered
offline by the page. Place every cue from the route data (hop landings,
reveals), never from hard-coded seconds, so a timing change cannot desync it.
Pencil scratches and sparse piano for sketches, a pad when the ink arrives,
a tick per landing, a short chord per stamp.

Check levels: peak under ~0.8, quiet stretches still audible. Put gain after
a compressor rather than raising the master into clipping. You cannot hear
the result: tell the user the levels were measured but not listened to.

## 5. Pitfalls seen in practice

- **Two renders at once**: two 4-worker renders on one machine crashed a
  Chrome worker ("Target closed"). Queue renders; one at a time.
- **Stopping a render**: kill both the node process and ffmpeg, then delete
  `out/.<name>-*` staging folders and any half-written mp4.
- **Background waits**: a wait loop that checks one file name can hang when a
  rename step never runs; check the log for the finish line and the process.
- **Rendering while switching branches**: a script that checks out branches
  in the working folder must not overlap with edits; use `git worktree` for
  side experiments.
- **Apostrophes** in single-quoted JS strings break the whole file.
- **A frame counter's `S`/`T` shadowing** globals: see project layout.
- **A still that looks right can hide a timing bug**: pops at hand-overs and
  stutter only show in strips or playback.

## 6. Samples, teasers and short cuts

The first-look preview is the 30-second storyboard page, and the animated
preview is the studio, so a sample MP4 is only needed when the user wants a
file to share early. The example's act I (`raven-route.js`) is not a
standalone film: the feature card (`featureCard`), dates and the camera's
extended route live in `raven-flight.js`. For a sample, keep every script
loaded and shorten the
`TIMELINE` array in the film HTML to the segments you want (and end the score
there). Nothing else changes, and the sample stays frame-identical to the
final film.

For a teaser that plays the same story faster, map output time to story time
(`story = start + (t - start) * k`) in one wrapper, and run the score's cue
times through the same map. Quantize poses on the *output* clock after the
map, not on the story clock, or a 1.45× speed-up exposes drawings for odd
frame counts. Hops shorter than ~8 frames stop reading as hops: shorten the
walk instead of speeding it up past that.

To leave out the ending, drop its segments from `TIMELINE`; `render-hq.mjs`
then uses the new last frame as the cover, which is usually a mid-flight frame:
pass `--no-cover`.
