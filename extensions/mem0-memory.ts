/**
 * mem0-memory — Long-term memory for pi sessions
 *
 * Stores session context across pi restarts. When a new session starts,
 * previous sessions' key info is injected so the agent "remembers"
 * what happened last time.
 *
 * Storage: ~/.pi/agent/mem0-store.json
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import * as fs from "node:fs";
import * as path from "node:path";

// ─── Types ───

interface MemoryEntry {
  id: string;
  timestamp: string;
  project: string;
  userMessages: string[];
  keyToolResults: { toolName: string; summary: string }[];
  decisions: string[];
  summary: string;
}

interface MemoryStore {
  version: number;
  entries: MemoryEntry[];
}

// ─── Storage ───

const STORE_PATH = path.join(
  process.env.HOME || "/root",
  ".pi",
  "agent",
  "mem0-store.json"
);

function loadStore(): MemoryStore {
  try {
    if (fs.existsSync(STORE_PATH)) {
      const raw = fs.readFileSync(STORE_PATH, "utf-8");
      return JSON.parse(raw);
    }
  } catch {
    /* corrupt file — start fresh */
  }
  return { version: 1, entries: [] };
}

function saveStore(store: MemoryStore): void {
  try {
    const dir = path.dirname(STORE_PATH);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(STORE_PATH, JSON.stringify(store, null, 2));
  } catch {
    /* silently fail — non-critical */
  }
}

// ─── Helpers ───

