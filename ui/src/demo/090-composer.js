/* ══ composer ═════════════════════════════════════════════════════
   The dock is the composer island (ui/src/features/composer/): the field, the
   send/stop button, the queued rows, the attachment tray, the slash palette
   and the live turn row that rides the tail of the transcript. What remains
   here is the island's shell face -- the names the rest of the page still
   calls, the fixture half of DS.composer, and the demo replay's own send. */

/* The field itself stays a name: the skills panel drops a prompt into it and
   the draft store reads it back. */
const ta = $('#ta');

function goState() { RavenIslands.composer.goPaint(); }
function drawQ() { RavenIslands.composer.drawQueue(); }
function drawMeter() { RavenIslands.composer.drawMeter(); }
function taFit() { RavenIslands.composer.fitField(); }
function dockLift() { RavenIslands.composer.dockLift(); }

/* ══ session commands ═════════════════════════════════════════════
   Only what acts on THIS conversation. Navigation lives in the rail, so
   putting it here too would just be a second, worse way to click it.
   Commands carry ids, not spellings: the palette renders whatever the active
   language calls them, and both spellings stay typeable.

   Two commands, deliberately. Anything else a palette could offer already has
   a button -- send/stop, the model chip, the answer footer, the rail -- and a
   second entry point for the same action is one more thing to keep in sync.
   What is left is what has no button: acting on the session as a whole. */
const SLASH = [
  { id: 'gui.compress', fn: () => {} },
  { id: 'gui.clear', fn: () => confirmAsk(T('gui.clear_title'), T('gui.clear_body'), T('gui.clear_yes'), () => {
      const s = sess(cur);
      $('#stage').innerHTML = ''; pitch();
      if (s) { s.run = null; s.last = T('gui.sess.not_started'); }
      use = null; drawMeter(); drawList();
    }) }
];

/* The fixture half of DS.composer. `busy` and `queue` are closures over the
   page globals the turn machine owns, never snapshots -- the island reads them
   at paint time exactly as the renderer it replaces did. Live mode installs
   its own meter wording and the upload transport over this. */
DS.composer ??= {
  busy: () => busy,
  queue: () => q,
  meter: () => (busy ? T('gui.meter.running')
    : use ? T('gui.meter.usage', { calls: use.calls, in: (use.in / 1000).toFixed(1), out: (use.out / 1000).toFixed(1) })
    : ''),
  slash: SLASH,
  pickHint: 'demo：正式版在这里选文件或直接拖进来',
  send: (text) => send(text),
  stop: () => halt(),
};

const pickRun = (s) => /超时|timeout|登录|bug|修|fix|报错|定位|回调/.test(s) ? RUNS.fix : RUNS.gtm;

function send(text) {
  if (busy) { q.push(text); drawQ(); toast('已排队，本轮结束后发出'); return; }
  const p = $('#stage').querySelector('.pitch'); if (p) p.remove();
  ask(text);
  busy = true; use = null;
  drawMeter(); goState(); drawList();
  const run = pickRun(text);
  const s = sess(cur);
  if (s && !s.run) {
    s.run = run.key; s.status = null;
    if (s.title === '新任务') { s.title = run.title; $('#title').textContent = plainTitle(s.title); }
    drawList();
  }
  replay(run, false);
}

function halt() {
  stop_(); busy = false;
  noteRow('已中断 · 上面的步骤保留', '', { quiet: true, host: $('#stage') });
  drawMeter(); goState(); drawList();
}
