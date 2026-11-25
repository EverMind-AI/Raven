/* The title row every module page starts with.
 *
 * The page's name, what the page offers beside it (a filter, the tabs over its
 * lists), and the actions pushed to the far end. The four module pages
 * (state/pages.ts) used to draw this row each in their own way -- one with a
 * heading, two with none, at three different heights -- so switching pages on
 * the rail moved the top of the page. One component, one row, and the page
 * frame they sit in is the task board's (scripts/gates/module-page-frame-css).
 *
 * `page-bar*` classes are this file's (scripts/check-class-namespace.mjs); the
 * rules are in src/styles/page.css beside the module page frame.
 */

import type { JSX, ReactNode } from 'react'

export function PageBar({ title, children, end }: {
  readonly title: string
  readonly children?: ReactNode
  readonly end?: ReactNode
}): JSX.Element {
  return (
    <div className="page-bar">
      <h2 className="page-bar-title">{title}</h2>
      {children}
      {end ? <div className="page-bar-end">{end}</div> : null}
    </div>
  )
}
