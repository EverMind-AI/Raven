// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Small VT screen model for integration tests. It implements the cursor,
// erase, and scroll operations emitted by hermes-ink, plus enough SGR state to
// answer what colour and weight each written cell ended up with -- a paint bug
// that puts the right characters on screen in the wrong style is invisible to a
// character-only model. Hyperlink and terminal-mode sequences still do not
// affect cell content.

import { stringWidth } from '@hermes/ink'

const CSI_FINAL = /[@-~]/

/** The SGR state a cell was written under, as a comparable string. */
export type CellStyle = string

const SGR_NONE: CellStyle = ''

export class TerminalScreen {
  private cells: string[][]
  private styles: CellStyle[][]
  private sgr = { bg: '', bold: false, dim: false, fg: '', inverse: false, underline: false }
  // DECAWM pending wrap: set after writing into the last column, cleared by the
  // next print (which wraps first) or by any cursor movement.
  private pendingWrap = false
  private col = 0
  private row = 0
  private savedCol = 0
  private savedRow = 0
  private scrollBottom: number
  private scrollTop = 0

  constructor(
    public columns: number,
    public rows: number
  ) {
    this.cells = this.blankScreen()
    this.styles = this.blankStyles()
    this.scrollBottom = rows - 1
  }

  write(input: string) {
    let i = 0

    while (i < input.length) {
      const char = input[i]!

      if (char === '\x1b') {
        i = this.escape(input, i)
        continue
      }

      if (char === '\r') {
        this.col = 0
        this.pendingWrap = false
      } else if (char === '\n') {
        this.lineFeed()
        this.pendingWrap = false
      } else if (char === '\b') {
        this.col = Math.max(0, this.col - 1)
        this.pendingWrap = false
      } else if (char >= ' ') {
        const codePoint = input.codePointAt(i)!
        const glyph = String.fromCodePoint(codePoint)

        this.put(glyph)
        i += glyph.length
        continue
      }

      i++
    }
  }

  text() {
    return this.cells.map(line => line.join('').trimEnd()).join('\n')
  }

  resize(columns: number, rows: number) {
    const previous = this.cells

    this.columns = columns
    this.rows = rows
    const previousStyles = this.styles

    this.cells = this.blankScreen()
    this.styles = this.blankStyles()

    for (let row = 0; row < Math.min(rows, previous.length); row++) {
      for (let col = 0; col < Math.min(columns, previous[row]!.length); col++) {
        this.cells[row]![col] = previous[row]![col]!
        this.styles[row]![col] = previousStyles[row]?.[col] ?? SGR_NONE
      }
    }

    this.row = Math.min(this.row, rows - 1)
    this.col = Math.min(this.col, columns - 1)
    this.scrollTop = 0
    this.scrollBottom = rows - 1
  }

  private blankLine() {
    return Array.from({ length: this.columns }, () => ' ')
  }

  private blankScreen() {
    return Array.from({ length: this.rows }, () => this.blankLine())
  }

  private blankStyles() {
    return Array.from({ length: this.rows }, () => this.blankStyleLine())
  }

  private blankStyleLine(): CellStyle[] {
    return Array.from({ length: this.columns }, () => SGR_NONE)
  }

  /** Active SGR as a stable string, so two cells can be compared directly. */
  private currentStyle(): CellStyle {
    const s = this.sgr

    return [
      s.fg && `fg:${s.fg}`,
      s.bg && `bg:${s.bg}`,
      s.bold && 'bold',
      s.dim && 'dim',
      s.inverse && 'inverse',
      s.underline && 'underline'
    ]
      .filter(Boolean)
      .join(',')
  }

  /** The SGR the terminal is left in right now, between writes.
   *
   *  A frame is supposed to hand the terminal back with no style active: the
   *  next frame computes its transitions assuming that. Anything else here at
   *  a frame boundary leaks into whatever is written next. */
  activeStyle(): CellStyle {
    return this.currentStyle()
  }

  /** The SGR the cell at (x, y) was written under. */
  styleAt(col: number, row: number): CellStyle {
    return this.styles[row]?.[col] ?? SGR_NONE
  }

  /** Rows of `[glyph, style]` for every non-blank cell, for assertions. */
  styledRow(row: number): Array<[string, CellStyle]> {
    const line = this.cells[row] ?? []

    return line.map((glyph, col) => [glyph, this.styleAt(col, row)] as [string, CellStyle])
  }

