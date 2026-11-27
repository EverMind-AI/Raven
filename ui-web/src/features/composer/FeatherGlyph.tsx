/* The waiting row's glyph: a canvas of feathers falling one after another,
   white with a cool glow on the dark theme, ink with a soft shadow on the
   light one (lib/featherFall.ts draws them).

   CSS cannot stop a canvas, so this component reads what the page's motion
   rule reads: the appearance setting (`data-motion` on the root, state/look.ts)
   and the system's reduced-motion preference. Either one parks the stream on a
   single still frame. A hidden tab parks it too, and nothing is scheduled once
   the row is gone. */

import { useEffect, useRef, type ReactElement } from 'react'

import {
  drawFrame, featherSprites, featherTheme, motionAllowed, seedStream, stepStream, TAIL_STREAM,
} from '../../lib/featherFall'

/* The engine's offscreen canvases, made here because the engine keeps out of
   the document (lib/ takes its elements as arguments). */
const makeCanvas = (w: number, h: number): HTMLCanvasElement =>
  Object.assign(document.createElement('canvas'), { width: w, height: h })

/* The canvas's CSS box (page.css `.cp-feather`), used until layout reports one. */
const BOX_W = 40
const BOX_H = 46

export function FeatherGlyph(): ReactElement {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    const ctx = canvas?.getContext('2d')
    const sprites = featherSprites(makeCanvas)
    if (!canvas || !ctx || !sprites) return undefined
    const root = document.documentElement
    const darkQ = matchMedia('(prefers-color-scheme: dark)')
    const calmQ = matchMedia('(prefers-reduced-motion: reduce)')
    let w = BOX_W
    let h = BOX_H
    let frame = 0
    let last = 0
    let t = 0
    let ratio = 0

    const size = (): void => {
      const box = canvas.getBoundingClientRect()
      const dpr = window.devicePixelRatio || 1
      ratio = dpr
      w = box.width || BOX_W
      h = box.height || BOX_H
      const bw = Math.round(w * dpr)
      const bh = Math.round(h * dpr)
      /* Resizing a canvas clears it, so only when the backing size changed. */
      if (canvas.width !== bw || canvas.height !== bh) { canvas.width = bw; canvas.height = bh }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    size()
    const fs = seedStream(TAIL_STREAM, w, h)
    /* Zoom and a move to another display change the ratio but not the CSS
       box, so no resize is reported: every paint checks it. */
    const draw = (): void => {
      if ((window.devicePixelRatio || 1) !== ratio) size()
      drawFrame(ctx, fs, TAIL_STREAM, featherTheme(root, darkQ.matches), t, sprites, w, h)
    }

    const tick = (now: number): void => {
      const dt = last ? Math.min(0.05, (now - last) / 1000) : 0
      last = now
      t += dt
      stepStream(fs, TAIL_STREAM, w, h, dt)
      draw()
      frame = requestAnimationFrame(tick)
    }
    const stop = (): void => {
      if (frame) cancelAnimationFrame(frame)
      frame = 0
      last = 0
    }
    /* Also the theme listener: a parked stream is redrawn at once in the
       new theme, a running one picks it up on its next frame. */
    const sync = (): void => {
      const live = motionAllowed(root, calmQ.matches) && !document.hidden
      if (live) {
        if (!frame) frame = requestAnimationFrame(tick)
        return
      }
      stop()
      if (!document.hidden) draw()
    }
    sync()
    const watch = new MutationObserver(sync)
    watch.observe(root, { attributes: true, attributeFilter: ['data-theme', 'data-motion'] })
    darkQ.addEventListener('change', sync)
    calmQ.addEventListener('change', sync)
    document.addEventListener('visibilitychange', sync)
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => { size(); if (!frame) draw() })
    resize?.observe(canvas)
    return () => {
      stop()
      watch.disconnect()
      darkQ.removeEventListener('change', sync)
      calmQ.removeEventListener('change', sync)
      document.removeEventListener('visibilitychange', sync)
      resize?.disconnect()
    }
  }, [])

  return <canvas ref={ref} className="cp-feather" aria-hidden="true" />
}
