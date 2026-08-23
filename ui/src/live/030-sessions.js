/* ---- session list ------------------------------------------------ */
const DAY = 86400000;
/* A row's stamp says when its visible conversation last changed, so it carries a
   clock -- a bare date cannot tell two of yesterday's sessions apart. The year
   only appears once it is not this one; inside the current year it is noise. */
/* A clock only earns its place on today's rows: further back, the day is what
   the reader is placing the session by, and "yesterday 22:07" spends four
   characters saying something they did not ask. */
function whenLabel(epochS) {
  const d = new Date(epochS * 1000), now = new Date();
  const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = d.getTime();
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  const parts = { y: d.getFullYear(), m: d.getMonth() + 1, d: d.getDate() };
  const dated = d.getFullYear() === now.getFullYear()
    ? T('gui.time.md', parts)
    : T('gui.time.ymd', parts);
  if (t >= day0) return hm;
  if (t >= day0 - DAY) return T('gui.time.yest');
  if (t >= day0 - 2 * DAY) return T('gui.time.dbyest');
  return dated;
}

/* Scheduled runs live in cron:<job_id> sessions with no title of their own;
   the job's name is what the user recognises, so the list borrows it. */
let cronNames = {};

async function loadCronNames() {
  try {
    const r = await rpc.call('cron.list', {});
    cronNames = {};
    (r.jobs || []).forEach((j) => { cronNames[j.id] = j.name; });
  } catch { /* keep whatever we had */ }
}

function rowFrom(it) {
  /* Conversation activity, not creation: human messages, assistant replies,
     and runtime-injected visible content all move the row by the same clock. */
  const at = it.updated_at || it.started_at || 0;
  const when = whenLabel(at);
  const cron = it.source === 'cron';
  const jobId = cron ? String(it.id).split(':').pop() : null;
  /* Cron turns open with the scheduler's "[Scheduled Task] Timer ..." wrapper;
     the task's own words start after "Task '". Rows for deleted jobs (no name
     to borrow) fall back to that inner text rather than the wrapper. */
  let prev = (it.preview || '').trim();
  if (cron) {
    const m = prev.match(/Task '([^']+)'/);
    prev = m ? m[1] : prev.replace(/^\[Scheduled Task\]\s*/, '');
  }
  return {
    id: it.id,
    title: (cron && cronNames[jobId]) || it.title || prev.slice(0, 24)
      || T('gui.sess.fallback_title', { id: String(it.id).split(':').pop().slice(0, 15) }),
    last: (it.last_message_preview || it.preview)
      ? (it.last_message_preview || it.preview).slice(0, 60)
      : T('gui.sess.n_messages', { n: it.message_count }),
    when, at, run: null, live: true, from: cron ? 'cron' : undefined,
    pin: !!it.pinned, persisted: true,
  };
}

const rowPreview = (text) => String(text || '').trim().split('\n')[0].trim().slice(0, 60);

/* "Recent" means the latest visible conversation change. Human sends stamp
   immediately; completions stamp again when their visible result arrives. */
function touchSession(id, preview) {
  const s = sess(id);
  if (!s) return;
  const last = rowPreview(preview);
  if (last) s.last = last;
  s.at = Math.floor(Date.now() / 1000);
  s.when = whenLabel(s.at);
  SESS.sort((a, b) => (b.at || 0) - (a.at || 0));
  drawList();
}

const SESS_CHANNELS = ['tui', 'cron'];

async function loadSessions() {
  await loadCronNames();
  const r = await rpc.call('session.list', { channels: SESS_CHANNELS });
  SESS = (r.sessions || []).map(rowFrom).sort((a, b) => (b.at || 0) - (a.at || 0));
}

/* Tool results arrive wrapped in prompt-injection guards
   ([BEGIN UNTRUSTED ...] / [END UNTRUSTED ...]). Those markers protect the
   model, not the reader — strip them from every preview. */
function cleanPreview(text) {
  return String(text || '')
    .split('\n')
    .filter((l) => !/^\s*\[(BEGIN|END) UNTRUSTED /.test(l))
    .join('\n')
    .trim();
}

/* A completed call's success is guessed from its result text until the wire
   carries a real flag (pending backend change): the registry stamps failed
   calls with its retry hint, error-shaped first lines count, and
   understand_media reports per-file failures inline. */
function okOf(name, preview) {
  if (preview.includes('[Analyze the error above')) return false;
  if (/^\s*(error|traceback|failed)\b/i.test(preview)) return false;
  if (name === 'understand_media' && preview.includes('[could not understand:')) return false;
  return true;
}
