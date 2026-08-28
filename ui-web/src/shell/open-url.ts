/* Open a web URL on the reader's host, or copy a non-URL value. */

import { t } from './bridge'
import { show as toast } from './toast'

export function open(value: string): void {
  if (/^https?:\/\//.test(value)) {
    window.open(value, '_blank', 'noopener')
    return
  }
  navigator.clipboard.writeText(String(value)).then(
    () => toast(t('gui.ws.copy_path')),
    () => toast(String(value)),
  )
}
