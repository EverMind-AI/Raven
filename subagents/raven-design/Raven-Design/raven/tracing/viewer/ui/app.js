const state = {
  data: null,
  descriptors: {},
  appView: 'trace',
  selectedSessionId: null,
  selectedTraceKey: null,
  selectedSpanId: null,
  selectedTab: 'content',
  search: '',
  contentResults: null,
  highlightScrollPending: false,
  agent: 'all',
  provider: 'all',
  model: 'all',
  apiStatus: 'all',
  apiWindow: '24h',
  artifactCache: new Map(),
  isLoading: false,
  autoRefreshTimer: null,
  openDetailKeys: new Set(),
  scrollPositions: new Map(),
  windowScrollY: 0,
  pendingScrollRestore: null,
  detailsRenderSignature: null,
  lastUpdated: null,
  connectionStatus: 'idle',
  lang: (() => {
    try {
      const saved = localStorage.getItem('tracing:lang');
      return saved === 'zh' || saved === 'en' ? saved : 'en';
    } catch {
      return 'en';
    }
  })()
};

const AUTO_REFRESH_MS = 5000;

const elements = {
  connectionStatus: document.getElementById('connectionStatus'),
  lastUpdated: document.getElementById('lastUpdated'),
  traceScene: document.getElementById('traceScene'),
  apiScene: document.getElementById('apiScene'),
  listTitle: document.getElementById('listTitle'),
  sessionList: document.getElementById('sessionList'),
  traceList: document.getElementById('traceList'),
  detailsBody: document.getElementById('detailsBody'),
  flowEyebrow: document.getElementById('flowEyebrow'),
  traceTitle: document.getElementById('traceTitle'),
  traceMeta: document.getElementById('traceMeta'),
  detailsTitle: document.getElementById('detailsTitle'),
  searchInput: document.getElementById('searchInput'),
  contentSearchResults: document.getElementById('contentSearchResults'),
  agentFilter: document.getElementById('agentFilter'),
  providerFilter: document.getElementById('providerFilter'),
  modelFilter: document.getElementById('modelFilter'),
  statusFilter: document.getElementById('statusFilter'),
  refreshButton: document.getElementById('refreshButton'),
  tabs: [...document.querySelectorAll('.tab[data-tab]')],
  appViewButtons: [...document.querySelectorAll('.workspace-tab[data-app-view]:not([disabled])')],
  emptyStateTemplate: document.getElementById('emptyStateTemplate')
};

const I18N = {
  en: {
    'status.connected': 'Connected',
    'status.disconnected': 'Disconnected',
    'header.updated': 'Updated',
    'action.refresh': 'Refresh',
    'view.api': 'API Calls',
    'view.trace': 'Traces',
    'sessions.title': 'Sessions',
    'field.search': 'Search',
    'field.status': 'Status',
    'search.placeholder': 'session / trace / keyword',
    'status.all': 'All Statuses',
    'status.okOnly': 'Success Only',
    'status.errorOnly': 'Failed Only',
    'status.unreportedOnly': 'Token Unreported Only',
    'trace.selectSession': 'Select a Session',
    'details.selectSpan': 'Select a Span',
    'detailtab.content': 'Input / Output',
    'detailtab.metadata': 'Metadata',
    'detailtab.raw': 'Raw',
    'empty.title': 'Nothing to Show Yet',
    'empty.copy': 'Refresh, or send another message to try.',
    'error.loadFailed': 'Failed to Load',
    'session.resumedFrom': ({ id }) => `Resumed from ${id}`,
    'session.resumedTo': ({ id }) => `Resumed to ${id}`,
    'session.startedAt': ({ time }) => `Started ${time}`,
    'session.updatedAt': ({ time }) => `Updated ${time}`,
    'round.latest': 'Latest Turn',
    'round.nth': ({ n }) => `Turn ${n}`,
    'filter.all': 'All',
    'filter.allAgent': 'All Agents',
    'filter.allProvider': 'All Providers',
    'filter.allModel': 'All Models',
    'search.contentHits': ({ n, more }) => `${n}${more ? '+' : ''} Content Match${n === 1 && !more ? '' : 'es'}`,
    'search.noContentHits': 'No Content Matches',
    'subagent.openFullTrace': 'Open Subagent Full Trace',
    'subagent.backToMain': 'Back to Main Trace',
    'subagent.noResult': 'No final result from the subagent yet',
    'trace.noNodes': 'This trace has no primary execution nodes to show.',
    'span.noEvents': 'This span has no extra events.',
    'read.noContent': 'No content captured for this read',
    'artifact.empty': 'No Artifact',
    'section.metadata': 'Metadata',
    'common.success': 'Success',
    'common.failure': 'Failed',
    'usage.unreported': 'Token Unreported',
    'usage.input': 'Input',
    'usage.output': 'Output',
    'usage.total': 'Total',
    'usage.cacheHit': 'Cache Hit',
    'usage.cacheWrite': 'Cache Write',
    'usage.cost': 'Cost',
    'api.stat.totalCalls': 'Total Calls',
    'api.stat.reportedToken': 'Reported Token',
    'api.stat.unreportedToken': 'Unreported Token',
    'api.stat.inputUncached': 'Input Token (Cache Miss)',
    'api.stat.inputCached': 'Input Token (Cache Hit)',
    'api.stat.output': 'Output Token',
    'api.table.empty': 'No Detail',
    'api.table.noData': 'No Data',
    'api.keyMetrics': 'Key Metrics',
    'api.byProviderModel': 'By Provider / Model',
    'api.noCallsTitle': 'No Model Calls in the Current Range',
    'api.noCallsCopy': 'Adjust the time range or filters, then check key metrics and provider/model breakdown.',
    'api.callCount': ({ n }) => `${n} Calls`,
    'api.detailTitle': ({ n }) => `Call Detail (Latest ${n})`,
    'api.window.aria': 'Time Range',
    'status.unreported': 'Unreported',
    'win.all': 'All',
    'win.7d': 'Last 7d',
    'win.24h': 'Last 24h',
    'win.1h': 'Last 1h',
    'col.provider': 'Provider',
    'col.model': 'Model',
    'col.totalCalls': 'Total Calls',
    'col.success': 'Success',
    'col.failure': 'Failed',
    'col.reportedToken': 'Reported Token',
    'col.inputUncached': 'Input (Cache Miss)',
    'col.inputCached': 'Input (Cache Hit)',
    'col.inputCachedShort': 'Input (Hit)',
    'col.output': 'Output Token',
    'col.outputShort': 'Output',
    'col.time': 'Time',
    'col.status': 'Status',
    'col.totalOrError': 'Total Token / Failure Reason'
  },
  zh: {
    'status.connected': '已连接',
    'status.disconnected': '未连接',
    'header.updated': '更新',
    'action.refresh': '刷新',
    'view.api': 'API 调用',
    'view.trace': '会话追踪',
    'sessions.title': '会话列表',
    'field.search': '搜索',
    'field.status': '状态',
    'search.placeholder': 'session / trace / 内容关键词',
    'status.all': '全部状态',
    'status.okOnly': '仅成功',
    'status.errorOnly': '仅失败',
    'status.unreportedOnly': '仅 Token 未上报',
    'trace.selectSession': '选择一个会话',
    'details.selectSpan': '选择一个 Span',
    'detailtab.content': '输入输出',
    'detailtab.metadata': '元数据',
    'detailtab.raw': '原始数据',
    'empty.title': '还没有可展示的数据',
    'empty.copy': '刷新一下，或者再发一条消息试试。',
    'error.loadFailed': '加载失败',
    'session.resumedFrom': ({ id }) => `续接自 ${id}`,
    'session.resumedTo': ({ id }) => `已续接到 ${id}`,
    'session.startedAt': ({ time }) => `开始 ${time}`,
    'session.updatedAt': ({ time }) => `更新 ${time}`,
    'round.latest': '最近一轮',
    'round.nth': ({ n }) => `第 ${n} 轮`,
    'filter.all': '全部',
    'filter.allAgent': '全部 agent',
    'filter.allProvider': '全部 provider',
    'filter.allModel': '全部 model',
    'search.contentHits': ({ n, more }) => `内容命中 ${n}${more ? '+' : ''}`,
    'search.noContentHits': '无内容命中',
    'subagent.openFullTrace': '打开 subagent 的完整 trace',
    'subagent.backToMain': '返回主 trace',
    'subagent.noResult': '还没有拿到子 agent 的最终返回',
    'trace.noNodes': '这个 trace 当前没有可展示的主要执行节点。',
    'span.noEvents': '这个 span 没有额外事件。',
    'read.noContent': '没有拿到这次读取的内容',
    'artifact.empty': '暂无 artifact',
    'section.metadata': '元数据',
    'common.success': '成功',
    'common.failure': '失败',
    'usage.unreported': 'Token 未上报',
    'usage.input': 'input',
    'usage.output': 'output',
    'usage.total': 'total',
    'usage.cacheHit': 'cache hit',
    'usage.cacheWrite': 'cache write',
    'usage.cost': 'cost',
    'api.stat.totalCalls': '总调用',
    'api.stat.reportedToken': '已上报 Token',
    'api.stat.unreportedToken': '未上报 Token',
    'api.stat.inputUncached': '输入 Token（未命中缓存）',
    'api.stat.inputCached': '输入 Token（命中缓存）',
    'api.stat.output': '输出 Token',
    'api.table.empty': '暂无明细',
    'api.table.noData': '暂无数据',
    'api.keyMetrics': '关键指标',
    'api.byProviderModel': '按厂商 / 模型',
    'api.noCallsTitle': '当前范围没有模型调用',
    'api.noCallsCopy': '先调整时间范围或筛选条件，再看关键指标和厂商模型分布。',
    'api.callCount': ({ n }) => `${n} 次调用`,
    'api.detailTitle': ({ n }) => `调用明细（最近 ${n} 条）`,
    'api.window.aria': '时间范围',
    'status.unreported': '未上报',
    'win.all': '全部',
    'win.7d': '近一周',
    'win.24h': '近24h',
    'win.1h': '近1h',
    'col.provider': '厂商',
    'col.model': '模型',
    'col.totalCalls': '总调用',
    'col.success': '成功',
    'col.failure': '失败',
    'col.reportedToken': '已上报 Token',
    'col.inputUncached': '输入（未命中缓存）',
    'col.inputCached': '输入（命中缓存）',
    'col.inputCachedShort': '输入（命中）',
    'col.output': '输出 Token',
    'col.outputShort': '输出',
    'col.time': '时间',
    'col.status': '状态',
    'col.totalOrError': '总 Token / 失败原因'
  }
};

function t(key, params) {
  const table = I18N[state.lang] || I18N.en;
  const value = key in table ? table[key] : I18N.en[key];
  if (value == null) return key;
  return typeof value === 'function' ? value(params || {}) : value;
}

function translateTree(root) {
  root.querySelectorAll('[data-i18n]').forEach((node) => {
    node.textContent = t(node.getAttribute('data-i18n'));
  });
  root.querySelectorAll('[data-i18n-ph]').forEach((node) => {
    node.setAttribute('placeholder', t(node.getAttribute('data-i18n-ph')));
  });
  root.querySelectorAll('[data-i18n-aria]').forEach((node) => {
    node.setAttribute('aria-label', t(node.getAttribute('data-i18n-aria')));
  });
}

function applyStaticI18n() {
  document.documentElement.lang = state.lang === 'zh' ? 'zh-CN' : 'en';
  document.documentElement.dataset.lang = state.lang;
  translateTree(document);
  document.querySelectorAll('.lang-pill[data-lang]').forEach((btn) => {
    btn.classList.toggle('is-active', btn.dataset.lang === state.lang);
  });
}

function setLang(lang) {
  if (lang !== 'zh' && lang !== 'en') return;
  if (lang === state.lang) return;
  state.lang = lang;
  try {
    localStorage.setItem('tracing:lang', lang);
  } catch {
    /* ignore persistence failures (private mode, etc.) */
  }
  state.detailsRenderSignature = null;
  applyStaticI18n();
  render();
}

