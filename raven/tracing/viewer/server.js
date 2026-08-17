#!/usr/bin/env node

const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { URL } = require('url');
const { applyStateDirArg } = require('./state-dir');
applyStateDirArg();
const { readJsonl, getLogsDir } = require('./log-store');
const everosDeposits = require('./everos-deposits');

const PORT = Number(process.env.TRACE_UI_PORT || process.env.TRACING_UI_PORT || 4318);
const STATE_DIR =
  process.env.TRACING_STATE_DIR ||
  process.env.OPENCLAW_STATE_DIR ||
  path.join(os.homedir(), '.openclaw');
const LOGS_DIR = getLogsDir();
const ARTIFACTS_DIR = path.join(LOGS_DIR, 'audit-artifacts');
const STATIC_DIR = path.join(__dirname, 'ui');
const SHELL_HTML = require('./ui/shell.js');
const BUNDLED_DESCRIPTORS_DIR = path.join(__dirname, 'descriptors');
const STATE_DESCRIPTORS_DIR = path.join(STATE_DIR, 'descriptors');
const CLI_DESCRIPTORS = (() => {
  const argv = process.argv;
  const i = argv.indexOf('--descriptors');
  if (i !== -1 && argv[i + 1]) return argv[i + 1];
  const eq = argv.find((a) => a.startsWith('--descriptors='));
  return eq ? eq.slice('--descriptors='.length) : null;
})();

function sendJson(res, statusCode, payload) {
  res.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store'
  });
  res.end(JSON.stringify(payload));
}

function sendText(res, statusCode, text, contentType = 'text/plain; charset=utf-8') {
  res.writeHead(statusCode, {
    'Content-Type': contentType,
    'Cache-Control': 'no-store'
  });
  res.end(text);
}

function parseTime(value) {
  const parsed = Date.parse(value || '');
  return Number.isFinite(parsed) ? parsed : 0;
}

function shortId(value, len = 8) {
  if (!value) return '';
  const text = String(value);
  return text.length <= len ? text : text.slice(0, len);
}

function durationMs(start, end) {
  const a = parseTime(start);
  const b = parseTime(end);
  if (!a || !b) return 0;
  return Math.max(0, b - a);
}

function compareSpansByTime(a, b) {
  const timeDiff = parseTime(a.startTime) - parseTime(b.startTime);
  if (timeDiff !== 0) return timeDiff;
  const rank = (span) => {
    if (span?.name === 'session.turn') return 0;
    if (span?.name === 'llm.call') return 1;
    if (span?.name === 'subagent.call') return 2;
    return 3;
  };
  const rankDiff = rank(a) - rank(b);
  if (rankDiff !== 0) return rankDiff;
  return String(a?.spanId || '').localeCompare(String(b?.spanId || ''));
}

function pickMostFrequent(values) {
  const counts = new Map();
  for (const value of values || []) {
    if (!value) continue;
    counts.set(value, (counts.get(value) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])))[0]?.[0] || null;
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

function readJsonFileMaybe(filePath) {
  if (!filePath || typeof filePath !== 'string') return null;
  if (!fs.existsSync(filePath)) return null;
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return null;
  }
}

function dedupeSpans(spans) {
  const byId = new Map();
  for (const span of spans) {
    if (!span || !span.spanId) continue;
    byId.set(span.spanId, span);
  }
  return [...byId.values()];
}

function isUuidLike(value) {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

function buildSessionIdentityIndex(spans) {
  const countsBySessionKey = new Map();
  const metaBySessionId = new Map();

  for (const span of spans) {
    if (span?.sessionKey) {
      if (!countsBySessionKey.has(span.sessionKey)) countsBySessionKey.set(span.sessionKey, new Map());
      const counts = countsBySessionKey.get(span.sessionKey);
      const sessionId = span.sessionId || '';
      counts.set(sessionId, (counts.get(sessionId) || 0) + 1);
    }

    if (span?.sessionId) {
      const current = metaBySessionId.get(span.sessionId) || {
        keyCounts: new Map(),
        agentCounts: new Map(),
        workspaceCounts: new Map()
      };
      if (span.sessionKey) current.keyCounts.set(span.sessionKey, (current.keyCounts.get(span.sessionKey) || 0) + 1);
      if (span.agentId) current.agentCounts.set(span.agentId, (current.agentCounts.get(span.agentId) || 0) + 1);
      if (span.workspaceDir) current.workspaceCounts.set(span.workspaceDir, (current.workspaceCounts.get(span.workspaceDir) || 0) + 1);
      metaBySessionId.set(span.sessionId, current);
    }
  }

  const canonicalIdBySessionKey = new Map();
  for (const [sessionKey, counts] of countsBySessionKey.entries()) {
    const entries = [...counts.entries()].filter(([sessionId]) => sessionId);
    if (!entries.length) continue;
    entries.sort((a, b) => {
      const [idA, countA] = a;
      const [idB, countB] = b;
      const uuidBias = Number(isUuidLike(idB)) - Number(isUuidLike(idA));
      if (uuidBias !== 0) return uuidBias;
      if (countB !== countA) return countB - countA;
      return idA.localeCompare(idB);
    });
    canonicalIdBySessionKey.set(sessionKey, entries[0][0]);
  }

  const preferredValue = (counts) =>
    [...counts.entries()].sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])))[0]?.[0] || null;

  const sessionMetaById = new Map();
  for (const [sessionId, meta] of metaBySessionId.entries()) {
    sessionMetaById.set(sessionId, {
      sessionKey: preferredValue(meta.keyCounts),
      agentId: preferredValue(meta.agentCounts),
      workspaceDir: preferredValue(meta.workspaceCounts)
    });
  }

  return { canonicalIdBySessionKey, sessionMetaById };
}

