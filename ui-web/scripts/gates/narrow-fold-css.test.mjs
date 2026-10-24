/* The shell's fold under 640px wins the cascade, and the number is one number.
 *
 * Below the fold the window holds one column and a standing rail is an overlay
 * (styles/page.css, the `@media (max-width: 639px)` block). Two of that block's
 * rules override declarations the sheet makes again further down -- the grid's
 * tracks (`.app`) and the module pages' inset (`.page`) -- and both base rules
 * come later at the same weight, so an override spelled with the same selector
 * is simply lost, and the main pane goes back into the second track beside an
 * empty first one whenever the overlay rail stands, which is the squeeze the
 * block exists to end. No DOM test can see a cascade, so the weight
 * is pinned here, computed rather than string-matched.
 *
 * The fold's width lives twice on purpose -- the stylesheet cannot read a
 * TypeScript constant and the boot cannot read a media block -- and this is
 * where the two are held to one value.
 *
 * The body's 780px floor lifts below 780, and the first-run wizard is the one
 * surface that drew a floor of its own: a fixed frame the boot opens over
 * everything, whose model step has no Skip and no close. Left at 780 in a
 * 390px window, its Next sat past the window's edge and a first run could not
 * finish. Its sheet lifts that floor at the body's width, and its narrow block
 * restacks the settings bodies it embeds, whose rules live in another sheet
 * concatenated in an order neither owns -- so there too the weight is what
 * wins, and is held here.
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { css, decls, rules } from './css.mjs'

const FOLD = '(max-width: 639px)'

const sheet = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8')

/* Every block under the query, each body by balanced braces: css.mjs's own
   matcher sees the inner rules and not the at-rule around them. More than one
   block may carry the width -- a rule that has to follow its base rule to win
   by order (the hub head's gap) sits beside that rule, not up here. */
function mediaBlocks(query, source = css) {
  const bodies = []
  let from = 0
  for (;;) {
    const open = source.indexOf(`@media ${query} {`, from)
    if (open < 0) return bodies
    const brace = source.indexOf('{', open)
    let depth = 0
    for (let i = brace; i < source.length; i++) {
      if (source[i] === '{') depth += 1
      else if (source[i] === '}' && --depth === 0) {
        bodies.push(source.slice(brace + 1, i))
        from = i
        break
      }
    }
    if (depth !== 0) throw new Error(`unbalanced @media ${query} block`)
  }
}

/* The fold itself: the block that sets the grid's tracks. */
function foldBlock() {
  const blocks = mediaBlocks(FOLD).filter((body) => /grid-template-columns/.test(body))
  expect(blocks.length, 'one block folds the grid').toBe(1)
  return blocks[0]
}

/* Selector weight as the cascade counts it: ids, then classes / attributes /
   pseudo-classes, then elements. `:root` is a pseudo-class; `:has()`, `:is()`
   and `:not()` weigh what their argument does, which the count below already
   reads, so their own names are not counted again. */
