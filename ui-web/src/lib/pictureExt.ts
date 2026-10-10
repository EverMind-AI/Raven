/* The extensions a file name is a picture by: one table, read by both domains
 * that ask the question -- the workspace viewer (features/workspace's fileKind)
 * and the composer (whether a staged file draws as a picture, and the name an
 * unnamed image upload goes up under) -- so the tray and the sent bubble cannot
 * drift apart about a name. SVG is not here: the viewer gives it its own kind
 * (fileKind returns 'svg', which the source toggle reads), and the composer
 * adds it to its own picture-name set.
 *
 * A small table two domains share, which is what puts it in lib/
 * (ui-web/CONTRIBUTING.md).
 */
export const IMG_EXT: ReadonlySet<string> = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'avif',
])
