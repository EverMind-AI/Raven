/* ══ module 1b: the conversation ══════════════════════════════════ */
function openSession(s) {
  parkDraft(); loadDraft(s.id);
  // Opening it IS reading it: the finished marker has done its job and the
  // row goes back to carrying its timestamp.
  if (s.status === 'done') s.status = null;
  markNewCurrent();
  stop_(); busy = false; q = []; use = null; kids = []; tl = []; raw = []; lastRun = null;
  wsReset();
  setWs(false);
  $('#title').textContent = plainTitle(s.title);
  $('#stage').innerHTML = '';
  $('#flash').textContent = '';
  drawQ(); drawMeter(); goState(); drawBanner();
  if (s.run) {
    const r = RUNS[s.run];
    use = r.use; kids = r.kids; lastRun = r;
    setCtx(((r.use && r.use.in) || 0) + ((r.use && r.use.out) || 0), 200000);
    ask(r.ask); replay(r, true);
  } else if (s.status === 'err') {
    ask('把支付回调那块拆成两个 handler');
    noteRow('找不到模块 stripe（internal/pay/callback.go:12）', '装上依赖或改用内置 http 客户端后重试。');
  } else {
    pitch();
  }
}

/* A standing memory fault, or null. Set from the `memory.health` event: three
   consecutive failed writes mean the backend is not coming back on its own, and
   the reader has been getting normal-looking replies the whole time. */
let memFault = null;

function drawBanner() {
  const host = $('#bannerHost'); host.innerHTML = '';
  if (memFault) {
    /* No dismiss: the condition lasts until it is fixed, and a banner the
       reader can wave away is one they will wave away and then forget. */
    const b = mk('div', 'banner bad');
    b.append(mk('b', null, T('gui.mem.down')), mk('span', null, memFault));
    host.appendChild(b);
    return;
  }
  const c = cap('websearch');
  if (!c || c.state !== 'need') return;
  const b = mk('div', 'banner');
  b.append(mk('b', null, '网页搜索还没配置'));
  b.appendChild(mk('span', null, '现在 Raven 只能抓你给出的网址，不能自己找资料。'));
  const go = mk('button', null, '去配置');
  go.onclick = () => { openPlugins(); openDetail('websearch'); };
  const x = mk('button', 'x', '✕');
  x.setAttribute('aria-label', '忽略');
  x.onclick = () => b.remove();
  b.append(go, x);
  host.appendChild(b);
}

function pitch() {
  /* The empty state is the composer itself, moved to the visual centre --
     no mark, no facts, no title. ask()/renderHistory lift the flag the
     moment real content lands. */
  const c = document.querySelector('.chat');
  if (c) c.dataset.fresh = '1';
}

/* The composer appends an "[attachments]" note plus "- path" bullets for the
   model; the reader gets chips instead. Parsed against both language variants
   of the note, since history may have been written under the other one. */
function splitAtts(text) {
  const notes = Object.values(I18N.ui['gui.att.note'] || {});
  for (const note of notes) {
    if (!note) continue;
    const ix = text.lastIndexOf('\n\n' + note + '\n');
    if (ix < 0) continue;
    const tail = text.slice(ix + note.length + 3).split('\n');
    if (!tail.length || !tail.every((l) => !l.trim() || /^- /.test(l))) continue;
    return {
      body: text.slice(0, ix),
      atts: tail.filter((l) => /^- /.test(l)).map((l) => l.slice(2).trim()),
    };
  }
  return { body: text, atts: [] };
}

/* Uploaded path -> data URL, filled in by the upload path (live.js). An image
   the reader just sent should look like an image in their own message, and the
   bytes are already in the page; attachments travel to the agent as paths, so
   this is the only place they can be recovered from. */
const ATT_IMG = new Map();

/* Full-size view for any image in the page — a staged thumbnail or one already
   sent. Clicking anywhere closes it; Escape is wired into the global chain. */
