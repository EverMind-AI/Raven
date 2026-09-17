/* The two hand-offs src/main.tsx makes, for a case that drives the desk.
 *
 * features/desk/store imports both of the stores below back and
 * subscribes to one as it evaluates, so neither of them may import it: the page
 * hands the desk's three openers over instead (setAgentPane, setDeskOpener). A
 * case that replays an open into a pane needs the same wiring, and reaching for
 * the page's entry point is not open to it -- src/main.tsx is the whole page.
 */
import { setAgentPane } from '../features/subagents/store'
import { openDeskAgent, openDeskAgentRecord, openDeskFile } from '../features/desk/store'
import { setDeskOpener } from '../features/workspace/store'

export function installDeskHandoff(): void {
  setAgentPane({ openAgent: openDeskAgent, openAgentRecord: openDeskAgentRecord })
  setDeskOpener(openDeskFile)
}