  /** Apply an SGR sequence's params to the active state. */
  private applySgr(params: number[]) {
    const s = this.sgr

    for (let i = 0; i < params.length; i++) {
      const p = params[i]!

      if (p === 0) {
        s.bg = ''
        s.bold = false
        s.dim = false
        s.fg = ''
        s.inverse = false
        s.underline = false
      } else if (p === 1) {
        s.bold = true
      } else if (p === 2) {
        s.dim = true
      } else if (p === 4) {
        s.underline = true
      } else if (p === 7) {
        s.inverse = true
      } else if (p === 22) {
        s.bold = false
        s.dim = false
      } else if (p === 24) {
        s.underline = false
      } else if (p === 27) {
        s.inverse = false
      } else if (p === 39) {
        s.fg = ''
      } else if (p === 49) {
        s.bg = ''
      } else if (p === 38 || p === 48) {
        // 38;5;n (256) or 38;2;r;g;b (truecolor)
        const mode = params[i + 1]
        const take = mode === 5 ? 2 : mode === 2 ? 4 : 0
        const value = params.slice(i + 1, i + 1 + take).join(';')

        if (p === 38) {
          s.fg = value
        } else {
          s.bg = value
        }

        i += take
      } else if ((p >= 30 && p <= 37) || (p >= 90 && p <= 97)) {
        s.fg = String(p)
      } else if ((p >= 40 && p <= 47) || (p >= 100 && p <= 107)) {
        s.bg = String(p)
      }
    }
  }

  private escape(input: string, start: number) {
    const kind = input[start + 1]

    if (kind === '[') {
      let end = start + 2

      while (end < input.length && !CSI_FINAL.test(input[end]!)) {
        end++
      }

      if (end >= input.length) {
        return input.length
      }

      this.csi(input.slice(start + 2, end), input[end]!)

      return end + 1
    }

    if (kind === ']') {
      let end = start + 2

      while (end < input.length && input[end] !== '\x07' && !(input[end] === '\x1b' && input[end + 1] === '\\')) {
        end++
      }

      return input[end] === '\x1b' ? end + 2 : Math.min(input.length, end + 1)
    }

    if (kind === '7') {
      this.savedRow = this.row
      this.savedCol = this.col
    } else if (kind === '8') {
      this.row = this.savedRow
      this.col = this.savedCol
    }

    return Math.min(input.length, start + 2)
  }

  private csi(raw: string, command: string) {
    const privateMode = raw.startsWith('?')
    const params = raw
      .replace(/^[?>!]/, '')
      .split(';')
      .map(value => (value === '' ? 0 : Number(value)))
    const n = Math.max(1, params[0] ?? 1)

    if (privateMode && (command === 'h' || command === 'l')) {
      if (command === 'h' && params.includes(1049)) {
        this.cells = this.blankScreen()
        this.styles = this.blankStyles()
        this.row = 0
        this.col = 0
      }

      return
    }

    // Only a bare `CSI ... m` is SGR. The private/extended forms share the
    // final byte but set terminal features, not styles: `CSI > 4 ; 2 m` is
    // xterm's modifyOtherKeys, and reading its `2` as SGR 2 latches a dim this
    // terminal never had -- which then reads as a paint bug in every cell
    // written afterwards.
    if (command === 'm') {
      if (/^[?>!<=]/.test(raw)) {
        return
      }

      this.applySgr(raw === '' ? [0] : raw.split(';').map(v => (v === '' ? 0 : Number(v))))

      return
    }

    // Every remaining CSI either moves the cursor or erases; both cancel a
    // pending wrap. SGR and private-mode sets returned above and do not.
    this.pendingWrap = false

    switch (command) {
      case 'A':
        this.row = Math.max(0, this.row - n)
        break
      case 'B':
        this.row = Math.min(this.rows - 1, this.row + n)
        break
      case 'C':
        this.col = Math.min(this.columns - 1, this.col + n)
        break
      case 'D':
        this.col = Math.max(0, this.col - n)
        break
      case 'E':
        this.row = Math.min(this.rows - 1, this.row + n)
        this.col = 0
        break
      case 'F':
        this.row = Math.max(0, this.row - n)
        this.col = 0
        break
      case 'G':
        this.col = Math.max(0, Math.min(this.columns - 1, n - 1))
        break
      case 'H':
      case 'f':
        this.row = Math.max(0, Math.min(this.rows - 1, (params[0] || 1) - 1))
        this.col = Math.max(0, Math.min(this.columns - 1, (params[1] || 1) - 1))
        break
      case 'J':
        this.eraseDisplay(params[0] ?? 0)
        break
      case 'K':
        this.eraseLine(params[0] ?? 0)
        break
      case 'S':
        this.scrollUp(n)
        break
      case 'T':
        this.scrollDown(n)
        break
      case 'd':
        this.row = Math.max(0, Math.min(this.rows - 1, n - 1))
        break
      case 'r':
        this.scrollTop = Math.max(0, (params[0] || 1) - 1)
        this.scrollBottom = Math.min(this.rows - 1, (params[1] || this.rows) - 1)
        this.row = 0
        this.col = 0
        break
      case 's':
        this.savedRow = this.row
        this.savedCol = this.col
        break
      case 'u':
        this.row = this.savedRow
        this.col = this.savedCol
        break
    }
  }