function escapeHtml(text) {
  return String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

function timeLocale() {
  return state.lang === 'zh' ? 'zh-CN' : 'en-US';
}

function formatTime(value) {
  if (!value) return '-';
  return new Intl.DateTimeFormat(timeLocale(), {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  }).format(new Date(value));
}

function formatTimeOnly(value) {
  if (!value) return '--:--:--';
  return new Intl.DateTimeFormat(timeLocale(), {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  }).format(new Date(value));
}

function formatDuration(ms) {
  if (ms == null) return '-';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`;
  const mins = Math.floor(ms / 60000);
  const secs = Math.round((ms % 60000) / 1000);
  return `${mins}m ${secs}s`;
}

function shortId(value, len = 12) {
  if (!value) return '-';
  const text = String(value);
  return text.length <= len ? text : `${text.slice(0, len)}...`;
}

function detailKey(span, key) {
  return `${span?.spanId || 'span'}::${key}`;
}

function captureOpenDetails() {
  const openKeys = new Set();
  elements.detailsBody
    ?.querySelectorAll('details[data-detail-key][open]')
    ?.forEach((node) => {
      const key = node.getAttribute('data-detail-key');
      if (key) openKeys.add(key);
    });
  state.openDetailKeys = openKeys;
}

function hydrateOpenDetails() {
  const details = elements.detailsBody?.querySelectorAll('details[data-detail-key]') || [];
  details.forEach((node) => {
    const key = node.getAttribute('data-detail-key');
    if (!key) return;
    if (state.openDetailKeys.has(key)) {
      node.setAttribute('open', '');
    } else {
      node.removeAttribute('open');
    }
    node.addEventListener('toggle', () => {
      if (node.open) state.openDetailKeys.add(key);
      else state.openDetailKeys.delete(key);
    });
  });
}

function scrollNodes() {
  return [
    elements.sessionList,
    elements.traceList,
    elements.detailsBody,
    ...Array.from(elements.apiScene?.querySelectorAll('[data-scroll-key]') || [])
  ].filter(Boolean);
}

function captureScrollState() {
  state.windowScrollY = window.scrollY || window.pageYOffset || 0;
  scrollNodes().forEach((node) => {
    const key = node.getAttribute('data-scroll-key') || node.id;
    if (!key) return;
    state.scrollPositions.set(key, {
      top: node.scrollTop,
      left: node.scrollLeft
    });
  });
}

function applyScrollState() {
  scrollNodes().forEach((node) => {
    const key = node.getAttribute('data-scroll-key') || node.id;
    if (!key) return;
    const pos = state.scrollPositions.get(key);
    if (!pos) return;
    if (typeof pos.top === 'number') node.scrollTop = pos.top;
    if (typeof pos.left === 'number') node.scrollLeft = pos.left;
  });
  if (typeof state.windowScrollY === 'number') {
    window.scrollTo({ top: state.windowScrollY, left: window.scrollX || 0, behavior: 'instant' });
  }
}

function restoreScrollState() {
  if (state.pendingScrollRestore) {
    cancelAnimationFrame(state.pendingScrollRestore);
    state.pendingScrollRestore = null;
  }
  state.pendingScrollRestore = requestAnimationFrame(() => {
    applyScrollState();
    state.pendingScrollRestore = requestAnimationFrame(() => {
      applyScrollState();
      state.pendingScrollRestore = null;
    });
  });
}

function sessionChainLabel(session) {
  if (!session) return '';
  if (session.resumedFrom) return t('session.resumedFrom', { id: shortId(session.resumedFrom, 12) });
  if (session.resumedTo) return t('session.resumedTo', { id: shortId(session.resumedTo, 12) });
  return '';
}

function traceRoundLabel(session, index) {
  if (!session) return t('round.nth', { n: index + 1 });
  const total = sortedTraces(session).length;
  if (index === 0) return t('round.latest');
  return t('round.nth', { n: total - index });
}

function cloneEmptyState() {
  const node = elements.emptyStateTemplate.content.firstElementChild.cloneNode(true);
  translateTree(node);
  return node;
}

function allSessions() {
  return state.data?.sessions || [];
}

function filteredSessions() {
  const query = state.search.trim().toLowerCase();
  return allSessions().filter((session) => {
    if (state.agent !== 'all' && session.agentId !== state.agent) return false;
    if (!query) return true;
    const haystack = [
      session.agentId,
      session.sessionId,
      session.sessionKey,
      session.workspaceDir,
      ...session.traces.map((trace) => `${trace.traceId} ${trace.traceKey || ''}`)
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    return haystack.includes(query);
  });
}

function allApiCalls() {
  return allSessions()
    .flatMap((session) =>
      (session.traces || []).flatMap((trace) =>
        (trace.spans || [])
          .filter((span) => span.name === 'llm.call')
          .map((span) => ({
            sessionId: session.sessionId,
            sessionKey: session.sessionKey,
            sessionAgentId: session.agentId,
            traceKey: trace.traceKey,
            traceId: trace.traceId,
            traceStartTime: trace.startTime,
            traceEndTime: trace.endTime,
            span
          }))
      )
    )
    .sort(
      (a, b) =>
        new Date(b.span.startTime || b.traceStartTime || 0).getTime() -
        new Date(a.span.startTime || a.traceStartTime || 0).getTime()
    );
}

function filteredApiCalls() {
  const query = state.search.trim().toLowerCase();
  const now = Date.now();
  const windowMs =
    state.apiWindow === '1h'
      ? 60 * 60 * 1000
      : state.apiWindow === '24h'
        ? 24 * 60 * 60 * 1000
        : state.apiWindow === '7d'
          ? 7 * 24 * 60 * 60 * 1000
          : null;
  return allApiCalls().filter((entry) => {
    const span = entry.span;
    const startTime = new Date(span.startTime || entry.traceStartTime || 0).getTime();
    if (windowMs != null && now - startTime > windowMs) return false;
    if (state.agent !== 'all' && entry.sessionAgentId !== state.agent) return false;
    if (state.provider !== 'all' && (span.attributes?.['llm.provider'] || '') !== state.provider) return false;
    if (state.model !== 'all' && (span.attributes?.['llm.model'] || '') !== state.model) return false;
    if (!query) return true;
    const haystack = [
      entry.sessionAgentId,
      entry.sessionId,
      entry.sessionKey,
      entry.traceId,
      span.spanId,
      span.displayTitle,
      span.attributes?.['llm.provider'],
      span.attributes?.['llm.model'],
      span.attributes?.['llm.input_preview'],
      span.attributes?.['llm.output_preview']
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    return haystack.includes(query);
  });
}

function renderApiFilters() {
  const calls = allApiCalls();
  const providers = ['all', ...new Set(calls.map((entry) => entry.span.attributes?.['llm.provider']).filter(Boolean))];
  const models = ['all', ...new Set(calls.map((entry) => entry.span.attributes?.['llm.model']).filter(Boolean))];
  elements.providerFilter.innerHTML = providers
    .map((value) => `<option value="${escapeHtml(value)}">${value === 'all' ? t('filter.allProvider') : escapeHtml(value)}</option>`)
    .join('');
  elements.providerFilter.value = state.provider;
  elements.modelFilter.innerHTML = models
    .map((value) => `<option value="${escapeHtml(value)}">${value === 'all' ? t('filter.allModel') : escapeHtml(value)}</option>`)
    .join('');
  elements.modelFilter.value = state.model;
  elements.statusFilter.value = state.apiStatus;
}

function aggregateApiOverview(calls) {
  const summary = {
    totalCalls: calls.length,
    failedCalls: 0,
    usageReportedCalls: 0,
    usageUnreportedCalls: 0,
    input: 0,
    output: 0,
    total: 0,
    cacheRead: 0,
    cacheWrite: 0,
    costTotal: 0,
    durationTotal: 0,
    providerModelCounts: new Map()
  };

  for (const entry of calls) {
    const span = entry.span;
    if (span.isFailed) summary.failedCalls += 1;
    summary.durationTotal += span.durationMs || 0;
    const usage = usageFromSpan(span);
    if (usageReported(usage)) {
      summary.usageReportedCalls += 1;
      if (usage.input != null) summary.input += usage.input;
      if (usage.output != null) summary.output += usage.output;
      if (usage.total != null) summary.total += usage.total;
      if (usage.cacheRead != null) summary.cacheRead += usage.cacheRead;
      if (usage.cacheWrite != null) summary.cacheWrite += usage.cacheWrite;
      if (usage.costTotal != null) summary.costTotal += usage.costTotal;
    } else {
      summary.usageUnreportedCalls += 1;
    }
    const providerModel = [span.attributes?.['llm.provider'], span.attributes?.['llm.model']]
      .filter(Boolean)
      .join(' / ');
    if (providerModel) {
      summary.providerModelCounts.set(providerModel, (summary.providerModelCounts.get(providerModel) || 0) + 1);
    }
  }

  summary.avgDurationMs = summary.totalCalls ? Math.round(summary.durationTotal / summary.totalCalls) : 0;
  summary.topModel =
    [...summary.providerModelCounts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || null;
  return summary;
}

function groupApiByProviderModel(calls) {
  const groups = new Map();
  for (const entry of calls) {
    const span = entry.span;
    const provider = span.attributes?.['llm.provider'] || '-';
    const model = span.attributes?.['llm.model'] || '-';
    const key = `${provider}::${model}`;
    if (!groups.has(key)) {
      groups.set(key, {
        provider,
        model,
        total: 0,
        success: 0,
        failed: 0,
        reported: 0,
        unreported: 0,
        input: 0,
        cacheHit: 0,
        output: 0
      });
    }
    const bucket = groups.get(key);
    bucket.total += 1;
    if (span.isFailed) bucket.failed += 1;
    else bucket.success += 1;
    const usage = usageFromSpan(span);
    if (usageReported(usage)) {
      bucket.reported += 1;
      if (usage.input != null) bucket.input += usage.input;
      if (usage.cacheRead != null) bucket.cacheHit += usage.cacheRead;
      if (usage.output != null) bucket.output += usage.output;
    } else {
      bucket.unreported += 1;
    }
  }
  return [...groups.values()].sort((a, b) => b.total - a.total);
}

function currentSession() {
  return allSessions().find((session) => session.sessionId === state.selectedSessionId) || null;
}

function sortedTraces(session) {
  return [...(session?.traces || [])].sort(
    (a, b) => new Date(b.startTime).getTime() - new Date(a.startTime).getTime()
  );
}

function currentTrace() {
  const session = currentSession();
  if (!session) return null;
  const traces = sortedTraces(session);
  return traces.find((trace) => trace.traceKey === state.selectedTraceKey) || traces[0] || null;
}

function jumpToTraceByTraceId(traceId, spanId) {
  // Navigate to another trace (subagent run ↔ parent turn) by its traceId.
  // The subagent trace lives in the same session, so search current session first.
  if (!traceId) return false;
  const ordered = currentSession()
    ? [currentSession(), ...allSessions().filter((s) => s !== currentSession())]
    : allSessions();
  for (const session of ordered) {
    const trace = (session.traces || []).find((t) => t.traceId === traceId);
    if (!trace) continue;
    state.selectedSessionId = session.sessionId;
    state.selectedTraceKey = trace.traceKey;
    state.selectedSpanId = spanId || trace.tree?.[0]?.spanId || trace.spans?.[0]?.spanId || null;
    state.detailsRenderSignature = null;
    render();
    return true;
  }
  return false;
}

// ── Content search (fuzzy locate across node inputs/outputs) ────────────────
// Server-side /api/search covers span attributes + full artifact text; here we
// render the hit list, navigate to the hit span, and highlight matched terms.

function contentSearchTerms() {
  const query = state.search.trim();
  if (query.length < 2) return [];
  return query.toLowerCase().split(/\s+/).filter(Boolean);
}

function highlightText(text, terms) {
  if (!text) return '';
  const lower = text.toLowerCase();
  const ranges = [];
  for (const term of terms) {
    let idx = 0;
    while (term && (idx = lower.indexOf(term, idx)) !== -1) {
      ranges.push([idx, idx + term.length]);
      idx += term.length;
    }
  }
  if (!ranges.length) return escapeHtml(text);
  ranges.sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const range of ranges) {
    const last = merged[merged.length - 1];
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
    else merged.push([...range]);
  }
  let out = '';
  let pos = 0;
  for (const [start, end] of merged) {
    out += escapeHtml(text.slice(pos, start));
    out += `<mark class="content-hit-mark">${escapeHtml(text.slice(start, end))}</mark>`;
    pos = end;
  }
  return out + escapeHtml(text.slice(pos));
}

async function runContentSearch(query) {
  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
    const payload = await res.json();
    if (state.search.trim() !== query) return;
    state.contentResults = payload.results || [];
  } catch {
    state.contentResults = [];
  }
  renderContentSearchResults();
}

function renderContentSearchResults() {
  const box = elements.contentSearchResults;
  if (!box) return;
  const terms = contentSearchTerms();
  if (!terms.length || state.contentResults == null) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  box.hidden = false;
  const results = state.contentResults;
  const items = results
    .map(
      (hit) => `
      <button class="content-hit-item" type="button"
        data-session-id="${escapeHtml(hit.sessionId)}"
        data-trace-key="${escapeHtml(hit.traceKey)}"
        data-span-id="${escapeHtml(hit.spanId)}">
        <span class="content-hit-title">${escapeHtml(hit.title || hit.name)}${
          hit.subtitle ? ` <span class="content-hit-sub">${escapeHtml(hit.subtitle)}</span>` : ''
        }</span>
        <span class="content-hit-snippet">${highlightText(hit.snippet || '', terms)}</span>
      </button>`
    )
    .join('');
  box.innerHTML =
    `<div class="content-hit-head">${escapeHtml(t('search.contentHits', { n: results.length, more: results.length >= 50 }))}</div>` +
    (items || `<div class="content-hit-empty">${t('search.noContentHits')}</div>`);
}

function applyDetailHighlights() {
  const container = elements.detailsBody;
  if (!container) return;
  container.querySelectorAll('mark.content-hit-mark').forEach((mark) => {
    const parent = mark.parentNode;
    parent.replaceChild(document.createTextNode(mark.textContent), mark);
    parent.normalize();
  });
  const terms = contentSearchTerms();
  if (!terms.length) return;
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  for (const node of textNodes) {
    const text = node.nodeValue;
    if (!text) continue;
    const lower = text.toLowerCase();
    if (!terms.some((term) => lower.includes(term))) continue;
    const wrapper = document.createElement('span');
    wrapper.innerHTML = highlightText(text, terms);
    node.parentNode.replaceChild(wrapper, node);
  }
  if (state.highlightScrollPending) {
    state.highlightScrollPending = false;
    const first = container.querySelector('mark.content-hit-mark');
    if (first) first.scrollIntoView({ block: 'center' });
  }
}

function currentApiCall() {
  return (
    filteredApiCalls().find(
      (entry) =>
        entry.sessionId === state.selectedSessionId &&
        entry.traceKey === state.selectedTraceKey &&
        entry.span.spanId === state.selectedSpanId
    ) || null
  );
}

function findSessionByIdentity(sessionId, sessionKey) {
  return allSessions().find((session) => {
    if (sessionId && session.sessionId === sessionId) return true;
    if (sessionKey && session.sessionKey === sessionKey) return true;
    return false;
  }) || null;
}

function currentSpan() {
  const trace = currentTrace();
  if (!trace) return null;
  return trace.spans.find((span) => span.spanId === state.selectedSpanId) || preferredSpan(trace) || null;
}

function preferredSpan(trace) {
  if (!trace) return null;
  return (
    trace.spans.find((span) => span.name === 'llm.call') ||
    trace.spans[0] ||
    null
  );
}

function promptSkills(span) {
  return span?.attributes?.['skills.prompt.names'] || [];
}

function toNumberOrNull(value) {
  if (value == null || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function usageFromSpan(span) {
  const attrs = span?.attributes || {};
  return {
    input: toNumberOrNull(attrs['llm.usage.input_tokens']),
    output: toNumberOrNull(attrs['llm.usage.output_tokens']),
    total: toNumberOrNull(attrs['llm.usage.total_tokens']),
    cacheRead: toNumberOrNull(attrs['llm.usage.cache_read_tokens']),
    cacheWrite: toNumberOrNull(attrs['llm.usage.cache_write_tokens']),
    costTotal: toNumberOrNull(attrs['llm.usage.cost_total'])
  };
}

function usageReported(usage) {
  if (!usage) return false;
  return ['input', 'output', 'total', 'cacheRead', 'cacheWrite', 'costTotal'].some((key) => usage[key] != null);
}

function usageChipsFromUsage(usage) {
  if (!usageReported(usage)) return [{ text: t('usage.unreported'), soft: true }];
  const chips = [];
  if (usage.input != null) chips.push({ text: `${usage.input} ${t('usage.input')}` });
  if (usage.output != null) chips.push({ text: `${usage.output} ${t('usage.output')}` });
  if (usage.total != null) chips.push({ text: `${usage.total} ${t('usage.total')}` });
  if (usage.cacheRead != null) chips.push({ text: `${usage.cacheRead} ${t('usage.cacheHit')}` });
  if (usage.cacheWrite != null) chips.push({ text: `${usage.cacheWrite} ${t('usage.cacheWrite')}` });
  if (usage.costTotal != null) chips.push({ text: `${t('usage.cost')} ${usage.costTotal}` });
  return chips;
}

function aggregateTraceUsage(trace) {
  const llmSpans = (trace?.spans || []).filter((span) => span.name === 'llm.call');
  const aggregate = {
    input: 0,
    output: 0,
    total: 0,
    cacheRead: 0,
    cacheWrite: 0,
    costTotal: 0,
    reportedCalls: 0,
    unreportedCalls: 0
  };

  for (const span of llmSpans) {
    const usage = usageFromSpan(span);
    if (!usageReported(usage)) {
      aggregate.unreportedCalls += 1;
      continue;
    }
    aggregate.reportedCalls += 1;
    if (usage.input != null) aggregate.input += usage.input;
    if (usage.output != null) aggregate.output += usage.output;
    if (usage.total != null) aggregate.total += usage.total;
    if (usage.cacheRead != null) aggregate.cacheRead += usage.cacheRead;
    if (usage.cacheWrite != null) aggregate.cacheWrite += usage.cacheWrite;
    if (usage.costTotal != null) aggregate.costTotal += usage.costTotal;
  }

  return aggregate;
}

function traceReadSkills(trace) {
  return [
    ...new Set(
      (trace?.spans || [])
        .filter((span) => span.name === 'skill.read')
        .map((span) => span.attributes?.['skill.name'])
        .filter(Boolean)
    )
  ];
}

function sortedTraceSpans(trace) {
  return [...(trace?.spans || [])].sort((a, b) => new Date(a.startTime).getTime() - new Date(b.startTime).getTime());
}

function buildSkillEvidence(trace) {
  const spans = sortedTraceSpans(trace);
  const visibleNames = new Set(['llm.call', 'tool.call', 'subagent.call']);
  const promptedSet = new Set(tracePromptSkills(trace));
  return spans
    .filter((span) => span.name === 'skill.read')
    .map((readSpan) => {
      const readTime = new Date(readSpan.endTime || readSpan.startTime).getTime();
      const readCallId = readSpan.attributes?.['skill.read.tool_call_id'];
      const followUps = spans
        .filter((candidate) => {
          if (candidate.spanId === readSpan.spanId) return false;
          if (!visibleNames.has(candidate.name)) return false;
          if (candidate.name === 'tool.call') {
            const sameReadTool =
              String(candidate.attributes?.['tool.name'] || '').toLowerCase() === 'read' &&
              candidate.attributes?.['tool.call_id'] === readCallId;
            if (sameReadTool) return false;
          }
          return new Date(candidate.startTime).getTime() > readTime;
        })
        .slice(0, 5);
      return {
        skillName: readSpan.attributes?.['skill.name'] || '-',
        source: readSpan.attributes?.['skill.source'] || '-',
        path: readSpan.attributes?.['skill.path'] || '',
        prompted: promptedSet.has(readSpan.attributes?.['skill.name']),
        readSpan,
        followUps,
        status: followUps.length ? 'follow-up seen' : 'read only'
      };
    });
}

function skillStatusTone(status) {
  if (status === 'follow-up seen') return 'success';
  if (status === 'read only') return 'neutral';
  if (status === 'prompted only') return 'soft';
  if (status === 'catalog only') return 'muted';
  return 'muted';
}

function tracePromptSkills(trace) {
  return [
    ...new Set(
      (trace?.spans || [])
        .filter((span) => span.name === 'llm.call')
        .flatMap((span) => promptSkills(span))
        .filter(Boolean)
    )
  ];
}

function traceSummary(trace) {
  const spans = trace?.spans || [];
  return {
    modelCalls: spans.filter((span) => span.name === 'llm.call').length,
    toolCalls: spans.filter((span) => span.name === 'tool.call').length,
    subagents: spans.filter((span) => span.name === 'subagent.call').length,
    readSkills: traceReadSkills(trace),
    promptSkills: tracePromptSkills(trace),
    usage: aggregateTraceUsage(trace)
  };
}

function buildDepthMap(nodes, depthMap = new Map()) {
  for (const node of nodes || []) {
    depthMap.set(node.spanId, node.depth || 0);
    buildDepthMap(node.children || [], depthMap);
  }
  return depthMap;
}

function orderedVisibleSpans(trace) {
  const depthMap = buildDepthMap(trace?.tree || []);
  return (trace?.spans || [])
    .filter((span) => !['skills.scan', 'skills.catalog_read', 'skills.cataloged'].includes(span.name))
    .sort((a, b) => new Date(a.startTime).getTime() - new Date(b.startTime).getTime())
    .map((span) => ({
      ...span,
      depth: depthMap.get(span.spanId) || 0
    }));
}

function visibleTraceTree(nodes) {
  return (nodes || [])
    .filter((node) => !['skills.scan', 'skills.catalog_read', 'skills.cataloged'].includes(node.name))
    .map((node) => ({
      ...node,
      children: visibleTraceTree(node.children || [])
    }));
}

function collectArtifacts(span) {
  const attrs = span?.attributes || {};
  const entries = [];
  const push = (label, filePath) => {
    if (!filePath) return;
    entries.push({ label, path: filePath });
  };
  push('Model Input', attrs['llm.input.artifact_path']);
  push('Model Output', attrs['llm.output.artifact_path']);
  push('Tool Input', attrs['tool.input.artifact_path']);
  push('Tool Output', attrs['tool.output.artifact_path']);
  push('Tool Persisted', attrs['tool.persisted.artifact_path']);
  push('Skill Read', attrs['skill.read.artifact_path']);
  push('Skill Injected', attrs['skill.inject.artifact_path']);
  push('Memory Recall', attrs['memory.recall.artifact_path']);
  push('Memory Store', attrs['memory.store.artifact_path']);
  push('Subagent Result', attrs['subagent.result.artifact_path']);
  if (span?.name === 'skill.read' && span?.parentSpanId) {
    const parentTool = currentTrace()?.spans?.find((candidate) => candidate.spanId === span.parentSpanId) || null;
    const parentAttrs = parentTool?.attributes || {};
    push('Read Request', parentAttrs['tool.input.artifact_path']);
    push('Read Content', parentAttrs['tool.output.artifact_path']);
  }
  return entries;
}

function parsePromptSkillEntries(span, artifacts) {
  const names = promptSkills(span);
  const llmInputArtifact = artifacts.find((entry) => entry.label === 'Model Input')?.artifact;
  const systemPrompt =
    llmInputArtifact?.parsed?.systemPrompt ||
    llmInputArtifact?.parsed?.system_prompt ||
    llmInputArtifact?.parsed?.prompt ||
    llmInputArtifact?.content ||
    '';

  const entries = [];
  const seen = new Set();
  const xmlPattern = /<skill>\s*<name>([\s\S]*?)<\/name>\s*<description>([\s\S]*?)<\/description>\s*<location>([\s\S]*?)<\/location>\s*<\/skill>/g;
  let match;
  while ((match = xmlPattern.exec(systemPrompt)) !== null) {
    const name = match[1]?.trim();
    if (!name || seen.has(name)) continue;
    seen.add(name);
    entries.push({
      name,
      description: match[2]?.trim() || '',
      location: match[3]?.trim() || ''
    });
  }

  if (!entries.length) {
    return names.map((name) => ({ name, description: '', location: '' }));
  }

  const byName = new Map(entries.map((entry) => [entry.name, entry]));
  return names.map((name) => byName.get(name) || { name, description: '', location: '' });
}

function artifactByLabel(artifacts, label) {
  return artifacts.find((entry) => entry.label === label)?.artifact || null;
}

function parseJsonMaybe(value) {
  if (value == null) return null;
  if (typeof value === 'object') return value;
  if (typeof value !== 'string') return null;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function prettyValue(value, fallback = '(empty)') {
  if (value == null || value === '') return fallback;
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function parseProjectContextEntries(systemPrompt) {
  if (!systemPrompt) return [];
  const entries = [];
  const pattern = /^## (\/[^\n]+)\n([\s\S]*?)(?=^## \/[^\n]+\n|\Z)/gm;
  let match;
  while ((match = pattern.exec(systemPrompt)) !== null) {
    const filePath = match[1]?.trim();
    const body = match[2] || '';
    entries.push({
      path: filePath,
      status: body.trim().startsWith('[MISSING]') ? 'missing' : 'loaded',
      preview: body.trim().split('\n').slice(0, 3).join('\n')
    });
  }
  return entries;
}

function extractSystemPromptSection(systemPrompt, heading) {
  if (!systemPrompt) return '';
  const escaped = heading.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(`^## ${escaped}\\n([\\s\\S]*?)(?=^## [^\\n]+\\n|\\Z)`, 'm');
  const match = systemPrompt.match(pattern);
  return match?.[1]?.trim() || '';
}

function extractAvailableTools(systemPrompt) {
  if (!systemPrompt) return [];
  const toolBlockMatch = systemPrompt.match(/Tool names are case-sensitive\. Call tools exactly as listed\.\n([\s\S]*?)\nTOOLS\.md does not control tool availability;/);
  if (!toolBlockMatch) return [];
  return toolBlockMatch[1]
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.startsWith('- '))
    .map((line) => {
      const name = line.slice(2).split(':')[0]?.trim();
      return name || null;
    })
    .filter(Boolean);
}

