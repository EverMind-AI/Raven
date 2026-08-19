/* ---- history rendering ------------------------------------------- */
/* session.resume carries the assistant's tool_calls as [{id, name, arguments}]
   with `arguments` a raw JSON string, and each tool result names the call it
   answers. Indexing them by id is what lets a restored row say which file it
   read instead of just "read". */
function callIndex(messages) {
  const byId = new Map();
  messages.forEach((m) => {
    if (!m || m.role !== 'assistant' || !Array.isArray(m.tool_calls)) return;
    m.tool_calls.forEach((c) => {
      let args = {};
      try { args = JSON.parse(c.arguments || '{}') || {}; } catch (e) { args = {}; }
      byId.set(String(c.id || ''), { name: c.name || '', args });
    });
  });
  return byId;
}

/* The turn's own clock is gone by the time it is restored (nothing stores how
   long a call took), so a restored fold header says only that the work
   happened -- see collapseTurn(null). Wall-clock stamps do survive. */
/* Raven's own words, drawn as Raven's own words. The runtime writes this text
   in English and hands it over as a KIND, so the sentence is chosen here, in
   the reader's language -- the old canned string was pushed down the token
   stream and arrived as the model's answer, in English, in whatever paragraph
   the model happened to be mid-way through.

   Quiet: the runtime reporting a decision it made is not a failure, and the
   card this used to be -- clay stripe, warning triangle, its own header --
   shouted louder than the failures further down the same transcript. */
/* An unknown kind still gets a row: a client one release behind the runtime
   should say something happened rather than drop it on the floor. */
const noticeRow = (kind, detail) =>
  noteRow(T('gui.notice.' + kind, null, kind), detail, { quiet: true });

function renderHistory(messages) {
  /* Drawing somebody else's run into the panel says nothing about whether
     THIS conversation has started, so the new-task state stays as it was. */
  const ch = stageHost ? null : document.querySelector('.chat');
  if (ch) delete ch.dataset.fresh;
  let toolRun = null;
  const sealTools = () => { if (toolRun) { toolRun.seal(); toolRun = null; } };
  const calls = callIndex(messages);
  /* One footer per turn: only the LAST assistant text before the next user
     message is the turn's answer; the ones before it are the model narrating
     mid-turn and render as quiet prose, exactly like a live turn. */
  const isFinal = messages.map((m, i) => {
    if (!(m && m.role === 'assistant' && m.text && m.text.trim())) return false;
    for (let j = i + 1; j < messages.length; j += 1) {
      const n = messages[j];
      if (n && n.role === 'user' && n.text && n.text.trim()) return true;
      if (n && n.role === 'assistant' && n.text && n.text.trim()) return false;
    }
    return true;
  });
  /* Call durations were never stored, but the wall-clock stamps were: the gap
     between the question and the answer that closed it IS how long the turn
     took, so a restored fold header carries the same time a live one does. */
  let turnAt = 0;
  const msOf = (t) => { const d = new Date(t); const v = d.getTime(); return isNaN(v) ? 0 : v; };
  messages.forEach((m, i) => {
    if (m.role === 'user' && m.text && m.text.trim()) {
      sealTools(); turnAt = msOf(m.timestamp); ask(m.text, stamp(m.timestamp)); return;
    }
    if (m.role === 'assistant' && m.notice) {
      /* Stored as an assistant message because the MODEL has to read it on the
         next turn -- but a reader must not, or the reload contradicts the live
         view about who said it. */
      sealTools();
      const endAt = msOf(m.timestamp);
      collapseTurn(turnAt && endAt && endAt - turnAt >= 1000 ? dur(endAt - turnAt) : null);
      noticeRow(m.notice.kind || '', m.notice.detail || '');
      return;
    }
    if (m.role === 'assistant' && m.turn_ended) {
      /* The closing marker of a turn that was stopped or died: everything the
         turn streamed is already drawn above; this renders the same note the
         live stop shows, instead of the marker's model-facing text. */
      sealTools();
      const endAt = msOf(m.timestamp);
      collapseTurn(turnAt && endAt && endAt - turnAt >= 1000 ? dur(endAt - turnAt) : null);
      const stopped = m.turn_ended.status === 'cancelled';
      /* The catalogue entry carries its own separator before {e}; the row draws
         that separator itself, so the reason is handed over as the detail and
         the entry is emptied down to its label -- same shape as gui.compress.fail
         below. Handing over the formatted string instead would leave the reason
         inside the label, where nothing can keep the untruncated text. */
      noteRow(stopped ? T('gui.halted') : T('gui.turn_died', { e: '' }).replace(/\s*[-·]\s*$/, ''),
        stopped ? '' : (m.turn_ended.reason || ''), { quiet: stopped });
      return;
    }
    if (m.role === 'assistant') {
      const thought = String(m.reasoning_content || '').trim();
      const text = String(m.text || '').trim();
      /* The thought opens the step its calls will land in, so a restored turn
         reads in the same order it happened: thought, then work. */
      if (thought) {
        sealTools();
        toolRun = newStep();
        toolRun.hasThink = true;
        toolRun.cot.textContent = thought;
        toolRun.reveal();
        /* The server's own measurement of the thought, so a reloaded turn folds
           to the same "thought - Ns" the live one did. Absent on a session
           written before it was recorded, and on an unstreamed call: 0 there,
           which prints no clock rather than inventing one. */
        toolRun.thinkDone(m.reasoning_ms != null ? Math.round(m.reasoning_ms / 1000) : 0);
      }
      if (!text) return;
      if (isFinal[i]) {
        sealTools();
        const endAt = msOf(m.timestamp);
        /* Sessions written before the turn-start stamp landed have the whole
           turn on one clock read. A sub-second gap is that artifact, not a
           measurement -- show the bare header rather than a fake "1s". */
        collapseTurn(turnAt && endAt && endAt - turnAt >= 1000 ? dur(endAt - turnAt) : null);
        const body = answerBlock(text, stamp(m.timestamp));
        body.innerHTML = md(text);
      } else if (thought) {
        toolRun.hasSay = true;
        toolRun.say.innerHTML = md(text);
      } else {
        sealTools();
        const stp = mk('div', 'step in');
        const s = mk('div', 'say prose');
        s.innerHTML = md(text);
        stp.appendChild(s);
        stageBox().appendChild(stp);
      }
      return;
    }
    if (m.role === 'tool') {
      if (!toolRun) { toolRun = newStep(); }
      /* The result names the call it answers, so the row can carry the target
         it was given -- and an edit's arguments still yield its diff. */
      const hit = calls.get(String(m.tool_call_id || '')) || {};
      /* An ACP transcript puts a human title where a tool name goes, so the
         program becomes the verb and the command becomes the argument -- the
         same shape every other call in this transcript already has. A raven
         tool name has no colon and comes back unchanged. */
      const parts = callParts(hit.name || m.name || 'tool');
      const h = toolRun.tool(parts.name, hit.args || null, parts.display || null);
      const preview = cleanPreview(m.text).split('\n').slice(0, 8).map((l) => l.slice(0, 160)).join('\n');
      /* The stored diff is the live event's diff, written down: same argument,
         same numbered rows after a reload as before it -- and duration_ms is the
         same for the clock, which a delegation card shows. 0 when the entry
         predates it: an ordinary row draws no clock either way, and a card
         reads 0 as "no time to show" rather than "took no time". */
      h.done(okOf(m.name || '', preview), preview, m.duration_ms != null ? m.duration_ms : 0, m.diff);
    }
  });
  sealTools();
  down();
}