function generateId(): string {
  return `mem-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function truncate(text: string, maxLen = 500): string {
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen) + "...";
}

function extractKeyInfo(ctx: ExtensionContext): {
  userMessages: string[];
  toolResults: { toolName: string; summary: string }[];
  decisions: string[];
  summary: string;
} {
  const userMessages: string[] = [];
  const toolResults: { toolName: string; summary: string }[] = [];
  const decisions: string[] = [];

  try {
    const entries = ctx.sessionManager.getEntries();
    let allUserText = "";
    let allAssistantText = "";

    for (const entry of entries) {
      if (entry.type !== "message") continue;
      const msg = entry.message;

      if (msg.role === "user") {
        const texts = (msg.content || [])
          .filter((c: any) => c.type === "text")
          .map((c: any) => c.text)
          .join("\n");
        if (texts.trim()) {
          userMessages.push(truncate(texts, 300));
          allUserText += texts + "\n";
        }
      }

      if (msg.role === "assistant") {
        const texts = (msg.content || [])
          .filter((c: any) => c.type === "text")
          .map((c: any) => c.text)
          .join("\n");
        allAssistantText += texts + "\n";
      }

      if (msg.role === "toolResult") {
        const toolName = msg.toolName || "unknown";
        const texts = (msg.content || [])
          .filter((c: any) => c.type === "text")
          .map((c: any) => c.text)
          .join("\n");
        if (texts.trim()) {
          const isError = msg.isError;
          toolResults.push({
            toolName,
            summary: isError
              ? `[ERROR] ${toolName}: ${truncate(texts, 200)}`
              : `${toolName}: ${truncate(texts, 200)}`,
          });
        }
      }
    }

    // Build a simple summary from user messages and key findings
    const userSummary = userMessages.slice(-5).join(" | ");
    const keyToolSummary = toolResults
      .filter((t) =>
        ["write", "edit", "bash", "a2a_call", "a2a_parallel"].includes(
          t.toolName
        )
      )
      .slice(-5)
      .map((t) => t.summary)
      .join(" | ");

    // Extract potential decisions (messages containing "install", "create", "deploy", "configure", etc.)
    const decisionKeywords = [
      "install",
      "create",
      "deploy",
      "configure",
      "setup",
      "decide",
      "choose",
      "select",
      "enable",
      "disable",
      "add",
      "remove",
      "update",
      "upgrade",
      "migrate",
      "fix",
      "resolve",
      "implement",
    ];
    for (const msg of userMessages) {
      if (decisionKeywords.some((kw) => msg.toLowerCase().includes(kw))) {
        decisions.push(truncate(msg, 200));
      }
    }
    // Only keep last 5 decisions
    const trimmedDecisions = decisions.slice(-5);

    const summary = [
      `Project: ${ctx.cwd}`,
      `User intent: ${userSummary || "(no user messages)"}`,
      `Key actions: ${keyToolSummary || "(no key actions)"}`,
      trimmedDecisions.length > 0
        ? `Decisions: ${trimmedDecisions.join("; ")}`
        : "",
    ]
      .filter(Boolean)
      .join("\n");

    return {
      userMessages: userMessages.slice(-10),
      toolResults: keyToolSummary
        ? toolResults.filter((t) =>
            ["write", "edit", "bash", "a2a_call", "a2a_parallel"].includes(
              t.toolName
            )
          )
        : [],
      decisions: trimmedDecisions,
      summary: truncate(summary, 1000),
    };
  } catch {
    return { userMessages: [], toolResults: [], decisions: [], summary: "" };
  }
}

function buildMemoryContext(store: MemoryStore, currentProject: string): string {
  if (store.entries.length === 0) return "";

  // Get last 10 entries, most recent first
  const recent = store.entries.slice(-10).reverse();

  const lines: string[] = [
    "## 🧠 MEM0 LONG-TERM MEMORY",
    "",
    "The following is a summary of **previous pi sessions** preserved across restarts.",
    "Use this context to understand what was being worked on before this session.",
    "",
  ];

  // Group by project
  const byProject: Record<string, MemoryEntry[]> = {};
  for (const entry of recent) {
    const proj = entry.project || "(unknown)";
    if (!byProject[proj]) byProject[proj] = [];
    if (byProject[proj].length < 3) byProject[proj].push(entry);
  }

  // Show current project's history first
  if (byProject[currentProject]) {
    lines.push(`### 📁 Current project: ${currentProject}`);
    lines.push("");
    for (const entry of byProject[currentProject]) {
      const date = new Date(entry.timestamp).toLocaleString();
      lines.push(`**${date}** (${entry.id})`);
      lines.push(`- ${entry.summary.replace(/\n/g, "\n  ")}`);
      lines.push("");
    }
    delete byProject[currentProject];
  }

  // Show other projects
  for (const [proj, entries] of Object.entries(byProject)) {
    const shortProj = proj.split("/").slice(-2).join("/") || proj;
    lines.push(`### 📁 ${shortProj}`);
    lines.push("");
    for (const entry of entries) {
      const date = new Date(entry.timestamp).toLocaleString();
      lines.push(`**${date}**: ${entry.summary.split("\n")[0]}`);
    }
    lines.push("");
  }

  lines.push("---");
  lines.push("You are NOT starting from scratch. Use the above context to provide continuity.");
  lines.push("");

  return lines.join("\n");
}

// ─── Extension ───

