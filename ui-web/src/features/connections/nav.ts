/* The one way into this page from outside the island.
 *
 * A file of its own because it is the only member another feature may import:
 * opening a page is navigation, not data, and the rest of the store stays the
 * island's. The settings dialog's "entrances" rows and the nav flyout both
 * come through here.
 */

import { shell } from '../../shell/bridge'

import { closeDialog, refresh } from './store'

export function open(): void {
  closeDialog()
  shell().showPage('connPage')
  void refresh(true)
}
