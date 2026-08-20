/* ══ replay (swap for a socket to go live) ════════════════════════ */
/* Every event is scheduled up front, so `t` is the cumulative offset from
   the start of the run. later() measures from the moment it is called, so
   anything scheduled from *inside* a fired callback must use a plain delay
   — passing the cumulative offset there would defer it by the whole run. */
function replay(run, instant) {
  let st = null; const open_ = {}; let t = 0; const steps = [];
  const fire = (fn, d) => {
    if (instant) { fn(); return; }
    t += d || 0;
    later(t, fn);
  };
  const TYPE_MS = 24;

  eventsFor(run).forEach((e) => {
    if (e.t === 'ep') fire(() => {
      if (st) st.seal();
      st = newStep(); steps.push(st); raw.push('episode.start');
    }, e.d);
    else if (e.t === 'think') fire(() => {
      st.hasThink = true;
      st.thinkAppend(e.x);
      st.reveal();
      st.thinkDone(e.s);
      if (!instant) { st.setThinkOpen(true); later(1400, () => st.setThinkOpen(false)); }
    }, e.d);
    else if (e.t === 'say') fire(() => { st.setSay(e.x); }, e.d);
    else if (e.t === 't+') fire(() => {
      open_[e.id] = st.tool(e.n, e.a); raw.push(`tool.start     ${e.n}`);
      open_[e.id].meta = { n: e.n, a: e.a, st };
      wsOnTool(e.n, e.a, instant);
    }, e.d);
    else if (e.t === 't-') fire(() => {
      const h = open_[e.id];
      if (!h) return;
      h.done(e.ok, e.r, e.ms, e.diff);
      if (!e.ok) h.meta.st.failed = true;
      wsOnToolDone(h.meta.n, h.meta.a, e.ok, e.r, e.ms, e.diff);
    }, e.d);
    else if (e.t === 'answer') {
      const parts = e.x.match(/[\s\S]{1,26}/g) || [];
      fire(() => {
        if (instant) { RavenIslands.transcript.answer(e.x); return; }
        const typed = RavenIslands.transcript.answerTyped(e.x);
        let n = 0;
        parts.forEach((_, k) => later(k * TYPE_MS, () => {
          n += parts[k].length;
          if (k < parts.length - 1) typed.progress(n);
          else typed.done();
        }));
      }, e.d);
      if (!instant) t += parts.length * TYPE_MS;
    }
    else if (e.t === 'end') fire(() => {
      if (st) st.seal();
      foldSilentRuns(steps);
      collapseTurn(null);
      busy = false; use = run.use; kids = run.kids;
      setCtx(((run.use && run.use.in) || 0) + ((run.use && run.use.out) || 0), 200000);
      raw.push('message.complete');
      const s = sess(cur);
      if (s) { s.last = run.key === 'gtm' ? '抓取了官网，出了对比表' : '3 runs, 0 failures'; s.status = null; }
      drawMeter(); goState(); drawList();
      if (q.length) { const nx = q.shift(); drawQ(); send(nx); }
    }, 300);
  });
}