export default function (pi: ExtensionAPI) {
  // Track whether we've injected memory context this session
  let memoryInjected = false;

  pi.on("session_start", async (_event, ctx) => {
    const store = loadStore();

    if (store.entries.length === 0) {
      if (ctx.mode === "tui" || ctx.hasUI) {
        ctx.ui?.notify?.(
          "🧠 mem0: No previous memories found. I'll remember this session.",
          "info"
        );
      }
      return;
    }

    const memoryContext = buildMemoryContext(store, ctx.cwd);

    if (ctx.mode === "tui" || ctx.hasUI) {
      ctx.ui?.notify?.(
        `🧠 mem0: Loaded ${store.entries.length} memory entries from previous sessions.`,
        "info"
      );
    }

    // Store for injection in before_agent_start
    (pi as any).__mem0Context = memoryContext;
    (pi as any).__mem0Store = store;
  });

  pi.on("before_agent_start", async (event, ctx) => {
    if (memoryInjected) return;
    const mem0Context = (pi as any).__mem0Context as string | undefined;
    if (!mem0Context) return;

    memoryInjected = true;

    // Inject memory context as a system message addition
    const currentPrompt = event.systemPrompt || ctx.getSystemPrompt?.() || "";
    return {
      systemPrompt: currentPrompt + "\n\n" + mem0Context,
    };
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    const info = extractKeyInfo(ctx);
    if (!info.summary && info.userMessages.length === 0) return;

    const store = loadStore();

    // Clean up old entries (keep last 50)
    if (store.entries.length > 50) {
      store.entries = store.entries.slice(-40);
    }

    const entry: MemoryEntry = {
      id: generateId(),
      timestamp: new Date().toISOString(),
      project: ctx.cwd,
      userMessages: info.userMessages,
      keyToolResults: info.toolResults,
      decisions: info.decisions,
      summary: info.summary,
    };

    store.entries.push(entry);
    saveStore(store);

    if (ctx.mode === "tui" || ctx.hasUI) {
      ctx.ui?.notify?.(
        `🧠 mem0: Saved memory entry ${entry.id} (${store.entries.length} total)`,
        "info"
      );
    }
  });

  // ─── Commands ───

  pi.registerCommand("mem0-search", {
    description: "Search mem0 memories for relevant past sessions",
    handler: async (args, ctx) => {
      const query = args.trim();
      const store = loadStore();

      if (store.entries.length === 0) {
        ctx.ui?.notify?.("No memories stored yet.", "info");
        return;
      }

      if (!query) {
        // Show recent
        const recent = store.entries.slice(-10).reverse();
        const lines = recent.map(
          (e) =>
            `${new Date(e.timestamp).toLocaleString()} [${e.id.slice(0, 10)}]\n  ${e.summary.split("\n")[0]}`
        );
        ctx.ui?.notify?.(
          `🧠 Recent memories:\n${lines.join("\n\n")}`,
          "info"
        );
        return;
      }

      // Simple keyword search
      const q = query.toLowerCase();
      const results = store.entries
        .filter(
          (e) =>
            e.summary.toLowerCase().includes(q) ||
            e.userMessages.some((m) => m.toLowerCase().includes(q)) ||
            e.decisions.some((d) => d.toLowerCase().includes(q)) ||
            e.keyToolResults.some((t) => t.summary.toLowerCase().includes(q))
        )
        .slice(-10)
        .reverse();

      if (results.length === 0) {
        ctx.ui?.notify?.(
          `No memories matching "${query}"`,
          "info"
        );
        return;
      }

      const lines = results.map(
        (e) =>
          `**${new Date(e.timestamp).toLocaleString()}** (${e.project.split("/").slice(-1)[0]})\n${e.summary}`
      );
      ctx.ui?.notify?.(
        `🧠 Found ${results.length} memory entries:\n\n${lines.join("\n\n")}`,
        "info"
      );
    },
  });

  pi.registerCommand("mem0-clear", {
    description: "Clear all mem0 memories",
    handler: async (_args, ctx) => {
      const ok = ctx.ui?.confirm
        ? await ctx.ui.confirm(
            "Clear mem0?",
            "Delete all long-term memories? This cannot be undone."
          )
        : true;

      if (!ok) return;

      saveStore({ version: 1, entries: [] });
      ctx.ui?.notify?.("🧠 All memories cleared.", "success");
    },
  });

  pi.registerCommand("mem0-stats", {
    description: "Show mem0 memory statistics",
    handler: async (_args, ctx) => {
      const store = loadStore();
      if (store.entries.length === 0) {
        ctx.ui?.notify?.("No memories stored.", "info");
        return;
      }

      const projects = new Set(store.entries.map((e) => e.project));
      const oldest = store.entries[0];
      const newest = store.entries[store.entries.length - 1];

      const lines = [
        `Total entries: ${store.entries.length}`,
        `Projects: ${projects.size}`,
        `Oldest: ${oldest ? new Date(oldest.timestamp).toLocaleString() : "N/A"}`,
        `Newest: ${newest ? new Date(newest.timestamp).toLocaleString() : "N/A"}`,
        `Storage: ${STORE_PATH}`,
      ];

      ctx.ui?.notify?.(`🧠 Memory stats:\n${lines.join("\n")}`, "info");
    },
  });
}