function projectSpanForDisplay(span, identityIndex) {
  const { canonicalIdBySessionKey, sessionMetaById } = identityIndex;
  const canonicalIdForKey = span.sessionKey ? canonicalIdBySessionKey.get(span.sessionKey) : null;
  const canonicalMetaForSession = span.sessionId ? sessionMetaById.get(span.sessionId) || null : null;
  const parentAliasSessionId = span.sessionId && !isUuidLike(span.sessionId)
    ? canonicalIdBySessionKey.get(span.sessionId) || null
    : null;
  const resolvedSessionId = isUuidLike(span.sessionId)
    ? span.sessionId
    : span.sessionId || parentAliasSessionId || canonicalIdForKey || null;

  if (span.name === 'subagent.call') {
    const parentSessionId = isUuidLike(span.sessionId)
      ? span.sessionId
      : parentAliasSessionId || canonicalIdForKey || span.sessionId || null;
    const parentMeta = (parentSessionId && sessionMetaById.get(parentSessionId)) || canonicalMetaForSession || {};
    return {
      ...span,
      sessionId: parentSessionId,
      sessionKey: parentMeta.sessionKey || span.sessionKey || null,
      agentId: parentMeta.agentId || span.agentId || null,
        workspaceDir: parentMeta.workspaceDir || span.workspaceDir || null
    };
  }

  return {
    ...span,
    sessionId: resolvedSessionId,
    sessionKey: span.sessionKey || canonicalMetaForSession?.sessionKey || null,
    agentId: span.agentId || canonicalMetaForSession?.agentId || null,
    workspaceDir: span.workspaceDir || canonicalMetaForSession?.workspaceDir || null
  };
}

function normalizeSpan(span) {
  const attrs = span.attributes || {};
  const kind = attrs['span.type'] || 'internal';
  const failure = detectSpanFailure(span, attrs);
  return {
    traceKey: null,
    traceId: span.traceId,
    spanId: span.spanId,
    parentSpanId: span.parentSpanId || null,
    name: span.name,
    kind,
    startTime: span.startTime,
    endTime: span.endTime,
    durationMs: durationMs(span.startTime, span.endTime),
    status: span.status || { code: 'OK', message: '' },
    isFailed: failure.isFailed,
    failureLabel: failure.failureLabel,
    attributes: attrs,
    events: span.events || [],
    sessionId: attrs['session.id'] || null,
    sessionKey: attrs['session.key'] || null,
    agentId: attrs['agent.id'] || null,
    workspaceDir: attrs['workspace.dir'] || null,
    runId: attrs['run.id'] || null,
    trigger: attrs.trigger || null,
    channelId: attrs['channel.id'] || null,
    displayTitle: buildSpanTitle(span.name, attrs),
    displaySubtitle: buildSpanSubtitle(span.name, attrs)
  };
}

