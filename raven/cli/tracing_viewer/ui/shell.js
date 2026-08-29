// Served by server.js for `/`. The dashboard shell lives here as a JS
// module (not an .html asset) so the viewer ships as plain JS/CSS only.
module.exports = String.raw`<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Tracing Dashboard</title>
    <link rel="stylesheet" href="/app.css">
  </head>
  <body>
    <header class="app-header">
      <div class="app-brand">
        <div class="app-logo" aria-hidden="true">∿</div>
        <div>
          <h1>Tracing Dashboard</h1>
        </div>
      </div>
      <div class="app-status">
        <span class="status-pill" id="connectionStatus" data-i18n="status.disconnected">Disconnected</span>
        <span class="status-text"><span data-i18n="header.updated">Updated</span>: <strong id="lastUpdated">--:--:--</strong></span>
        <div class="lang-switch" role="group" aria-label="Language">
          <button class="lang-pill" data-lang="en" type="button">EN</button>
          <button class="lang-pill" data-lang="zh" type="button">中</button>
        </div>
        <button class="ghost-button app-action" id="refreshButton" type="button" data-i18n="action.refresh">Refresh</button>
      </div>
    </header>
    <nav class="workspace-tabs">
      <button class="workspace-tab" data-app-view="api" type="button" data-i18n="view.api">API Calls</button>
      <button class="workspace-tab is-active" data-app-view="trace" type="button" data-i18n="view.trace">Traces</button>
    </nav>
    <div class="scene scene-trace shell" id="traceScene">
      <aside class="pane pane-sessions">
        <div class="pane-head">
          <div>
            <h1 id="listTitle" data-i18n="sessions.title">Sessions</h1>
          </div>
        </div>
        <div class="controls">
          <label class="field">
            <span>Agent</span>
            <select id="agentFilter"></select>
          </label>
          <label class="field field-search">
            <span data-i18n="field.search">Search</span>
            <input id="searchInput" type="search" data-i18n-ph="search.placeholder" placeholder="session / trace / keyword">
          </label>
          <label class="field api-only">
            <span>Provider</span>
            <select id="providerFilter"></select>
          </label>
          <label class="field api-only">
            <span>Model</span>
            <select id="modelFilter"></select>
          </label>
          <label class="field api-only">
            <span data-i18n="field.status">Status</span>
            <select id="statusFilter">
              <option value="all" data-i18n="status.all">All statuses</option>
              <option value="ok" data-i18n="status.okOnly">Success only</option>
              <option value="error" data-i18n="status.errorOnly">Failed only</option>
              <option value="unreported" data-i18n="status.unreportedOnly">Token unreported only</option>
            </select>
          </label>
        </div>
        <div class="content-search-results" id="contentSearchResults" hidden></div>
        <div class="session-list" id="sessionList" data-scroll-key="trace-session-list"></div>
      </aside>

      <main class="pane pane-traces">
        <div class="pane-head">
          <div>
            <h2 id="traceTitle" data-i18n="trace.selectSession">Select a session</h2>
          </div>
          <div class="head-meta" id="traceMeta"></div>
        </div>
        <div class="trace-list" id="traceList" data-scroll-key="trace-list"></div>
      </main>

      <section class="pane pane-details">
        <div class="pane-head">
          <div>
            <h2 id="detailsTitle" data-i18n="details.selectSpan">Select a span</h2>
          </div>
          <div class="tabs">
            <button class="tab is-active" data-tab="content" type="button" data-i18n="detailtab.content">Input / Output</button>
            <button class="tab" data-tab="metadata" type="button" data-i18n="detailtab.metadata">Metadata</button>
            <button class="tab" data-tab="raw" type="button" data-i18n="detailtab.raw">Raw</button>
          </div>
        </div>
        <div class="details-body" id="detailsBody" data-scroll-key="trace-details"></div>
      </section>
    </div>
    <main class="scene scene-api" id="apiScene"></main>

    <template id="emptyStateTemplate">
      <div class="empty-state">
        <p class="empty-title" data-i18n="empty.title">Nothing to show yet</p>
        <p class="empty-copy" data-i18n="empty.copy">Refresh, or send another message to try.</p>
      </div>
    </template>

    <script src="/app.js"></script>
  </body>
  </html>
`;