function parseSafetyBullets(systemPrompt) {
  const section = extractSystemPromptSection(systemPrompt, 'Safety');
  if (!section) return [];
  return section
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.startsWith('- '))
    .map((line) => line.slice(2).trim())
    .filter(Boolean);
}

function parseRuntimeSummary(systemPrompt) {
  const section = extractSystemPromptSection(systemPrompt, 'Runtime');
  if (!section) return [];
  return section
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 4);
}

function classifyHistoryMessage(message) {
  const role = message?.role || 'unknown';
  if (role === 'toolResult') return 'tool_result';
  const items = Array.isArray(message?.content) ? message.content : [message?.content];
  const hasToolCall = items.some((item) => item && typeof item === 'object' && item.type === 'toolCall');
  if (role === 'assistant' && hasToolCall) return 'assistant_tool';
  if (role === 'assistant') return 'assistant';
  if (role === 'user') return 'user';
  return 'system';
}

function summarizeHistoryMessage(message) {
  const role = message?.role || 'unknown';
  const content = message?.content;
  const items = Array.isArray(content) ? content : [content];
  const parts = [];
  let toolCalls = 0;
  let toolResults = 0;

  for (const item of items) {
    if (typeof item === 'string') {
      if (item.trim()) parts.push(item.trim());
      continue;
    }
    if (!item || typeof item !== 'object') continue;
    if (item.type === 'text' && typeof item.text === 'string' && item.text.trim()) {
      parts.push(item.text.trim());
      continue;
    }
    if (item.type === 'toolCall') {
      toolCalls += 1;
      parts.push(`[toolCall] ${item.name || 'tool'}`);
      continue;
    }
  }

  if (role === 'toolResult') {
    toolResults += 1;
    if (message.toolName) parts.push(`[toolResult] ${message.toolName}`);
  }

  return {
    role,
    kind: classifyHistoryMessage(message),
    timestamp: message?.timestamp || null,
    text: parts.join('\n').trim() || message?.errorMessage || '',
    toolCalls,
    toolResults,
    stopReason: message?.stopReason || null,
    provider: message?.provider || null,
    model: message?.model || null,
    toolName: message?.toolName || null,
    errorMessage: message?.errorMessage || null
  };
}

function renderHistoryMessageItem(item) {
  const kindLabel = {
    user: 'user',
    assistant: 'assistant',
    assistant_tool: 'assistant + tool',
    tool_result: 'tool result',
    system: 'system'
  }[item.kind] || item.role;
  const badges = [
    item.toolCalls ? `${item.toolCalls} tool call` : '',
    item.toolResults ? `${item.toolResults} tool result` : '',
    item.toolName || '',
    item.stopReason || '',
    item.errorMessage ? 'error' : ''
  ].filter(Boolean);
  return `
    <article class="history-item history-item-${escapeHtml(item.kind)}">
      <div class="history-item-head">
        <div class="history-item-meta">
          <span class="summary-chip">${escapeHtml(kindLabel)}</span>
          <span class="evidence-time">${escapeHtml(item.timestamp ? formatTime(item.timestamp) : '-')}</span>
        </div>
        ${
          badges.length
            ? `<div class="history-badges">${badges.map((badge) => `<span class="summary-chip summary-chip-soft">${escapeHtml(badge)}</span>`).join('')}</div>`
            : ''
        }
      </div>
      <div class="history-text">${escapeHtml(item.text || '(empty)')}</div>
    </article>
  `;
}

