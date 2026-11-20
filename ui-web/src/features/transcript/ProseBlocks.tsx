/* A prose box drawn block by block.
 *
 * A streamed reply repaints on every frame, and setting the whole box's
 * markup each time re-parses and re-lays-out everything above the line being
 * written -- for a long answer with a coloured listing in it, thousands of
 * elements a frame -- and drops any selection the reader was making in it.
 * The reply only grows at its end, so this keeps the nodes of every block
 * whose markup is unchanged and redraws from the first block that differs.
 *
 * The blocks go straight into the box, unwrapped: the stylesheet reads them as
 * the box's own children (`.prose > :first-child`), so the tree is the one a
 * single `innerHTML` would have built. React renders the box and never its
 * children, which are this component's to keep.
 */

import { useLayoutEffect, useRef } from 'react'

import type { ReactElement } from 'react'

interface Drawn {
  html: string
  nodes: ChildNode[]
}

export function ProseBlocks({ blocks, className }: { blocks: string[]; className: string }): ReactElement {
  const box = useRef<HTMLDivElement | null>(null)
  const drawn = useRef<Drawn[]>([])
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    const was = drawn.current
    let keep = 0
    while (keep < was.length && keep < blocks.length && was[keep]!.html === blocks[keep]) keep++
    for (const block of was.slice(keep)) for (const node of block.nodes) node.remove()
    const now = was.slice(0, keep)
    for (const html of blocks.slice(keep)) {
      const tpl = document.createElement('template')
      tpl.innerHTML = html
      const nodes = [...tpl.content.childNodes]
      el.append(...nodes)
      now.push({ html, nodes })
    }
    drawn.current = now
  }, [blocks])
  return <div ref={box} className={className} />
}