function detectSpanFailure(span, attrs) {
  const statusCode = String(span?.status?.code || 'OK').toUpperCase();
  if (statusCode && statusCode !== 'OK') {
    return { isFailed: true, failureLabel: statusCode };
  }

  if (span?.name === 'llm.call') {
    const outputPreview = parseJsonMaybe(attrs['llm.output_preview']);
    const stopReason = outputPreview?.stopReason;
    const errorMessage = outputPreview?.errorMessage;
    if (stopReason && String(stopReason).toLowerCase() === 'error') {
      return { isFailed: true, failureLabel: errorMessage ? 'model error' : 'error' };
    }
    if (errorMessage) {
      return { isFailed: true, failureLabel: 'model error' };
    }
  }

  if (span?.name === 'tool.call') {
    const preview = parseJsonMaybe(attrs['tool.result_preview']);
    const toolError = attrs['tool.error'];
    if (toolError) return { isFailed: true, failureLabel: 'tool error' };
    if (preview?.status && String(preview.status).toLowerCase() === 'error') {
      return { isFailed: true, failureLabel: preview.error ? 'tool error' : 'error result' };
    }
    if (preview?.error) return { isFailed: true, failureLabel: 'tool error' };
  }

  if (span?.name === 'subagent.call') {
    // Only genuine-failure statuses are failures. openclaw used 'accepted' for
    // a successful spawn; raven uses 'ok' / 'completed' / 'ended'. Flagging
    // anything != 'accepted' wrongly marked successful raven subagents red —
    // the span's own status.code (checked above) is authoritative.
    const subagentStatus = String(attrs['subagent.status'] || '').toLowerCase();
    if (subagentStatus === 'error' || subagentStatus === 'failed') {
      return { isFailed: true, failureLabel: subagentStatus };
    }
  }

  if (span?.name === 'skill.read') {
    // openclaw's skill.read signals success via skill.read.* byte/sha attrs;
    // raven's read_file→skill.read signals it via skill.result_preview /
    // skill.path / tool.output.artifact_bytes. Accept EITHER family as evidence
    // of a real read so a successful raven read (status.code already OK above)
    // isn't false-flagged "read failed" just because the openclaw attrs are absent.
    const bytes = attrs['skill.read.file_bytes'];
    const sha1 = attrs['skill.read.file_sha1'];
    const preview = attrs['skill.read.preview'] || attrs['skill.result_preview'];
    const artifactBytes = attrs['skill.read.artifact_bytes'] ?? attrs['tool.output.artifact_bytes'];
    const artifactPath = attrs['skill.read.artifact_path'] || attrs['skill.path'];
    if (
      (bytes == null || bytes === 0) &&
      !sha1 &&
      !String(preview || '').trim() &&
      !(artifactBytes > 0) &&
      !artifactPath
    ) {
      return { isFailed: true, failureLabel: 'read failed' };
    }
  }

  return { isFailed: false, failureLabel: '' };
}

function buildSpanTitle(name, attrs) {
  if (name === 'llm.call') return 'Model Call';
  if (name === 'tool.call') return 'Tool Call';
  if (name === 'subagent.call') return 'Subagent Dispatch';
  if (name === 'subagent.run') return 'Subagent Run';
  if (name === 'skills.cataloged') return 'Skills Cataloged';
  if (name === 'skills.catalog_read') return 'Skill Catalog Read';
  if (name === 'skill.read') return 'Skill';
  if (name === 'skills.scan') return 'Skills Scan';
  if (name === 'session.turn') return 'Turn';
  if (name === 'skill.inject') return 'Skill Inject';
  if (name === 'skill.rewrite') return 'Query Rewrite';
  if (name === 'skill.gate') return 'Skill Gate';
  if (name === 'context.curate') return 'Context Curation';
  if (name.startsWith('personalize.')) return 'Personalize ' + name.slice('personalize.'.length);
  if (name === 'memory.recall') return 'Memory Recall';
  if (name === 'memory.store') return 'Memory Store';
  if (name === 'memory.extract') return 'Memory Extract';
  if (name === 'memory.consolidate') return 'Memory Consolidate';
  if (name === 'memory.profile_refresh') return 'Profile Refresh';
  if (name === 'memory.feedback') return 'Memory Feedback';
  if (name === 'plugin.load') return 'Plugin Load';
  if (name === 'tracing.bootstrap') return 'Tracing Bootstrap';
  return name;
}