function weight(selector) {
  const ids = (selector.match(/#[\w-]+/g) || []).length
  const classes = (selector.match(/\.[\w-]+|\[[^\]]*\]|:(?!:)(?!(?:has|is|not)\()[\w-]+/g) || []).length
  const elements = (selector.replace(/[#.:[][^\s>+~#.:[]*/g, '').match(/[a-zA-Z][\w-]*/g) || []).length
  return ids * 100 + classes * 10 + elements
}

const overrides = (property) =>
  rules(null, foldBlock()).filter(([, body]) => new RegExp(`(^|;)\\s*${property}\\s*:`).test(body))

describe('the fold under 640px', () => {
  it('names the width state/rail.ts folds on, and no other width near it', () => {
    const rail = readFileSync(new URL('../../src/state/rail.ts', import.meta.url), 'utf8')
    expect(rail).toContain(`FOLDS = '${FOLD}'`)
    /* A second number in the six hundreds would be a fold that drifted from
       the one the boot and the rail watch. */
    const near = [...css.matchAll(/@media\s*\((max-width:\s*6\d\dpx)\)/g)].map((m) => `(${m[1]})`)
    expect(near.length).toBeGreaterThan(0)
    expect(new Set(near)).toEqual(new Set([FOLD]))
    foldBlock()
  })

  it.each([
    ['grid-template-columns', '.app'],
    ['left', '.page'],
  ])('outweighs the later base rule for %s on %s', (property, base) => {
    const found = overrides(property)
    expect(found.length, `an override of ${property} in the fold block`).toBeGreaterThan(0)
    for (const [selector] of found) {
      expect(weight(selector), `${selector} against ${base}, which the sheet declares again below the block`)
        .toBeGreaterThan(weight(base))
    }
  })
})

/* The body's floor and the width it lifts at, read from the body rule rather
   than written down a second time. */
const FLOOR = decls('body').get('min-width')
const LIFT = `(max-width: ${parseInt(FLOOR, 10) - 1}px)`
const onboard = sheet('features/onboard/styles.css')
const settings = sheet('features/settings/styles.css')

/* The wizard's narrow block: the one in its sheet under the body's lift. */
function wizardBlock() {
  const blocks = mediaBlocks(LIFT, onboard)
  expect(blocks.length, `one ${LIFT} block in features/onboard/styles.css`).toBe(1)
  return rules(null, blocks[0])
}

/* The override in the wizard's block that sets `property` on a selector
   naming every one of `parts`, as [selector, value]. */
function wizardSets(property, ...parts) {
  const hits = []
  for (const [selector, body] of wizardBlock()) {
    for (const one of selector.split(',').map((x) => x.trim())) {
      if (!parts.every((part) => one.includes(part))) continue
      const value = new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`).exec(body)
      if (value) hits.push([one, value[1].trim()])
    }
  }
  expect(hits.length, `the wizard's narrow block sets ${property} on ${parts.join(' ')}`).toBe(1)
  return hits[0]
}

/* One declaration of the wizard's narrow block, by a selector it lists exactly. */
function wizardRule(selector, property) {
  const hits = []
  for (const [head, body] of wizardBlock()) {
    if (!head.split(',').map((x) => x.trim()).includes(selector)) continue
    const value = new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`).exec(body)
    if (value) hits.push(value[1].trim())
  }
  expect(hits.length, `the wizard's narrow block sets ${property} on ${selector}`).toBe(1)
  return hits[0]
}

/* A settings rule's selectors and one declaration, by a selector it lists. */
function settingsRule(selector, property) {
  for (const [head, body] of rules(null, settings)) {
    if (!head.split(',').map((x) => x.trim()).includes(selector)) continue
    const value = new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`).exec(body)
    if (value) return value[1].trim()
  }
  return null
}

describe('the first-run wizard under the body floor', () => {
  it('lifts its own floor at the width the body floor lifts', () => {
    expect(FLOOR).toBe('780px')
    expect(mediaBlocks(LIFT).some((body) => /\bbody\s*\{\s*min-width:\s*0;?\s*\}/.test(body))).toBe(true)
    expect(decls('.ob-app', onboard).get('min-width'), 'the frame draws a floor of its own, the body floor\'s width').toBe(FLOOR)
    expect(wizardBlock().some(([selector, body]) => selector === '.ob-app' && /min-width:\s*0\s*;?\s*$/.test(body.trim()))).toBe(true)
    /* A floor-lift at any other width would leave a band where the body
       folds and the wizard does not. */
    const lifts = [...onboard.matchAll(/@media\s*\((max-width:\s*\d+px)\)/g)].map((m) => `(${m[1]})`)
    expect(new Set(lifts)).toEqual(new Set([LIFT]))
  })

  /* The base rules below are why a 390px window ran past its edge: a 132px
     label column beside 280px boxes in the add form, and a 320px pill beside a
     role's label. Stacked, the control takes the column, and the column is the
     window less the wizard's gutters. */
  it.each([
    ['grid-template-columns', ['.settings-padd', '.settings-row'], '.settings-cfg.settings-padd .settings-row', 'minmax(0, 1fr)'],
    ['grid-template-columns', ['.settings-row', '.settings-mpill'], '.settings-row', 'minmax(0, 1fr)'],
    ['justify-self', ['.settings-padd', '.settings-ctl'], '.settings-cfg.settings-padd .settings-row .settings-ctl', 'stretch'],
    ['width', ['.settings-padd', '.settings-vpick'], '.settings-cfg.settings-padd .settings-vpick', '100%'],
    ['width', ['.settings-padd', '.keyfield'], '.settings-cfg.settings-padd .keyfield', '100%'],
    ['width', ['.settings-padd', '.settings-ctl > .settings-tbox'], '.settings-cfg.settings-padd .settings-ctl > .settings-tbox', '100%'],
  ])('sets %s on %j over the settings rule %s', (property, parts, base, want) => {
    expect(settingsRule(base, property), `${base} still declares ${property} in features/settings/styles.css`).not.toBe(null)
    const [selector, value] = wizardSets(property, ...parts)
    expect(selector.startsWith('.ob-app ')).toBe(true)
    expect(value).toBe(want)
    expect(weight(selector), `${selector} against ${base}`).toBeGreaterThan(weight(base))
  })

  /* The page sheet's select wrapper is an inline box, so a 100% vendor box
     inside it is 100% of its own shrink-to-fit width: the select's 140px. */
  it('makes the vendor box\'s wrapper a block, over the page sheet\'s inline one', () => {
    expect(decls('.selw').get('display')).toBe('inline-block')
    const [selector, value] = wizardSets('display', '.settings-padd', '.selw')
    expect(value).toBe('block')
    expect(weight(selector)).toBeGreaterThan(weight('.selw'))
  })

  /* Same sheet, same weight: the narrow block wins by coming after the base
     rules. At their 160px the two ends left the step strip no width at all. */
  it.each(['.ob-brand', '.ob-lang'])('lets %s give its desktop width up to the step strip', (selector) => {
    expect(decls(selector, onboard).get('min-width')).toBe('160px')
    expect(wizardSets('min-width', selector)).toEqual([selector, '0'])
    expect(onboard.indexOf(`@media ${LIFT} {`)).toBeGreaterThan(onboard.indexOf(`${selector} {`))
  })

  /* With the ends gone the strip is what gives: a label that will not wrap
     has to shrink inside its item, or it runs under the language button. A
     later step's item grows from a zero basis, so it would give first even
     when it is the current step; it holds its content's width then, and the
     current label never shrinks. */
  it('lets the step labels other than the current one shrink to an ellipsis', () => {
    expect(decls('.ob-sitem', onboard).get('white-space')).toBe('nowrap')
    expect(decls('.ob-sitem + .ob-sitem', onboard).get('flex')).toBe('1')
    expect(wizardRule('.ob-sitem', 'min-width')).toBe('0')
    const label = '.ob-sitem:not([data-state="current"]) > span:last-child'
    expect(wizardRule(label, 'overflow')).toBe('hidden')
    expect(wizardRule(label, 'text-overflow')).toBe('ellipsis')
    expect(wizardRule('.ob-sitem + .ob-sitem[data-state="current"]', 'min-width')).toBe('auto')
    expect(weight('.ob-sitem + .ob-sitem[data-state="current"]')).toBeGreaterThan(weight('.ob-sitem + .ob-sitem'))
    expect(wizardRule('.ob-dot', 'flex')).toBe('none')
  })

  /* A box narrower than the ellipsis gets no ellipsis: Chromium draws the
     first letter clipped in half, which read as half an "M" at 320px. Under
     its floor the label wraps onto a second line the one-line item clips, and
     the item keeps the width its connector and dot need. */
  it('folds a label away rather than give it less than its ellipsis floor', () => {
    const item = '.ob-sitem:not([data-state="current"])'
    const label = `${item} > span:last-child`
    expect(wizardRule(label, 'min-width')).toBe('2.5em')
    expect(wizardRule(label, 'flex')).toBe('1000 1 2.5em')
    expect(wizardRule(label, 'max-width')).toBe('max-content')
    expect(wizardRule(item, 'flex-wrap')).toBe('wrap')
    expect(wizardRule(item, 'overflow')).toBe('hidden')
    expect(wizardRule(item, 'height')).toBe('22px')
    expect(wizardRule(item, 'min-width')).toBe('20px')
    const connector = wizardRule('.ob-sitem + .ob-sitem::before', 'min-width')
    const [, side] = /^0\s+(\d+)px$/.exec(wizardRule('.ob-sitem + .ob-sitem::before', 'margin'))
    const gap = parseInt(decls('.ob-sitem', onboard).get('gap'), 10)
    const dot = parseInt(decls('.ob-dot', onboard).get('width'), 10)
    expect(wizardRule(`.ob-sitem + ${item}`, 'min-width')).toBe(`${parseInt(connector, 10) + 2 * Number(side) + gap + dot}px`)
  })

  it('shortens the connector below its desktop minimum', () => {
    expect(decls('.ob-sitem + .ob-sitem::before', onboard).get('min-width')).toBe('34px')
    expect(wizardRule('.ob-sitem + .ob-sitem::before', 'min-width')).toBe('12px')
  })

  it('leaves a stacked pill free to take the column rather than its 320px', () => {
    expect(settingsRule('.settings-mpill', 'width')).toBe('320px')
    expect(settingsRule('.settings-mpill', 'max-width')).toBe('100%')
  })
})