function renderModelInputCard(span, artifacts) {
  if (span.name !== 'llm.call') return '';
  const inp = artifactByLabel(artifacts, 'Model Input')?.parsed;
  if (!inp) return '';
  // Show the ACTUAL request sent to the model — tools + the raw messages list
  // (inp.messages is the ground truth), not a system/history split. Model /
  // provider / token usage already live in the top hero block, so not repeated.
  const messages = Array.isArray(inp.messages) ? inp.messages : [];
  const tools = Array.isArray(inp.tools) ? inp.tools : [];
  const note = `${messages.length} Messages${tools.length ? ` · ${tools.length} Tools` : ''}`;
  const toolsBlock = tools.length
    ? `
      <details class="model-diagnostic-details" data-detail-key="${escapeHtml(detailKey(span, 'req-tools'))}">
        <summary class="model-panel-head"><strong>Tools</strong> <span class="summary-chip">${tools.length}</span></summary>
        <pre class="structured-pre">${escapeHtml(
          tools
            .map((t) => {
              const fn = (t && t.function) || t || {};
              return `- ${fn.name || '?'}${fn.description ? `: ${fn.description}` : ''}`;
            })
            .join('\n')
        )}</pre>
      </details>`
    : '';
  return `
    <article class="content-card wide-card">
      <header><h4>Model Input</h4><span class="card-note">${escapeHtml(note)}</span></header>
      ${toolsBlock}
      ${renderMessages(messages)}
    </article>`;
}

// One message exactly as sent to the model: role header + content (string or
// content blocks) + any tool_calls, mirroring the provider payload.
function renderRequestMessage(m) {
  if (!m || typeof m !== 'object') return String(m);
  const role = m.role || '?';
  const name = m.name ? ` (${m.name})` : '';
  let body = '';
  const c = m.content;
  if (typeof c === 'string') body = c;
  else if (Array.isArray(c))
    body = c
      .map((b) => (b && typeof b === 'object' ? (b.type === 'text' ? b.text || '' : `[${b.type || 'block'}]`) : String(b)))
      .join('\n');
  else if (c != null) body = JSON.stringify(c);
  let calls = '';
  if (Array.isArray(m.tool_calls) && m.tool_calls.length) {
    calls =
      '\n' +
      m.tool_calls
        .map((tc) => {
          const fn = (tc && tc.function) || {};
          const args = typeof fn.arguments === 'string' ? fn.arguments : JSON.stringify(fn.arguments || {});
          return `  → ${fn.name || '?'}(${args})`;
        })
        .join('\n');
  }
  return `[${role}${name}]\n${body}${calls}`;
}

function assistantContentText(message) {
  if (!message) return '';
  if (typeof message === 'string') return message;
  const content = Array.isArray(message.content) ? message.content : [];
  return content
    .map((part) => {
      if (!part || typeof part !== 'object') return '';
      if (part.type === 'text') return part.text || '';
      if (part.type === 'toolCall') {
        return `[toolCall] ${part.name || 'unknown'}`;
      }
      return '';
    })
    .filter(Boolean)
    .join('\n')
    .trim();
}

function llmUsageSummary(span) {
  return usageChipsFromUsage(usageFromSpan(span));
}

function renderAssistantStep(text, index, total) {
  return `
    <article class="history-item history-item-assistant">
      <div class="history-item-head">
        <div class="history-item-meta">
          <span class="summary-chip">Assistant Step</span>
          <span class="evidence-time">${index + 1} / ${total}</span>
        </div>
      </div>
      <div class="history-text">${escapeHtml(text || '(empty)')}</div>
    </article>
  `;
}

function renderModelOutputCard(span, artifacts) {
  if (span.name !== 'llm.call') return '';
  const llmOutput = artifactByLabel(artifacts, 'Model Output')?.parsed;
  if (!llmOutput) return '';

  // OpenClaw dialect: assistantTexts / lastAssistant. raven dialect: output/content
  // + a top-level tool_calls array. A tool-calling turn often has empty text and
  // carries everything in tool_calls, so we must render tool_calls too or the card
  // collapses to "(no assistant output)" and drops the arguments entirely.
  const assistantTexts = Array.isArray(llmOutput.assistantTexts) ? llmOutput.assistantTexts : [];
  const finalMessage =
    assistantContentText(llmOutput.lastAssistant) ||
    llmOutput.output ||
    llmOutput.content ||
    assistantTexts[assistantTexts.length - 1] ||
    '';

  // Normalize raven's flat {id,name,arguments} into the {function:{name,arguments}}
  // shape messageBodyText renders (also tolerate the already-nested OpenAI shape).
  const toolCalls = (Array.isArray(llmOutput.tool_calls) ? llmOutput.tool_calls : []).map((tc) => ({
    function: {
      name: (tc && (tc.name ?? tc.function?.name)) || '?',
      arguments: (tc && (tc.arguments ?? tc.function?.arguments)) ?? {}
    }
  }));

  const steps = assistantTexts.length ? assistantTexts : finalMessage ? [finalMessage] : [];
  let body;
  if (steps.length || toolCalls.length) {
    const msgs = steps.map((t) => ({ role: 'assistant', content: t }));
    if (toolCalls.length) {
      if (msgs.length) msgs[msgs.length - 1].tool_calls = toolCalls;
      else msgs.push({ role: 'assistant', content: '', tool_calls: toolCalls });
    }
    body = renderMessages(msgs);
  } else {
    body = preBody('(no assistant output)');
  }

  const reasoning = typeof llmOutput.reasoning_content === 'string' ? llmOutput.reasoning_content : '';
  const reasoningBlock = reasoning
    ? `<details class="model-diagnostic-details"><summary class="model-panel-head"><strong>Reasoning</strong></summary><pre class="structured-pre">${escapeHtml(reasoning)}</pre></details>`
    : '';

  const finish = llmOutput.finish_reason || span.attributes?.['llm.finish_reason'] || '';
  const note = [
    steps.length > 1 ? `${steps.length} steps` : '',
    toolCalls.length ? `${toolCalls.length} tool call${toolCalls.length > 1 ? 's' : ''}` : '',
    finish
  ]
    .filter(Boolean)
    .join(' · ');
  // Reasoning precedes the answer chronologically (the model thinks, then
  // responds), so render it above the body — collapsed, since it is long and
  // diagnostic.
  return `
    <article class="content-card wide-card">
      <header><h4>Model Output</h4>${note ? `<span class="card-note">${escapeHtml(note)}</span>` : ''}</header>
      ${reasoningBlock}
      ${body}
    </article>`;
}

function renderToolCallCard(span, artifacts) {
  if (span.name !== 'tool.call') return '';
  const attrs = span.attributes || {};
  const toolInput = artifactByLabel(artifacts, 'Tool Input')?.parsed;
  const toolOutput = artifactByLabel(artifacts, 'Tool Output')?.parsed;
  const toolPersisted = artifactByLabel(artifacts, 'Tool Persisted')?.parsed;
  const toolName = attrs['tool.name'] || span.displaySubtitle || '-';
  const inputParams = toolInput?.params ?? parseJsonMaybe(attrs['tool.args_preview']) ?? null;
  const outputResult = toolOutput?.result ?? toolOutput?.output ?? toolOutput?.error ?? null;
  const persistedResult = toolPersisted?.message ?? toolPersisted?.result ?? toolPersisted ?? null;

  return `
    <article class="content-card wide-card">
      <header>
        <h4>Tool Input</h4>
      </header>
      <pre class="structured-pre">${escapeHtml(prettyValue(inputParams))}</pre>
    </article>
    <article class="content-card wide-card">
      <header>
        <h4>Tool Output</h4>
      </header>
      <pre class="structured-pre">${escapeHtml(prettyValue(outputResult))}</pre>
      ${
        persistedResult != null
          ? `
            <details class="model-diagnostic-details" data-detail-key="${escapeHtml(detailKey(span, 'tool-persisted'))}">
              <summary class="model-panel-head">
                <strong>Persisted</strong>
              </summary>
              <pre class="structured-pre">${escapeHtml(prettyValue(persistedResult))}</pre>
            </details>
          `
          : ''
      }
    </article>
  `;
}

function latestChildOutput(session) {
  if (!session) return null;
  const traces = sortedTraces(session);
  for (const trace of traces) {
    const llmSpans = [...(trace.spans || [])]
      .filter((item) => item.name === 'llm.call')
      .sort((a, b) => new Date(b.endTime || b.startTime).getTime() - new Date(a.endTime || a.startTime).getTime());
    const latest = llmSpans[0];
    if (!latest) continue;
    const preview = latest.attributes?.['llm.output_preview'] || '';
    if (preview) {
      return {
        text: preview,
        time: latest.endTime || latest.startTime || null,
        source: 'child_session'
      };
    }
  }
  return null;
}

function renderSubagentCallCard(span, artifacts) {
  if (span.name !== 'subagent.call') return '';
  const attrs = span.attributes || {};
  const toolOutput = artifactByLabel(artifacts, 'Tool Output')?.parsed;
  const dispatchKind = attrs['subagent.id'] ? `named subagent / ${attrs['subagent.id']}` : 'derived subagent';
  const task = attrs['subagent.task'] || attrs['subagent.label'] || '';
  const dispatchInput = task || '';
  const dispatchOutput = toolOutput?.result ?? toolOutput?.output ?? null;
  // raven persists the subagent's final return ON the span itself — the
  // `Subagent Result` artifact ({task, result}) plus the `subagent.result_preview`
  // attribute — NOT as a separate child session (the openclaw model just below).
  // Without this, the card only checked the child-session / spawn-tool paths,
  // found nothing for raven, and fell back to the empty-result placeholder
  // even though the result was right there.
  const subagentResult = artifactByLabel(artifacts, 'Subagent Result')?.parsed;
  const resultText = subagentResult?.result ?? attrs['subagent.result_preview'] ?? null;
  const childSession = findSessionByIdentity(
    attrs['subagent.session_id'] || null,
    attrs['subagent.session_key'] || null
  );
  const childOutcome = latestChildOutput(childSession);
  const outputText = childOutcome?.text || resultText || null;
  const outputSource = childOutcome
    ? 'child session final output'
    : (resultText ? 'subagent result artifact' : 'spawn accepted payload');

  // Forward link: raven runs the subagent as its OWN trace; this dispatch
  // node only summarizes it. Offer a jump into that trace's full tree.
  const subTraceId = attrs['subagent.trace_id'] || null;
  const jumpCard = subTraceId
    ? `
    <article class="content-card wide-card">
      <button type="button" class="jump-link" data-jump-trace-id="${escapeHtml(subTraceId)}">
        → ${t('subagent.openFullTrace')}
      </button>
    </article>`
    : '';

  return `
    ${jumpCard}
    <article class="content-card wide-card">
      <header>
        <h4>Subagent Input</h4>
      </header>
      <pre class="structured-pre">${escapeHtml(prettyValue(dispatchInput))}</pre>
    </article>
    <article class="content-card wide-card">
      <header>
        <h4>Subagent Output</h4>
      </header>
      <pre class="structured-pre">${escapeHtml(prettyValue(outputText ?? dispatchOutput, outputText || dispatchOutput ? '(empty)' : t('subagent.noResult')))}</pre>
    </article>
  `;
}

function renderSubagentRunCard(span) {
  if (span.name !== 'subagent.run') return '';
  const attrs = span.attributes || {};
  const parentTraceId = attrs['subagent.parent_trace_id'] || null;
  const parentSpanId = attrs['subagent.parent_span_id'] || null;
  const task = attrs['subagent.task'] || attrs['subagent.label'] || '';
  // Back link: this trace is one subagent run; return to the turn that spawned it.
  const backCard = parentTraceId
    ? `
    <article class="content-card wide-card">
      <button type="button" class="jump-link" data-jump-trace-id="${escapeHtml(parentTraceId)}"${parentSpanId ? ` data-jump-span-id="${escapeHtml(parentSpanId)}"` : ''}>
        ← ${t('subagent.backToMain')}
      </button>
    </article>`
    : '';
  return `
    ${backCard}
    <article class="content-card wide-card">
      <header>
        <h4>Subagent Task</h4>
      </header>
      <pre class="structured-pre">${escapeHtml(prettyValue(task))}</pre>
    </article>
  `;
}

async function fetchArtifact(filePath) {
  if (!filePath) return null;
  if (state.artifactCache.has(filePath)) return state.artifactCache.get(filePath);
  const response = await fetch(`/api/artifact?path=${encodeURIComponent(filePath)}`);
  if (!response.ok) throw new Error(`Artifact request failed: ${response.status}`);
  const data = await response.json();
  state.artifactCache.set(filePath, data);
  return data;
}

async function loadArtifacts(span) {
  const entries = collectArtifacts(span);
  const items = [];
  for (const entry of entries) {
    try {
      const artifact = await fetchArtifact(entry.path);
      items.push({ ...entry, artifact });
    } catch (error) {
      items.push({
        ...entry,
        artifact: { path: entry.path, parsed: null, content: '', error: error.message }
      });
    }
  }
  return items;
}

function renderAgentFilter() {
  const source = state.appView === 'api'
    ? allApiCalls().map((entry) => entry.sessionAgentId)
    : allSessions().map((session) => session.agentId);
  const agents = ['all', ...new Set(source.filter(Boolean))];
  elements.agentFilter.innerHTML = agents
    .map((agent) => `<option value="${agent}">${agent === 'all' ? t('filter.allAgent') : agent}</option>`)
    .join('');
  elements.agentFilter.value = state.agent;
}

function syncSelection() {
  if (state.appView === 'api') {
    const calls = filteredApiCalls();
    if (!calls.length) {
      state.selectedSessionId = null;
      state.selectedTraceKey = null;
      state.selectedSpanId = null;
      return;
    }

    const active =
      calls.find(
        (entry) =>
          entry.sessionId === state.selectedSessionId &&
          entry.traceKey === state.selectedTraceKey &&
          entry.span.spanId === state.selectedSpanId
      ) || calls[0];

    state.selectedSessionId = active.sessionId;
    state.selectedTraceKey = active.traceKey;
    state.selectedSpanId = active.span.spanId;
    return;
  }

  const sessions = filteredSessions();
  if (!sessions.length) {
    state.selectedSessionId = null;
    state.selectedTraceKey = null;
    state.selectedSpanId = null;
    return;
  }

  const session = sessions.find((item) => item.sessionId === state.selectedSessionId) || sessions[0];
  state.selectedSessionId = session.sessionId;
  const traces = sortedTraces(session);
  const trace = traces.find((item) => item.traceKey === state.selectedTraceKey) || traces[0];
  state.selectedTraceKey = trace?.traceKey || null;
  const span = trace?.spans.find((item) => item.spanId === state.selectedSpanId) || preferredSpan(trace);
  state.selectedSpanId = span?.spanId || null;
}

