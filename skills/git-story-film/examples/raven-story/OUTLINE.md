# Raven tells its story - Outline v3 (one continuous path to the right)

About two minutes, 16:9, 1920×1080, 24 fps. Plays as a local HTML film and can be rendered to MP4.
The visual reference is [hand-drawn-canvas-animation](https://github.com/alesha-pro/tools/tree/main/skills/hand-drawn-canvas-animation) (MIT): redraw complete poses, keep strokes visible, and change materials with the story.

## Camera rules: one continuous shot, always moving right

- From hatching onward, Raven moves only right. It can stop for an event, but its next step is forward.
- The camera follows without cuts or reversals. Its position is a moving average of Raven's position, keeping the whole journey moving right.
- The branch is the timeline. Each date is a stop; the top-right card updates the date and cumulative commit count as Raven arrives.
- Materials change by segment. Important commits create seams on the page; crossing a seam changes Raven's material.
- Movement grows with capability: small pencil hops, larger ink hops and the first takeoff, flight through riso, leading a flock through screen print, and finally the whole flock flying together.

## Protagonist

A cartoon raven with large white eyes, large pupils and two highlights. See `cast-sheet.html`.

- Raven features: a heavy beak, shaggy throat feathers, wedge-shaped tail, fingered flight feathers and a crown tuft.
- Stages: pencil chick → black puffball on entering the ink → fledgling after shaking off the ink → adult.

## Four recurring devices

| Device | Role |
|---|---|
| Timeline branch | Extends left to right. The past is solid; the future is dashed. Each new segment is drawn only when its day arrives. |
| Shiny beads | One bead per commit, hung under its day in the actual daily count. |
| Margin notes | Handwritten hashes and PR numbers pinned to the page, left behind as the camera advances. |
| Eggshell hat | Lands on Raven's head at hatching, stays through the early journey, then falls off as the wings open and remains at v0.1.0 as a keepsake. |

## Segments and seams

| Segment | Material | Seam | Story reason |
|---|---|---|---|
| Jun 28–30 | Pencil | Starting point | The first commits are a draft |
| Jun 30–Jul 16 | Ink | v0.1.0: a wet brush edge inks Raven as it enters | Going open source makes the sketch permanent |
| Jul 17–Aug 9 | Riso | #140: a misregistered print edge | Evolution means making proof prints and choosing the best |
| Aug 10–28 | Screen print | subagent: a stencil edge | Flat colors distinguish the helper birds |
| Aug 29–31 | Blueprint | generation model: an ink drop spreads across the sky | The story turns to structure and internals |
| Sep 4–24 | Mixed | Birds of every material share the frame | Harness of Harnesses |

## Route (full film, about two minutes, for newcomers)

The **feature card** sits at the top right: a large feature name, a short benefit and a final line with the date and cumulative commits.
The second half follows one request: "Research three competitors, write a scraper, watch the data overnight, and turn the results into a deck." Four built-in agents each take one part.

This is the historical outline; the completed film runs 128 seconds. The film code and storyboard refine the timings and closing sequence below. Captions here are English adaptations; `zh/` contains the Chinese film copy.

| Time | Feature card | Picture | Raven's caption |
|---|---|---|---|
| 0:00–0:11 | Raven: the agent that runs your agents | A pencil draws the branch and egg; Raven hatches | Hi, I'm Raven. Let me show you how I hatched. Each commit is one more shiny thing for the pile. |
| 0:11–0:19 | Safety: flag unfamiliar content | A note receives an "untrusted" tag | Flag anything a stranger slips me. |
| 0:19–0:25 | Long-term memory: a shaky first attempt | The memory pouch is erased; Raven falls | My brand-new memory got rolled back. |
| 0:25–0:32 | Open source: install with one command | Raven enters the ink and becomes a fledgling | They opened the nest. Now I'm in ink. |
| 0:32–0:35 | Long-term memory: remember you across sessions | The pouch returns and the eraser bounces off | Memory's back. This time it sticks. |
| 0:35–0:38 | Skills on demand: over 110,000, fetched when needed | A tool roll closes; Raven takes one tool | I don't lug every tool around, just the one the job needs. |
| 0:38–0:44 | Raven-Research: answers with sources | First flight; Raven retrieves a report labeled "sources" | I'll fly a long way for an answer, and bring back the sources. |
| 0:44–1:00 | Self-evolution: keep only versions that beat the baseline | Five gates: diagnose failures, design changes, quick screening, full retest, beat the baseline. Candidate birds A/B/C compete; B wins and rejoins Raven | I can swap out my own feathers. Every new version must clear five gates, and only stays if it beats me. |
| 1:00–1:03 | Import memories from other AI tools | Proof prints pass beside the route | Coming from another AI tool? Bring your memories with you. |
| 1:03–1:09 | Multi-agent orchestration: one request becomes a task graph | A request arrives; Raven draws a task graph and helpers catch up | You just say what you need. I break it down for the right birds. |
| 1:09–1:13 | Raven-Code: turn requests into working code | Code hands over a page stamped "12 tests passed" | Coding goes to this one, and it's tested before it ships. |
| 1:13–1:17 | Raven-Oncall: watch overnight, report at dawn | Oncall keeps watch with a lantern until morning | Need someone to watch it all night? This one's on it. Go get some sleep. |
| 1:17–1:26 | Third-party agents: orchestrate Claude Code, Codex and Kimi Code | Named birds join: Claude Code, Codex, Kimi Code, OpenClaw and MiroThinker | Bring in other agents, too, all in the same task graph. |
| 1:26–1:33 | Upgrades without restart | Raven swaps skeletons in the blueprint sky; 137 beads fall for Aug 30 | Our busiest day: 137 commits. I swapped my skeleton mid-flight, so settings change with no restart. |
| 1:33–1:38 | Raven-Design: decks, charts and brand materials | Design opens a three-slide deck; the request is complete | And for the finale: a polished deck. Job done. |
| 1:38–1:43 | Playbook: rerun a proven workflow | The task graph folds into a booklet with a "Run again" stamp | A flow that works becomes a Playbook. Next time, just say the word. |
| 1:43–1:47 | Proactive reminders and channels | An alarm rings; WeChat signs in the Chinese cut, Slack in English, alongside Telegram and Web | I'll nudge you when it's time, and you can find me in Slack. |
| 1:47–1:53 | Harness of Harnesses: the one holding all the reins | Raven holds every bird's reins; each bird holds its own tools | That's why they call me The Harness of Harnesses. |
| 1:53–2:00 | Ending | Pull back; Raven peeks from behind the title and blinks | |

## Sound

An original synthesized score, with cues attached to the route to stay in sync with the picture.

- Each landing makes a soft tick; together they become footsteps.
- Pencil: rustling texture and sparse piano.
- Crossing the seam: a splash and opening strings.
- Riso: a printing-press rhythm.
- Screen print: plucked strings and flock calls.
- Blueprint: a sustained bass tone.
- Ending: the full chord progression.

## Completed film

The completed `raven.html` runs 128 seconds, from 0:00 to 2:08.

- **Playback:** open it in a browser, click play or press Space, and enable sound. Scrub to inspect individual frames.
- **Preview render:** in a prepared film folder with the engine copied alongside the HTML, run `node render.mjs raven.html --grid 60`.
- **MP4 delivery:** install ffmpeg if needed (`brew install ffmpeg`); use `node render-hq.mjs raven.html --width 3840 --fps 60` after studio approval, with the render helper copied into the film folder.
- **Code:** `raven-route.js` covers Act I; `raven-flight.js` covers later acts and the ending; `raven-cast.js` draws the cast; `raven-world.js` draws the page elements; `raven-score.js` supplies the score.

## Correction

An earlier draft said "39 hands". That was unreliable: excluding dependabot left 38 author names, but some names belonged to the same person. The captions therefore avoid a specific contributor count.
