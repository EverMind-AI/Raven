/* The shape of a rendered tree, and nothing else: one line per element with
 * its tag, id, classes and data-* attributes, indented by depth. Text, inline
 * styles and event handlers are left out on purpose -- this is the surface the
 * refactor promises not to move, and a golden of it is what lets a test say a
 * region still renders the same DOM after its owner changed.
 *
 * `script` and `style` elements are skipped because the build changes how
 * many of them a page carries, and neither is part of what a reader sees.
 */
export function domSnapshot(root: Element): string {
  const lines: string[] = []
  const walk = (node: Element, depth: number): void => {
    const tag = node.tagName.toLowerCase()
    if (tag === 'script' || tag === 'style') return
    const parts = [tag]
    if (node.id) parts.push(`#${node.id}`)
    const classes = (node.getAttribute('class') ?? '').trim()
    if (classes) parts.push(`.${classes.split(/\s+/).join('.')}`)
    const data = node
      .getAttributeNames()
      .filter((name) => name.startsWith('data-'))
      .sort()
    for (const name of data) parts.push(`[${name}=${node.getAttribute(name) ?? ''}]`)
    lines.push(`${'  '.repeat(depth)}${parts.join('')}`)
    for (const child of Array.from(node.children)) walk(child, depth + 1)
  }
  for (const child of Array.from(root.children)) walk(child, 0)
  return lines.join('\n')
}
