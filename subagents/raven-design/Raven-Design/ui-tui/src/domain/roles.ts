import type { Theme } from '../theme.js'
import type { Role } from '../types.js'

export const ROLE: Record<Role, (t: Theme) => { body: string; glyph: string; prefix: string }> = {
  assistant: t => ({ body: t.color.text, glyph: t.brand.tool, prefix: t.color.muted }),
  system: t => ({ body: '', glyph: '·', prefix: t.color.muted }),
  tool: t => ({ body: t.color.muted, glyph: '⚡', prefix: t.color.muted }),
  // The user's own words get their own tier: an accent chevron plus full-weight
  // text, so a prompt reads as the turn's heading instead of blending into the
  // dim reasoning/tool activity lines (which also used `label`).
  user: t => ({ body: t.color.text, glyph: t.brand.prompt, prefix: t.color.accent })
}
