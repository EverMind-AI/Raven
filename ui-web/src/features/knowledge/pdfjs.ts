/* pdf.js, fetched when a document is first opened rather than bundled.
 *
 * This page ships as one classic script inlined into one HTML file -- the
 * contract `ui-web/build.py` keeps and the wheel depends on -- so there is no
 * code splitting to hide a megabyte behind. Bundled, pdf.js would roughly
 * double the file every visit to every part of Raven pays for, to draw a
 * preview on one panel. It would also not run: an IIFE build emits no worker
 * asset and rewrites the `import.meta.url` pdf.js resolves its own parts with.
 *
 * So the library stays a library. `build.py` copies its two files out of
 * node_modules into `dist/assets/pdfjs/`, the gateway already serves that
 * directory at `/assets`, and the import below is a real one the browser makes
 * the first time someone opens a PDF.
 */

/* Absolute, because this page is served from one path and the preview can be
   opened from any route the shell has pushed. */
const LIBRARY = '/assets/pdfjs/pdf.min.mjs'
const WORKER = '/assets/pdfjs/pdf.worker.min.mjs'

/* The slice this tree uses. Named here rather than imported from the package:
   nothing imports the package at build time any more, so its types are not in
   scope, and a structural type keeps the viewer honest about what it calls. */
export interface Pdfjs {
  getDocument(options: { url: string }): { promise: Promise<PdfDocument> }
  GlobalWorkerOptions: { workerSrc: string }
}

export interface PdfDocument {
  numPages: number
  getPage(n: number): Promise<PdfPage>
}

export interface PdfPage {
  getViewport(options: { scale: number }): { width: number; height: number }
  render(options: { canvas: HTMLCanvasElement; viewport: unknown }): { promise: Promise<void> }
}

let held: Promise<Pdfjs> | null = null

/* The library, loaded once per page. Held as the promise rather than the
   module so two panels opening at the same moment share one fetch instead of
   racing two. */
export function loadPdfjs(): Promise<Pdfjs> {
  held ??= import(/* @vite-ignore */ LIBRARY).then((module: Pdfjs) => {
    module.GlobalWorkerOptions.workerSrc = WORKER
    return module
  })
  return held
}
