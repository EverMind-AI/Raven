/* The one way into this page from outside the island.
 *
 * A file of its own because it is the only member another feature may import:
 * opening a page is navigation, not data, and the rest of the store stays the
 * island's. The settings dialog's "entrances" rows and the nav flyout both
 * come through here.
 */


import { closeDialog, refresh } from './store'
import * as page from '../../state/page'

export function open(): void {
  closeDialog()
  page.show('connectionsPage')
  void refresh(true)
}
