/* The falling-feather glyph's engine: feather sprites drawn once per page,
   the stream that moves them, and the frame that paints them.

   No CanvasRenderingContext2D.filter anywhere. The macOS app runs on the
   system WebView back to macOS 12, where canvas filters are not reliably
   there, so every softening is a box blur computed here, once, when the
   sprites are built. The sprites are white strokes on transparency, so only
   the alpha channel is blurred and the colour is pinned to white; the light
   theme's ink feathers and both glows are tinted from those white layers. */

export type FeatherTheme = 'light' | 'dark'
type Tier = 'sharp' | 'soft' | 'far'

export interface Feather {
  x: number
  y: number
  z: number
  size: number
  side: 1 | -1
  spr: number
  sway: number
  sf: number
  sp: number
  rot: number
  rf: number
  ra: number
  ff: number
  fp: number
}

export interface StreamSpec {
  count: number
  speed: number
  size: [number, number]
  swayRate: number
  flipMin: number
  glow: number
}

/* The waiting row's stream: four feathers at one speed, evenly spaced, so one
   lands about every 0.8 s and the window is never empty. */
export const TAIL_STREAM: StreamSpec = { count: 4, speed: 27, size: [13, 21], swayRate: 1.7, flipMin: 0.5, glow: 0.7 }

const TAU = Math.PI * 2
/* A feather is at most 21 CSS px on the row, 42 device px at 2x: 128 px of
   sprite keeps three times that and costs about a third of 220's blur work
   on the first frame. The geometry and the blur radii scale with it. */
const SPRITE_LENGTH = 128
const SEEDS = [11, 29, 47, 83, 131]
const LOOKS: Record<FeatherTheme, { glow: string; glowAlpha: number; shadow: boolean; tint: string | null }> = {
  dark: { glow: 'rgba(175, 205, 255, 1)', glowAlpha: 0.85, shadow: false, tint: null },
  light: { glow: 'rgba(30, 24, 16, 1)', glowAlpha: 0.22, shadow: true, tint: '#17130e' },
}

const span = (spec: StreamSpec, h: number): number => h + 2 * spec.size[1]

function roll(f: Feather, spec: StreamSpec, w: number, rnd: () => number): void {
  const [small, large] = spec.size
  f.z = rnd()
  f.size = small + (large - small) * Math.pow(f.z, 1.4)
  f.spr = Math.floor(rnd() * SEEDS.length)
  f.sway = f.size * (0.25 + rnd() * 0.35)
  f.sf = 0.25 + rnd() * 0.35
  f.sp = rnd() * TAU
  f.rot = (rnd() - 0.5) * 1.9
  f.rf = 0.3 + rnd() * 0.3
  f.ra = 0.25 + rnd() * 0.35
  f.ff = 0.12 + rnd() * 0.22
  f.fp = rnd() * TAU
  f.x = w / 2 + f.side * w * (0.12 + rnd() * 0.14) - f.size * 0.05
}

/** The stream's opening frame: feathers evenly spaced along the whole travel,
 *  alternating sides, so the window is already in rhythm on the first paint. */
export function seedStream(spec: StreamSpec, w: number, h: number, rnd: () => number = Math.random): Feather[] {
  return Array.from({ length: spec.count }, (_, i) => {
    const f = { side: i % 2 ? 1 : -1 } as Feather
    roll(f, spec, w, rnd)
    f.y = -spec.size[1] + span(spec, h) * (i / spec.count)
    return f
  })
}

/** Moves every feather by `dt` seconds. All share one speed, and a feather
 *  that leaves the bottom moves up by exactly one travel, so the spacing never
 *  drifts; it comes back as a new feather on its own side. */
export function stepStream(fs: Feather[], spec: StreamSpec, w: number, h: number, dt: number, rnd: () => number = Math.random): void {
  for (const f of fs) {
    f.y += spec.speed * dt
    if (f.y - spec.size[1] > h) {
      const y = f.y - span(spec, h)
      roll(f, spec, w, rnd)
      f.y = y
    }
  }
}

/** In-place box blur of an RGBA buffer's alpha channel, `passes` times in each
 *  direction (three approximate a gaussian of about `radius`). Edges extend. */
