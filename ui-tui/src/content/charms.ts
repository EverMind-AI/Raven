/* The long-run tool phrases, shared with the GUI: ui-shared/charms.json is the
   source and scripts/gen-shared.mjs projects it here. The TUI appends one to
   the activity stream as `phrase (tool · elapsed)`; the page shows the phrase
   alone beside the tool row's own clock, so one elapsed time is not printed
   twice. */
export { LONG_RUN_CHARMS } from './charms.generated.js'