function buildSpanSubtitle(name, attrs) {
  if (name === 'session.turn') {
    const q = attrs['turn.input_preview'];
    return q ? String(q).replace(/\s+/g, ' ').trim() : '';
  }
  if (name === 'llm.call') return [attrs['llm.provider'], attrs['llm.model']].filter(Boolean).join(' / ');
  if (name === 'tool.call') return attrs['tool.name'] || '';
  if (name === 'subagent.call') {
    if (attrs['subagent.id']) return `named subagent / ${attrs['subagent.id']}`;
    return attrs['subagent.label'] || 'derived subagent';
  }
  if (name === 'subagent.run') return attrs['subagent.label'] || attrs['subagent.task'] || 'subagent';
  if (name === 'skills.cataloged') return `${attrs['skills.cataloged.count'] || 0} skills`;
  if (name === 'skills.catalog_read') return attrs['skills.catalog_read.skill_name'] || '';
  if (name === 'skill.read') {
    const label = attrs['skill.name'] || attrs['skill.id'] || '';
    return attrs['skill.scripts_dir'] ? `${label} + scripts`.trim() : label;
  }
  if (name === 'skills.scan') return `${attrs['skills.scan.total_count'] || 0} skills`;
  if (name === 'skill.rewrite') {
    const nr = attrs['skill.rewrite.need_retrieval'];
    return nr === false ? 'no retrieval' : (attrs['skill.rewrite.query_preview'] || '');
  }
  if (name === 'skill.gate') {
    return `${attrs['skill.gate.selected_count'] ?? 0}/${attrs['skill.gate.candidate_count'] ?? 0} selected`;
  }
  if (name === 'context.curate') return attrs['context.curate.produced'] ? 'curated' : '';
  if (name.startsWith('personalize.')) return attrs['personalize.ok'] === false ? 'failed' : '';
  if (name === 'skill.inject') {
    const names = attrs['skill.inject.names'];
    const label = Array.isArray(names) && names.length ? names.join(', ') : `${attrs['skill.inject.count'] || 0} skills`;
    return attrs['skill.inject.via'] ? `${label} (${attrs['skill.inject.via']})` : label;
  }
  if (name === 'memory.recall') {
    return [attrs['memory.scope'], attrs['memory.hits'] != null ? `${attrs['memory.hits']} hits` : null]
      .filter(Boolean).join(' / ');
  }
  if (name === 'memory.store') {
    const base = attrs['memory.message_count'] != null ? `${attrs['memory.message_count']} msgs` : '';
    if (attrs['memory.deposit_summary']) return base ? `${base} → ${attrs['memory.deposit_summary']}` : attrs['memory.deposit_summary'];
    if (attrs['memory.deposit_status'] === 'pending') return base ? `${base} · not yet distilled` : 'not yet distilled';
    return base;
  }
  if (name === 'memory.extract') return attrs['memory.surface'] || '';
  if (name === 'memory.consolidate') return attrs['memory.message_count'] != null ? `${attrs['memory.message_count']} msgs` : '';
  if (name === 'memory.profile_refresh') return attrs['memory.sections_rewritten'] != null ? `${attrs['memory.sections_rewritten']} sections` : '';
  if (name === 'memory.feedback') return attrs['memory.kind'] || '';
  if (name === 'plugin.load') return [attrs['plugin.name'], attrs['plugin.contribution']].filter(Boolean).join(' / ');
  if (name === 'tracing.bootstrap') return attrs['plugin.id'] || '';
  return attrs['hook.name'] || '';
}

function buildTraceTree(spans) {
  const spanIds = new Set((spans || []).map((span) => span.spanId));
  const byParent = new Map();
  for (const span of spans) {
    const effectiveParentId = span.displayParentSpanId ?? span.parentSpanId;
    const parentKey = effectiveParentId && spanIds.has(effectiveParentId) ? effectiveParentId : '__root__';
    if (!byParent.has(parentKey)) byParent.set(parentKey, []);
    byParent.get(parentKey).push(span);
  }

  for (const [key, children] of byParent.entries()) {
    children.sort(compareSpansByTime);
    byParent.set(key, dedupeSiblingSpans(children));
  }

  function visit(node, depth) {
    return {
      ...node,
      depth,
      children: (byParent.get(node.spanId) || []).map((child) => visit(child, depth + 1))
    };
  }

  return (byParent.get('__root__') || []).map((root) => visit(root, 0));
}

function dedupeSiblingSpans(spans) {
  const seen = new Set();
  const result = [];
  for (const span of spans) {
    const attrs = span.attributes || {};
    const key = [
      span.name,
      span.displayParentSpanId || span.parentSpanId || '',
      attrs['skills.cataloged.artifact_sha1'] || '',
      attrs['skills.catalog_read.path'] || attrs['skills.load.path'] || '',
      attrs['skill.path'] || '',
      attrs['tool.call_id'] || '',
      attrs['llm.input.artifact_sha1'] || '',
      attrs['llm.output.artifact_sha1'] || '',
      span.startTime || ''
    ].join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(span);
  }
  return result;
}

function extractSessionsSpawnPayload(span, identityIndex) {
  const attrs = span?.attributes || {};
  if (String(attrs['tool.name'] || '').trim().toLowerCase() !== 'sessions_spawn') return null;

  const inputArtifact = readJsonFileMaybe(attrs['tool.input.artifact_path']);
  const outputArtifact = readJsonFileMaybe(attrs['tool.output.artifact_path']);
  const persistedArtifact = readJsonFileMaybe(attrs['tool.persisted.artifact_path']);

  const input =
    inputArtifact?.params ||
    inputArtifact?.input ||
    inputArtifact?.payload ||
    parseJsonMaybe(attrs['tool.args_preview']) ||
    null;

  const persistedText =
    persistedArtifact?.message?.details?.status
      ? persistedArtifact.message.details
      : persistedArtifact?.message?.content?.find?.((part) => typeof part?.text === 'string')?.text ||
        persistedArtifact?.result?.content?.find?.((part) => typeof part?.text === 'string')?.text ||
        null;

  const output =
    parseJsonMaybe(persistedText) ||
    persistedArtifact?.message?.details ||
    outputArtifact?.result?.details ||
    outputArtifact?.result ||
    parseJsonMaybe(attrs['tool.result_preview']) ||
    null;

  if (!input || !output) return null;
  if (String(input.runtime || '').trim().toLowerCase() !== 'subagent') return null;
  if (String(output.status || '').trim().toLowerCase() !== 'accepted') return null;

  const childSessionKey = output.childSessionKey || null;
  const childSessionId =
    output.childSessionId ||
    (childSessionKey ? identityIndex.canonicalIdBySessionKey.get(childSessionKey) || null : null);

  return {
    input,
    output,
    childSessionKey,
    childSessionId
  };
}