export function boxBlur(data: Uint8ClampedArray, w: number, h: number, radius: number, passes = 3): void {
  const r = Math.max(0, Math.round(radius))
  if (!r) return
  /* Blurred in floats and written back once: rounding into bytes on every
     pass would shave a little alpha off each time. */
  const plane = new Float32Array(w * h)
  for (let i = 0; i < plane.length; i++) plane[i] = data[i * 4 + 3]!
  const line = new Float32Array(Math.max(w, h))
  const width = 2 * r + 1
  const run = (len: number, at: (i: number) => number): void => {
    for (let i = 0; i < len; i++) line[i] = plane[at(i)]!
    let sum = 0
    for (let k = -r; k <= r; k++) sum += line[Math.min(len - 1, Math.max(0, k))]!
    for (let i = 0; i < len; i++) {
      plane[at(i)] = sum / width
      sum += line[Math.min(len - 1, i + r + 1)]! - line[Math.max(0, i - r)]!
    }
  }
  for (let p = 0; p < passes; p++) {
    for (let y = 0; y < h; y++) run(w, (x) => y * w + x)
    for (let x = 0; x < w; x++) run(h, (y) => y * w + x)
  }
  for (let i = 0; i < plane.length; i++) data[i * 4 + 3] = plane[i]!
}

export function featherTheme(root: HTMLElement, prefersDark: boolean): FeatherTheme {
  const set = root.dataset.theme
  if (set === 'dark' || set === 'light') return set
  return prefersDark ? 'dark' : 'light'
}

export function motionAllowed(root: HTMLElement, prefersReduced: boolean): boolean {
  return root.dataset.motion !== 'off' && !prefersReduced
}

/* ── sprites ─────────────────────────────────────────────────────────── */

/** Makes a detached canvas of the given backing size; the caller owns the
 *  document, so this module never reaches for one itself. */
export type CanvasMaker = (w: number, h: number) => HTMLCanvasElement

let canvasOf: CanvasMaker = () => { throw new Error('featherSprites() was not given a canvas maker') }

/* Blur a white-on-transparent canvas in place: colour pinned to white, alpha
   blurred, so no dark fringe bleeds in from the transparent pixels. */
function blurWhite(c: HTMLCanvasElement, radius: number): void {
  const g = c.getContext('2d')!
  const img = g.getImageData(0, 0, c.width, c.height)
  const d = img.data
  for (let i = 0; i < d.length; i += 4) { d[i] = 255; d[i + 1] = 255; d[i + 2] = 255 }
  boxBlur(d, c.width, c.height, radius)
  g.putImageData(img, 0, 0)
}

function padded(src: HTMLCanvasElement, radius: number): HTMLCanvasElement {
  const pad = Math.ceil(radius * 3)
  const c = canvasOf(src.width + pad * 2, src.height + pad * 2)
  c.getContext('2d')!.drawImage(src, pad, pad)
  blurWhite(c, radius)
  return c
}

function tintOf(src: HTMLCanvasElement, color: string, deepen: boolean): HTMLCanvasElement {
  const c = canvasOf(src.width, src.height)
  const g = c.getContext('2d')!
  g.drawImage(src, 0, 0)
  g.globalCompositeOperation = 'source-in'
  g.fillStyle = color
  g.fillRect(0, 0, c.width, c.height)
  if (deepen) {
    /* Ink on a light ground needs the opacity white earns by glowing on a
       dark one: lay the tinted feather over itself, then let a trace of the
       white strokes back in so the barbs still read. */
    g.globalCompositeOperation = 'source-over'
    g.drawImage(c, 0, 0)
    g.drawImage(c, 0, 0)
    g.globalCompositeOperation = 'source-atop'
    g.globalAlpha = 0.07
    g.drawImage(src, 0, 0)
  }
  return c
}

/* A seeded generator, so each of the five feathers is the same feather on
   every page load. */
function seeded(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6D2B79F5) >>> 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/* One white feather, quill down: a curved rachis, a filled vane softened
   under some 250 barbs a side, a downy base and a split or two. The canvas
   is wider than the feather (1.02 x its length) because a strongly bent
   feather's barbs would otherwise be cut along a straight edge. */