function renderSessionList() {
  if (state.appView === 'api') {
    elements.sessionList.innerHTML = '';
    return;
  }
  const sessions = filteredSessions();
  elements.sessionList.innerHTML = '';
  if (!sessions.length) {
    elements.sessionList.appendChild(cloneEmptyState());
    return;
  }

  for (const session of sessions) {
    const chainLabel = sessionChainLabel(session);
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `session-card${session.sessionId === state.selectedSessionId ? ' is-active' : ''}`;
    card.title = `${session.sessionId}\n${session.sessionKey || ''}`.trim();
    card.innerHTML = `
      <div class="session-card-head">
        <span class="session-pill">${escapeHtml(session.agentId || 'agent')}</span>
        <span class="session-badge">${session.traceCount} traces</span>
      </div>
      <div class="session-title-row">
        <div class="session-id">${escapeHtml(shortId(session.sessionId, 18))}</div>
        <div class="session-channel">${escapeHtml(session.channelId || 'local')}</div>
      </div>
      <div class="session-meta">
        <div>${escapeHtml(t('session.startedAt', { time: formatTime(session.startedAt) }))}</div>
        <div>${escapeHtml(t('session.updatedAt', { time: formatTime(session.updatedAt) }))}</div>
      </div>
      ${chainLabel ? `<div class="session-chain">${escapeHtml(chainLabel)}</div>` : ''}
    `;
    card.addEventListener('click', () => {
      const traces = sortedTraces(session);
      state.selectedSessionId = session.sessionId;
      state.selectedTraceKey = traces[0]?.traceKey || null;
      state.selectedSpanId = preferredSpan(traces[0])?.spanId || null;
      render();
    });
    elements.sessionList.appendChild(card);
  }
}

function renderTraceList() {
  const session = currentSession();
  elements.traceList.innerHTML = '';
  if (!session) {
    elements.traceTitle.textContent = t('trace.selectSession');
    elements.traceMeta.textContent = '';
    elements.traceList.appendChild(cloneEmptyState());
    return;
  }

  elements.traceTitle.textContent = `${session.agentId || 'agent'} / ${shortId(session.sessionId, 18)}`;
  elements.traceMeta.innerHTML = `
    ${session.workspaceDir ? `<div>${escapeHtml(session.workspaceDir)}</div>` : ''}
    <div>${session.traceCount} traces</div>
    ${sessionChainLabel(session) ? `<div>${escapeHtml(sessionChainLabel(session))}</div>` : ''}
  `;

  sortedTraces(session).forEach((trace, index) => {
    const summary = traceSummary(trace);
    const visibleSpans = orderedVisibleSpans(trace);
    const visibleTree = visibleTraceTree(trace.tree || []);
    const group = document.createElement('section');
    group.className = `trace-group${trace.traceKey === state.selectedTraceKey ? ' is-active' : ''}`;
    group.title = trace.traceKey || trace.traceId;
    group.innerHTML = `
      <div class="trace-header">
        <div class="trace-title">
          <div class="trace-topline">
            <span class="trace-pill">${traceRoundLabel(session, index)}</span>
            <span class="span-chip trace-mini-id">${escapeHtml(shortId(trace.traceId, 10))}</span>
            <span class="trace-status">${formatDuration(trace.durationMs)}</span>
          </div>
          <div class="trace-meta">
            <div>${formatTime(trace.startTime)} -> ${formatTime(trace.endTime)}</div>
            <div>${trace.spanCount} spans</div>
          </div>
        </div>
      </div>
      <div class="trace-summary">
        <span class="summary-chip">${summary.modelCalls} model</span>
        <span class="summary-chip">${summary.toolCalls} tool</span>
        <span class="summary-chip">${summary.subagents} subagent</span>
      </div>
      <div class="trace-tree"></div>
    `;

    const treeHost = group.querySelector('.trace-tree');
    if (!visibleSpans.length) {
      const empty = document.createElement('div');
      empty.className = 'trace-tree-note';
      empty.textContent = t('trace.noNodes');
      treeHost.appendChild(empty);
    } else {
      visibleTree.forEach((span) => renderSpanNode(span, treeHost));
    }
    elements.traceList.appendChild(group);
  });
}

function renderApiOverview(summary) {
  return `
    <section class="api-overview">
      <article class="api-stat-card">
        <div class="api-stat-label">${t('api.stat.totalCalls')}</div>
        <div class="api-stat-value">${summary.totalCalls}</div>
      </article>
      <article class="api-stat-card api-stat-card-success">
        <div class="api-stat-label">${t('common.success')}</div>
        <div class="api-stat-value">${summary.totalCalls - summary.failedCalls}</div>
      </article>
      <article class="api-stat-card api-stat-card-error">
        <div class="api-stat-label">${t('common.failure')}</div>
        <div class="api-stat-value">${summary.failedCalls}</div>
      </article>
      <article class="api-stat-card">
        <div class="api-stat-label">${t('api.stat.reportedToken')}</div>
        <div class="api-stat-value">${summary.usageReportedCalls}</div>
      </article>
      <article class="api-stat-card api-stat-card-warning">
        <div class="api-stat-label">${t('api.stat.unreportedToken')}</div>
        <div class="api-stat-value">${summary.usageUnreportedCalls}</div>
      </article>
      <article class="api-stat-card">
        <div class="api-stat-label">${t('api.stat.inputUncached')}</div>
        <div class="api-stat-value">${summary.input}</div>
      </article>
      <article class="api-stat-card">
        <div class="api-stat-label">${t('api.stat.inputCached')}</div>
        <div class="api-stat-value">${summary.cacheRead}</div>
      </article>
      <article class="api-stat-card">
        <div class="api-stat-label">${t('api.stat.output')}</div>
        <div class="api-stat-value">${summary.output}</div>
      </article>
    </section>
  `;
}

function renderApiCallListRows(calls) {
  if (!calls.length) {
    return `
      <tr>
        <td class="api-table-empty" colspan="8">${t('api.table.empty')}</td>
      </tr>
    `;
  }
  return calls.map((entry) => {
    const span = entry.span;
    const usage = usageFromSpan(span);
    const provider = span.attributes?.['llm.provider'] || '-';
    const model = span.attributes?.['llm.model'] || '-';
    return `
      <tr class="api-table-row${span.spanId === state.selectedSpanId ? ' is-active' : ''}${span.isFailed ? ' is-failed' : ''}" data-span-id="${escapeHtml(span.spanId)}">
        <td>${escapeHtml(formatTime(span.startTime))}</td>
        <td>${span.isFailed ? `<span class="summary-chip summary-chip-error">${t('common.failure')}</span>` : `<span class="summary-chip summary-chip-success">${t('common.success')}</span>`}</td>
        <td>${escapeHtml(provider)}</td>
        <td>${escapeHtml(model)}</td>
        <td>${escapeHtml(String(usage.input ?? 0))}</td>
        <td>${escapeHtml(String(usage.cacheRead ?? 0))}</td>
        <td>${escapeHtml(String(usage.output ?? 0))}</td>
        <td>${usageReported(usage) ? escapeHtml(String(usage.total ?? 0)) : escapeHtml(span.failureLabel || t('usage.unreported'))}</td>
      </tr>
    `;
  }).join('');
}

function apiWindowLabel() {
  return {
    all: t('win.all'),
    '7d': t('win.7d'),
    '24h': t('win.24h'),
    '1h': t('win.1h')
  }[state.apiWindow] || t('win.24h');
}

function renderApiToolbar(calls) {
  const providers = ['all', ...new Set(allApiCalls().map((entry) => entry.span.attributes?.['llm.provider']).filter(Boolean))];
  const models = ['all', ...new Set(allApiCalls().map((entry) => entry.span.attributes?.['llm.model']).filter(Boolean))];
  const agents = ['all', ...new Set(allApiCalls().map((entry) => entry.sessionAgentId).filter(Boolean))];

  return `
    <div class="api-toolbar">
      <div class="api-toolbar-groups">
        <div class="api-window-switch" role="tablist" aria-label="${t('api.window.aria')}">
          ${[
            ['all', t('win.all')],
            ['7d', t('win.7d')],
            ['24h', t('win.24h')],
            ['1h', t('win.1h')]
          ]
            .map(
              ([value, label]) => `
                <button class="api-window-pill${state.apiWindow === value ? ' is-active' : ''}" type="button" data-api-window="${value}">
                  ${label}
                </button>
              `
            )
            .join('')}
        </div>
        <div class="api-select-group">
          <label class="api-inline-field">
            <span>Agent</span>
            <select id="apiAgentFilterInline">
              ${agents
                .map((value) => `<option value="${escapeHtml(value)}"${value === state.agent ? ' selected' : ''}>${value === 'all' ? t('filter.allAgent') : escapeHtml(value)}</option>`)
                .join('')}
            </select>
          </label>
          <label class="api-inline-field">
            <span>Provider</span>
            <select id="apiProviderFilterInline">
              ${providers
                .map((value) => `<option value="${escapeHtml(value)}"${value === state.provider ? ' selected' : ''}>${value === 'all' ? t('filter.all') : escapeHtml(value)}</option>`)
                .join('')}
            </select>
          </label>
          <label class="api-inline-field">
            <span>Model</span>
            <select id="apiModelFilterInline">
              ${models
                .map((value) => `<option value="${escapeHtml(value)}"${value === state.model ? ' selected' : ''}>${value === 'all' ? t('filter.all') : escapeHtml(value)}</option>`)
                .join('')}
            </select>
          </label>
        </div>
      </div>
      <div class="api-toolbar-side">
        <div class="api-filter-summary">
          <span class="summary-chip">${escapeHtml(t('api.callCount', { n: calls.length }))}</span>
        </div>
        <button class="ghost-button" type="button" id="apiRefreshButton">${t('action.refresh')}</button>
      </div>
    </div>
  `;
}

function renderApiDashboard() {
  const scopedCalls = filteredApiCalls();
  const detailCalls = scopedCalls.filter((entry) => {
    const usage = usageFromSpan(entry.span);
    if (state.apiStatus === 'error') return entry.span.isFailed;
    if (state.apiStatus === 'ok') return !entry.span.isFailed;
    if (state.apiStatus === 'unreported') return !usageReported(usage);
    return true;
  });
  const overview = aggregateApiOverview(scopedCalls);
  const providerRows = groupApiByProviderModel(scopedCalls);
  const hasScopedCalls = scopedCalls.length > 0;
  return `
    <section class="api-dashboard">
      <section class="api-dashboard-board api-scope-panel">
        <div class="api-dashboard-inner api-board-scope">
          ${renderApiToolbar(scopedCalls)}
        </div>
        ${
          hasScopedCalls
            ? `
              <div class="api-board-main">
                <section class="api-board-section api-overview-panel">
                  <div class="api-panel-head">
                    <div class="api-panel-title">
                      <h3>${t('api.keyMetrics')}</h3>
                    </div>
                  </div>
                  <div class="api-dashboard-inner">
                    ${renderApiOverview(overview)}
                  </div>
                </section>
                <section class="api-board-section api-provider-panel">
                  <div class="api-panel-head">
                    <div class="api-panel-title">
                      <h3>${t('api.byProviderModel')}</h3>
                    </div>
                  </div>
                  <div class="api-dashboard-inner">
                      <div class="api-table-wrap" data-scroll-key="api-provider-table">
                      <table class="api-table">
                        <thead>
                          <tr>
                            <th>${t('col.provider')}</th>
                            <th>${t('col.model')}</th>
                            <th>${t('col.totalCalls')}</th>
                            <th>${t('col.success')}</th>
                            <th>${t('col.failure')}</th>
                            <th>${t('col.reportedToken')}</th>
                            <th>${t('col.inputUncached')}</th>
                            <th>${t('col.inputCached')}</th>
                            <th>${t('col.output')}</th>
                          </tr>
                        </thead>
                        <tbody>
                          ${
                            providerRows.length
                              ? providerRows.map((row) => `
                                <tr>
                                  <td>${escapeHtml(row.provider)}</td>
                                  <td>${escapeHtml(row.model)}</td>
                                  <td>${row.total}</td>
                                  <td>${row.success}</td>
                                  <td>${row.failed}</td>
                                  <td>${row.reported}</td>
                                  <td>${row.input}</td>
                                  <td>${row.cacheHit}</td>
                                  <td>${row.output}</td>
                                </tr>
                              `).join('')
                              : `<tr><td class="api-table-empty" colspan="9">${t('api.table.noData')}</td></tr>`
                          }
                        </tbody>
                      </table>
                    </div>
                  </div>
                </section>
              </div>
            `
            : `
              <div class="api-board-empty">
                <h3>${t('api.noCallsTitle')}</h3>
                <p>${t('api.noCallsCopy')}</p>
              </div>
            `
        }
      </section>
      <section class="api-dashboard-panel api-detail-panel">
        <div class="api-panel-head">
          <div class="api-panel-title">
            <h3>${escapeHtml(t('api.detailTitle', { n: Math.min(detailCalls.length, 500) }))}</h3>
          </div>
          <div class="overview-tags">
            <button class="api-status-pill${state.apiStatus === 'all' ? ' is-active' : ''}" data-api-status="all" type="button">${t('filter.all')}</button>
            <button class="api-status-pill${state.apiStatus === 'ok' ? ' is-active' : ''}" data-api-status="ok" type="button">${t('common.success')}</button>
            <button class="api-status-pill${state.apiStatus === 'error' ? ' is-active' : ''}" data-api-status="error" type="button">${t('common.failure')}</button>
            <button class="api-status-pill${state.apiStatus === 'unreported' ? ' is-active' : ''}" data-api-status="unreported" type="button">${t('status.unreported')}</button>
          </div>
        </div>
        <div class="api-dashboard-inner">
          <div class="api-table-wrap" data-scroll-key="api-detail-table">
            <table class="api-table api-call-detail-table">
              <thead>
                <tr>
                  <th>${t('col.time')}</th>
                  <th>${t('col.status')}</th>
                  <th>${t('col.provider')}</th>
                  <th>${t('col.model')}</th>
                  <th>${t('col.inputUncached')}</th>
                  <th>${t('col.inputCachedShort')}</th>
                  <th>${t('col.outputShort')}</th>
                  <th>${t('col.totalOrError')}</th>
                </tr>
              </thead>
              <tbody>
                ${renderApiCallListRows(detailCalls)}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </section>
  `;
}