function synthesizeSubagentCallSpans(spans, identityIndex) {
  const existingToolCallIds = new Set(
    spans
      .filter((span) => span.name === 'subagent.call')
      .map((span) => span.attributes?.['subagent.tool_call_id'])
      .filter(Boolean)
  );
  const derived = [];

  for (const span of spans) {
    if (span.name !== 'tool.call') continue;
    const attrs = span.attributes || {};
    const toolCallId = attrs['tool.call_id'] || null;
    if (toolCallId && existingToolCallIds.has(toolCallId)) continue;
    const payload = extractSessionsSpawnPayload(span, identityIndex);
    if (!payload) continue;
    const { input, output, childSessionKey, childSessionId } = payload;
    derived.push({
      ...span,
      spanId: `${span.spanId}::subagent`,
      name: 'subagent.call',
      kind: 'subagent',
      startTime: span.startTime,
      endTime: span.endTime,
      durationMs: span.durationMs,
      displayTitle: 'Subagent Dispatch',
      displaySubtitle: input.agentId || input.label || '',
      parentSpanId: span.spanId,
      displayParentSpanId: span.spanId,
      attributes: {
        ...attrs,
        'subagent.id': input.agentId || null,
        'subagent.task': input.task || input.label || '',
        'subagent.label': input.label || '',
        'subagent.mode': input.mode || output.mode || null,
        'subagent.session_key': childSessionKey,
        'subagent.session_id': childSessionId,
        'subagent.run_id': output.runId || null,
        'subagent.status': 'accepted',
        'subagent.source': 'sessions_spawn',
        'subagent.tool_call_id': toolCallId
      },
      events: [...(span.events || []), { time: span.endTime || span.startTime, name: 'subagent_spawn_accepted' }]
    });
  }

  return [...spans, ...derived];
}

function chooseTraceRoot(groups, traceId, timestamp) {
  const candidates = (groups || [])
    .filter((group) => group.root && (!traceId || group.traceId === traceId))
    .sort((a, b) => compareSpansByTime(a.root, b.root));
  if (!candidates.length) return null;
  const targetTime = parseTime(timestamp);
  const before = candidates.filter((group) => parseTime(group.root.startTime) <= targetTime);
  return before[before.length - 1] || candidates[0];
}

function buildTraceGroups(sessionSpans) {
  const spans = [...(sessionSpans || [])].sort(compareSpansByTime);
  const spanById = new Map(spans.map((span) => [span.spanId, span]));
  const childrenByParent = new Map();

  for (const span of spans) {
    if (!span.parentSpanId || !spanById.has(span.parentSpanId)) continue;
    if (!childrenByParent.has(span.parentSpanId)) childrenByParent.set(span.parentSpanId, []);
    childrenByParent.get(span.parentSpanId).push(span);
  }

  for (const children of childrenByParent.values()) {
    children.sort(compareSpansByTime);
  }

  // session.turn roots the main trace; subagent.run roots a subagent's OWN
  // trace (raven subagents run as a separate trace, linked via the dispatch
  // node's subagent.trace_id). Both are first-class trace roots.
  let rootCandidates = spans.filter((span) => (span.name === 'session.turn' || span.name === 'subagent.run') && (!span.parentSpanId || !spanById.has(span.parentSpanId)));
  if (!rootCandidates.length) {
    rootCandidates = spans.filter((span) => !span.parentSpanId || !spanById.has(span.parentSpanId));
  }

  const groups = rootCandidates
    .sort(compareSpansByTime)
    .map((root) => ({
      id: root.spanId,
      root,
      traceId: root.traceId,
      spans: [],
      events: []
    }));

  const assignedGroupIdBySpanId = new Map();
  const assignDescendants = (group, span) => {
    if (!span || assignedGroupIdBySpanId.has(span.spanId)) return;
    assignedGroupIdBySpanId.set(span.spanId, group.id);
    group.spans.push({ ...span });
    for (const child of childrenByParent.get(span.spanId) || []) {
      assignDescendants(group, child);
    }
  };

  for (const group of groups) {
    assignDescendants(group, group.root);
  }

  const keptGroups = [];
  for (const group of groups) {
    const hasMeaningfulDescendant = group.spans.some(
      (span) => span.spanId !== group.root.spanId && span.name !== 'session.turn'
    );
    const richerSiblingExists = groups.some(
      (candidate) =>
        candidate !== group &&
        candidate.traceId === group.traceId &&
        candidate.root.sessionKey === group.root.sessionKey &&
        candidate.root.agentId === group.root.agentId &&
        candidate.spans.length > group.spans.length
    );
    if (!hasMeaningfulDescendant && richerSiblingExists) {
      for (const span of group.spans) {
        assignedGroupIdBySpanId.delete(span.spanId);
      }
      continue;
    }
    keptGroups.push(group);
  }

  const syntheticGroups = [];
  const unassignedSpans = spans.filter((span) => !assignedGroupIdBySpanId.has(span.spanId));
  for (const span of unassignedSpans) {
    if (
      span.name === 'session.turn' &&
      keptGroups.some(
        (group) =>
          group.root &&
          group.traceId === span.traceId &&
          group.root.sessionId === span.sessionId &&
          group.root.sessionKey === span.sessionKey
      )
    ) {
      continue;
    }
    let group = chooseTraceRoot(keptGroups, span.traceId, span.startTime);
    if (!group) {
      group = {
        id: `synthetic::${span.spanId}`,
        root: null,
        traceId: span.traceId,
        spans: [],
        events: []
      };
      syntheticGroups.push(group);
      keptGroups.push(group);
    }
    assignedGroupIdBySpanId.set(span.spanId, group.id);
    group.spans.push({
      ...span,
      displayParentSpanId: group.root ? group.root.spanId : null
    });
  }

  return keptGroups.map((group) => {
    const spanIds = new Set(group.spans.map((span) => span.spanId));
    const normalizedSpans = group.spans
      .map((span) => {
        const effectiveParent = span.displayParentSpanId ?? span.parentSpanId;
        const displayParentSpanId =
          effectiveParent && spanIds.has(effectiveParent)
            ? effectiveParent
            : group.root && span.spanId !== group.root.spanId
              ? group.root.spanId
              : null;
        return {
          ...span,
          displayParentSpanId
        };
      })
      .sort(compareSpansByTime);

    return {
      id: group.id,
      root: group.root,
      traceId: group.traceId,
      startTime: normalizedSpans[0]?.startTime || group.root?.startTime || null,
      endTime: normalizedSpans
        .map((span) => span.endTime)
        .sort((a, b) => parseTime(b) - parseTime(a))[0] || group.root?.endTime || null,
      spans: normalizedSpans,
      events: []
    };
  });
}