function drawFeather(seed: number, L: number): HTMLCanvasElement {
  const R = seeded(seed)
  const U = Math.round(L * 0.66)
  const W = Math.round(L * 1.02)
  const H = Math.round(L * 1.04)
  const k = L / 220
  const bend = (R() < 0.5 ? -1 : 1) * (0.08 + R() * 0.16)
  const P = (t: number): [number, number] =>
    [W * 0.5 + bend * U * Math.sin(Math.PI * t * 0.9) + (t - 0.5) * U * 0.05, H * 0.985 - t * H * 0.95]
  const T = (t: number): [number, number] => {
    const a = P(Math.max(0, t - 0.01))
    const b = P(Math.min(1, t + 0.01))
    const dx = b[0] - a[0]
    const dy = b[1] - a[1]
    const n = Math.hypot(dx, dy)
    return [dx / n, dy / n]
  }
  const env = (t: number): number => {
    if (t < 0.1) return 0
    const u = (t - 0.1) / 0.9
    return U * 0.44 * Math.pow(Math.sin(Math.PI * Math.min(1, u * 1.03)), 0.62) * (1 - 0.28 * u)
  }
  const splits: Array<[number, number, number]> = [
    [0.42 + R() * 0.3, 0.05, R() < 0.5 ? -1 : 1],
    [0.55 + R() * 0.3, 0.04, R() < 0.5 ? -1 : 1],
  ]

  const vane = canvasOf(W, H)
  const v = vane.getContext('2d')!
  v.fillStyle = 'rgba(255, 255, 255, .34)'
  for (const side of [-1, 1]) {
    const sf = side < 0 ? 0.8 : 1
    const [bx, by] = P(0.1)
    v.beginPath()
    v.moveTo(bx, by)
    for (let i = 0; i <= 60; i++) {
      const t = 0.1 + (0.9 * i) / 60
      const [px, py] = P(t)
      const [tx, ty] = T(t)
      const w = env(t) * sf * 0.9
      v.lineTo(px - ty * side * w + tx * w * 0.45, py + tx * side * w + ty * w * 0.45)
    }
    const [qx, qy] = P(1)
    v.lineTo(qx, qy)
    v.closePath()
    v.fill()
  }
  blurWhite(vane, 3 * k)

  const c = canvasOf(W, H)
  const g = c.getContext('2d')!
  g.lineCap = 'round'
  g.drawImage(vane, 0, 0)
  for (const side of [-1, 1]) {
    const sf = side < 0 ? 0.74 + R() * 0.1 : 1
    for (let i = 0; i < 250; i++) {
      const t = 0.1 + (0.9 * (i + R() * 0.6)) / 250
      const [px, py] = P(t)
      const [tx, ty] = T(t)
      const nx = -ty * side
      const ny = tx * side
      const down = t < 0.3
      let a = down ? 1.05 + R() * 0.55 : 0.82 + (R() - 0.5) * 0.14 - t * 0.12
      let len = ((down ? env(Math.max(t, 0.2)) * 1.25 : env(t)) * sf) / Math.max(0.55, Math.sin(a))
      for (const [c0, w, s] of splits) {
        if (s === side && Math.abs(t - c0) < w) { a += t < c0 ? -0.32 : 0.26; len *= 0.88 }
      }
      len *= 0.84 + R() * 0.3
      if (!down && R() < 0.06) len *= 1.25
      const dx = tx * Math.cos(a) + nx * Math.sin(a)
      const dy = ty * Math.cos(a) + ny * Math.sin(a)
      const ex = px + dx * len
      const ey = py + dy * len
      g.beginPath()
      g.moveTo(px, py)
      if (down) {
        const w1 = (R() - 0.5) * len * 0.5
        const w2 = (R() - 0.5) * len * 0.5
        g.bezierCurveTo(px + dx * len * 0.35 + nx * w1, py + dy * len * 0.35 + ny * w1,
          px + dx * len * 0.7 + tx * w2, py + dy * len * 0.7 + ty * w2, ex + tx * len * 0.15, ey + ty * len * 0.15)
        g.strokeStyle = `rgba(255, 255, 255, ${0.14 + R() * 0.14})`
        g.lineWidth = (0.6 + R() * 0.5) * k
      } else {
        g.quadraticCurveTo(px + dx * len * 0.55 + tx * len * 0.14, py + dy * len * 0.55 + ty * len * 0.14,
          ex + tx * len * 0.08, ey + ty * len * 0.08)
        g.strokeStyle = `rgba(255, 255, 255, ${0.36 + R() * 0.3})`
        g.lineWidth = (0.8 + R() * 0.55) * k
      }
      g.stroke()
    }
  }
  for (let i = 0; i < 70; i++) {
    const t = 0.04 + R() * 0.24
    const [px, py] = P(t)
    const side = R() < 0.5 ? -1 : 1
    const [tx, ty] = T(t)
    const len = U * (0.18 + R() * 0.22)
    const a = 0.9 + R() * 1.4
    const dx = tx * Math.cos(a) - ty * side * Math.sin(a)
    const dy = ty * Math.cos(a) + tx * side * Math.sin(a)
    g.beginPath()
    g.moveTo(px, py)
    g.bezierCurveTo(px + dx * len * 0.4 + (R() - 0.5) * len * 0.5, py + dy * len * 0.4 + (R() - 0.5) * len * 0.5,
      px + dx * len * 0.8, py + dy * len * 0.8, px + dx * len, py + dy * len)
    g.strokeStyle = `rgba(255, 255, 255, ${0.08 + R() * 0.12})`
    g.lineWidth = (0.5 + R() * 0.5) * k
    g.stroke()
  }
  for (let i = 0; i < 40; i++) {
    const a = P(i / 40)
    const b = P((i + 1) / 40)
    g.beginPath()
    g.moveTo(a[0], a[1])
    g.lineTo(b[0], b[1])
    g.strokeStyle = `rgba(255, 255, 255, ${i < 4 ? 0.55 : 0.92})`
    g.lineWidth = (2.6 - (2.1 * i) / 40) * k
    g.stroke()
  }

  /* A soft copy under the crisp one: the barbs keep their line and lose
     the hard pixel edge. */
  const soft = canvasOf(W, H)
  soft.getContext('2d')!.drawImage(c, 0, 0)
  blurWhite(soft, Math.max(1, 0.7 * k))
  const s = soft.getContext('2d')!
  s.globalAlpha = 0.75
  s.drawImage(c, 0, 0)
  return soft
}

