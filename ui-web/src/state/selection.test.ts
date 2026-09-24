// @vitest-environment happy-dom
/* A selection that starts in the transcript ends in the transcript. */
import { afterEach, describe, expect, it } from 'vitest'

import { clamp } from './selection'

function page(): { first: Element; last: Element; field: Element } {
  document.body.innerHTML = [
    '<div class="scroll" id="scroll">',
    '<p id="first">first line</p>',
    '<p id="blank">   </p>',
    '<p id="last">last line</p>',
    '</div>',
    '<div class="dock"><div class="field"><textarea id="ta"></textarea></div></div>',
  ].join('')
  return {
    first: document.getElementById('first')!,
    last: document.getElementById('last')!,
    field: document.getElementById('ta')!,
  }
}

/** A selection from inside the transcript to a node outside it. */
function select(from: Element, to: Element): Selection {
  const selection = document.getSelection()!
  const range = document.createRange()
  range.setStart(from.firstChild!, 0)
  range.setEnd(to, 0)
  selection.removeAllRanges()
  selection.addRange(range)
  return selection
}

afterEach(() => {
  document.getSelection()?.removeAllRanges()
  document.body.innerHTML = ''
})

describe('the selection clamp', () => {
  /* A paragraph selection ends at the next selectable position in document
     order, which for the last transcript line is the composer -- and every
     empty box on the way got a selection rect of its own. */
  it('pulls an end that ran past the transcript back to its last text', () => {
    const { first, last, field } = page()
    const selection = select(first, field)
    clamp()
    const range = selection.getRangeAt(0)
    expect(range.endContainer).toBe(last.firstChild)
    expect(range.endOffset).toBe('last line'.length)
    /* And the start is untouched: the reader chose it. */
    expect(range.startContainer).toBe(first.firstChild)
  })

  /* Idempotent by construction, which is what keeps it from looping on the
     selectionchange it causes. */
  it('leaves a selection that already ends inside the transcript alone', () => {
    const { first, last } = page()
    const selection = select(first, last)
    const before = selection.getRangeAt(0).endContainer
    clamp()
    expect(selection.getRangeAt(0).endContainer).toBe(before)
    clamp()
    expect(selection.getRangeAt(0).endContainer).toBe(before)
  })

  it('leaves a selection that starts outside the transcript alone', () => {
    const { field } = page()
    const outside = document.querySelector('.field')!
    const selection = document.getSelection()!
    const range = document.createRange()
    range.setStart(outside, 0)
    range.setEnd(field, 0)
    selection.removeAllRanges()
    selection.addRange(range)
    clamp()
    expect(selection.getRangeAt(0).startContainer).toBe(outside)
    expect(selection.getRangeAt(0).endContainer).toBe(field)
  })

  it('does nothing without a selection, and nothing without a transcript', () => {
    page()
    expect(() => clamp()).not.toThrow()
    document.body.innerHTML = '<p id="loose">text</p>'
    select(document.getElementById('loose')!, document.body)
    expect(() => clamp()).not.toThrow()
  })
})
