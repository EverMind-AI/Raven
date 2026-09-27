# Text, languages and fonts

## Fonts: licence first

System fonts are licensed for use on that computer, not for rendering into a
published video. macOS's Chinese handwriting faces (Hannotate/手札体,
HanziPen/翩翩体, Libian...) belong to commercial foundries (DynaComware etc.).
Read the font's `name` table copyright before using any face you did not
download yourself.

Use SIL OFL 1.1 faces, bundled in `fonts/` with their licence files, loaded
with `FontFace` before the first frame. Bundling also makes every machine
render identical pixels. `scripts/fetch_fonts.sh <dir> <zh> <en>` downloads a
set with licences.

Tested choices:

| Use | Face | Notes |
|---|---|---|
| Chinese (default) | LXGW WenKai 霞鹜文楷 | calm, legible handwriting-kaishu; the user's final pick |
| Chinese, playful | Xiaolai 小赖, ZCOOL KuaiLe 站酷快乐体 | cartoonish; wider set |
| Chinese, avoid | brush/running-script faces | fight the cartoon style |
| English (default) | Comic Neue (Bold for 600) | upright, reads as handwriting; the user's final pick |
| English, alternatives | Patrick Hand, Short Stack, Architects Daughter | upright |
| English, avoid | Caveat, Kalam | the slant was rejected as too italic for captions |

Only list faces in the loader that `fonts/` actually contains: a face that fails
to load stops the film from ever becoming ready (`frames.mjs load` reports it).

Render a sample sheet with the film's real copy before committing to a face.

Font stack per language:
- Chinese cut: `"LXGW WenKai", ...`: digits and Latin in the same face, so a
  date never switches to a slanted script mid-line.
- English cut: `"Comic Neue", "LXGW WenKai", ...`.

Declare a weight range on each `FontFace` so asking for 600 never triggers
synthetic bold. If a Latin face runs small next to CJK, use `sizeAdjust`.

## The letter-by-letter writer

Captions appear one character at a time (`writeOn` in
`examples/raven-story/raven-world.js`). Three bugs it already fixes, keep them fixed:

1. The handwriting face's space is a hairline, so "调度所有 Agent 的 Agent"
   loses its gaps. Give spaces a minimum advance (a third of the size) and
   lay out with the same advance.
2. Set `textAlign = 'left'` inside the writer; an inherited `center` shifts
   every glyph.
3. Multiply by the context's current `globalAlpha`, or group fades leave
   ghost text.

## Copy rules

- One caption voice per film, chosen in the outline: usually the protagonist
  in the first person, in short sentences, talking to a newcomer. A narrator
  works too, but don't switch voices mid-film. The feature card is the product
  speaking and is the only other voice.
- Chinese: put a space between Chinese and Latin/digits (`多 Agent 编排`,
  `累计 1,686 次提交`); dates as `6月28日`.
- English: write it as a native speaker would say it, never a literal
  translation. Read every line aloud. Examples of fixes the user asked for:
  - "I'm a raven called Raven" / "Yes, I'm a raven" → both rejected; the
    second sentence should say something new: "Hi, I'm Raven. Let me show you
    how I hatched."
  - "Every commit is one more" → "Each commit is one more for the pile."
  - "Made By Raven, Made For You." → "Made by Raven, / made for you."
- Localize, don't transliterate: WeChat in the Chinese cut became Slack in the
  English cut (only because the product has a Slack channel).
- Product names keep their official casing ("The Harness of Harnesses";
  the closing card may use lowercase "the harness of harnesses" if the user
  wants it). Check with the README.
- No CJK punctuation in English strings (、，…). Apostrophes in JS strings:
  use double quotes for "I'm", "don't", "it's".
- Dates in English: `Jun 28`; counts: `1,686 commits so far`.

## Layout checks after any text change

Longer English lines and wider fonts collide with the feature card, labels
and birds. After changing copy or font, render full-size stills of every
text-heavy beat (`node frames.mjs film.html shots <seconds>`, 1920 wide by
default) and look for:

- caption vs feature card (top-left vs top-right on long lines);
- caption vs a material seam or its label, and caption vs a raised wing;
- margin note vs margin note when two stops are close (fade the older one out
  before the next arrives);
- labels vs captions (scores, "current me", task pills);
- text running out of a box (gate plaques, request note, name tags);
- the date line under the feature card vs the card's subtitle.