function closeImage() {
  document.querySelectorAll('.lightbox').forEach((n) => n.remove());
}

function openImage(src, name) {
  closeImage();
  const box = mk('button', 'lightbox');
  box.setAttribute('aria-label', T('gui.img.close'));
  const img = mk('img');
  img.src = src;
  img.alt = name || '';
  box.appendChild(img);
  box.onclick = closeImage;
  document.body.appendChild(box);
  box.focus();
}

/* Past this many, the row collapses: a wall of thumbnails buries the sentence
   it was sent with, and past two filenames the chips stop being readable. */
const ATT_SHOW_IMG = 3;
const ATT_SHOW_DOC = 2;

/* What was handed over, drawn above what was said about it. Images show
   themselves; a stack of them collapses to one pile with a count, files to one
   chip with a count -- either opens the whole set on click. */
function attBox(paths, expanded) {
  const box = mk('div', 'abox');
  const imgs = paths.filter((p) => ATT_IMG.has(String(p)));
  const docs = paths.filter((p) => !ATT_IMG.has(String(p)));
  const openAll = () => box.replaceWith(attBox(paths, true));

  const thumb = (p, live) => {
    const src = ATT_IMG.get(String(p));
    const nm = String(p).split('/').pop();
    const img = mk('img', 'shot');
    img.src = src;
    img.alt = nm;
    if (live) {
      img.title = T('gui.img.open', { name: nm });
      img.onclick = () => openImage(src, nm);
    }
    return img;
  };
  const chip = (p) => {
    const c = mk('button', 'achip');
    c.appendChild(mk('span', 'nm', String(p).split('/').pop()));
    c.title = p;
    c.onclick = () => { if (typeof showFile === 'function') showFile(p); };
    return c;
  };

  /* A lone image keeps its own proportions; several are squared off so the row
     reads as one set instead of a ragged filmstrip. */
  if (imgs.length > 1) box.classList.add('set');
  if (imgs.length && (expanded || imgs.length <= ATT_SHOW_IMG)) {
    imgs.forEach((p) => box.appendChild(thumb(p, true)));
  } else if (imgs.length) {
    const pile = mk('button', 'pile');
    pile.append(thumb(imgs[0], false), mk('span', 'cnt', `${imgs.length}`));
    pile.title = T('gui.att.show_all');
    pile.onclick = openAll;
    box.appendChild(pile);
  }
  if (docs.length && (expanded || docs.length <= ATT_SHOW_DOC)) {
    docs.forEach((p) => box.appendChild(chip(p)));
  } else if (docs.length) {
    const more = mk('button', 'achip more');
    more.appendChild(mk('span', 'nm', T('gui.att.n_files', { n: docs.length })));
    more.title = T('gui.att.show_all');
    more.onclick = openAll;
    box.appendChild(more);
  }
  return box;
}