// Enrich memory.store spans with the everos deposit family (episode/fact/foresight/…)
// distilled from that turn's memcell. Joined per-trace by (session_id, timestamp).
// Read fresh each call so the view always reflects the latest async distillation.
function enrichStoreDeposits(spans) {
  const storeSpans = spans.filter((span) => span.name === 'memory.store');
  if (!storeSpans.length) return;
  let index;
  try {
    index = everosDeposits.buildDepositIndex(everosDeposits.resolveEverosRoot('raven'));
  } catch {
    return;
  }
  if (!index || !index.size) return;
  for (const span of storeSpans) {
    const attrs = span.attributes || {};
    const sessionId = attrs['memory.session_id'] || span.sessionKey || span.sessionId;
    const deposit = everosDeposits.resolveDeposit(index, sessionId, parseTime(span.startTime));
    if (!deposit) {
      attrs['memory.deposit_status'] = 'pending';
      span.attributes = attrs;
      span.displaySubtitle = buildSpanSubtitle(span.name, attrs);
      continue;
    }
    const payload = {
      parentId: deposit.parentId,
      timestamp: deposit.timestamp,
      deltaMs: deposit.deltaMs,
      counts: deposit.counts,
      families: {}
    };
    for (const [type, entries] of Object.entries(deposit.types)) {
      payload.families[type] = entries.map((entry) => ({
        id: entry.id,
        subject: entry.subject,
        text: entry.text,
        startTime: entry.startTime,
        endTime: entry.endTime
      }));
    }
    attrs['memory.deposit_status'] = 'distilled';
    attrs['memory.deposit_summary'] = everosDeposits.summarize(deposit);
    attrs['memory.deposit_json'] = JSON.stringify(payload);
    span.attributes = attrs;
    span.displaySubtitle = buildSpanSubtitle(span.name, attrs);
  }
}

