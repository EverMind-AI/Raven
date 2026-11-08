// The rail row's height contract, read straight off the stylesheet. jsdom does
// not do layout, so the component test cannot see any of this -- and the whole
// class of bug being locked down here is geometric: a session row whose title
// is still being generated used to be 4.95px shorter than its neighbours, and
// 11.27px shorter while the pointer was on it.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../../src/styles/page.css', import.meta.url), 'utf8')

const rule = (selector) => {
  const at = css.indexOf(`\n${selector} {`)
  if (at < 0) throw new Error(`no rule for ${selector}`)
  return css.slice(at, css.indexOf('}', at))
}
const oneLiner = (selector) => {
  const line = css.split('\n').find((l) => l.startsWith(`${selector} {`))
  if (!line) throw new Error(`no rule for ${selector}`)
  return line
}

describe('the rail row CSS contract', () => {
  it('makes the title slot own the row height', () => {
    /* Not the timestamp: hover replaces the stamp with the pin/archive pair, so
       a row leaning on the stamp for its height lost it exactly when the
       pointer arrived. 1lh is the slot's own line height, so a bar-only slot is
       as tall as one carrying text without a number to keep in sync by hand. */
    const t = rule('.sess .t')
    expect(t).toContain('min-height: 1lh')
    /* em fallback first, for engines without the lh unit. Presence before
       order: indexOf returns -1 when the string is absent, and -1 sits below
       any real index, so an ordering assertion on its own passes with the
       fallback deleted. */
    expect(t).toContain('min-height: 1.65em')
    expect(t.indexOf('min-height: 1.65em')).toBeLessThan(t.indexOf('min-height: 1lh'))
  })

  it('takes the stamp away only when the actions come up to replace it', () => {
    /* The two halves of one gesture, and they used to answer to different
       conditions. A row is a div[role=button][tabindex=0], so CLICKING a
       session leaves the ROW focused -- `.sess:focus-within` hid the stamp
       there, while the actions, which answer to focus inside `.quick`, stayed
       away. The session you had just opened was the one row in the rail
       carrying no time, with a gap of nothing where it had been.

       Read off the stylesheet because this is a cascade fact: jsdom does no
       layout and computes no :focus-within, so the component test cannot see
       it, and a real browser only shows it while something holds focus. */
    const hideStamp = css.split('\n').filter((l) => /^\.sess.*\.w \{ display: none/.test(l));
    expect(hideStamp).toHaveLength(1);
    expect(hideStamp[0]).not.toContain('.sess:focus-within');
    expect(hideStamp[0]).toContain('.sess:has(.quick:focus-within) .w');
    /* The room made for them moves on the same condition, or the title reflows
       without the buttons arriving. */
    const padTitle = css.split('\n').filter((l) => /^\.sess.*\.t \{ padding-right: \d+px/.test(l));
    expect(padTitle).toHaveLength(1);
    expect(padTitle[0]).not.toContain('.sess:focus-within');
    expect(padTitle[0]).toContain('.sess:has(.quick:focus-within) .t');
    /* And that condition is the one that raises them. */
    const raise = css.split('\n').find((l) => l.startsWith('.sess:hover .quick,'));
    expect(raise).toBeTruthy();
    expect(raise).toContain('.sess .quick:focus-within');
  });

  it('sizes the naming placeholder in pixels, once, for every row', () => {
    /* A percentage resolves against the title slot, and the slot's width
       depends on the neighbouring timestamp -- "yesterday" and a full date gave
       the same state two bars 35px apart, and hover moved either by 8px. */
    const bar = oneLiner('.sess .t.skel .sk')
    expect(bar).toContain('width: 116px')
    expect(bar).toContain('flex: none')
    expect(bar).not.toContain('%')
  })

  it('keeps the shimmer off the surfaces it has to sit on', () => {
    /* The bar's base used to be --surface, which IS the rail's ground: at rest
       it was the colour behind it, and on a hovered row (--raised) it inverted
       and matched the shimmer's own peak. A wash over whatever is behind it
       reads on every ground and in both themes. */
    const sk = rule('.skel .sk')
    expect(sk).toContain('color-mix(in oklab, var(--text)')
    expect(sk).not.toContain('var(--surface)')
    expect(sk).not.toContain('var(--raised)')
  })

  it('keeps a name being renamed at the size it is shown at', () => {
    /* The field and its width mirror both take the wrapper's font, so the
       wrapper is the one place the size is set: sizing the field alone would
       leave the mirror measuring the text at another size than it is drawn. */
    const row = rule('.navi, .sess').match(/font-size: ([^;]+);/)[1]
    expect(rule('.sess .rensize')).toContain(`font-size: ${row};`)
    expect(rule('.sess .rensize::after,\n.sess .ren')).toContain('font: inherit')
  })

  it('gives the boot placeholder rows a real row height, with the same hedge', () => {
    /* 28px was a rounded-down guess at the 32.27px a text row measures, so the
       list grew when the real sessions replaced these. The em declaration is
       not decoration: an unknown unit inside calc() invalidates the whole
       value, so on an engine without lh this rule would be dropped entirely
       and the row would fall to its content height -- about 21px, shorter than
       the 28px being replaced. */
    const boot = rule('.sess.skel')
    expect(boot).toContain('min-height: calc(1.65em + 10px)')
    expect(boot).toContain('min-height: calc(1lh + 10px)')
    expect(boot.indexOf('calc(1.65em')).toBeLessThan(boot.indexOf('calc(1lh'))
  })

  it('stops the rename column at the row and scrolls a long name in the field', () => {
    /* The mirror is unwrappable text, so its automatic minimum is its whole
       length, and an `auto` column never shrinks below that: the wrapper's
       max-width held its own box at the row while the column, and the field
       stretched across it, ran on past it -- a 526px field in a 223px title
       slot, its edge clipped and its caret drawn outside the rail. Waiving the
       mirror's minimum is what lets the column stop; clipping it keeps the text
       that runs on from giving the wrapper something to scroll. */
    const mirror = rule('.sess .rensize::after')
    expect(mirror).toContain('min-width: 0')
    expect(mirror).toContain('overflow: hidden')
    expect(mirror).toContain('white-space: pre')
    /* The bounds the column now stops at, and the floor a short name keeps. */
    const box = rule('.sess .rensize')
    expect(box).toContain('min-width: 6ch')
    expect(box).toContain('max-width: 100%')
    /* The field is the scroller now; clipping it would take its caret-follow.
       Its own rule, not the one it shares with the mirror. */
    const at = css.indexOf('}\n.sess .ren {')
    expect(at).toBeGreaterThan(0)
    const field = css.slice(at + 1, css.indexOf('}', at + 1))
    expect(field).toContain('min-width: 0')
    expect(field).not.toContain('overflow')
  })
})
