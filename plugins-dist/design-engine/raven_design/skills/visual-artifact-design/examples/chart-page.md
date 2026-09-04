# Worked example: one data source drives a chart page

This example demonstrates a transferable mechanism: one data structure drives
marks, labels, summaries, tooltips, and the fallback table. The page wrapper is
intentionally plain so it does not become a reusable visual style.

## Transferable mechanism

1. **Write the analytical contract first.** This brief asks for a conclusion,
   so the heading states a conclusion. A neutral exploration tool should use a
   neutral heading instead.
2. **Store the values once.** `DATA` and `SERIES` are the only authored facts.
   Every visible number and sentence is derived from them.
3. **Derive geometry from one frame.** Margins, plot size, scales, bar widths,
   and label positions share the same parameters.
4. **Keep the basic result available without hover.** Exact values are printed
   above the bars and repeated in a real table. Tooltips add context; they do
   not carry the only copy of a value.
5. **Do not rely on color alone.** The second series uses a hatch as well as a
   different color, and the table remains an equivalent text representation.
6. **Preserve legibility at narrow widths.** The chart keeps its readable
   drawing width inside a horizontal region; the data table and conclusion
   remain in normal document flow.

## Deliberately not reusable

The fallback colors, system font, bar chart type, wording, and page composition
are not a design recommendation. A real task must derive those choices from its
audience, question, brand, medium, and accessibility contract. Copy the data
flow and validation approach, not this surface treatment.

