/* ═══════════════════════════════════════════════════════════════════
   The DataSource seam. Loads before the demo shell and the live layer;
   owned by neither. A page's renderer lives once (in the shell) and
   reads data only through DS.<domain>; the shell REGISTERS its fixture
   source (DS.x ??= fixture), the live layer INSTALLS the real one
   (DS.x = rpcSource) -- and because seam, shell, and live all run
   synchronously before the load-event paint, whichever source is
   installed last is the one the first paint reads. No flags, no
   clearing, no repaint. Design: docs/specs/2026-08-19-page-datasource-seam.md
   ═══════════════════════════════════════════════════════════════════ */
const DS = {};
