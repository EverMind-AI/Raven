/* The row drawer and the standalone box, read off the settings sheet.
 *
 * `.settings-cfg` is both: on its own it is the panel a row opens under itself
 * (no edges of its own, a rail tying it to that row, controls aligned to the
 * row's own control edge), and paired with `.settings-padd` it is the
 * add-a-provider form, which keeps a box. The two differ on padding,
 * background, the rail and the control alignment, and every one of those
 * declarations is a single class against a single class -- so the pair's rules
 * only win by naming both classes AND sitting later in the sheet. They did
 * neither at first, and the form silently took the drawer's 31px indent, its
 * transparent ground and its right-aligned controls. jsdom applies no
 * stylesheet, so the component tests cannot see any of it.
 */
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import { decls, rules } from './css.mjs'

const sheet = readFileSync(new URL('../../src/features/settings/styles.css', import.meta.url), 'utf8')
const at = (selector) => {
  const i = sheet.indexOf(`\n${selector} {`)
  if (i < 0) throw new Error(`no rule for ${selector}`)
  return i
}

describe('the settings row drawer', () => {
  it('has no edges of its own and hangs its content off a rail', () => {
    const cfg = decls('.settings-cfg', sheet)
    expect(cfg.get('border')).toBeUndefined()
    expect(cfg.get('border-radius')).toBeUndefined()
    expect(cfg.get('background')).toBe('transparent')
    /* The rail is absolute, so the drawer must be its containing block. */
    expect(cfg.get('position')).toBe('relative')
    const rail = decls('.settings-cfg::before', sheet)
    expect(rail.get('position')).toBe('absolute')
    expect(rail.get('width')).toBe('2px')
    /* Content clears the rail: the rail sits at the row's own 15px gutter. */
    expect(cfg.get('padding')).toContain('31px')
    expect(rail.get('left')).toBe('15px')
  })

  it('lines its controls up with the control edge of the row above, not a second column', () => {
    expect(decls('.settings-cfg .settings-row', sheet).get('grid-template-columns')).toBe('minmax(0,1fr) auto')
    expect(decls('.settings-cfg .settings-row .settings-ctl', sheet).get('justify-self')).toBe('end')
  })

  it('shows the chevron as a mark that turns, not a control in a box', () => {
    const chev = decls('.settings-xrow .settings-chev', sheet)
    expect(chev.get('border')).toBeUndefined()
    expect(chev.get('background')).toBeUndefined()
    expect(chev.get('border-radius')).toBeUndefined()
    expect(decls('.settings-xrow.settings-open .settings-chev', sheet).get('transform')).toBe('rotate(90deg)')
  })

  it('takes the separator off the row it opens under, of either kind', () => {
    /* The role row carries no separator until a drawer opens under it, which is
       the moment it stops being its wrapper's :last-child; the tool and plugin
       rows carry one all along and hand it to the drawer below. */
    expect(decls('.settings-row.settings-open,.settings-xrow.settings-open', sheet).get('border-bottom')).toBe('0')
    expect(decls('.settings-xrow.settings-open + .settings-cfg', sheet).get('border-bottom')).toContain('1px')
    expect(decls('.settings-xrow.settings-open + .settings-cfg:last-child', sheet).get('border-bottom')).toBe('0')
  })
})