  private put(glyph: string) {
    const width = stringWidth(glyph)

    if (width === 0) {
      if (this.col > 0) {
        this.cells[this.row]![this.col - 1] += glyph
      }

      return
    }

    // Deferred wrap (DECAWM): writing into the last column leaves the cursor
    // ON that column with a pending-wrap flag rather than moving to the next
    // row. Only the NEXT printable character wraps. Getting this wrong drifts
    // the model a row against the real terminal, because the renderer steers
    // with relative moves (CUU/CUD) from wherever the cursor actually is --
    // and a row of drift makes every style comparison after it meaningless.
    if (this.pendingWrap) {
      this.pendingWrap = false
      this.col = 0
      this.lineFeed()
    }

    // A wide glyph that cannot fit the remaining columns moves down whole.
    if (this.col + width > this.columns) {
      this.col = 0
      this.lineFeed()
    }

    this.cells[this.row]![this.col] = glyph
    this.styles[this.row]![this.col] = this.currentStyle()

    for (let offset = 1; offset < width && this.col + offset < this.columns; offset++) {
      this.cells[this.row]![this.col + offset] = ' '
      this.styles[this.row]![this.col + offset] = this.currentStyle()
    }

    this.col += width

    if (this.col >= this.columns) {
      this.col = this.columns - 1
      this.pendingWrap = true
    }
  }

  private lineFeed() {
    if (this.row === this.scrollBottom) {
      this.scrollUp(1)
    } else {
      this.row = Math.min(this.rows - 1, this.row + 1)
    }
  }

  private scrollUp(count: number) {
    for (let i = 0; i < count; i++) {
      this.cells.splice(this.scrollTop, 1)
      this.cells.splice(this.scrollBottom, 0, this.blankLine())
      this.styles.splice(this.scrollTop, 1)
      this.styles.splice(this.scrollBottom, 0, this.blankStyleLine())
    }
  }

  private scrollDown(count: number) {
    for (let i = 0; i < count; i++) {
      this.cells.splice(this.scrollBottom, 1)
      this.cells.splice(this.scrollTop, 0, this.blankLine())
      this.styles.splice(this.scrollBottom, 1)
      this.styles.splice(this.scrollTop, 0, this.blankStyleLine())
    }
  }

  private eraseDisplay(mode: number) {
    if (mode === 2 || mode === 3) {
      if (mode === 2) {
        this.cells = this.blankScreen()
        this.styles = this.blankStyles()
      }

      return
    }

    if (mode === 0) {
      this.eraseLine(0)

      for (let row = this.row + 1; row < this.rows; row++) {
        this.cells[row] = this.blankLine()
        this.styles[row] = this.blankStyleLine()
      }
    } else {
      this.eraseLine(1)

      for (let row = 0; row < this.row; row++) {
        this.cells[row] = this.blankLine()
        this.styles[row] = this.blankStyleLine()
      }
    }
  }

  private eraseLine(mode: number) {
    const start = mode === 0 ? this.col : 0
    const end = mode === 1 ? this.col : this.columns - 1

    for (let col = start; col <= end; col++) {
      this.cells[this.row]![col] = ' '
      // Erase paints with the active background, so the cleared cell carries
      // the current style -- that is how a bg leak shows up on screen.
      this.styles[this.row]![col] = this.currentStyle()
    }
  }
}
