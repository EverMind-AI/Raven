/* An API key field: masked by default, with an eye to read back what you typed.
 *
 * Every key in the app goes through here rather than each page spelling out its
 * own `type="password"`. A key is pasted more often than typed and mistyped
 * more often than either, and a field that can never be read is a field you
 * cannot check before pressing Connect -- but one that starts readable puts a
 * credential on screen for anyone walking past. Masked with a way out is the
 * only combination that serves both.
 *
 * A plain input underneath, so a caller keeps handing it a ref and reading
 * `.value` the way it always did.
 */

import { forwardRef, useState } from 'react'

import { t } from './bridge'

import type { InputHTMLAttributes, JSX } from 'react'

/* Open and struck through. Two drawings rather than one with a line toggled on
   top: the struck eye has to break around the stroke to read as struck rather
   than as an eye with something in front of it. */
function EyeMark({ open }: { open: boolean }): JSX.Element {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      {open ? (
        <>
          <path d="M1.6 8s2.4-4.2 6.4-4.2S14.4 8 14.4 8s-2.4 4.2-6.4 4.2S1.6 8 1.6 8z" />
          <circle cx="8" cy="8" r="1.9" />
        </>
      ) : (
        <>
          <path d="M6.3 3.9A6.9 6.9 0 018 3.8c4 0 6.4 4.2 6.4 4.2a12 12 0 01-2.2 2.7" />
          <path d="M9.9 9.9A2 2 0 016.1 8.1" />
          <path d="M4.4 5.1A11.7 11.7 0 001.6 8s2.4 4.2 6.4 4.2c.85 0 1.63-.19 2.33-.5" />
          <path d="M2.6 2.6l10.8 10.8" />
        </>
      )}
    </svg>
  )
}

export const KeyInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function KeyInput({ className, ...rest }, ref): JSX.Element {
    const [shown, setShown] = useState(false)
    const label = t(shown ? 'gui.key.hide' : 'gui.key.show')
    return (
      <span className="keyfield">
        <input
          ref={ref}
          type={shown ? 'text' : 'password'}
          autoComplete="off"
          spellCheck={false}
          className={className}
          {...rest}
        />
        <button
          type="button"
          className="peek"
          aria-pressed={shown}
          aria-label={label}
          title={label}
          /* Not a tab stop: a key field is tabbed into and typed, and a control
             between it and the button that saves it interrupts that. */
          tabIndex={-1}
          onClick={() => setShown((v) => !v)}
        >
          <EyeMark open={shown} />
        </button>
      </span>
    )
  },
)