function renderSpanNode(node, host) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `span-node depth-${Math.min(node.depth, 5)}${node.spanId === state.selectedSpanId ? ' is-active' : ''}${node.isFailed ? ' is-failed' : ''}`;
  button.dataset.kind = node.kind;
  button.title = node.spanId;
  const primaryMeta = node.displaySubtitle || (node.kind ? node.kind.charAt(0).toUpperCase() + node.kind.slice(1) : '');
  button.innerHTML = `
    <div class="span-node-main">
      <div class="span-node-time">${formatTimeOnly(node.startTime)}</div>
      <div class="span-node-body">
        <div class="span-node-header">
          <div class="span-node-title">
            <strong>${escapeHtml(node.displayTitle)}</strong>
            ${node.isFailed ? '<span class="summary-chip summary-chip-error">Error</span>' : ''}
          </div>
          <span class="span-chip">${formatDuration(node.durationMs)}</span>
        </div>
        <div class="span-node-subtitle">
          <span>${escapeHtml(primaryMeta)}</span>
        </div>
      </div>
    </div>
  `;
  button.addEventListener('click', () => {
    state.selectedTraceKey = node.traceKey || currentTrace()?.traceKey || null;
    state.selectedSpanId = node.spanId;
    state.detailsRenderSignature = null;
    render();
  });
  host.appendChild(button);
  (node.children || []).forEach((child) => renderSpanNode(child, host));
}

function renderMetadata(span) {
  if (span.name === 'session.turn') {
    const trace = currentTrace();
    const summary = traceSummary(trace);
    const attrs = span.attributes || {};
    const traceStart = trace?.startTime || span.startTime;
    const traceEnd = trace?.endTime || span.endTime;
    const traceDuration = trace?.durationMs ?? span.durationMs;
    const rows = [
      ['Span ID', span.spanId],
      ['Trace ID', span.traceId],
      ['Span Type', span.kind],
      ['Status', span.isFailed ? (span.failureLabel || span.status?.code || 'FAILED') : (span.status?.code || 'OK')],
      ['Failed', span.isFailed ? 'yes' : 'no'],
      ['Duration', formatDuration(traceDuration)],
      ['Started', formatTime(traceStart)],
      ['Ended', formatTime(traceEnd)],
      ['Agent', span.agentId],
      ['Session ID', span.sessionId],
      ['Session Key', span.sessionKey],
      ['Run ID', span.runId],
      ['Trigger', triggerLabel(span.trigger || attrs['trigger'])],
      ['Model Calls', summary.modelCalls],
      ['Tool Calls', summary.toolCalls],
      ['Subagent Calls', summary.subagents],
      ['Skill Reads', summary.readSkills.length]
    ].filter(([, value]) => value || value === 0);

    const events = (span.events || [])
      .map((event) => `<li><span>${formatTime(event.time)}</span><strong>${escapeHtml(event.name)}</strong></li>`)
      .join('');

    return `
      <div class="detail-grid">
        <section class="detail-panel">
          <div class="detail-panel-head">
            <h3>${t('section.metadata')}</h3>
            <span class="meta-tag">${escapeHtml(span.kind)}</span>
          </div>
          <dl class="metadata-grid">
            ${rows
              .map(
                ([label, value]) => `
                  <div class="meta-row">
                    <dt>${escapeHtml(label)}</dt>
                    <dd>${escapeHtml(String(value))}</dd>
                  </div>
                `
              )
              .join('')}
          </dl>
        </section>
        <section class="detail-panel">
          <div class="detail-panel-head">
            <h3>Span Events</h3>
            <span class="meta-tag">${span.events?.length || 0} events</span>
          </div>
          <div class="events-list">
            ${events ? `<ul>${events}</ul>` : `<div class="trace-tree-note">${t('span.noEvents')}</div>`}
          </div>
        </section>
      </div>
    `;
  }
  const attrs = span.attributes || {};
  const rows = [
    ['Span ID', span.spanId],
    ['Trace ID', span.traceId],
    ['Parent Span', span.parentSpanId],
    ['Span Type', span.kind],
    ['Status', span.isFailed ? (span.failureLabel || span.status?.code || 'FAILED') : (span.status?.code || 'OK')],
    ['Failed', span.isFailed ? 'yes' : 'no'],
    ['Duration', formatDuration(span.durationMs)],
    ['Started', formatTime(span.startTime)],
    ['Ended', formatTime(span.endTime)],
    ['Session ID', span.sessionId],
    ['Session Key', span.sessionKey],
    ['Run ID', span.runId],
    ['Agent', span.agentId],
    ['Workspace', span.workspaceDir],
    ['Trigger', span.trigger],
    ['Provider', attrs['llm.provider']],
    ['Provider Class', attrs['llm.provider_class']],
    ['Model', attrs['llm.model']],
    ['Tool', attrs['tool.name']],
    ['Tool Call ID', attrs['tool.call_id']],
    ['Subagent Kind', attrs['subagent.id'] ? 'named subagent' : (span.name === 'subagent.call' ? 'derived subagent' : null)],
    ['Subagent ID', attrs['subagent.id']],
    ['Subagent Label', attrs['subagent.label']],
    ['Subagent Session Key', attrs['subagent.session_key']],
    ['Subagent Run ID', attrs['subagent.run_id']],
    ['Subagent Status', attrs['subagent.status']],
    ['Skills In This Prompt', attrs['skills.prompt.count']],
    ['Read Skill', attrs['skill.name']],
    ['Read Skill Source', attrs['skill.source']],
    ['Read Skill Path', attrs['skill.path']]
  ].filter(([, value]) => value || value === 0);

  const events = (span.events || [])
    .map((event) => `<li><span>${formatTime(event.time)}</span><strong>${escapeHtml(event.name)}</strong></li>`)
    .join('');

  return `
    <div class="detail-grid">
      <section class="detail-panel">
        <div class="detail-panel-head">
          <h3>${t('section.metadata')}</h3>
          <span class="meta-tag">${escapeHtml(span.kind)}</span>
        </div>
        <dl class="metadata-grid">
          ${rows
            .map(
              ([label, value]) => `
                <div class="meta-row">
                  <dt>${escapeHtml(label)}</dt>
                  <dd>${escapeHtml(String(value))}</dd>
                </div>
              `
            )
            .join('')}
        </dl>
      </section>
      <section class="detail-panel">
        <div class="detail-panel-head">
          <h3>Span Events</h3>
          <span class="meta-tag">${span.events?.length || 0} events</span>
        </div>
        <div class="events-list">
          ${events ? `<ul>${events}</ul>` : `<div class="trace-tree-note">${t('span.noEvents')}</div>`}
        </div>
      </section>
    </div>
  `;
}

function prettyArtifactContent(artifact) {
  if (!artifact) return '';
  if (artifact.parsed) return JSON.stringify(artifact.parsed, null, 2);
  return artifact.content || '';
}

function renderSkillReadCard(trace, span, artifacts) {
  if (span.name !== 'skill.read') return '';
  const evidence = buildSkillEvidence(trace).find((item) => item.readSpan.spanId === span.spanId);
  if (!evidence) return '';
  const attrs = span.attributes || {};
  const skillReadArtifact = artifactByLabel(artifacts, 'Skill Read');
  const readContent = artifactByLabel(artifacts, 'Read Content');
  const readRequest = artifactByLabel(artifacts, 'Read Request');
  // raven's read_file→skill.read carries content/params on the re-typed tool
  // span's own Tool Input/Output artifacts (+ skill.result_preview), not the
  // openclaw Skill Read / Read Content / Read Request artifacts. Fall back to
  // raven's so the card isn't empty.
  const toolInput = artifactByLabel(artifacts, 'Tool Input');
  const toolOutput = artifactByLabel(artifacts, 'Tool Output');
  const readInputValue = {
    skill: attrs['skill.name'] || evidence.skillName || '-',
    path: attrs['skill.path'] || evidence.path || '-',
    resolvedPath: attrs['skill.resolved_path'] || skillReadArtifact?.parsed?.resolvedFilePath || '-',
    source: attrs['skill.source'] || evidence.source || '-',
    // Content-driven distinction: a materialized scripts bundle => the agent
    // pulled runnable files; absent => it only loaded the instruction body.
    scriptsDir: attrs['skill.scripts_dir'] || '(instructions only)',
    request: readRequest?.parsed?.params ?? toolInput?.parsed?.params ?? null
  };
  const readContentValue =
    readContent?.parsed?.result ??
    readContent?.parsed?.output ??
    readContent?.content ??
    skillReadArtifact?.parsed?.fileInfo?.preview ??
    toolOutput?.parsed?.result ??
    attrs['skill.result_preview'] ??
    '';
  return `
    <article class="content-card wide-card">
      <header>
        <h4>Skill Input</h4>
      </header>
      <pre class="structured-pre">${escapeHtml(prettyValue(readInputValue))}</pre>
    </article>
    <article class="content-card wide-card">
      <header>
        <h4>Skill Output</h4>
      </header>
      <pre class="structured-pre">${escapeHtml(prettyValue(readContentValue, t('read.noContent')))}</pre>
      ${
        evidence.followUps.length
          ? `
            <details class="model-diagnostic-details" data-detail-key="${escapeHtml(detailKey(span, 'follow-up'))}">
              <summary class="model-panel-head"><strong>Follow-up</strong> <span class="summary-chip">${evidence.followUps.length} spans</span></summary>
              <pre class="structured-pre">${escapeHtml(
                evidence.followUps
                  .map(
                    (follow) =>
                      `${formatTime(follow.startTime)}  ${follow.displayTitle}${
                        follow.displaySubtitle ? `  ·  ${follow.displaySubtitle}` : ''
                      }`
                  )
                  .join('\n')
              )}</pre>
            </details>
          `
          : ''
      }
    </article>
  `;
}

function renderSkillEvidenceCard(trace, span) {
  return '';
}

function triggerLabel(value) {
  if (!value) return 'unknown';
  if (value === 'user') return 'user';
  return String(value);
}

function renderSessionTurnCard(trace, span) {
  if (span.name !== 'session.turn') return '';
  const summary = traceSummary(trace);
  const attrs = span.attributes || {};
  const session = currentSession();
  const start = trace?.startTime ? formatTime(trace.startTime) : (span.startTime ? formatTime(span.startTime) : '-');
  const end = trace?.endTime ? formatTime(trace.endTime) : (span.endTime ? formatTime(span.endTime) : '-');
  const duration = trace?.durationMs != null ? formatDuration(trace.durationMs) : (span.durationMs != null ? formatDuration(span.durationMs) : '-');
  const trigger = triggerLabel(span.trigger || attrs['trigger']);
  const agent = span.agentId || 'agent';
  // "Loaded this turn": tools / plugin backend+tools / skills, captured on the
  // turn span by the plugin. Omit the whole block for old traces that predate it.
  const tools = Array.isArray(attrs['turn.tools']) ? attrs['turn.tools'] : [];
  const pluginBackend = attrs['turn.plugin.backend'] || null;
  const pluginTools = Array.isArray(attrs['turn.plugin.tools']) ? attrs['turn.plugin.tools'] : [];
  const skills = Array.isArray(attrs['turn.skills']) ? attrs['turn.skills'] : [];
  const skillCount = attrs['turn.skill_count'] != null ? attrs['turn.skill_count'] : skills.length;
  const hasCaps = attrs['turn.tools'] !== undefined || attrs['turn.skills'] !== undefined || attrs['turn.plugin.backend'] !== undefined;
  const overview = [
    `summary   ${agent} · ${trigger} triggered turn`,
    `session   ${span.sessionId || '-'}`,
    span.sessionKey && span.sessionKey !== span.sessionId ? `key       ${span.sessionKey}` : null,
    sessionChainLabel(session) ? `chain     ${sessionChainLabel(session)}` : null,
    span.runId ? `run       ${span.runId}` : null,
    `time      ${start}  →  ${end}   (${duration})`
  ]
    .filter(Boolean)
    .join('\n');
  const callChips = [
    `${summary.modelCalls} model`,
    `${summary.toolCalls} tool`,
    `${summary.subagents} subagent`,
    `${summary.readSkills.length} skill.read`
  ]
    .map((c) => `<span class="summary-chip">${escapeHtml(c)}</span>`)
    .join('');
  const capsBlock = hasCaps
    ? `<details class="model-diagnostic-details" data-detail-key="${escapeHtml(detailKey(span, 'turn-caps'))}">
        <summary class="model-panel-head"><strong>Loaded This Turn</strong> <span class="summary-chip">${tools.length} Tools · ${skillCount} Skills</span></summary>
        <pre class="structured-pre">${escapeHtml(
          `plugin backend: ${pluginBackend || 'none'}\n` +
            `plugin tools:   ${pluginTools.length ? pluginTools.join(', ') : '(none)'}\n` +
            `tools (${tools.length}): ${tools.join(', ') || '(none)'}\n` +
            `skills (${skillCount}): ${skills.join(', ') || '(none)'}`
        )}</pre>
      </details>`
    : '';
  return `
    <article class="content-card wide-card">
      <header><h4>Turn Overview</h4><div class="chip-row">${callChips}</div></header>
      <pre class="structured-pre">${escapeHtml(overview)}</pre>
      ${capsBlock}
    </article>`;
}