## Code

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Revenue by channel</title>
<style>
  :root {
    --surface: #ffffff;
    --text: #111827;
    --text-subtle: #374151;
    --rule: #94a3b8;
    --series-retail: #075985;
    --series-online: #9a3412;
  }
  * { box-sizing: border-box; }
  html { background: var(--surface); color: var(--text); font-family: system-ui, sans-serif; }
  body { margin: 0; font-size: 1rem; line-height: 1.5; }
  main { width: min(100% - 2rem, 68rem); margin-inline: auto; padding-block: 3rem; }
  h1 { max-width: 28ch; margin: 0; font-size: 2rem; line-height: 1.12; }
  .context { max-width: 68ch; margin: 0.75rem 0 2rem; color: var(--text-subtle); }
  .chart-viewport { overflow-x: auto; border-block: 1px solid var(--rule); }
  svg { display: block; width: 100%; min-width: 45rem; height: auto; }
  svg text { fill: var(--text); font-family: system-ui, sans-serif; font-size: 16px; }
  svg .secondary { fill: var(--text-subtle); }
  .bar { transition: opacity 120ms linear; }
  .bar:hover { opacity: 0.78; }
  .summary { max-width: 70ch; margin: 1.5rem 0; font-size: 1.125rem; }
  details { border-block: 1px solid var(--rule); padding-block: 1rem; }
  summary { cursor: pointer; font-weight: 700; }
  table { width: 100%; margin-top: 1rem; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th, td { padding: 0.625rem; border-bottom: 1px solid var(--rule); text-align: right; }
  th:first-child, td:first-child { text-align: left; }
  .source { margin: 1rem 0 0; color: var(--text-subtle); font-size: 0.875rem; }
  @media (max-width: 44rem) {
    main { width: min(100% - 1.25rem, 68rem); padding-block: 1.5rem; }
    h1 { font-size: 1.625rem; }
    .chart-viewport { margin-inline: -0.625rem; padding-inline: 0.625rem; }
  }
  @media (prefers-reduced-motion: reduce) {
    .bar { transition: none; }
  }
</style>
</head>
<body>
<main>
  <h1>Online sales nearly matched retail by the fourth quarter</h1>
  <p class="context">Quarterly revenue by channel, 2025, in thousands of US dollars.</p>

  <div class="chart-viewport">
    <svg id="chart" viewBox="0 0 720 420" role="img"
         aria-labelledby="chart-title chart-description">
      <title id="chart-title">Retail and online revenue by quarter</title>
      <desc id="chart-description">Grouped bars with exact values. Online rises each quarter and nearly matches retail in Q4.</desc>
      <defs>
        <pattern id="online-pattern" width="8" height="8" patternUnits="userSpaceOnUse">
          <rect width="8" height="8" fill="var(--series-online)"></rect>
          <path d="M-2 2 L2 -2 M0 8 L8 0 M6 10 L10 6" stroke="var(--surface)" stroke-width="2"></path>
        </pattern>
      </defs>
      <g id="plot"></g>
    </svg>
  </div>

  <p class="summary" id="summary"></p>

  <details>
    <summary>View the source values</summary>
    <table>
      <thead><tr id="table-head"></tr></thead>
      <tbody id="table-body"></tbody>
    </table>
  </details>
  <p class="source">Source: fictional values for demonstrating data reconciliation.</p>
</main>

<script>
  const DATA = [
    { quarter: 'Q1', retail: 312, online: 194 },
    { quarter: 'Q2', retail: 305, online: 231 },
    { quarter: 'Q3', retail: 318, online: 272 },
    { quarter: 'Q4', retail: 324, online: 316 },
  ];
  const SERIES = [
    { key: 'retail', label: 'Retail', fill: 'var(--series-retail)' },
    { key: 'online', label: 'Online', fill: 'url(#online-pattern)' },
  ];

  const svg = document.getElementById('plot');
  const NS = 'http://www.w3.org/2000/svg';
  const FRAME = { left: 64, right: 24, top: 72, bottom: 54, width: 720, height: 420 };
  const plotWidth = FRAME.width - FRAME.left - FRAME.right;
  const plotHeight = FRAME.height - FRAME.top - FRAME.bottom;
  const yMax = Math.ceil(Math.max(...DATA.flatMap(row => SERIES.map(series => row[series.key]))) / 100) * 100;
  const y = value => FRAME.top + plotHeight - (value / yMax) * plotHeight;

  function add(tag, attrs, value) {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([key, attribute]) => node.setAttribute(key, attribute));
    if (value !== undefined) node.textContent = value;
    svg.appendChild(node);
    return node;
  }

  for (let value = 0; value <= yMax; value += 100) {
    add('line', {
      x1: FRAME.left,
      x2: FRAME.left + plotWidth,
      y1: y(value),
      y2: y(value),
      stroke: 'var(--rule)',
      'stroke-width': 1,
    });
    add('text', {
      class: 'secondary',
      x: FRAME.left - 12,
      y: y(value) + 5,
      'text-anchor': 'end',
    }, value);
  }

  add('text', { class: 'secondary', x: FRAME.left, y: 32 }, 'USD thousands');
  SERIES.forEach((series, index) => {
    const legendX = FRAME.width - 250 + index * 122;
    add('rect', { x: legendX, y: 20, width: 18, height: 18, fill: series.fill });
    add('text', { x: legendX + 26, y: 35 }, series.label);
  });

  const groupWidth = plotWidth / DATA.length;
  const barWidth = 48;
  const barGap = 12;
  DATA.forEach((row, rowIndex) => {
    const centerX = FRAME.left + groupWidth * rowIndex + groupWidth / 2;
    SERIES.forEach((series, seriesIndex) => {
      const x = centerX - barWidth - barGap / 2 + seriesIndex * (barWidth + barGap);
      const value = row[series.key];
      const bar = add('rect', {
        class: 'bar',
        x,
        y: y(value),
        width: barWidth,
        height: y(0) - y(value),
        fill: series.fill,
      });
      const tooltip = document.createElementNS(NS, 'title');
      tooltip.textContent = `${row.quarter}, ${series.label}: $${value}k`;
      bar.appendChild(tooltip);
      add('text', {
        x: x + barWidth / 2,
        y: y(value) - 9,
        'text-anchor': 'middle',
      }, value);
    });
    add('text', {
      x: centerX,
      y: FRAME.top + plotHeight + 34,
      'text-anchor': 'middle',
    }, row.quarter);
  });

  const first = DATA[0];
  const last = DATA.at(-1);
  const growth = Math.round((last.online / first.online - 1) * 100);
  const openingGap = first.retail - first.online;
  const closingGap = last.retail - last.online;
  document.getElementById('summary').textContent =
    `Online revenue rose ${growth}% from Q1 to Q4, narrowing the gap with retail from $${openingGap}k to $${closingGap}k.`;

  const header = document.getElementById('table-head');
  ['Quarter', ...SERIES.map(series => `${series.label} ($k)`)].forEach(label => {
    const cell = document.createElement('th');
    cell.scope = 'col';
    cell.textContent = label;
    header.appendChild(cell);
  });
  const body = document.getElementById('table-body');
  DATA.forEach(row => {
    const tableRow = document.createElement('tr');
    [row.quarter, ...SERIES.map(series => row[series.key])].forEach((value, index) => {
      const cell = document.createElement(index === 0 ? 'th' : 'td');
      if (index === 0) cell.scope = 'row';
      cell.textContent = value;
      tableRow.appendChild(cell);
    });
    body.appendChild(tableRow);
  });
</script>
</body>
</html>
```
