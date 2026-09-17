/* The page-wide clipboard action and its success notice. */

import { show as toast } from '../state/toast'

export function copy(text: string, done: string): void {
  if (!navigator.clipboard) return
  void navigator.clipboard.writeText(String(text)).then(() => toast(done), () => {})
}