// ── Descriptor-driven node rendering (see TRACING_STANDARD.md) ──────────────
// The body of a node's detail is rendered from its descriptor: either a
// `custom:<id>` whole-body renderer, or declarative `panels`. Unknown types fall
// back to top-level input/output (or an attribute dump). raven's own rich cards
// are registered here as custom renderers, so dispatch is data-driven with zero
// regression, and any provider's node type renders without viewer code changes.

const CUSTOM_RENDERERS = {
  sessionTurn: (span, trace) => renderSessionTurnCard(trace, span),
  llmCall: (span, trace, artifacts) => renderModelInputCard(span, artifacts) + renderModelOutputCard(span, artifacts),
  subagentCall: (span, trace, artifacts) => renderSubagentCallCard(span, artifacts) + renderSubagentRunCard(span),
  skillRead: (span, trace, artifacts) => renderSkillReadCard(trace, span, artifacts) + renderSkillEvidenceCard(trace, span),
  memoryRecall: (span, trace, artifacts) => renderMemoryRecallCard(span, artifacts),
  memoryStore: (span, trace, artifacts) => renderMemoryStoreCard(span, artifacts),
  memoryFeedback: (span) => renderMemoryFeedbackCard(span)
};

function descriptorFor(name) {
  return (state.descriptors && state.descriptors[name]) || null;
}

function resolveCustom(id, span, trace, artifacts) {
  const fn = CUSTOM_RENDERERS[id];
  return fn ? fn(span, trace, artifacts) : '';
}

function applyTemplate(tmpl, obj) {
  return String(tmpl).replace(/\{([^}]+)\}/g, (_, k) => {
    const v = obj?.[k.trim()];
    return v == null ? '' : String(v);
  });
}

// resolve top-level input/output payload: artifact if referenced, else preview.
function topLevelPayload(io, artifacts) {
  if (!io) return undefined;
  if (io.artifact_path) {
    const found = (artifacts || []).find((a) => a.artifact?.path === io.artifact_path);
    if (found) return found.artifact?.parsed ?? found.artifact?.content;
  }
  return io.preview;
}

// resolve a panel `source` to a value: input | output | attr:x | artifact:prefix.
function resolveSource(span, source, pick, artifacts) {
  if (!source) return undefined;
  let value;
  if (source === 'input') value = topLevelPayload(span.input, artifacts);
  else if (source === 'output') value = topLevelPayload(span.output, artifacts);
  else if (source.startsWith('attr:')) value = span.attributes?.[source.slice(5)];
  else if (source.startsWith('artifact:')) {
    const p = span.attributes?.[`${source.slice('artifact:'.length)}.artifact_path`];
    const found = (artifacts || []).find((a) => a.artifact?.path === p);
    value = found?.artifact?.parsed ?? found?.artifact?.content;
  }
  if (pick && value && typeof value === 'object') value = value[pick];
  return value;
}

function renderByKind(value, kind, panel) {
  if (kind === 'list' && Array.isArray(value)) {
    const text = value
      .map((it, i) => {
        if (panel?.item) return `${i + 1}. ${applyTemplate(panel.item, it)}`;
        if (it && typeof it === 'object') return `${i + 1}. ${it.text ?? JSON.stringify(it)}`;
        return `${i + 1}. ${it}`;
      })
      .join('\n\n');
    return preBody(text);
  }
  if (kind === 'messages' && Array.isArray(value)) {
    return preBody(value.map((m) => `[${(m && m.role) || '?'}]\n${(m && m.content) || ''}`).join('\n\n'));
  }
  if (kind === 'kv' && value && typeof value === 'object') {
    const lines = Object.entries(value).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`);
    return preBody(lines.join('\n'));
  }
  if (kind === 'text') return preBody(typeof value === 'string' ? value : prettyValue(value));
  return `<pre class="structured-pre">${escapeHtml(prettyValue(value))}</pre>`;
}

function renderPanel(span, panel, artifacts, trace) {
  if (panel.render && panel.render.startsWith('custom:')) {
    return resolveCustom(panel.render.slice('custom:'.length), span, trace, artifacts);
  }
  const value = resolveSource(span, panel.source, panel.pick, artifacts);
  return memoryIoCard(panel.title || '', '', renderByKind(value, panel.render || 'json', panel));
}

function renderFallbackBody(span, artifacts) {
  const cards = [];
  if (span.input) {
    cards.push(memoryIoCard('Input', span.input.kind || '', renderByKind(topLevelPayload(span.input, artifacts), span.input.kind || 'json')));
  }
  if (span.output) {
    cards.push(memoryIoCard('Output', span.output.kind || '', renderByKind(topLevelPayload(span.output, artifacts), span.output.kind || 'json')));
  }
  if (cards.length) return cards.join('');
  const arts = artifacts || [];
  if (arts.length) {
    return arts
      .map(
        ({ label, artifact }) => `
          <article class="content-card">
            <header><h4>${escapeHtml(label)}</h4>
              <span class="card-note">${escapeHtml(shortId(artifact?.path || '-', 46))}</span></header>
            <pre>${escapeHtml(prettyArtifactContent(artifact))}</pre>
          </article>`
      )
      .join('');
  }
  return `
    <article class="content-card"><header><h4>Attributes</h4></header>
      <pre class="structured-pre">${escapeHtml(JSON.stringify(span.attributes || {}, null, 2))}</pre></article>`;
}

function renderNodeBody(span, trace, artifacts) {
  const desc = descriptorFor(span.name);
  if (desc) {
    if (desc.render && desc.render.startsWith('custom:')) {
      return resolveCustom(desc.render.slice('custom:'.length), span, trace, artifacts);
    }
    if (Array.isArray(desc.panels)) {
      return desc.panels.map((panel) => renderPanel(span, panel, artifacts, trace)).join('');
    }
  }
  return renderFallbackBody(span, artifacts);
}

function renderContent(span, trace, artifacts) {
  const heroSubtitle = span.displaySubtitle || '';
  const heroModelInfo = span.name === 'llm.call'
    ? [span.attributes?.['llm.provider'], span.attributes?.['llm.model']].filter(Boolean).join(' / ')
    : '';
  const usageChips = span.name === 'llm.call' ? llmUsageSummary(span) : [];
  const heroStatusText = span.isFailed ? (span.failureLabel || span.status?.code || 'FAILED') : (span.status?.code || 'OK');
  const heroStatusClass = span.isFailed ? 'summary-chip summary-chip-error' : 'summary-chip';
  return `
    <div class="content-stack">
      <article class="content-card hero-card">
        <header>
          <h4>${escapeHtml(span.displayTitle)}</h4>
          ${heroSubtitle ? `<span class="card-note">${escapeHtml(heroSubtitle)}</span>` : ''}
        </header>
        <div class="hero-metrics">
          <span class="summary-chip">${formatDuration(span.durationMs)}</span>
          <span class="${heroStatusClass}">${escapeHtml(heroStatusText)}</span>
          ${heroModelInfo ? `<span class="summary-chip">${escapeHtml(heroModelInfo)}</span>` : ''}
          ${usageChips.map((chip) => `<span class="summary-chip${chip.soft ? ' summary-chip-soft' : ''}">${escapeHtml(chip.text)}</span>`).join('')}
        </div>
      </article>
      ${renderNodeBody(span, trace, artifacts)}
    </div>
  `;
}

function renderContentLegacy(span, trace, artifacts) {
  const hiddenArtifactLabels = new Set();
  if (span.name === 'llm.call') {
    hiddenArtifactLabels.add('Model Input');
    hiddenArtifactLabels.add('Model Output');
  }
  if (span.name === 'tool.call' || span.name === 'subagent.call') {
    hiddenArtifactLabels.add('Tool Input');
    hiddenArtifactLabels.add('Tool Output');
    hiddenArtifactLabels.add('Tool Persisted');
  }
  if (span.name === 'skill.read') {
    hiddenArtifactLabels.add('Skill Read');
    hiddenArtifactLabels.add('Read Request');
    hiddenArtifactLabels.add('Read Content');
    // raven's read_file→skill.read is a re-typed tool call: its Tool
    // Input/Output ARE the skill's input/output, now consumed by
    // renderSkillReadCard. Hide them so they don't double-render as
    // separate Tool cards alongside the Skill cards.
    hiddenArtifactLabels.add('Tool Input');
    hiddenArtifactLabels.add('Tool Output');
  }
  if (span.name === 'memory.recall') hiddenArtifactLabels.add('Memory Recall');
  if (span.name === 'memory.store') hiddenArtifactLabels.add('Memory Store');
  const artifactEntries = artifacts.filter((entry) => !hiddenArtifactLabels.has(entry.label));
  const artifactCards = span.name === 'session.turn'
    ? ''
    : (span.name === 'llm.call' || span.name === 'tool.call' || span.name === 'subagent.call' || span.name === 'skill.read' || span.name === 'memory.recall' || span.name === 'memory.store' || span.name === 'memory.feedback') && !artifactEntries.length
    ? ''
    : artifactEntries.length
    ? artifactEntries
        .map(
          ({ label, artifact }) => `
          <article class="content-card">
            <header>
              <h4>${escapeHtml(label)}</h4>
                <span class="card-note">${escapeHtml(shortId(artifact?.path || '-', 46))}</span>
              </header>
              <pre>${escapeHtml(prettyArtifactContent(artifact))}</pre>
            </article>
          `
        )
        .join('')
    : `
      <article class="content-card">
        <header>
          <h4>${t('artifact.empty')}</h4>
      </header>
        <pre>${escapeHtml(JSON.stringify(span.attributes || {}, null, 2))}</pre>
      </article>
    `;

  const heroSubtitle = span.displaySubtitle || '';
  const heroModelInfo = span.name === 'llm.call'
    ? [span.attributes?.['llm.provider'], span.attributes?.['llm.model']].filter(Boolean).join(' / ')
    : '';
  const usageChips = span.name === 'llm.call'
    ? llmUsageSummary(span)
    : [];
  const heroStatusText = span.isFailed ? (span.failureLabel || span.status?.code || 'FAILED') : (span.status?.code || 'OK');
  const heroStatusClass = span.isFailed ? 'summary-chip summary-chip-error' : 'summary-chip';

  return `
    <div class="content-stack">
      <article class="content-card hero-card">
        <header>
          <h4>${escapeHtml(span.displayTitle)}</h4>
          ${heroSubtitle ? `<span class="card-note">${escapeHtml(heroSubtitle)}</span>` : ''}
        </header>
        <div class="hero-metrics">
          <span class="summary-chip">${formatDuration(span.durationMs)}</span>
          <span class="${heroStatusClass}">${escapeHtml(heroStatusText)}</span>
          ${heroModelInfo ? `<span class="summary-chip">${escapeHtml(heroModelInfo)}</span>` : ''}
          ${usageChips.map((chip) => `<span class="summary-chip${chip.soft ? ' summary-chip-soft' : ''}">${escapeHtml(chip.text)}</span>`).join('')}
        </div>
      </article>
      ${renderSessionTurnCard(trace, span)}
      ${renderModelInputCard(span, artifacts)}
      ${renderModelOutputCard(span, artifacts)}
      ${renderToolCallCard(span, artifacts)}
      ${renderSubagentCallCard(span, artifacts)}
      ${renderSubagentRunCard(span)}
      ${renderSkillReadCard(trace, span, artifacts)}
      ${renderSkillEvidenceCard(trace, span)}
      ${renderMemoryCard(span, artifacts)}
      ${artifactCards}
    </div>
  `;
}

// Memory nodes as Input → Output, mirroring the llm/tool cards. Under the everos
// backend only recall/store/feedback fire; each is a request step with a real
// input and output. store's output is the async-distilled deposit family.
function renderMemoryCard(span, artifacts) {
  if (span.name === 'memory.recall') return renderMemoryRecallCard(span, artifacts);
  if (span.name === 'memory.store') return renderMemoryStoreCard(span, artifacts);
  if (span.name === 'memory.feedback') return renderMemoryFeedbackCard(span);
  return '';
}

function memoryIoCard(title, note, bodyHtml) {
  return `
    <article class="content-card wide-card">
      <header><h4>${escapeHtml(title)}</h4>${note ? `<span class="card-note">${escapeHtml(note)}</span>` : ''}</header>
      ${bodyHtml}
    </article>`;
}

// All memory bodies render as a single structured-pre text block — identical
// styling/wrapping/alignment to the llm/tool cards (no bespoke list/flex markup).
function preBody(text) {
  const t = text == null ? '' : String(text);
  return `<pre class="structured-pre">${escapeHtml(t.length ? t : '(empty)')}</pre>`;
}

// Shared role-colored message list — the `messages` body renderer. Used by any
// node whose input/output is a chat message array (llm.call, memory.store).
// content: string OR content-block list; assistant tool_calls appended; the
// system message (reliably huge boilerplate) collapses by default.
function messageBodyText(m) {
  let body = '';
  const c = m && m.content;
  if (typeof c === 'string') body = c;
  else if (Array.isArray(c))
    body = c
      .map((b) => (b && typeof b === 'object' ? (b.type === 'text' ? b.text || '' : `[${b.type || 'block'}]`) : String(b)))
      .join('\n');
  else if (c != null) body = JSON.stringify(c);
  if (Array.isArray(m && m.tool_calls) && m.tool_calls.length) {
    body +=
      (body ? '\n' : '') +
      m.tool_calls
        .map((tc) => {
          const fn = (tc && tc.function) || {};
          const args = typeof fn.arguments === 'string' ? fn.arguments : JSON.stringify(fn.arguments || {});
          return `→ ${fn.name || '?'}(${args})`;
        })
        .join('\n');
  }
  return body;
}

function renderMessages(messages) {
  const list = Array.isArray(messages) ? messages : [];
  if (!list.length) return preBody('(no messages)');
  return `<div class="msg-list">${list
    .map((m) => {
      const role = (m && m.role) || '?';
      const name = m && m.name ? ` · ${escapeHtml(m.name)}` : '';
      const body = messageBodyText(m);
      const bodyHtml = escapeHtml(body || '(empty)');
      const collapse = role === 'system' || body.length > 800;
      const inner = collapse
        ? `<details class="msg-collapse"><summary><span class="msg-role">${escapeHtml(role)}${name}</span><span class="msg-len">${body.length} chars</span></summary><div class="msg-body">${bodyHtml}</div></details>`
        : `<div class="msg-role">${escapeHtml(role)}${name}</div><div class="msg-body">${bodyHtml}</div>`;
      return `<div class="msg msg-${escapeHtml(role)}">${inner}</div>`;
    })
    .join('')}</div>`;
}

function renderMemoryRecallCard(span, artifacts) {
  const attrs = span.attributes || {};
  const query = attrs['memory.query'] || '';
  const inNote = [
    attrs['memory.scope'] ? `scope=${attrs['memory.scope']}` : null,
    attrs['memory.user_id'] ? `user=${attrs['memory.user_id']}` : null,
    attrs['memory.top_k'] != null ? `top_k=${attrs['memory.top_k']}` : null
  ]
    .filter(Boolean)
    .join(' · ');
  const recalled = artifactByLabel(artifacts, 'Memory Recall')?.parsed;
  const list = Array.isArray(recalled) ? recalled : [];
  const hits = attrs['memory.hits'];
  let outText;
  if (list.length) {
    outText = list
      .map((m, i) => {
        const score = m && m.score != null ? ` (score ${Number(m.score).toFixed(4)})` : '';
        return `${i + 1}.${score}\n${(m && m.text) || ''}`;
      })
      .join('\n\n');
  } else {
    outText = hits === 0 ? 'No memories recalled (0 hits).' : 'No recalled-memory artifact captured.';
  }
  return (
    memoryIoCard('Recall · Input', inNote, preBody(query || '(empty query)')) +
    memoryIoCard('Recall · Output', hits != null ? `${hits} hit${hits === 1 ? '' : 's'}` : '', preBody(outText))
  );
}

function renderMemoryStoreCard(span, artifacts) {
  const attrs = span.attributes || {};
  const stored = artifactByLabel(artifacts, 'Memory Store')?.parsed;
  const messages = stored && Array.isArray(stored.messages) ? stored.messages : [];
  const inBody = messages.length ? renderMessages(messages) : preBody('No stored-messages artifact captured.');
  const inNote = [
    attrs['memory.message_count'] != null ? `${attrs['memory.message_count']} msgs` : null,
    attrs['memory.session_id'] || null
  ]
    .filter(Boolean)
    .join(' · ');

  const status = attrs['memory.deposit_status'];
  let outText;
  let outNote = attrs['memory.deposit_summary'] || '';
  if (status === 'pending') {
    outNote = 'async · ~30s';
    outText =
      "Not yet distilled — messages ingested as a raw memcell; everos's async cascade (~30s) hasn't produced episode/fact/foresight yet. Refresh later.";
  } else if (status === 'distilled') {
    let payload = {};
    try {
      payload = JSON.parse(attrs['memory.deposit_json'] || '{}');
    } catch {
      payload = {};
    }
    const families = payload.families || {};
    const TYPE_LABEL = {
      episode: 'episodes',
      atomic_fact: 'atomic facts',
      foresight: 'foresights',
      agent_case: 'agent cases',
      agent_skill: 'agent skills'
    };
    const ORDER = ['episode', 'atomic_fact', 'foresight', 'agent_case', 'agent_skill'];
    const blocks = ORDER.filter((type) => families[type] && families[type].length)
      .map((type) => {
        const lines = families[type]
          .map((entry, i) => {
            const win = entry.startTime && entry.endTime ? `  [${entry.startTime} → ${entry.endTime}]` : '';
            const subj = entry.subject ? `${entry.subject}\n     ` : '';
            return `  ${i + 1}. ${subj}${entry.text || ''}${win}`;
          })
          .join('\n');
        return `${TYPE_LABEL[type] || type} (${families[type].length})\n${lines}`;
      })
      .join('\n\n');
    const mc = payload.parentId ? ` · memcell ${payload.parentId}` : '';
    const delta = payload.deltaMs == null ? '?' : payload.deltaMs;
    const header = `everos distilled this from the turn's memcell${mc}\njoined by (session_id, timestamp), Δ≈${delta}ms. Profile (user.md) is a merged everos doc — async, not per-turn.`;
    outText = `${header}\n\n${blocks || 'Ingested, but nothing was distilled from this turn.'}`;
  } else {
    outText = 'Deposit not resolved (everos data unavailable to the viewer).';
  }
  return memoryIoCard('Store · Input', inNote, inBody) + memoryIoCard('Store · Output', outNote, preBody(outText));
}

