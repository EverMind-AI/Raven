/* -- the embedded browser: the rpc source -----------------------------
   The renderer is the browser island (ui-web/src/features/browser/); this
   module owns what only a live gateway can answer: the browser.* calls, and
   the screencast frames the gateway pushes -- decoded here and forwarded
   through the source's onFrame hook the island subscribes to.

   Two frame paths, both registered: a current gateway sends a binary frame,
   an older one notifies the same page state with the JPEG base64 inside it,
   and a page cannot know which it will be sent until one arrives. */

import type { ParamsOf } from '../../rpc/generated'
import type { BrowserFramePushParams } from '../../rpc/notifications'
import type { ChromiumSource, FrameHead } from './types'

import { gateway } from '../../rpc/gateway'
import { islands } from '../registry'

const b64Blob = (b64: string): Blob => {
  const s = atob(b64)
  const u = new Uint8Array(s.length)
  for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i)
  return new Blob([u], { type: 'image/jpeg' })
}

/* Where the island's own subscription lands. Held here rather than read back
   off the source object so the two decoders below have one hook to reach. */
export const browserSource: ChromiumSource & { openUrl(u: string): void } = {
  embedded: true,
  urls: () => islands.workspace.urls(),
  /* Not on ChromiumSource: the links variant declares it and this one carried
     it too, so a caller holding the union could reach it either way. */
  openUrl: (u: string) => islands.chrome.openUrl(u),
  frame: (p) => gateway().call('browser.frame', p),
  open: (p) => gateway().call('browser.open', p),
  watch: (p) => gateway().call('browser.watch', p),
  mode: (p) => gateway().call('browser.mode', p),
  close: () => gateway().call('browser.close', {}),
  tabs: (p) => gateway().call('browser.tabs', p),
  input: (p) => gateway().call('browser.input', p as unknown as ParamsOf<'browser.input'>),
  onFrame: null,
}

/* Screencast frames arrive as binary WS messages:
   "RVF1" + u32 header length + JSON header + raw JPEG. */
export function onFrameBytes(buf: ArrayBuffer): void {
  const u8 = new Uint8Array(buf)
  if (u8.length < 8 || u8[0] !== 0x52 || u8[1] !== 0x56 || u8[2] !== 0x46 || u8[3] !== 0x31) return
  const hl = new DataView(buf).getUint32(4)
  let head: FrameHead
  try { head = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + hl))) as FrameHead } catch { return }
  if (browserSource.onFrame) browserSource.onFrame(head, new Blob([u8.subarray(8 + hl)], { type: 'image/jpeg' }))
}

/* Old servers still notify frames as base64 JSON; same hook after decode.
   Takes the raw frame, like every other push handler: the transport hands
   every notification over as `unknown` and the name is what says the shape. */
export function onFrameJson(frame: unknown): void {
  const p = frame as BrowserFramePushParams
  if (browserSource.onFrame) browserSource.onFrame(p as FrameHead, p.jpeg ? b64Blob(p.jpeg) : null)
}