function ask(text, when) {
  const ch = document.querySelector('.chat');
  if (ch) delete ch.dataset.fresh;
  stick = true;                      // sending always snaps back to the tail
  const { body, atts: files } = splitAtts(String(text));
  const w = mk('div', 'ask in');
  if (files.length) w.appendChild(attBox(files, false));
  /* No empty bubble under a file sent on its own. */
  if (body.trim()) {
    const bEl = mk('div', 'b', body);
    /* A long message folds to a preview so it cannot bury the reply;
       the fold keeps the full text in the DOM (copy still copies it all). */
    if (body.length > 640 || body.split('\n').length > 12) {
      bEl.classList.add('clip');
      const tg = mk('button', 'qfold', T('gui.ask.expand'));
      tg.setAttribute('aria-expanded', 'false');
      tg.onclick = () => {
        const open = !bEl.classList.toggle('clip');
        tg.textContent = T(open ? 'gui.ask.collapse' : 'gui.ask.expand');
        tg.setAttribute('aria-expanded', String(open));
        if (!open) bEl.scrollIntoView({ block: 'nearest' });
      };
      w.append(bEl, tg);
    } else w.appendChild(bEl);
  }
  /* Same footer grammar as an answer -- what the reader typed is quotable and
     locatable in time too. Mirrored, since the bubble sits on the right. */
  const foot = mk('div', 'ansfoot');
  const acts = mk('div', 'acts');
  const cp = mk('button');
  cp.dataset.tip = T('gui.answer.copy');
  cp.setAttribute('aria-label', T('gui.answer.copy'));
  cp.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${COPY_ICO}</svg>`;
  cp.onclick = () => {
    if (navigator.clipboard) navigator.clipboard.writeText(body);
    tipFlash(cp, T('gui.answer.copied'));
  };
  acts.appendChild(cp);
  foot.appendChild(acts);
  foot.appendChild(mk('span', 'turnmeta', when || stamp(Date.now())));
  w.appendChild(foot);
  stageBox().appendChild(w); down();
}

/* Writes what the row shows and what it can give back, together. Both have to
   move as one: a caller that rewrites the visible line and leaves the recoverable
   text behind (compressNow swapping its result over "Compacting...") would strand
   the row holding the wrong thing.

   `title` goes up unconditionally rather than only when the line is measured to
   overflow. A measurement at insert is a snapshot of a layout that keeps moving:
   opening the workspace re-lays the chat column and its divider drags, so a row
   that fitted the full column when it landed is ellipsised with nothing to hover
   the moment the panel opens -- the recovery path disappearing exactly when the
   truncation arrives. A tooltip duplicating a fully visible line costs nothing;
   it is only ever drawn on hover. */
function noteSay(row, label, detail) {
  const head = String(label || '');
  const full = String(detail || '');
  const brief = firstErrLine(full, 140) || full;
  row.querySelector('.tx').textContent = brief ? `${head} · ${brief}` : head;
  row.title = full ? `${head} · ${full}` : head;
}

/* The one row for everything raven says about itself in the transcript, so a
   new kind of notice cannot arrive wearing its own shape. `label` leads, the
   detail follows after a `·`, and the whole thing is one line: a failure and a
   framework note differ only in colour.

   A failure still reports in two registers -- one line a person can act on, and
   the provider's own text for whoever has to debug it -- and the second one no
   longer costs a panel, but it does need to survive as text and not just as a
   tooltip: a hover cannot be selected or copied and does not exist on touch,
   and the visible line is capped at 140 characters. So the row carries the same
   right-click Copy the answer bubble does, over the UNTRUNCATED text -- which is
   what someone pasting a litellm traceback into an issue actually needs, since
   that paragraph repeats itself inside two nested tracebacks and the line can
   only ever hold the one fact in it.

   Options: `quiet` for a framework note (no failure happened, so no red),
   `host` for a caller that must write to the main stage even while a delegated
   transcript owns stageBox(), and `retry`, which is the caller's -- this row
   cannot know what should happen next, and guessing was worse than not
   offering. Omit it and no action appears. */
function noteRow(label, detail, opts) {
  const o = opts || {};
  const w = mk('div', 'tnote in' + (o.quiet ? '' : ' bad'));
  w.appendChild(mk('span', 'tx'));
  noteSay(w, label, detail);
  /* `title` is the whole text by construction, so it is also what Copy hands
     over -- no second copy of the string to fall out of step with the row. */
  ctxMenu(w, () => [
    { label: T('gui.answer.copy'), fn: () => copyToClip(w.title, T('gui.answer.copied')) },
  ]);
  if (typeof o.retry === 'function') {
    const b = mk('button', 'rt', T('gui.retry'));
    b.onclick = () => { w.remove(); o.retry(); };
    w.appendChild(b);
  }
  /* Above the live glyph, like every other row that lands mid-turn -- under it
     the note would sit beneath a spinner still claiming to be working. */
  const host = o.host || stageBox();
  host.insertBefore(w, host.querySelector(':scope > .turnlive'));
  down();
  return w;
}

