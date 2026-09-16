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

const skipped = (node: Element): boolean => {
  const tag = node.tagName.toLowerCase()
  return tag === 'script' || tag === 'style'
}

/** One element's own line, in the format `domSnapshot` writes for it. */
const signature = (node: Element): string => {
  const parts = [node.tagName.toLowerCase()]
  if (node.id) parts.push(`#${node.id}`)
  const classes = (node.getAttribute('class') ?? '').trim()
  if (classes) parts.push(`.${classes.split(/\s+/).join('.')}`)
  const data = node
    .getAttributeNames()
    .filter((name) => name.startsWith('data-'))
    .sort()
  for (const name of data) parts.push(`[${name}=${node.getAttribute(name) ?? ''}]`)
  return parts.join('')
}

/* The body's direct children, one signature each, in document order.
 *
 * What this pins is the standing order of the page's top-level regions and of
 * everything mounted beside them at the body: two of the `--z` steps are ties
 * broken by that order alone, so the order is a contract of its own and not a
 * by-product of the region snapshots below.
 */
export function bodySiblings(doc: Document = document): string[] {
  return Array.from(doc.body.children)
    .filter((child) => !skipped(child))
    .map(signature)
}

/* One element and its subtree, unlike `domSnapshot`, which walks a root's
 * children and leaves the root itself out. A region's own tag, id and data-*
 * are part of what may not move, so they have to be in the golden.
 */
export function elementSnapshot(root: Element): string {
  if (skipped(root)) return ''
  const head = signature(root)
  const inner = domSnapshot(root)
  if (!inner) return head
  return `${head}\n${inner.split('\n').map((line) => `  ${line}`).join('\n')}`
}

/** `elementSnapshot` of the element carrying `id`, looked up under `root`. */
export function regionSnapshot(root: Document | Element, id: string): string {
  const found = 'getElementById' in root ? root.getElementById(id) : root.querySelector(`#${id}`)
  if (!found) throw new Error(`regionSnapshot: no element with id "${id}"`)
  return elementSnapshot(found)
}