export interface FeatherSprites {
  layer(spr: number, tier: Tier, theme: FeatherTheme): HTMLCanvasElement
  glow(spr: number, theme: FeatherTheme): HTMLCanvasElement
}

const SCALE = SPRITE_LENGTH / 220
const BLUR: Record<Exclude<Tier, 'sharp'>, number> = { soft: 2.4 * SCALE, far: 6.5 * SCALE }
const GLOW_BLUR = 13 * SCALE
let built: FeatherSprites | null | undefined

/** The page's feather sprites, built on first use. The white tiers are made
 *  up front; the light theme's ink copies and each theme's glow on the first
 *  frame that needs them, so a page that never turns light never pays for
 *  ink. `null` where there is no 2D canvas. `make` supplies the offscreen
 *  canvases; the first call's maker is the one the cached sprites use. */
export function featherSprites(make: CanvasMaker): FeatherSprites | null {
  if (built !== undefined) return built
  canvasOf = make
  if (!canvasOf(1, 1).getContext('2d')) return (built = null)
  const white = SEEDS.map((seed) => {
    const sharp = drawFeather(seed, SPRITE_LENGTH)
    return {
      sharp,
      soft: padded(sharp, BLUR.soft),
      far: padded(sharp, BLUR.far),
    } as Record<Tier, HTMLCanvasElement>
  })
  const ink = new Map<string, HTMLCanvasElement>()
  const glows = new Map<string, HTMLCanvasElement>()
  built = {
    layer(spr, tier, theme) {
      const base = white[spr]![tier]
      const tint = LOOKS[theme].tint
      if (!tint) return base
      const key = `${spr}:${tier}`
      let c = ink.get(key)
      if (!c) { c = tintOf(base, tint, true); ink.set(key, c) }
      return c
    },
    glow(spr, theme) {
      const key = `${spr}:${theme}`
      let c = glows.get(key)
      if (!c) { c = tintOf(padded(white[spr]!.sharp, GLOW_BLUR), LOOKS[theme].glow, false); glows.set(key, c) }
      return c
    },
  }
  return built
}

/** One frame of the stream, in CSS pixels (the caller sets the device-pixel
 *  transform). Nearer feathers are larger, sharper and more opaque; each
 *  sways, tilts and turns over as it falls, never narrower than `flipMin`. */
export function drawFrame(
  ctx: CanvasRenderingContext2D, fs: readonly Feather[], spec: StreamSpec, theme: FeatherTheme,
  t: number, sprites: FeatherSprites, w: number, h: number,
): void {
  const look = LOOKS[theme]
  ctx.clearRect(0, 0, w, h)
  const rate = spec.swayRate
  for (const f of [...fs].sort((a, b) => a.z - b.z)) {
    const tier: Tier = f.z < 0.28 ? 'far' : f.z < 0.55 ? 'soft' : 'sharp'
    const x = f.x + Math.sin((t * rate * f.sf * TAU) / 3 + f.sp) * f.sway
    const rot = f.rot + Math.sin((t * rate * f.rf * TAU) / 3 + f.sp) * f.ra
    const flip = Math.cos((t * rate * f.ff * TAU) / 3 + f.fp)
    const sx = (flip < 0 ? -1 : 1) * (spec.flipMin + (1 - spec.flipMin) * Math.abs(flip))
    const sc = f.size / SPRITE_LENGTH
    const alpha = (0.35 + 0.65 * f.z) * Math.max(0, Math.min(1, (f.y + f.size) / (f.size * 0.9)))
    if (alpha <= 0) continue
    ctx.save()
    ctx.translate(x, f.y)
    ctx.rotate(rot)
    ctx.scale(sx * sc, sc)
    const glow = sprites.glow(f.spr, theme)
    ctx.globalAlpha = alpha * spec.glow * look.glowAlpha * (0.5 + 0.5 * f.z)
    ctx.drawImage(glow, -glow.width / 2, -glow.height / 2 + (look.shadow ? SPRITE_LENGTH * 0.07 : 0))
    const img = sprites.layer(f.spr, tier, theme)
    ctx.globalAlpha = alpha
    ctx.drawImage(img, -img.width / 2, -img.height / 2)
    ctx.restore()
  }
}
