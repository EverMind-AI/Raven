/* Keeps uploaded image bytes available to the composer and transcript renderers. */

const images = new Map<string, string>()

export const get = (path: string): string | undefined => images.get(String(path))

export function set(path: string, url: string): void {
  images.set(String(path), url)
}

export function _resetForTests(): void {
  images.clear()
}