describe('what a drawer sizes', () => {
  it('gives every control in it one size, a notch under the dialog', () => {
    /* The dialog's own scale is 32px and 13px (page.css's `.smodal .mini`), and
       these are the page's controls, reached into where this domain embeds
       them -- so they are named unprefixed on purpose. */
    const one = decls('.settings-cfg .mini,.settings-cfg .sel,.settings-cfg .settings-tbox,.settings-cfg .settings-seg', sheet)
    expect(one.get('height')).toBe('28px')
    expect(one.get('font-size')).toBe('12px')
    expect(decls('.settings-cfg .settings-stepper', sheet).get('height')).toBe('28px')
  })

  it('sizes classes the markup actually carries', () => {
    /* `.settings-cfg .settings-mini` sized the drawer's buttons for as long as
       the drawer existed and never matched anything: every button on this page
       is the page's own `.mini`. Nothing said so -- the button just stayed the
       page size inside a panel built to be one notch smaller. */
    const dir = new URL('../../src/features/settings/', import.meta.url)
    const words = new Set()
    const walk = (at) => {
      for (const entry of readdirSync(at, { withFileTypes: true })) {
        const path = join(at, entry.name)
        if (entry.isDirectory()) { walk(path); continue }
        if (!entry.name.endsWith('.tsx') || entry.name.endsWith('.test.tsx')) continue
        for (const quoted of readFileSync(path, 'utf8').matchAll(/'([^'\n]*)'|"([^"\n]*)"|`([^`\n]*)`/g)) {
          for (const word of (quoted[1] ?? quoted[2] ?? quoted[3] ?? '').split(/[\s{}]+/)) if (word) words.add(word)
        }
      }
    }
    walk(new URL('.', dir).pathname)
    const unused = []
    for (const [selector] of rules(/^\.settings-cfg\s+\./, sheet)) {
      for (const part of selector.split(',')) {
        const found = part.trim().match(/^\.settings-cfg\s+\.([A-Za-z0-9_-]+)/)
        if (found && !words.has(found[1])) unused.push(`${selector}: no markup carries .${found[1]}`)
      }
    }
    expect(unused).toEqual([])
  })
})

describe('a section label', () => {
  it('does not lend its bold to the controls that sit in it', () => {
    /* Every button on the page inherits its weight, and `Sec` seats its action
       inside the bold label -- so "Add model" came out as bold as "Models",
       on every build since the two were put on one line. */
    expect(decls('.settings-lab', sheet).get('font-weight')).toBe('600')
    expect(decls('.settings-lab .mini,.settings-lab .settings-sub2,.settings-tp-name .exlink', sheet).get('font-weight')).toBe('400')
  })
})

describe('a provider page', () => {
  it('sizes every field as the row less one button slot, and stands the button at the right edge', () => {
    /* Sized by content the boxes came out 186 and 208; filling their row made
       the one box with no button beside it wider than the rest; either way the
       buttons formed a second column left of "Add model". */
    const field = decls('.settings-tp-main .settings-sec .settings-tbox,.settings-tp-main .settings-sec .keyfield,\n.settings-tp-main .settings-sec .selw', sheet)
    expect(field.get('width')).toBe('calc(100% - var(--tp-act) - 6px)')
    expect(field.get('flex')).toBe('none')
    expect(decls('.settings-tp-main .settings-taglist:has(> .settings-tbox) > .mini,.settings-tp-main .settings-taglist:has(> .keyfield) > .mini,\n.settings-tp-main .settings-taglist:has(> .selw) > .mini', sheet).get('margin-left')).toBe('auto')
  })
})

describe('the add-a-provider box', () => {
  it('names both classes on every rule that corrects the drawer', () => {
    for (const [selector, prop, value] of [
      ['.settings-cfg.settings-padd', 'padding', '6px 14px 8px'],
      ['.settings-cfg.settings-padd', 'background', 'var(--paper)'],
      ['.settings-cfg.settings-padd .settings-row', 'grid-template-columns', '132px minmax(0,1fr)'],
      ['.settings-cfg.settings-padd .settings-row .settings-ctl', 'justify-self', 'start'],
    ]) {
      expect(decls(selector, sheet).get(prop)).toBe(value)
    }
    expect(decls('.settings-cfg.settings-padd', sheet).get('border')).toContain('1px')
    expect(decls('.settings-cfg.settings-padd::before', sheet).get('display')).toBe('none')
  })

  it('sits after the drawer it corrects, which is the half specificity cannot supply', () => {
    /* Equal-specificity pairs -- the two ::before rules -- are decided by order
       alone, so this is not redundant with naming both classes above. */
    expect(at('.settings-cfg.settings-padd')).toBeGreaterThan(at('.settings-cfg'))
    expect(at('.settings-cfg.settings-padd::before')).toBeGreaterThan(at('.settings-cfg::before'))
  })

  it('shapes every row, not only the last one', () => {
    /* `:last-child` was how the box used to hold its label column, which left
       a multi-row form taking the drawer's shape on every row but the last. */
    expect(decls('.settings-cfg.settings-padd .settings-row:last-child', sheet)).toBeNull()
  })
})