function buildSessions() {
  const rawSpans = dedupeSpans(readJsonl('spans')).map(normalizeSpan);
  const identityIndex = buildSessionIdentityIndex(rawSpans);
  const projectedSpans = rawSpans.map((span) => projectSpanForDisplay(span, identityIndex)).filter(Boolean);
  const spans = synthesizeSubagentCallSpans(projectedSpans, identityIndex);
  enrichStoreDeposits(spans);
  const events = readJsonl('events');
  const spansBySessionId = new Map();
  for (const span of spans) {
    if (!span.sessionId) continue;
    if (!spansBySessionId.has(span.sessionId)) spansBySessionId.set(span.sessionId, []);
    spansBySessionId.get(span.sessionId).push(span);
  }

  const sessions = [];
  for (const [sessionId, sessionSpans] of spansBySessionId.entries()) {
    const traceGroups = buildTraceGroups(sessionSpans);
    const sessionKey = pickMostFrequent(sessionSpans.map((span) => span.sessionKey));
    const agentId = pickMostFrequent(sessionSpans.map((span) => span.agentId));
    const workspaceDir = pickMostFrequent(sessionSpans.map((span) => span.workspaceDir));
    const trigger = pickMostFrequent(sessionSpans.map((span) => span.trigger));
    const channelId = pickMostFrequent(sessionSpans.map((span) => span.channelId));
    const sessionEvents = events.filter((event) => {
      if (event.sessionId && event.sessionId === sessionId) return true;
      if (sessionKey && event.sessionKey === sessionKey) return true;
      return false;
    });
    const sessionStartEvent = sessionEvents.find(
      (event) => event.type === 'session_start' && event.sessionId === sessionId
    );
    const resumedFrom = sessionStartEvent?.event?.resumedFrom || null;

    for (const event of sessionEvents.sort((a, b) => parseTime(a.timestamp) - parseTime(b.timestamp))) {
      const group = chooseTraceRoot(traceGroups, event.traceId, event.timestamp);
      if (group) group.events.push(event);
    }

    const hiddenSpanNames = new Set(['skills.scan', 'skills.catalog_read', 'skills.cataloged']);
    const traces = traceGroups
      .map((trace) => {
        const traceKey = `${sessionId}::${trace.root?.spanId || trace.id}`;
        const normalizedSpans = trace.spans.map((span) => ({ ...span, traceKey }));
        const meaningfulVisibleSpans = normalizedSpans.filter(
          (span) => !hiddenSpanNames.has(span.name) && span.name !== 'session.turn'
        );
        return {
          traceKey,
          traceId: trace.traceId,
          sessionId,
          sessionKey,
          agentId,
          workspaceDir,
          trigger,
          channelId,
          startTime: trace.startTime,
          endTime: trace.endTime,
          durationMs: durationMs(trace.startTime, trace.endTime),
          spanCount: normalizedSpans.length,
          visibleSpanCount: meaningfulVisibleSpans.length,
          spans: normalizedSpans,
          tree: buildTraceTree(normalizedSpans),
          events: trace.events.sort((a, b) => parseTime(a.timestamp) - parseTime(b.timestamp))
        };
      })
      .filter((trace) => trace.visibleSpanCount > 0)
      .sort((a, b) => parseTime(b.startTime) - parseTime(a.startTime));

    sessions.push({
      sessionId,
      sessionKey,
      agentId,
      workspaceDir,
      trigger,
      channelId,
      resumedFrom,
      resumedTo: null,
      startedAt: sessionSpans.map((span) => span.startTime).sort((a, b) => parseTime(a) - parseTime(b))[0] || null,
      updatedAt: sessionSpans.map((span) => span.endTime).sort((a, b) => parseTime(b) - parseTime(a))[0] || null,
      traceCount: traces.length,
      traces
    });
  }

  const sessionById = new Map(sessions.map((session) => [session.sessionId, session]));
  for (const session of sessions) {
    if (!session.resumedFrom) continue;
    const parent = sessionById.get(session.resumedFrom);
    if (parent) parent.resumedTo = session.sessionId;
  }

  sessions.sort((a, b) => parseTime(b.updatedAt) - parseTime(a.updatedAt));

  return {
    generatedAt: new Date().toISOString(),
    sessions
  };
}

function isSafeArtifactPath(filePath) {
  const resolved = path.resolve(filePath);
  return resolved.startsWith(path.resolve(ARTIFACTS_DIR) + path.sep) || resolved === path.resolve(ARTIFACTS_DIR);
}

function readArtifact(filePath) {
  if (!filePath || !isSafeArtifactPath(filePath)) return null;
  if (!fs.existsSync(filePath)) return null;
  const content = fs.readFileSync(filePath, 'utf8');
  let parsed = null;
  try {
    parsed = JSON.parse(content);
  } catch {}
  return {
    path: filePath,
    content,
    parsed
  };
}

function serveStatic(reqPath, res) {
  if (reqPath === '/' || reqPath === '/index.html') {
    sendText(res, 200, SHELL_HTML, 'text/html; charset=utf-8');
    return;
  }
  const filePath = path.resolve(path.join(STATIC_DIR, reqPath));
  if (!filePath.startsWith(path.resolve(STATIC_DIR) + path.sep)) {
    sendText(res, 403, 'Forbidden');
    return;
  }
  if (!fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
    sendText(res, 404, 'Not found');
    return;
  }
  const ext = path.extname(filePath).toLowerCase();
  const typeByExt = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8'
  };
  sendText(res, 200, fs.readFileSync(filePath, 'utf8'), typeByExt[ext] || 'text/plain; charset=utf-8');
}

// Content search across all spans: title/subtitle/attributes plus the FULL text
// of every artifact a span references (messages, recalled memories, deposits).
// Fuzzy = case-insensitive, whitespace-split terms, all must match (AND).
const MAX_SEARCH_RESULTS = 50;
const MAX_ARTIFACT_SEARCH_BYTES = 512 * 1024;

