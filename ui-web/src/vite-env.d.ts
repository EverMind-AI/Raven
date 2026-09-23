/* The one build-time flag the page reads.
 *
 * Vite substitutes `import.meta.env.PROD` at build time, and the project
 * compiles with `types: []` (tsconfig.json) so that no ambient package typing
 * is in scope -- including vite/client, which would also declare module
 * shapes for every asset extension. This is the whole of what is needed.
 */
interface ImportMeta {
  readonly env: { readonly PROD: boolean }
}
