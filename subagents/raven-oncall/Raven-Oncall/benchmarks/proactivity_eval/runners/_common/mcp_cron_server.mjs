#!/usr/bin/env node
/**
 * Longrun MCP cron server — stdio JSON-RPC.
 *
 * Exposes set_reminder / list_reminders / cancel_reminder tools so the
 * OpenClaw agent (under `agent --local`) can register future reminders
 * against the longrun fake-clock. State is persisted to a JSON file
 * pointed at by $LONGRUN_STORE; the host harness reads that file each
 * tick and fires matured reminders as synthetic user turns.
 *
 * Why a Node server (not Python): the OC docker container has node but
 * not python, and OC spawns MCP stdio children inside the container.
 * Bind-mount the .openclaw home directory and both the .mjs server and
 * the store file are visible to the host.
 *
 * Protocol: MCP 2024-11-05 over newline-delimited JSON-RPC. No deps.
 */
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

const STORE = process.env.LONGRUN_STORE || "/tmp/longrun-cron-store.json";
const LOG = process.env.LONGRUN_MCP_LOG;

function logErr(msg) {
  if (!LOG) return;
  try {
    writeFileSync(LOG, `${new Date().toISOString()} ${msg}\n`, { flag: "a" });
  } catch {
    /* best-effort logging */
  }
}

function loadStore() {
  try {
    if (!existsSync(STORE)) return { reminders: [] };
    return JSON.parse(readFileSync(STORE, "utf-8")) || { reminders: [] };
  } catch (e) {
    logErr(`loadStore err: ${e.message}`);
    return { reminders: [] };
  }
}

function saveStore(s) {
  try {
    mkdirSync(dirname(STORE), { recursive: true });
    writeFileSync(STORE, JSON.stringify(s, null, 2));
  } catch (e) {
    logErr(`saveStore err: ${e.message}`);
  }
}

const TOOLS = [
  {
    name: "set_reminder",
    description:
      "Schedule a reminder to fire at a specific future time. The reminder will appear later as a system notification with the given message. Use ISO8601 datetime including timezone (e.g., \"2026-04-28T09:00:00+08:00\"). The current sim time is provided at the top of each user message — use it as your reference for relative phrases (\"in 5 minutes\", \"tomorrow at 9\").",
    inputSchema: {
      type: "object",
      properties: {
        when: {
          type: "string",
          description:
            "ISO8601 datetime with timezone, must be in the future relative to the sim time.",
        },
        message: {
          type: "string",
          description: "The reminder text the user will receive when it fires.",
        },
      },
      required: ["when", "message"],
    },
  },
  {
    name: "list_reminders",
    description: "List all reminders that have not yet fired.",
    inputSchema: { type: "object", properties: {}, required: [] },
  },
  {
    name: "cancel_reminder",
    description: "Cancel a previously scheduled reminder by its ID.",
    inputSchema: {
      type: "object",
      properties: {
        reminder_id: {
          type: "string",
          description: "The ID returned by set_reminder.",
        },
      },
      required: ["reminder_id"],
    },
  },
];

function handleToolCall(name, args) {
  const store = loadStore();
  if (name === "set_reminder") {
    const when = String(args.when || "").trim();
    const message = String(args.message || "").trim();
    if (!when || !message) {
      return {
        isError: true,
        content: [
          {
            type: "text",
            text: JSON.stringify({ ok: false, error: "when and message are required" }),
          },
        ],
      };
    }
    const id = `rem-${store.reminders.length + 1}-${Date.now().toString(36)}`;
    store.reminders.push({
      id,
      when,
      message,
      registered_at: new Date().toISOString(),
      fired: false,
    });
    saveStore(store);
    return {
      content: [
        { type: "text", text: JSON.stringify({ ok: true, reminder_id: id, when, message }) },
      ],
    };
  }
  if (name === "list_reminders") {
    const active = store.reminders.filter((r) => !r.fired);
    return {
      content: [{ type: "text", text: JSON.stringify({ reminders: active }) }],
    };
  }
  if (name === "cancel_reminder") {
    const rid = String(args.reminder_id || "").trim();
    const before = store.reminders.length;
    store.reminders = store.reminders.filter((r) => r.id !== rid);
    saveStore(store);
    const cancelled = store.reminders.length < before;
    return {
      content: [{ type: "text", text: JSON.stringify({ cancelled, reminder_id: rid }) }],
    };
  }
  throw new Error(`unknown tool: ${name}`);
}

// ─── JSON-RPC stdio loop ────────────────────────────────────────────
function respond(id, result, error) {
  const msg = { jsonrpc: "2.0", id };
  if (error) msg.error = error;
  else msg.result = result;
  process.stdout.write(`${JSON.stringify(msg)}\n`);
}

function handle(req) {
  const { id, method, params } = req;
  try {
    if (method === "initialize") {
      respond(id, {
        protocolVersion: "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name: "longrun-cron", version: "1.0.0" },
      });
    } else if (method === "notifications/initialized") {
      // notifications have no id; no response.
    } else if (method === "tools/list") {
      respond(id, { tools: TOOLS });
    } else if (method === "tools/call") {
      const result = handleToolCall(params.name, params.arguments || {});
      respond(id, result);
    } else if (id !== undefined) {
      respond(id, null, { code: -32601, message: `method not found: ${method}` });
    }
  } catch (e) {
    logErr(`handle err (${method}): ${e.message}`);
    if (id !== undefined) respond(id, null, { code: -32000, message: e.message });
  }
}

process.stdin.setEncoding("utf-8");
let buffer = "";
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let nl;
  while ((nl = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, nl).trim();
    buffer = buffer.slice(nl + 1);
    if (!line) continue;
    let req;
    try {
      req = JSON.parse(line);
    } catch (e) {
      logErr(`bad json: ${line.slice(0, 200)}`);
      continue;
    }
    handle(req);
  }
});

process.stdin.on("end", () => {
  process.exit(0);
});

logErr(`mcp-cron started, store=${STORE}`);
