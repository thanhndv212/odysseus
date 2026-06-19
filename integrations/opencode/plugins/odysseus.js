/**
 * Odysseus Plugin for OpenCode
 *
 * Provides custom tools that connect OpenCode to a scoped Odysseus instance
 * via the /api/codex/* agent API. Requires ODYSSEUS_URL and ODYSSEUS_API_TOKEN
 * environment variables.
 *
 * Install: copy this file to ~/.config/opencode/plugins/odysseus.js
 *          and add "odysseus" to the plugin array in opencode.json,
 *          OR extract from /api/opencode/plugin.zip.
 */

import { tool } from "@opencode-ai/plugin";

const ODYSSEUS_URL = () => (process.env.ODYSSEUS_URL || "").replace(/\/+$/, "");
const ODYSSEUS_TOKEN = () => process.env.ODYSSEUS_API_TOKEN || "";

function missingConfig() {
  const missing = [];
  if (!ODYSSEUS_URL()) missing.push("ODYSSEUS_URL");
  if (!ODYSSEUS_TOKEN()) missing.push("ODYSSEUS_API_TOKEN");
  return missing.length ? `Missing ${missing.join(", ")}. Create an OpenCode Agent token in Odysseus Settings > Integrations.` : null;
}

async function odysseusFetch(path, { method = "GET", body = null } = {}) {
  const err = missingConfig();
  if (err) return { ok: false, error: err };

  const url = ODYSSEUS_URL() + path;
  const headers = {
    Accept: "application/json",
    Authorization: `Bearer ${ODYSSEUS_TOKEN()}`,
  };
  const opts = { method, headers };
  if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }

  try {
    const resp = await fetch(url, opts);
    const text = await resp.text();
    let data;
    try { data = JSON.parse(text); } catch { data = text; }
    if (!resp.ok) {
      return { ok: false, status: resp.status, error: data?.detail || data?.error || text };
    }
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// ── Tool definitions ────────────────────────────────────────────────────────

const odysseusCapabilities = tool({
  description: "Check which Odysseus tool surfaces the current token can access. Always call this first to discover available actions.",
  args: {},
  async execute() {
    return odysseusFetch("/api/codex/capabilities");
  },
});

const odysseusTodos = tool({
  description: "List, add, update, delete, or toggle Odysseus todos. Use due_date for reminders.",
  args: {
    action: tool.schema.string().describe("list | add | update | delete | toggle_item"),
    title: tool.schema.string().optional().describe("Title for add action"),
    due_date: tool.schema.string().optional().describe("Natural language due date, e.g. 'tomorrow 5pm'"),
    todo_id: tool.schema.string().optional().describe("Todo ID for update/delete/toggle_item"),
    payload: tool.schema.object({}).optional().describe("Full JSON payload for advanced actions"),
  },
  async execute({ action, title, due_date, todo_id, payload }) {
    if (action === "list") {
      return odysseusFetch("/api/codex/todos");
    }
    const body = payload || { action, title, due_date, todo_id };
    if (!body.action) body.action = action;
    return odysseusFetch("/api/codex/todos", { method: "POST", body });
  },
});

const odysseusEmails = tool({
  description: "List or read Odysseus emails. Requires email:read scope.",
  args: {
    action: tool.schema.string().describe("list | read"),
    folder: tool.schema.string().optional().describe("IMAP folder (default INBOX)"),
    limit: tool.schema.number().optional().describe("Max emails to list (default 10)"),
    uid: tool.schema.string().optional().describe("Email UID for read action"),
  },
  async execute({ action, folder = "INBOX", limit = 10, uid }) {
    if (action === "list") {
      return odysseusFetch(`/api/codex/emails?folder=${encodeURIComponent(folder)}&limit=${limit}&offset=0&filter=all`);
    }
    if (action === "read" && uid) {
      return odysseusFetch(`/api/codex/emails/${encodeURIComponent(uid)}?folder=${encodeURIComponent(folder)}`);
    }
    return { ok: false, error: "Invalid email action. Use 'list' or 'read' with a uid." };
  },
});

const odysseusMemory = tool({
  description: "List, add, or delete Odysseus memories. Use for persistent facts and preferences about the user.",
  args: {
    action: tool.schema.string().describe("list | add | delete"),
    text: tool.schema.string().optional().describe("Memory text for add"),
    category: tool.schema.string().optional().describe("fact | preference | instruction (default: fact)"),
    memory_id: tool.schema.string().optional().describe("Memory ID for delete"),
  },
  async execute({ action, text, category = "fact", memory_id }) {
    if (action === "list") {
      return odysseusFetch("/api/codex/memory");
    }
    if (action === "add") {
      return odysseusFetch("/api/codex/memory", {
        method: "POST",
        body: { text, category, source: "user", session_id: null },
      });
    }
    if (action === "delete" && memory_id) {
      return odysseusFetch(`/api/codex/memory/${encodeURIComponent(memory_id)}`, { method: "DELETE" });
    }
    return { ok: false, error: "Invalid memory action. Use 'list', 'add', or 'delete' with memory_id." };
  },
});

const odysseusCalendar = tool({
  description: "List, create, or delete Odysseus calendar events. Use for meetings, appointments, and time blocks — NOT for reminders.",
  args: {
    action: tool.schema.string().describe("list_events | create_event | delete_event"),
    start: tool.schema.string().optional().describe("ISO start time for list_events"),
    end: tool.schema.string().optional().describe("ISO end time for list_events"),
    payload: tool.schema.object({}).optional().describe("EventCreate body for create_event (summary, dtstart, dtend, ...)"),
    uid: tool.schema.string().optional().describe("Event UID for delete_event"),
  },
  async execute({ action, start, end, payload, uid }) {
    if (action === "list_events" && start && end) {
      return odysseusFetch(`/api/codex/calendar/events?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`);
    }
    if (action === "create_event" && payload) {
      return odysseusFetch("/api/codex/calendar/events", { method: "POST", body: payload });
    }
    if (action === "delete_event" && uid) {
      return odysseusFetch(`/api/codex/calendar/events/${encodeURIComponent(uid)}`, { method: "DELETE" });
    }
    return { ok: false, error: "Invalid calendar action or missing parameters." };
  },
});

const odysseusDocuments = tool({
  description: "List, read, create, or delete Odysseus documents.",
  args: {
    action: tool.schema.string().describe("list | read | create | delete"),
    doc_id: tool.schema.string().optional().describe("Document ID for read/delete"),
    search: tool.schema.string().optional().describe("Search query for list"),
    limit: tool.schema.number().optional().describe("Max results for list (default 50)"),
    payload: tool.schema.object({}).optional().describe("DocumentCreate body for create (title, content, language, ...)"),
  },
  async execute({ action, doc_id, search, limit = 50, payload }) {
    if (action === "list") {
      const params = new URLSearchParams({ limit: String(limit) });
      if (search) params.set("search", search);
      return odysseusFetch(`/api/codex/documents?${params}`);
    }
    if (action === "read" && doc_id) {
      return odysseusFetch(`/api/codex/documents/${encodeURIComponent(doc_id)}`);
    }
    if (action === "create" && payload) {
      return odysseusFetch("/api/codex/documents", { method: "POST", body: payload });
    }
    if (action === "delete" && doc_id) {
      return odysseusFetch(`/api/codex/documents/${encodeURIComponent(doc_id)}`, { method: "DELETE" });
    }
    return { ok: false, error: "Invalid document action or missing parameters." };
  },
});

const odysseusCookbook = tool({
  description: "Manage Odysseus Cookbook model-serve tasks: list tasks/servers/cached models, tail output, launch/stop serves, run presets.",
  args: {
    action: tool.schema.string().describe("tasks | servers | cached | presets | output | serve | preset | adopt | stop"),
    session_id: tool.schema.string().optional().describe("Session ID for output/stop actions"),
    tail: tool.schema.number().optional().describe("Lines to tail for output (default 400)"),
    host: tool.schema.string().optional().describe("Host name for cached action, or remote_host for serve"),
    repo_id: tool.schema.string().optional().describe("Model repo ID for serve action"),
    cmd: tool.schema.string().optional().describe("Launch command for serve action"),
    preset_name: tool.schema.string().optional().describe("Preset name for preset action"),
    payload: tool.schema.object({}).optional().describe("Full JSON payload for serve/adopt"),
  },
  async execute({ action, session_id, tail = 400, host, repo_id, cmd, preset_name, payload }) {
    if (action === "tasks") {
      return odysseusFetch("/api/codex/cookbook/tasks");
    }
    if (action === "servers") {
      return odysseusFetch("/api/codex/cookbook/servers");
    }
    if (action === "cached") {
      const params = host ? `?host=${encodeURIComponent(host)}` : "";
      return odysseusFetch(`/api/codex/cookbook/cached${params}`);
    }
    if (action === "presets") {
      return odysseusFetch("/api/codex/cookbook/presets");
    }
    if (action === "output" && session_id) {
      return odysseusFetch(`/api/codex/cookbook/output/${encodeURIComponent(session_id)}?tail=${tail}`);
    }
    if (action === "serve") {
      const body = payload || { repo_id, cmd };
      if (host) body.remote_host = host;
      return odysseusFetch("/api/codex/cookbook/serve", { method: "POST", body });
    }
    if (action === "preset" && preset_name) {
      return odysseusFetch(`/api/codex/cookbook/preset/${encodeURIComponent(preset_name)}`, { method: "POST" });
    }
    if (action === "adopt" && payload) {
      return odysseusFetch("/api/codex/cookbook/adopt", { method: "POST", body: payload });
    }
    if (action === "stop" && session_id) {
      return odysseusFetch(`/api/codex/cookbook/stop/${encodeURIComponent(session_id)}`, { method: "POST" });
    }
    return { ok: false, error: `Invalid cookbook action or missing parameters for '${action}'.` };
  },
});

// ── Plugin export ────────────────────────────────────────────────────────────

export default {
  name: "odysseus",
  version: "0.1.0",
  description: "Connect OpenCode to a scoped Odysseus instance for todos, email, calendar, memory, documents, and cookbook model-serve management.",

  tools: {
    odysseus_capabilities: odysseusCapabilities,
    odysseus_todos: odysseusTodos,
    odysseus_emails: odysseusEmails,
    odysseus_memory: odysseusMemory,
    odysseus_calendar: odysseusCalendar,
    odysseus_documents: odysseusDocuments,
    odysseus_cookbook: odysseusCookbook,
  },
};