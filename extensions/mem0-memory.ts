/**
 * mem0-memory — Long-term memory for pi sessions
 *
 * Uses mem0.ai semantic memory service (running on localhost:7011)
 * with vector DB for proper semantic search.
 *
 * Both pi and telegram-bot share the same mem0 service.
 * Conversations are saved incrementally to prevent data loss.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import * as http from "node:http";

// ─── Config ────────────────────────────────────────────────────────────────

const MEM0_URL = "http://127.0.0.1:7011";
const SAVE_INTERVAL = 3; // Save every N user messages
const DEFAULT_LIMIT = 5; // Default search limit

// ─── HTTP Client ───────────────────────────────────────────────────────────

async function mem0Request(method: string, path: string, body?: any): Promise<any> {
  return new Promise((resolve) => {
    const url = new URL(`${MEM0_URL}${path}`);
    const payload = body ? JSON.stringify(body) : undefined;
    const options: http.RequestOptions = {
      hostname: url.hostname,
      port: parseInt(url.port, 10),
      path: url.pathname + url.search,
      method,
      headers: {
        "Content-Type": "application/json",
        ...(payload ? { "Content-Length": Buffer.byteLength(payload) } : {}),
      },
      timeout: 5000,
      family: 4, // Force IPv4 — Python HTTP server only listens on IPv4
    };

    const req = http.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          const result = JSON.parse(data);
          if (res.statusCode && res.statusCode >= 400) {
            console.error(`[mem0] HTTP ${res.statusCode}: ${JSON.stringify(result)}`);
          }
          resolve(result);
        } catch {
          console.error(`[mem0] JSON parse error, status=${res.statusCode}, raw=${data.slice(0,200)}`);
          resolve(null);
        }
      });
    });

    req.on("error", (err: Error) => {
      console.error(`[mem0] request error: ${err.message}`);
      resolve(null);
    });
    req.on("timeout", () => {
      console.error(`[mem0] request timeout: ${method} ${path}`);
      req.destroy();
      resolve(null);
    });

    if (payload) req.write(payload);
    req.end();
  });
}

async function checkService(): Promise<boolean> {
  const res = await mem0Request("GET", "/health");
  return res?.status === "ok";
}

// ─── Helpers ───────────────────────────────────────────────────────────────

function truncate(text: string, maxLen = 500): string {
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen) + "...";
}

function extractSessionContext(ctx: ExtensionContext): {
  summary: string;
  userMessages: string[];
} {
  try {
    const entries = ctx.sessionManager.getEntries();
    const userMessages: string[] = [];
    const toolActions: string[] = [];

    for (const entry of entries) {
      if (entry.type !== "message") continue;
      const msg = entry.message;

      if (msg.role === "user") {
        let texts = "";
        if (Array.isArray(msg.content)) {
          texts = msg.content
            .filter((c: any) => c.type === "text")
            .map((c: any) => c.text)
            .join("\n");
        } else if (typeof msg.content === "string") {
          texts = msg.content;
        }

        if (texts.trim()) {
          userMessages.push(truncate(texts, 400));
        }
      }

      if (msg.role === "toolResult") {
        const toolName = msg.toolName || "unknown";
        const isError = msg.isError;
        let texts = "";
        if (Array.isArray(msg.content)) {
          texts = msg.content
            .filter((c: any) => c.type === "text")
            .map((c: any) => c.text)
            .join("\n");
        } else if (typeof msg.content === "string") {
          texts = msg.content;
        }

        if (texts.trim()) {
          toolActions.push(`[${isError ? "FAIL" : "OK"}] ${toolName}: ${truncate(texts, 200)}`);
        }
      }
    }

    const recentIntent = userMessages.slice(-3).join("; ");
    const importantActions = toolActions
      .filter((t) => /write|edit|bash|a2a_call|curl|pip|npm|docker/i.test(t))
      .slice(-6)
      .join(". ");

    const summary = [
      `Project: ${ctx.cwd}`,
      recentIntent ? `User intent: ${recentIntent}` : "",
      importantActions ? `Key actions: ${importantActions}` : "",
    ]
      .filter(Boolean)
      .join("\n");

    return { summary: truncate(summary, 3000), userMessages };
  } catch {
    return { summary: `Project: ${ctx.cwd || "unknown"}`, userMessages: [] };
  }
}

function formatMemoryContext(memories: any[]): string {
  if (!memories || memories.length === 0) return "";

  const lines: string[] = [
    "## 🧠 RELEVANT PAST CONTEXT",
    "",
    "These memories were retrieved using semantic similarity search.",
    "Use them for continuity — ignore anything that doesn't match your current task.",
    "",
  ];

  for (let i = 0; i < memories.length; i++) {
    const m = memories[i];
    const score = m.score ? ` (relevance: ${m.score.toFixed(2)})` : "";
    const metadata = m.metadata ? ` [${m.metadata.project || ""}]` : "";
    lines.push(`### ${i + 1}. ${m.memory}${score}${metadata}`);
    if (m.created_at) {
      lines.push(`   ${new Date(m.created_at).toLocaleString()}`);
    }
    lines.push("");
  }

  lines.push("---");
  lines.push("End of relevant context. Focus on your current task.");
  lines.push("");

  return lines.join("\n");
}

// ─── Bootstrap memory instruction ──────────────────────────────────────────

const MEMORY_BOOTSTRAP = `
## 🧠 LONG-TERM MEMORY AVAILABLE

You have access to a persistent semantic memory system. You can actively search it using the **mem0_search** tool to find past context, decisions, configurations, and project details.

**When to use it:**
- User asks "what were we working on", "what did we do last time", or any variation of recalling past work
- User mentions a project or topic you don't have context about in the current conversation
- You need to recall deployment configurations, URLs, credentials, or setup steps
- Any time the answer might be in past sessions — **search BEFORE guessing or exploring the filesystem**

**How to use it:** Call \`mem0_search\` with a descriptive query. It returns semantically matched memories ranked by relevance.
`;

// ─── Extension ─────────────────────────────────────────────────────────────

export default function (pi: ExtensionAPI) {
  let serviceReady = false;
  let memoryInjected = false;
  let messageCount = 0;

  // ─── mem0_search TOOL (LLM-callable) ────────────────────────────────────

  pi.registerTool({
    name: "mem0_search",
    label: "Search Memory",
    description: "Search long-term memory for past context, decisions, configurations, and project details. Use this when the user asks about past work, mentions unfamiliar projects, or needs context from previous sessions. Returns semantically matched memories ranked by relevance.",
    parameters: Type.Object({
      query: Type.String({ description: "Search query describing what you need to recall (e.g., 'deployment config', 'what did we work on yesterday', 'diary app architecture')" }),
      limit: Type.Optional(Type.Number({ description: "Max results (default 10)" })),
    }),
    promptSnippet: "mem0_search(query: str, limit?: int) — Search long-term semantic memory for past context, decisions, and project details",
    promptGuidelines: [
      "Use mem0_search BEFORE exploring the filesystem when the user asks about past work, projects, or context",
    ],
    async execute(toolCallId, params, _signal, _onUpdate, ctx) {
      if (!serviceReady) {
        serviceReady = await checkService();
        if (!serviceReady) {
          return {
            content: [{ type: "text", text: "⚠️ mem0 service is unavailable at localhost:7011." }],
            details: {},
          };
        }
      }

      const limit = params.limit || 10;
      const res = await mem0Request("POST", "/memory/search", {
        query: params.query,
        user_id: "pi-agent",
        limit,
      });

      if (!res || !res.results || res.results.length === 0) {
        return {
          content: [{ type: "text", text: `No memories found matching "${params.query}".` }],
          details: {},
        };
      }

      const formatted = formatMemoryContext(res.results);
      return {
        content: [{ type: "text", text: formatted }],
        details: { count: res.results.length, query: params.query },
      };
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    serviceReady = await checkService();

    if (!serviceReady) {
      if (ctx.mode === "tui" || ctx.hasUI) {
        ctx.ui?.notify?.("⚠️ mem0: Service unavailable at localhost:7011. Check if mem0-service is running.", "warning");
      }
      return;
    }

    messageCount = 0;
    memoryInjected = false;

    // Search for relevant memories:
    // 1) Project-specific (based on cwd)
    // 2) Bootstrap/critical memories that should always be loaded
    const project = ctx.cwd || "global";
    
    // Search for project-relevant + bootstrap memories across all projects
    const res = await mem0Request("POST", "/memory/search", {
      query: `project ${project} recent work infrastructure bootstrap critical setup`,
      user_id: "pi-agent",
      limit: DEFAULT_LIMIT,
    });

    if (res && res.results && res.results.length > 0) {
      const contextText = formatMemoryContext(res.results);
      (pi as any).__mem0Context = contextText;

      if (ctx.mode === "tui" || ctx.hasUI) {
        ctx.ui?.notify?.(
          `🧠 mem0: Loaded ${res.results.length} relevant memories (semantic search)`,
          "info"
        );
      }
    } else {
      (pi as any).__mem0Context = "";
    }
  });

  pi.on("before_agent_start", async (event, ctx) => {
    // Always inject bootstrap instruction so the LLM knows it has memory
    // Also inject any pre-loaded session memories (but don't block future searches)
    if (!serviceReady) return;

    const contextText = (pi as any).__mem0Context as string | undefined;
    const currentPrompt = event.systemPrompt || ctx.getSystemPrompt?.() || "";

    if (!memoryInjected) {
      memoryInjected = true;
      if (contextText) {
        return { systemPrompt: currentPrompt + "\n\n" + contextText };
      }
      // At minimum, inject bootstrap so the LLM knows the memory tool exists
      return { systemPrompt: currentPrompt + MEMORY_BOOTSTRAP };
    }

    // Don't block — just return undefined to pass through
  });

  // Save incrementally on user messages (input event fires for every non-command user input)
  pi.on("input", async (_event, ctx) => {
    if (!serviceReady) return;
    messageCount++;

    if (messageCount % SAVE_INTERVAL === 0) {
      const { summary } = extractSessionContext(ctx);
      if (summary) {
        await mem0Request("POST", "/memory/add", {
          text: summary,
          user_id: "pi-agent",
          metadata: {
            project: ctx.cwd || "global",
            type: "session-snapshot",
            timestamp: new Date().toISOString(),
          },
        });
      }
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    if (!serviceReady) return;

    const { summary } = extractSessionContext(ctx);
    if (!summary) return;

    await mem0Request("POST", "/memory/add", {
      text: summary,
      user_id: "pi-agent",
      metadata: {
        project: ctx.cwd || "global",
        type: "session-final",
        timestamp: new Date().toISOString(),
      },
    });

    if (ctx.mode === "tui" || ctx.hasUI) {
      ctx.ui?.notify?.("🧠 mem0: Session saved to memory.");
    }
  });

  // ─── Commands ──────────────────────────────────────────────────────────

  pi.registerCommand("mem0-search", {
    description: "Search memories using semantic search",
    handler: async (args, ctx) => {
      if (!serviceReady) {
        serviceReady = await checkService();
        if (!serviceReady) {
          ctx.ui?.notify?.("⚠️ mem0 service unavailable.", "warning");
          return;
        }
      }

      const query = args.trim();
      if (!query) {
        // List recent memories
        const res = await mem0Request("GET", "/memory/list?user_id=pi-agent&limit=10");
        if (res && res.memories) {
          const lines = res.memories.map((m: any) =>
            `${new Date(m.created_at || "").toLocaleString()}\n  ${m.memory?.slice(0, 100)}`
          );
          ctx.ui?.notify?.(
            `🧠 Recent memories:\n\n${lines.join("\n\n")}`,
            "info"
          );
        } else {
          ctx.ui?.notify?.("No memories found.", "info");
        }
        return;
      }

      // Semantic search
      const res = await mem0Request("POST", "/memory/search", {
        query,
        user_id: "pi-agent",
        limit: 10,
      });

      if (!res || !res.results || res.results.length === 0) {
        ctx.ui?.notify?.(`No memories matching "${query}"`, "info");
        return;
      }

      const lines = res.results.map((m: any) => {
        const score = m.score ? ` (score: ${m.score.toFixed(3)})` : "";
        const project = m.metadata?.project || "";
        return `**${new Date(m.created_at || "").toLocaleString()}**${score} [${project}]\n${m.memory}`;
      });
      ctx.ui?.notify?.(
        `🧠 Found ${res.results.length} memories:\n\n${lines.join("\n\n---\n")}`,
        "info"
      );
    },
  });

  pi.registerCommand("mem0-add", {
    description: "Add a memory manually",
    handler: async (args, ctx) => {
      const text = args.trim();
      if (!text) {
        ctx.ui?.notify?.("Usage: mem0-add <text to remember>", "warning");
        return;
      }

      const res = await mem0Request("POST", "/memory/add", {
        text,
        user_id: "pi-agent",
        metadata: {
          project: ctx.cwd || "global",
          type: "manual",
          timestamp: new Date().toISOString(),
        },
      });

      if (res?.status === "ok") {
        ctx.ui?.notify?.("🧠 Memory added.", "success");
      } else {
        ctx.ui?.notify?.("⚠️ Failed to add memory.", "warning");
      }
    },
  });

  pi.registerCommand("mem0-stats", {
    description: "Show memory service status",
    handler: async (_args, ctx) => {
      const health = await mem0Request("GET", "/health");
      if (!health) {
        ctx.ui?.notify?.("⚠️ mem0 service unavailable at localhost:7011", "warning");
        return;
      }

      const list = await mem0Request("GET", "/memory/list?user_id=pi-agent&limit=1000");
      const count = list?.memories?.length || 0;

      const lines = [
        `Service: ${health.status === "ok" ? "✅ Running" : "❌ Down"}`,
        `URL: ${MEM0_URL}`,
        `Total memories: ${count}`,
      ];

      ctx.ui?.notify?.(`🧠 mem0 stats:\n${lines.join("\n")}`, "info");
    },
  });
}
