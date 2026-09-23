/*
 * The table-of-contents rail: one continuous polyline through every entry, and
 * the stretch of it the reader is currently inside.
 *
 * The line is a single path walked in document order, not one line per nesting
 * level: each entry contributes a vertical run at its own depth, and the 12px
 * between two entries is where the line changes column, which draws as a
 * diagonal when their depths differ and as plain vertical when they do not.
 * Colouring a contiguous stretch of that one path is what makes a parent and
 * the children under it read as a single bracket.
 *
 * The path only moves when the column is laid out again, so it is rebuilt on
 * resize and load; scrolling moves the clip rectangle alone.
 */

(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const CLIP_ID = "raven-toc-progress-clip";

  /* Each level sits 10px right of the one above; the first is inset by 1 so a
     1px stroke lands whole inside the 12px viewBox. */
  const columnFor = (depth) => 1 + depth * 10;

  /* Half of the run an entry boundary turns through. Entries butt against each
     other, so reserving this at both ends of every run leaves exactly one 12px
     gap per boundary for the line to cross columns in. */
  const JOIN = 6;

  const listSelector = ".md-sidebar--secondary .md-nav--secondary > .md-nav__list";

  const entriesOf = (list) => {
    const found = [];
    const walk = (ul, depth) => {
      for (const item of ul.querySelectorAll(":scope > .md-nav__item")) {
        const link = item.querySelector(":scope > .md-nav__link");
        if (link) found.push({ link, depth });
        const nested = item.querySelector(":scope > .md-nav > .md-nav__list");
        if (nested) walk(nested, depth + 1);
      }
    };
    walk(list, 0);
    return found;
  };

  const railFor = (list) => {
    let rail = list.querySelector(":scope > .raven-toc-progress");
    if (rail) return rail;

    rail = document.createElementNS(SVG_NS, "svg");
    rail.classList.add("raven-toc-progress");
    rail.setAttribute("aria-hidden", "true");

    const clip = document.createElementNS(SVG_NS, "clipPath");
    clip.id = CLIP_ID;
    clip.append(document.createElementNS(SVG_NS, "rect"));

    const base = document.createElementNS(SVG_NS, "path");
    base.classList.add("raven-toc-progress__base");

    const lead = document.createElementNS(SVG_NS, "path");
    lead.classList.add("raven-toc-progress__active");
    lead.setAttribute("clip-path", `url(#${CLIP_ID})`);

    rail.append(clip, base, lead);
    list.prepend(rail);
    return rail;
  };

  /* Where an entry's own vertical run starts and ends. The outermost ends are
     flush: nothing joins onto the first entry from above or the last from
     below, so neither reserves a gap there. */
  const runOf = (spans, index) => {
    const span = spans[index];
    return {
      start: index === 0 ? span.top : span.top + JOIN,
      end: index === spans.length - 1 ? span.bottom : span.bottom - JOIN,
    };
  };

  const state = { list: null, entries: [], spans: [], height: 0 };

  const layout = () => {
    const list = document.querySelector(listSelector);
    if (!list) return false;

    const entries = entriesOf(list);
    if (!entries.length) return false;

    const origin = list.getBoundingClientRect().top;
    const spans = entries.map(({ link, depth }) => {
      const box = link.getBoundingClientRect();
      return { top: box.top - origin, bottom: box.bottom - origin, depth };
    });

    const commands = [];
    spans.forEach((span, index) => {
      const run = runOf(spans, index);
      const column = columnFor(span.depth);
      if (index === 0) commands.push(`M${column} ${run.start}`);
      commands.push(`L${column} ${run.end}`);
      const next = spans[index + 1];
      if (next) {
        commands.push(`L${columnFor(next.depth)} ${runOf(spans, index + 1).start}`);
      }
    });

    const height = spans[spans.length - 1].bottom;
    const rail = railFor(list);
    rail.setAttribute("viewBox", `0 0 12 ${height}`);
    rail.setAttribute("width", "12");
    rail.setAttribute("height", String(height));
    const d = commands.join(" ");
    rail.querySelector(".raven-toc-progress__base").setAttribute("d", d);
    rail.querySelector(".raven-toc-progress__active").setAttribute("d", d);

    Object.assign(state, { list, entries, spans, height });
    return true;
  };

  /* An entry is current while its heading is anywhere in the viewport, which
     is why several can be current at once and why the lit stretch grows and
     shrinks rather than jumping between entries. Partway through a section
     longer than the screen no heading is in view, yet the reader is still
     inside the section the nearest heading above opened, so that entry is
     current on its own until the next heading comes on screen. */
  const sync = () => {
    if (!state.list && !layout()) return;

    const boxes = state.entries.map(({ link }) => {
      const id = (link.getAttribute("href") || "").slice(1);
      const heading = id ? document.getElementById(id) : null;
      return heading && heading.getBoundingClientRect();
    });
    const active = [];
    boxes.forEach((box, index) => {
      if (box && box.bottom > 0 && box.top < window.innerHeight) active.push(index);
    });
    if (!active.length) {
      const above = boxes.reduce(
        (last, box, index) => (box && box.top < 0 ? index : last),
        -1,
      );
      if (above !== -1) active.push(above);
    }
    state.entries.forEach(({ link }, index) => {
      link.classList.toggle("md-nav__link--raven-active", active.includes(index));
    });

    const rect = state.list.querySelector(`.raven-toc-progress clipPath rect`);
    if (!rect) return;

    const top = active.length ? runOf(state.spans, active[0]).start : 0;
    const bottom = active.length ? runOf(state.spans, active[active.length - 1]).end : 0;
    const height = Math.max(0, bottom - top);
    rect.setAttribute("x", "0");
    rect.setAttribute("width", "12");
    rect.setAttribute("y", String(top));
    rect.setAttribute("height", String(height));
    /* The transition runs on the styled value, so both are kept in step. */
    rect.style.y = `${top}px`;
    rect.style.height = `${height}px`;

    follow(active);
  };

  /*
   * The column scrolls on its own, so on a long outline the lit entry walks
   * off its bottom edge and the reader loses their place. The reference layout
   * centres the first lit entry and clamps at both ends -- the first, not the
   * lit stretch, so the anchor does not drift as the stretch grows and shrinks.
   */
  const follow = (active) => {
    if (!active.length) return;
    const scroller = state.list.closest(".md-sidebar__scrollwrap");
    if (!scroller) return;

    const link = state.entries[active[0]].link;
    const box = link.getBoundingClientRect();
    const frame = scroller.getBoundingClientRect();
    const centre = scroller.scrollTop + (box.top - frame.top) + box.height / 2;
    const limit = scroller.scrollHeight - scroller.clientHeight;
    const target = Math.max(0, Math.min(limit, centre - scroller.clientHeight / 2));

    /* A hair of tolerance: writing scrollTop every frame fights a reader who
       is dragging the column, and a sub-pixel correction is never visible. */
    if (Math.abs(target - scroller.scrollTop) > 1) scroller.scrollTop = target;
  };

  const relayout = () => {
    layout();
    sync();
  };

  const start = () => {
    relayout();
    window.addEventListener("scroll", sync, { passive: true });
    window.addEventListener("resize", relayout);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(relayout);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
