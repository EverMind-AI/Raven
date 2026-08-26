/** Whether the desk palette is open, remembered per conversation.

The default is not one answer, because the two screens are not one screen: a
conversation shows the desk, and the new-task screen does not. Nothing has been
written, delivered or delegated on a draft yet, so a palette there is three
empty lists over an empty composer -- while a conversation the reader returns to
is exactly where the desk earns its place.

What the reader does themselves outranks both. A collapse is filed under the
conversation it was made in and read back the next time that one is opened, so
the desk stays out of the way where it was put away and stays up where it was
wanted -- rather than one global flag that made the last conversation's decision
for every other one.

`localStorage`, unlike the layout notes in shell/persist.ts: this is a
preference the reader stated, not a record of what was on screen, and it has to
survive the tab being closed to mean what they meant by it.
*/

const KEY = 'raven.gui.desk.open'

/* Session rows, coldest dropped. A boolean per conversation is nothing to
   store, but a machine left open for a month should not carry a row for every
   conversation ever opened in it either. Same shape and the same reason as the
   composer's draft cap. */
const CAP = 50

interface Row {
  at: number
  open: boolean
}

type Kept = Record<string, Row>

/* The reader's own answer about the new-task screen, and null until they give
   one: a draft has no id to file under.
 *
 * Held across drafts on purpose: it is their last word about that screen rather
 * than a fact about one draft, so a reader who opened the desk on the new-task
 * screen does not have to open it again on the next one. That direction is the
 * whole of it -- clearing the answer would be invisible to a reader who put the
 * desk AWAY, since cleared and put-away both read as shut and shut is this
 * screen's default. Spent by `adopt` when a draft turns into a conversation,
 * and a conversation started from that screen is exactly what the answer was
 * about.
 *
 * Nothing else spends it, which is the line that matters: a conversation the
 * reader OPENED -- from the rail, by forking, by opening a cron run -- is found
 * as that conversation was left, never as the new-task screen was left. See
 * deskStore.claimDraft.
 *
 * Not stored, unlike a conversation's answer: "leave the desk out of my way in
 * this conversation" is worth keeping across reloads, "I was starting something
 * and did not want it then" is not, and a fresh page starts from the default. */
let draft: boolean | null = null

function read_(): Kept {
  try {
    /* An older build stored a single global boolean under this same name. It
       does not parse as a map, so it reads as "nothing stored" and the first
       write replaces it. */
    const value: unknown = JSON.parse(localStorage.getItem(KEY) || 'null')
    return value && typeof value === 'object' ? value as Kept : {}
  } catch {
    return {}
  }
}

function write_(all: Kept): void {
  const keys = Object.keys(all)
  if (keys.length > CAP) {
    keys.sort((a, b) => (all[a]?.at || 0) - (all[b]?.at || 0))
      .slice(0, keys.length - CAP)
      .forEach((key) => delete all[key])
  }
  try {
    localStorage.setItem(KEY, JSON.stringify(all))
  } catch {
    /* Private mode, or over quota. A preference that could not be stored is one
       the next visit does without. */
  }
}

export function read(key: string | null): boolean {
  if (!key) return draft ?? false
  const row = read_()[key]
  return row ? row.open === true : true
}

export function write(key: string | null, open: boolean): void {
  if (!key) {
    draft = open
    return
  }
  const all = read_()
  all[key] = { at: Date.now(), open }
  write_(all)
}

/* A draft that becomes a conversation takes its own answer with it.
 *
 * The first message turns the draft into a session in place -- same screen,
 * same composer, an id where there was none -- and without this the desk the
 * reader had just put away would open in their face the moment they hit send,
 * because the new conversation has no answer of its own and a conversation's
 * default is open. Only when it has none, and only once.
 *
 * Called from the page that performs that transition (deskStore.claimDraft),
 * never inferred from the pointer: "the draft became this session" and "the
 * reader opened this session while a draft was up" are the same move at the
 * pointer -- null to an id -- and three sites make the second one. Inferring it
 * adopted the draft's answer into a forked session and into a cron run. */
export function adopt(key: string | null): void {
  if (!key || draft === null) return
  const all = read_()
  if (!(key in all)) write(key, draft)
  draft = null
}

export function _clearForTests(): void {
  draft = null
  try {
    localStorage.removeItem(KEY)
  } catch {
    /* private mode */
  }
}
