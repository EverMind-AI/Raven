# git-story-film

English | [简体中文](README.zh-CN.md)

Turn a GitHub repository's history into a hand-drawn animated film of about two minutes.

Give the assistant a repository link. It acts as director: selecting real commits, releases and reverts, shaping a story with a setup, development, turning point and resolution, designing a protagonist, writing the outline and storyboard, animating the film, and rendering a 4K/60fps MP4. The film introduces newcomers to how the product grew and what it can do for them today.

A complete 128-second Raven example is included in `examples/raven-story/`, with an English cut and Chinese source variants in `zh/`.

## Quick start

1. **Load the skill.** This copy lives at `skills/git-story-film/` in the Raven project. Ask your assistant to read `SKILL.md` at that path. For Claude Code's automatic discovery, copy the complete folder to `~/.claude/skills/` or the project's `.claude/skills/` directory.
2. **Describe the film**, for example:
   - "Make a video about https://github.com/owner/repo. Start with the story direction."
   - "Animate the story of how this project grew."
   - In Claude Code, invoke `/git-story-film https://github.com/owner/repo`.
3. **Follow the seven steps.** The assistant presents a concrete artifact at each step and waits for your approval or feedback before moving on.

## The seven steps

| Step | What you see | Your part |
|---|---|---|
| 1. Story direction | `OUTLINE.md`: the story, key features and supporting facts | Confirm the direction, language, aspect ratio and logo treatment |
| 2. Character | An HTML cast sheet: expressions, action poses, materials and size checks | Choose the appearance and request changes |
| 3. Full outline | The complete two-minute structure, looks, beats, ending and claims to verify | Refine the copy and sequence |
| 4. First 30 seconds | A storyboard with a keyframe, captions and commit evidence for each shot | Set the voice, pacing and look |
| 5. Full build | The remaining shots and animation; new poses added to the cast sheet as needed | Give feedback throughout |
| 6. Studio preview | The film opens in the studio described below; no MP4 render yet | Watch the whole film and request changes |
| 7. Final delivery | A 4K/60fps MP4 with duration, resolution, file size and verification results | Accept the delivery |

## Studio: watch in the browser

The film is an HTML page. Open it with its companion scripts in place; no server is required. Keep the bundled folder structure intact when opening `examples/raven-story/raven.html`. When copying the example into a flat film folder, copy the engine too and change `../../engine/` script references to local filenames.

- **At the top:** the film and a scrub track as wide as the picture. Acts are color bands, shots are numbered cells, and hovering reveals timing and shot information.
- **Playback:** starts at the current position. Click `♪ sound` to enable the score; once ready, it joins at the current playback position.
- **Keyboard:** Space toggles playback; Left/Right move one frame; Shift+Left/Right move one second; `[` and `]` select the previous/next shot; Home/End jump to the beginning/end.
- **Below:** one frame per shot, drawn from the current film code. Refresh after code changes. Click a frame to seek; the active shot is highlighted during playback.

The bundled `examples/raven-story/storyboard/storyboard.html` is a text-only reference with bilingual captions. No pre-rendered images are bundled; the film studio draws its own storyboard frames from code. For your own films, `scripts/storyboard.py` can include generated stills, and `--embed` produces a single file for sharing.

## Render the final film

After approval in the studio, run these commands from the prepared film folder:

```bash
node render-hq.mjs film.html --width 3840 --fps 60              # 4K/60fps delivery
node render-hq.mjs film.html --width 1920 --fps 24 --no-cover   # Quick 1080p preview
```

- **Cover frame:** by default, the closing card is also written as frame 0 for players and feeds to use as the cover. It flashes for one frame at the start. Keep it for social uploads; add `--no-cover` for inline or looped playback.
- **One render at a time:** concurrent 4K renders can crash Chrome workers.

## More than one way to tell the story

A mascot hatching and moving right along a timeline is just one form. Choose, combine or invent a form that suits the product:

- **Journey:** a character follows the timeline in one continuous shot, as in the Raven example.
- **Growth:** a tree or city grows in place.
- **Building:** a machine is assembled piece by piece.
- **Levels:** a side-scrolling game, with releases as levels and bugs as enemies.
- **Companion:** a product grows alongside a user's daily life.

See `references/forms.md` for details.

## Folder layout

```text
git-story-film/
  README.md      English overview
  README.zh-CN.md Chinese overview
  SKILL.md       Director instructions, workflow and constraints
  references/    Directing, story forms, craft, fonts and production guidance
  engine/        Drawing and animation engine, including studio-ui.js
  scripts/       Git statistics, fonts, frame capture, storyboards and rendering
  examples/      Raven film; Chinese source variants live in zh/
```

## Requirements

- Node 22+
- Google Chrome
- ffmpeg (Homebrew installations may require `/opt/homebrew/bin` on PATH)
- Python 3.8+

## Ground rules

- **Use real evidence:** hashes, dates and commit counts come from the repository. Label illustrative numbers, distinguish development tools from runtime features, and show only supported integrations.
- **Bundle licensed fonts:** use SIL OFL fonts such as LXGW WenKai and Comic Neue, with their license files in the film folder.
- **No recorded narration:** captions carry the voice; Web Audio synthesizes the score. Leave timed gaps if recorded narration will be added later.
- **Keep localization intentional:** instructions and default examples are in English. Chinese film variants, Chinese UI labels and bilingual caption references remain in their original language to preserve that functionality.

## Credits

The drawing and animation engine (`core.js`, `cels.js`, `materials.js`, `studio.js` and `render.mjs` in `engine/`) is adapted from:

- **hand-drawn-canvas-animation**, Alexey Fateev, MIT license
  https://github.com/alesha-pro/tools/tree/main/skills/hand-drawn-canvas-animation

Changes include 60fps output and playback controls. Keep `engine/LICENSE.hand-drawn-canvas-animation` when copying the engine. The studio (`engine/studio-ui.js`), helper scripts and example film were written for this skill.