function artifactTextForSearch(filePath) {
  if (!filePath || !isSafeArtifactPath(filePath)) return '';
  try {
    const stat = fs.statSync(filePath);
    if (!stat.isFile() || stat.size > MAX_ARTIFACT_SEARCH_BYTES) return '';
    return fs.readFileSync(filePath, 'utf8');
  } catch {
    return '';
  }
}

function makeSnippet(text, term, radius = 60) {
  const idx = text.toLowerCase().indexOf(term);
  if (idx < 0) return '';
  const start = Math.max(0, idx - radius);
  const end = Math.min(text.length, idx + term.length + radius);
  const head = start > 0 ? '…' : '';
  const tail = end < text.length ? '…' : '';
  return (head + text.slice(start, end) + tail).replace(/\s+/g, ' ').trim();
}

function searchSpans(query) {
  const terms = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return [];
  const results = [];
  const data = buildSessions();
  for (const session of data.sessions) {
    for (const trace of session.traces || []) {
      for (const span of trace.spans || []) {
        const attrs = span.attributes || {};
        // pieces: [label, text]; artifacts carry the full node input/output.
        const pieces = [
          ['title', [span.displayTitle, span.displaySubtitle, span.name].filter(Boolean).join(' ')],
          ['attributes', JSON.stringify(attrs)]
        ];
        for (const [key, value] of Object.entries(attrs)) {
          if (key.endsWith('artifact_path') && typeof value === 'string') {
            const text = artifactTextForSearch(value);
            if (text) pieces.push([key.replace('.artifact_path', ''), text]);
          }
        }
        const combined = pieces.map(([, text]) => text).join('\n').toLowerCase();
        if (!terms.every((term) => combined.includes(term))) continue;
        // Snippet from the most specific piece hit by the first term (prefer artifacts).
        let snippet = '';
        let field = '';
        for (const [label, text] of pieces.slice(2).concat([pieces[1], pieces[0]])) {
          snippet = makeSnippet(text, terms[0]);
          if (snippet) {
            field = label;
            break;
          }
        }
        results.push({
          sessionId: session.sessionId,
          traceKey: trace.traceKey,
          traceId: trace.traceId,
          spanId: span.spanId,
          name: span.name,
          title: span.displayTitle,
          subtitle: span.displaySubtitle,
          startTime: span.startTime,
          field,
          snippet
        });
        if (results.length >= MAX_SEARCH_RESULTS * 4) break;
      }
    }
  }
  results.sort((a, b) => parseTime(b.startTime) - parseTime(a.startTime));
  return results.slice(0, MAX_SEARCH_RESULTS);
}

// Load node-type descriptors, merged by `type` in increasing precedence:
// bundled → state-dir drop-in → --descriptors CLI. Later sources override.
// See TRACING_STANDARD.md §7. Never throws — a bad file is skipped.
function readDescriptorDir(dir) {
  const out = [];
  let names;
  try {
    names = fs.readdirSync(dir);
  } catch {
    return out;
  }
  for (const name of names) {
    if (!name.endsWith('.json')) continue;
    try {
      const parsed = JSON.parse(fs.readFileSync(path.join(dir, name), 'utf8'));
      if (Array.isArray(parsed)) out.push(...parsed);
      else if (parsed && typeof parsed === 'object') out.push(parsed);
    } catch {
      // skip malformed descriptor file
    }
  }
  return out;
}

function loadDescriptors() {
  const sources = [
    ...readDescriptorDir(BUNDLED_DESCRIPTORS_DIR),
    ...readDescriptorDir(STATE_DESCRIPTORS_DIR),
    ...(CLI_DESCRIPTORS ? readDescriptorDir(CLI_DESCRIPTORS) : [])
  ];
  const byType = new Map();
  for (const desc of sources) {
    if (desc && typeof desc.type === 'string') byType.set(desc.type, desc);
  }
  return [...byType.values()];
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || '127.0.0.1'}`);

  if (url.pathname === '/api/health') {
    sendJson(res, 200, { ok: true, port: PORT, stateDir: STATE_DIR });
    return;
  }

  if (url.pathname === '/api/descriptors') {
    sendJson(res, 200, { descriptors: loadDescriptors() });
    return;
  }

  if (url.pathname === '/api/search') {
    sendJson(res, 200, { results: searchSpans(url.searchParams.get('q') || '') });
    return;
  }

  if (url.pathname === '/api/data') {
    sendJson(res, 200, buildSessions());
    return;
  }

  if (url.pathname === '/api/artifact') {
    const artifactPath = url.searchParams.get('path');
    const artifact = readArtifact(artifactPath);
    if (!artifact) {
      sendJson(res, 404, { error: 'Artifact not found' });
      return;
    }
    sendJson(res, 200, artifact);
    return;
  }

  serveStatic(url.pathname, res);
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`Trace UI running at http://127.0.0.1:${PORT}`);
});
