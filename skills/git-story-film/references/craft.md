# Character, motion and materials

The drawing engine is the MIT "hand-drawn-canvas-animation" core bundled in
`engine/` (upstream: github.com/alesha-pro/tools, skills/hand-drawn-canvas-animation).
Read the reference project in `examples/raven-story/` before writing new code:
it solves most of what follows.

## Contents
1. The protagonist
2. Poses and expressions
3. Timing: two clocks
4. Hops, flight, falls
5. Props that attach to the body
6. Pages, seams and materials
7. Companions
8. Other looks: pixel art

## 1. The protagonist

The protagonist can be an animal mascot, a person, an object, a plant, or the
product itself. The outline decides which (`forms.md`). The raven shows the
cartoon-animal case. Cartoon, big eyes: white sclera, large pupil, two
highlights. A mascot with a real animal behind it keeps a few true traits (the
raven: heavy beak, shaggy throat hackles, wedge tail, fingered primaries, a
crown tuft that carries half of every expression). A person drawn from
official art keeps that art's silhouette, palette and one or two signature
details (hair colour, an accessory), simplified to what reads at delivery size.

Build it as one function from a pose object to a drawing
(`examples/raven-story/raven-cast.js`):

- head and body are ellipses unioned into one silhouette (`rvRun` bisects the
  intersection edges so the outline slides smoothly as the head moves);
- the face, beak, wing, tail, legs are computed from the pose, then turned
  into stroke cels (`compileCel`) and drawn with `drawCel` in pencil or ink;
- `rvPal(look, pose)` swaps materials: pencil (paper fill, graphite),
  ink (black mass, pale gloss), chalk (navy blueprint), and a body colour
  for companions;
- sprites are cached per pose key, so a held pose costs nothing.

Fix the drawing before animating it. The cast sheet (`cast-sheet.html`,
`directing.md` §2) comes right after the story direction. It shows 6–12
expressions and key poses for every action the story needs, in each material,
at delivery size. When a later shot needs something new, add it to the sheet
and show the user.
Check that a flapping wing does not cover the face, and that a folded wing does
not read as a belly patch.

## 2. Poses and expressions

A pose is a flat object (`kind`, `lean`, `squash`, `tilt`, gaze `gx/gy`,
`lid`, `mood`, `spark`, `beakOpen`, `tuft`, `wingMode`, `flap`, `tuck`...).
`rvTrack([[t, pose, ease], ...])` blends numeric fields and switches discrete
ones (`mood`, `wingMode`) on the key that sets them.

Expressions that read at a glance: curious, look left/right, sparkle eyes,
happy (arched lids), suspicious (tilted lid), dizzy (spiral eyes + stars),
surprised (small pupils, tuft up), blink, squeeze.

## 3. Timing: two clocks

Output runs at 24 or 60 fps. Keep two times:

- **placement time** = every output frame: position, camera, parallax, text;
- **drawing time** = quantized: the pose is exposed on ones (1/24 s) for fast
  action and on twos (1/12 s) for thinking. That keeps the hand-drawn feel.

Compute the state twice and combine: position from the continuous time, pose
from the quantized time (`placedAt(state, t, exposure(t))` in the example).
Taking both from the quantized time makes the protagonist stutter against a camera
that moves every frame, especially at 60 fps. Companions' wing beats must also
use the quantized time.

## 4. Hops, flight, falls

A hop is a small performance: crouch (squash .86, lean forward) → push
(squash 1.12) → air (tucked legs, wing up/down alternating) → land (squash
.84) → recover. Keep the root motion on an explicit arc; put one landing tick
in the score per hop.

In a journey (`forms.md` §2), a fall must still move only rightward: jump
up-right toward something, hover, lose it, drop out of frame, climb back in
*further right*.

Flight: x from a keyed table of (time, x); y bobs gently. A flap cycle from
`cos(phase)`; if the beat frequency changes, carry the phase continuously
(`beatPhase`) or the wings jump.

## 5. Props that attach to the body

Anything worn or carried is placed from the drawn geometry, not guessed:
`rvGeom(pose)` exposes anchors (crown, back, chest, beak, feet) and
`rvWorld(localPoint, placement)` maps them into the page. The eggshell hat,
the memory pack, the pencil in the beak and the name tags all use this, so
they follow squash, tilt and flight.

## 6. Pages, seams and materials

The page is drawn per material region. Between two regions there is a seam
(a wobbly vertical line at a world x). Each region renders into its own layer
and is clipped by the seam polygon in screen space; the seam's own ink belongs
to the page and is drawn inside each region before characters.

Performance traps:
- Halftone over a whole landscape is hundreds of thousands of arcs; the
  browser silently drops the frame. Emit only dots inside the visible box.
- Keep the halftone lattice fixed to the page (world coordinates), or the dots
  shimmer as the camera moves.
- Anything a later act adds must fade in after a hand-over (0.8 s), or it
  pops into frame.

## 7. Companions

Companions are the same drawing with `body` colour and an accessory that says
who they are (monocle for research, `</>` for code, night cap + lantern for
on-call, beret for design). Third-party agents are drawn in pencil with a
name tag, so they read as guests. Each gets an offset path relative to the
mascot (`keys: [[t, dx, dy], ...]`) so they can fly forward for their own
beat and fall back afterwards. Reins drawn at the end connect the mascot to
each bird and each bird to its tool: the harness-of-harnesses image.

## 8. Other looks: pixel art

Pixel art suits a levels film (`forms.md` §5) or a retro act inside another
form. What changes:

- **Keep sprites as data.** A palette plus one string per row (`.` for
  transparent), generated by a script from shared parts (face, hair, legs,
  bob), so an edit to one part carries through every frame. Hand-editing
  frames drifts.
- **Whole output pixels.** The engine draws in logical units (the short side is
  1080) and scales by `S` = output width / logical width, so 1 at 1920 wide
  and 2 at 3840. A sprite pixel must cover a whole number of output pixels at
  every width you render. Drawing a sprite as one `fillRect` per pixel at a
  fractional size (cell 3.5 at 1920 wide) leaves visible seams between the
  pixels, and so does a fractional position.
- **Draw from a 1:1 bitmap.** Render each frame once into a small offscreen
  canvas at one pixel per sprite pixel, then `drawImage` it scaled with
  `imageSmoothingEnabled = false` at a position rounded to whole output
  pixels. That removes the seams at any scale, and one `drawImage` is far
  cheaper than thousands of rects.
- **Snap motion.** Move the sprite and the camera in whole sprite pixels, or
  at least whole output pixels, or the sprite shimmers as the camera moves.
- **Timing.** Sprite poses read best on twos or threes (8–12 poses per
  second). Two clocks still apply: position every output frame, pose from
  quantized time (§3).
- **Check the small sizes.** Render the model sheet at 1×, 2× and 3× the sprite
  size and on the real background: a face that reads at 3× can vanish at 1×.