function renderMemoryFeedbackCard(span) {
  const attrs = span.attributes || {};
  const norm = (v) => {
    if (typeof v === 'string') {
      const parsed = parseJsonMaybe(v);
      if (parsed !== undefined && parsed !== null) v = parsed;
    }
    if (v == null) return '(none)';
    if (Array.isArray(v)) return v.length ? v.join(', ') : '(none)';
    return String(v);
  };
  const inText = `injected skills: ${norm(attrs['memory.injected'])}\nused skills: ${norm(attrs['memory.used'])}`;
  const outText = 'No output — backend.feedback is a deliberate no-op in everos 1.0: the skill-usage signal is captured but not consumed.';
  return memoryIoCard('Feedback · Input', '', preBody(inText)) + memoryIoCard('Feedback · Output', 'everos no-op', preBody(outText));
}

function renderRaw(span, artifacts) {
  const payload = {
    span,
    artifacts: artifacts.map(({ label, artifact }) => ({
      label,
      path: artifact?.path,
      parsed: artifact?.parsed,
      content: artifact?.parsed ? undefined : artifact?.content,
      error: artifact?.error
    }))
  };
  return `
    <div class="raw-view">
      <pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>
    </div>
  `;
}

async function renderDetails() {
  if (state.appView === 'api') return;
  state.detailsRenderSignature = currentDetailsSignature();
  captureOpenDetails();
  const trace = currentTrace();
  const span = currentSpan();
  elements.detailsBody.innerHTML = '';
  if (!trace || !span) {
    elements.detailsTitle.textContent = t('details.selectSpan');
    elements.detailsBody.appendChild(cloneEmptyState());
    restoreScrollState();
    return;
  }

  elements.detailsTitle.textContent = span.name === 'llm.call'
    ? span.displayTitle
    : `${span.displayTitle}${span.displaySubtitle ? ` / ${span.displaySubtitle}` : ''}`;
  const artifacts = await loadArtifacts(span);
  if (!currentSpan() || currentSpan().spanId !== span.spanId) return;

  if (state.selectedTab === 'metadata') {
    elements.detailsBody.innerHTML = renderMetadata(span);
    hydrateOpenDetails();
    restoreScrollState();
    applyDetailHighlights();
    return;
  }

  if (state.selectedTab === 'raw') {
    elements.detailsBody.innerHTML = renderRaw(span, artifacts);
    hydrateOpenDetails();
    restoreScrollState();
    applyDetailHighlights();
    return;
  }

  elements.detailsBody.innerHTML = renderContent(span, trace, artifacts);
  elements.detailsBody.querySelectorAll('[data-jump-trace-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      jumpToTraceByTraceId(btn.dataset.jumpTraceId, btn.dataset.jumpSpanId || null);
    });
  });
  hydrateOpenDetails();
  restoreScrollState();
  applyDetailHighlights();
}

function currentDetailsSignature() {
  const trace = currentTrace();
  const span = currentSpan();
  if (!trace || !span) return null;
  return [state.appView, state.selectedTab, state.selectedSessionId, state.selectedTraceKey, state.selectedSpanId].join('::');
}

function render() {
  captureScrollState();
  syncSelection();
  renderAgentFilter();
  renderApiFilters();
  if (elements.connectionStatus) {
    elements.connectionStatus.textContent = state.connectionStatus === 'connected' ? t('status.connected') : t('status.disconnected');
    elements.connectionStatus.classList.toggle('is-connected', state.connectionStatus === 'connected');
    elements.connectionStatus.classList.toggle('is-disconnected', state.connectionStatus !== 'connected');
  }
  if (elements.lastUpdated) {
    elements.lastUpdated.textContent = formatTimeOnly(state.lastUpdated);
  }
  document.body.classList.toggle('app-view-api', state.appView === 'api');
  if (state.appView === 'api') {
    elements.apiScene.innerHTML = renderApiDashboard();
    restoreScrollState();
    const agentInline = document.getElementById('apiAgentFilterInline');
    const providerInline = document.getElementById('apiProviderFilterInline');
    const modelInline = document.getElementById('apiModelFilterInline');
    const apiRefreshButton = document.getElementById('apiRefreshButton');
    agentInline?.addEventListener('change', (event) => {
      state.agent = event.target.value;
      render();
    });
    providerInline?.addEventListener('change', (event) => {
      state.provider = event.target.value;
      render();
    });
    modelInline?.addEventListener('change', (event) => {
      state.model = event.target.value;
      render();
    });
    apiRefreshButton?.addEventListener('click', () => {
      state.artifactCache.clear();
      loadData();
    });
    elements.apiScene.querySelectorAll('[data-api-window]').forEach((button) => {
      button.addEventListener('click', () => {
        state.apiWindow = button.dataset.apiWindow;
        render();
      });
    });
    elements.apiScene.querySelectorAll('[data-api-status]').forEach((button) => {
      button.addEventListener('click', () => {
        state.apiStatus = button.dataset.apiStatus;
        render();
      });
    });
    elements.apiScene.querySelectorAll('.api-table-row[data-span-id]').forEach((row) => {
      row.addEventListener('click', () => {
        const entry = filteredApiCalls().find((item) => item.span.spanId === row.dataset.spanId);
        if (!entry) return;
        state.selectedSessionId = entry.sessionId;
        state.selectedTraceKey = entry.traceKey;
        state.selectedSpanId = entry.span.spanId;
        state.appView = 'trace';
        state.detailsRenderSignature = null;
        render();
      });
    });
  } else {
  elements.listTitle.textContent = t('sessions.title');
  elements.searchInput.placeholder = t('search.placeholder');
    renderSessionList();
    renderTraceList();
    const nextDetailsSignature = currentDetailsSignature();
    if (nextDetailsSignature !== state.detailsRenderSignature) {
      state.detailsRenderSignature = nextDetailsSignature;
      renderDetails();
    } else {
      restoreScrollState();
    }
  }
  elements.tabs.forEach((tab) => {
    tab.classList.toggle('is-active', tab.dataset.tab === state.selectedTab);
  });
  elements.appViewButtons.forEach((button) => {
    button.classList.toggle('is-active', button.dataset.appView === state.appView);
  });
}

async function fetchDescriptors() {
  try {
    const res = await fetch('/api/descriptors');
    const payload = await res.json();
    const map = {};
    for (const desc of payload.descriptors || []) {
      if (desc && desc.type) map[desc.type] = desc;
    }
    state.descriptors = map;
  } catch {
    state.descriptors = {};
  }
}

async function loadData(options = {}) {
  const { silent = false } = options;
  if (state.isLoading) return;
  state.isLoading = true;
  if (!silent) {
    elements.refreshButton.disabled = true;
  }
  try {
    const response = await fetch(`/api/data?ts=${Date.now()}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`API failed with status ${response.status}`);
    state.data = await response.json();
    state.connectionStatus = 'connected';
    state.lastUpdated = new Date().toISOString();
    render();
  } catch (error) {
    state.connectionStatus = 'disconnected';
    if (!silent) {
      elements.sessionList.innerHTML = `
        <div class="empty-state">
          <p class="empty-title">${t('error.loadFailed')}</p>
          <p class="empty-copy">${escapeHtml(error.message)}</p>
        </div>
      `;
    }
  } finally {
    state.isLoading = false;
    if (!silent) {
      elements.refreshButton.disabled = false;
    }
  }
}

function startAutoRefresh() {
  if (state.autoRefreshTimer) {
    clearInterval(state.autoRefreshTimer);
  }
  state.autoRefreshTimer = window.setInterval(() => {
    if (document.hidden) return;
    loadData({ silent: true });
  }, AUTO_REFRESH_MS);
}

let contentSearchTimer = null;
elements.searchInput.addEventListener('input', (event) => {
  state.search = event.target.value;
  state.detailsRenderSignature = null;
  render();
  applyDetailHighlights();
  if (contentSearchTimer) clearTimeout(contentSearchTimer);
  const query = state.search.trim();
  if (query.length < 2) {
    state.contentResults = null;
    renderContentSearchResults();
    return;
  }
  contentSearchTimer = setTimeout(() => runContentSearch(query), 250);
});

elements.contentSearchResults.addEventListener('click', (event) => {
  const item = event.target.closest('[data-span-id]');
  if (!item) return;
  state.appView = 'trace';
  state.selectedSessionId = item.dataset.sessionId;
  state.selectedTraceKey = item.dataset.traceKey;
  state.selectedSpanId = item.dataset.spanId;
  state.highlightScrollPending = true;
  state.detailsRenderSignature = null;
  render();
});

elements.agentFilter.addEventListener('change', (event) => {
  state.agent = event.target.value;
  state.detailsRenderSignature = null;
  render();
});

elements.providerFilter.addEventListener('change', (event) => {
  state.provider = event.target.value;
  render();
});

elements.modelFilter.addEventListener('change', (event) => {
  state.model = event.target.value;
  render();
});

elements.statusFilter.addEventListener('change', (event) => {
  state.apiStatus = event.target.value;
  render();
});

elements.refreshButton.addEventListener('click', () => {
  state.artifactCache.clear();
  loadData();
});

elements.appViewButtons.forEach((button) => {
  button.addEventListener('click', () => {
    state.appView = button.dataset.appView;
    render();
  });
});

elements.tabs.forEach((tab) => {
  tab.addEventListener('click', () => {
    state.selectedTab = tab.dataset.tab;
    elements.tabs.forEach((item) => item.classList.toggle('is-active', item === tab));
    renderDetails();
  });
});

document.querySelectorAll('.lang-pill[data-lang]').forEach((button) => {
  button.addEventListener('click', () => setLang(button.dataset.lang));
});

applyStaticI18n();

fetchDescriptors().then(() => {
  loadData();
  startAutoRefresh();
});
