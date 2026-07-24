# Pi A2A Agent Mesh — Setup & Operations

A framework for building self-organizing AI agent networks — agents that can
delegate tasks to each other, discover new peers, and reason about problems they
haven't seen before. Built on **pi coding agent** (v0.81.1+) and the
**[fasta2a](https://pypi.org/project/fasta2a/)** Python library (A2A protocol v0.3.0).

> **What's this good for?** When you need agents that understand natural language
> requests ("find out why the site is down and fix it"), adapt to unexpected
> situations, or negotiate with each other. For high-speed, deterministic
> coordination (health checks, file sync, event broadcasting), read the
> [deterministic channel](#6-deterministic-channel-vs-a2a) section below.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [What Problems Does This Solve?](#2-what-problems-does-this-solve)
3. [How It Works — Honest Tradeoffs](#3-how-it-works--honest-tradeoffs)
4. [Security Model](#4-security-model)
5. [Pi Extensions Included](#5-pi-extensions-included)
6. [Deterministic Channel vs. A2A](#6-deterministic-channel-vs-a2a)
7. [Quick Start](#7-quick-start)
8. [Message Protocol (A2A)](#8-message-protocol-a2a)
9. [Audit & Verification](#9-audit--verification)
10. [Troubleshooting](#10-troubleshooting)
11. [Files](#11-files)

---

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                       AGENT MESH                              │
│                                                               │
│   ┌──────────┐    A2A (JSON-RPC over HTTP)    ┌──────────┐   │
│   │ Agent A  │◄─────────────────────────────►│ Agent B  │   │
│   │  :8002   │     polling + HMAC-signed      │  :9090   │   │
│   └────┬─────┘                                └────┬─────┘   │
│        │                                           │          │
│   ┌────┴──────────┐                          ┌─────┴───────┐ │
│   │ pi (cheap)     │                          │ custom      │ │
│   │ pi (capable)   │                          │ backend     │ │
│   └───────────────┘                          └─────────────┘ │
│                                                               │
│   Each agent runs 1–2 pi LLM processes under the hood.       │
│   Requests are queued, executed, and polled for results.      │
└──────────────────────────────────────────────────────────────┘
```

### Why Two Pi Instances? (Dual-Model Pool with Fallback)

### Dual-Model Pool (Optional)

A single pi process is locked to one model at startup. To avoid restarting
processes (and losing conversation state), the server optionally runs two pi
instances in parallel:

| Process   | Typical Model          | Tools                    | Purpose                            |
|-----------|------------------------|--------------------------|------------------------------------|
| **Cheap** | gemma-3-12b-it (free)  | None (text-only)         | Handshakes, simple Q&A, coordination |
| **Capable** | deepseek-v4-pro (paid) | Full (read/bash/write)   | Code, deployment, debugging, real work |

**How switching works:** A bag-of-keywords heuristic inspects the incoming
message. If it contains words like `deploy`, `fix`, `debug`, `docker`, or is
longer than 300 characters, it goes to the capable model. Otherwise the cheap
model handles it. Pings, handshakes, and status checks are answered instantly
without touching either model (the "fast path").

> **⚠️ Cost note:** Running two pi subprocesses 24/7 means both models are
> always warm (no cold-start delay), but it also means you're paying for idle
> GPU/API time if traffic is low. If cost is a concern, set
> `A2A_CAPABLE_MODEL=$A2A_MODEL` to run a single process. The tradeoff is
> either higher cost or slower cold starts when you need full tools.

---

## 2. What Problems Does This Solve?

A2A is the right tool when:

### ✅ Tasks that require reasoning

You don't know in advance *how* to solve the problem — you need an agent to
figure it out:

```
User: "The bedtime-stories site is returning 502, fix it."
Agent A receives this, reads logs, discovers a crashed Docker container,
restarts it, and reports back. No predefined command set covers this.
```

A deterministic TCP service can execute `restart-container bedtime-stories`,
but it can't diagnose *which* container is down or *why*.

### ✅ Delegation between agents with different capabilities

Agent A has file system access and deployment tools. Agent B has database
access. A user asks Agent A:

```
"Show me the last 3 portfolio items and update their deployment timestamps."
```

Agent A handles the file editing locally, then delegates the database write to
Agent B — all through natural language, no hardcoded API contracts.

### ✅ Peer discovery without manual configuration

When an unknown IP calls the server, the auto-discovery system probes that IP
for an agent card (`/.well-known/agent-card.json`). The peer is registered
automatically. New agents can join the mesh without updating every other agent's
config file.

### ✅ Adapting to unexpected situations

```
"Try to deploy. If Docker fails, try Podman instead. If the port is taken,
find a free one and use that."
```

An LLM-based agent can react to runtime failures and try alternative
approaches. A deterministic protocol would need every fallback path
pre-programmed.

### ❌ What A2A is NOT good for

| Use Case                   | Why Not A2A                  | Better Approach        |
|----------------------------|------------------------------|------------------------|
| Health checks (every 500ms) | Minimum 4s poll, pays tokens | TCP ping/pong, zero cost |
| File sync between agents    | High latency, token cost     | TCP with checksum verification |
| Event broadcasting ("deploy done") | Polling delay + token cost | TCP push or pub/sub |
| Command dispatch (fixed set) | LLM variability, 4-56s delay | TCP with predefined commands |
| Metrics streaming           | Polling is pull, not push    | WebSocket or gRPC stream |
| Real-time coordination      | 4-56 second latency unacceptable | TCP with sub-ms latency |

For these cases, you want a deterministic channel alongside A2A. See
[section 6](#6-deterministic-channel-vs-a2a) for a recommended architecture.

---

## 3. How It Works — Honest Tradeoffs

### The Send → Poll → Verify Cycle

There is no persistent connection between agents. Every task follows three HTTP
requests:

```
Sender                       Receiver
  │                             │
  │── POST message/send ──────►│  Returns {"id": "task-uuid"} immediately
  │                             │  (pi starts processing in background)
  │                             │
  │  ... wait 4 seconds ...     │
  │                             │
  │── POST tasks/get ─────────►│  Poll: "working" or "completed"
  │  {"id": "task-uuid"}        │
  │                             │
  │  ... wait 4, 8, 8, 16...   │  (repeat until done or timeout)
```

**Polling schedule:** 4s, 4s, 8s, 8s, 16s, 16s → maximum 56 seconds total. Each
interval fires twice before doubling. In practice:

| Task type         | Typical polls | Typical latency |
|-------------------|---------------|-----------------|
| Handshake / ping  | 0 (fast path) | < 1ms            |
| Simple Q&A        | 1–2 polls     | 4–8 seconds     |
| Code generation   | 3–4 polls     | 16–24 seconds   |
| DeepSeek thinking | 4–6 polls     | 24–56 seconds   |

**Why not streaming?** The A2A spec supports SSE streaming. This implementation
uses polling because:
- It's trivial to debug (`curl` works directly)
- It passes through any HTTP proxy without special configuration
- Every state transition is logged to the audit file
- Results are immutable artifacts, not partial chunks that might change

The tradeoff is latency. For the use cases this targets (diagnosis,
deployment, code generation), 8–30 seconds is acceptable. For sub-second health
checks or real-time eventing, you need a separate channel.

### Complexity Detection (How Tasks Get Routed to the Right Model)

The heuristic is intentionally simple — a bag of keywords, not a neural
classifier:

1. **Fast path:** Exact matches for `ping`, `status`, `handshake` → instant string response (0ms, 0 tokens)
2. **Very short** (≤30 chars) handshake-like messages → instant response
3. **Length > 300 chars** → capable model (long messages usually mean real work)
4. **Keywords match:** `deploy`, `fix`, `debug`, `create`, `build`, `docker`, `review`, `error`, `broken`, etc. → capable model
5. **Action verbs** with length > 80: `"can you"`, `"please help"`, `"investigate"` → capable model
6. **Everything else** → cheap model

This is not perfect. A short message like `"fix it"` triggers the capable
model (keyword match), which is usually right. But a short message like
`"debug the meaning of life"` also triggers it unnecessarily. The heuristic
is tuned to err on the side of using the capable model when in doubt.

### Circuit Breaker

If pi crashes 3 times in a row, the circuit breaker opens for 60 seconds.
During this time, all requests get an immediate error response instead of
waiting for a timeout. After 60 seconds, one request is allowed through
("half-open"). If it succeeds, normal operation resumes.

This prevents cascading failures but also means a single pi crash blocks the
agent for 60 seconds. That's a long time for a machine-to-machine system — a
TCP-based solution would reconnect in milliseconds.

### Cost Per Task

| Task               | Model  | Tokens (typical) | Approximate Cost |
|--------------------|--------|-------------------|-------------------|
| Ping               | None   | 0                 | $0                |
| "List my projects" | Cheap  | ~200 in, ~150 out | ~$0.00004         |
| "Deploy new code"  | Capable | ~800 in, ~600 out + tools | API-dependent |

The cheap model is the primary cost driver for coordination traffic. The
capable model is only invoked for actual work. In a quiet mesh (just heartbeats
and status checks), the fast path means zero token cost for routine operations.

---

## 4. Security Model

The server applies four layers of defense before a request reaches pi:

```
Internet ──► [L1: IP Whitelist] ──► [L2: Bearer Token] ──►
            [L3: Rate Limiter]  ──► [L4: Input Guard Rails] ──► pi
```

A fifth layer (audit logging) records every interaction for post-hoc analysis.

### L1: IP Whitelist (ufw firewall)

Only trusted peer IPs and localhost can reach the A2A port:

```bash
ufw allow from <PEER_IP_1> to any port 8002 proto tcp
ufw allow from <PEER_IP_2> to any port 8002 proto tcp
ufw allow from 127.0.0.1 to any port 8002 proto tcp
```

### L2: Bearer Token

Requests from non-whitelisted IPs must include `Authorization: Bearer <TOKEN>`.
Whitelisted IPs skip this check. The token is a 64-character hex string shared
across the entire mesh:

```
Authorization: Bearer fdf9bce2d657e376ef751761101122bb5fb15e0e7dafd764e3920061124418ac
```

Requests are also **HMAC-SHA256 signed** using the same shared token. The
signature is sent in the `X-A2A-Signature` header and verified server-side
using constant-time comparison. This is symmetric authentication — all agents
share the same secret — not asymmetric key pairs. It prevents tampering but
does not provide non-repudiation (any agent with the token can forge a
signature).

### L3: Rate Limiter (untrusted IPs only)

| Limit                 | Value |
|-----------------------|-------|
| Requests per window   | 5     |
| Window duration       | 60 seconds |
| Max concurrent tasks  | 3     |

Tracked per IP in memory. Whitelisted IPs are exempt.

### L4: Input Guard Rails

Every incoming message is scanned against three sets of regex patterns before
reaching pi:

- **Prompt injection:** `"ignore previous instructions"`, `"[system]:"`,
  `"<|im_start|>"`, `"override all safety rules"`, and similar patterns.
- **Command injection:** `rm -rf`, `curl ... | sh`, `chmod 777`, backtick
  substitution with substantial content, `$(...)` with substantial content.
- **Data exfiltration:** `cat ~/.ssh`, `cat /etc/passwd`, `base64 ... |`,
  patterns accessing `/root/` or sending key files.

Violations return HTTP 400 with error code `-32006` and are logged to the audit
file. These are syntactic checks — they block obvious attacks but won't catch
sophisticated, obfuscated injections. For production-facing deployments, add a
WAF or CDN-level filtering in front.

### L5: Audit Logging

Every task is recorded to `audit.log` as one JSON line:

```json
{
  "timestamp": "2026-07-22T22:38:00.465821+00:00",
  "event": "task_completed",
  "task_id": "39ce0539-8c47-4730-9ab3-a36e0b957b40",
  "routed_to": "pisti (local)",
  "duration_ms": 3293.78,
  "response_length": 199
}
```

Events: `task_completed`, `task_failed`, `task_cancelled`, `invalid_json`,
`auth_failed`, `guard_rail_blocked`, `rate_limited`, `bad_signature`.

---

## 5. Pi Extensions Included

### Long-Term Memory (`mem0-memory`)

Automatically saves session summaries when the agent shuts down and injects
memory context when it starts up again. This means an agent "remembers" the
last tasks it worked on, even across restarts. Stored in
`~/.pi/agent/mem0-store.json`.

### A2A Adaptor (`pi-a2a-adaptor`)

Gives pi tools to call other agents directly: `a2a_call` (send to one peer) and
`a2a_parallel` (broadcast to multiple). The adaptor automatically includes the
bearer token on outgoing requests.

### Web Browser (`pi-agent-browser-native`)

Full browser automation. Agents can navigate websites, take screenshots, fill
out forms, and click elements by text or label.

### Scheduling (`pi-schedule-prompt`)

Cron-based and one-shot scheduled prompts. An agent can be told "check the
deployment every hour" and it will wake up and do so. Supports subagents for
background execution.

---

## 6. Deterministic Channel vs. A2A

A2A trades speed and cost for flexibility. In many situations, you don't need
an LLM — you need a fast, cheap, predictable communication channel between
machines. The recommended architecture runs **both** in parallel:

```
                  ┌────────────────────────────────┐
                  │        Pi Agent (A2A)           │
                  │  For: diagnosis, deployment,    │
                  │  natural language, adaptation    │
                  │  Latency: 4-56 seconds           │
                  │  Cost: tokens per request        │
                  └────────────┬───────────────────┘
                               │
                  ┌────────────▼───────────────────┐
                  │   Deterministic TCP Channel     │
                  │  For: health checks, file sync, │
                  │  event broadcast, command dispatch│
                  │  Latency: < 1 millisecond        │
                  │  Cost: $0                        │
                  └────────────────────────────────┘
```

### When to use which

| Situation                                          | Channel     | Why |
|----------------------------------------------------|-------------|-----|
| Health check every 500ms                           | TCP         | A2A minimum poll is 4 seconds — 8000x too slow |
| "Deploy the new portfolio build"                   | A2A         | Multiple steps, might need debugging |
| Once built, push `projects.json` to peer agents    | TCP         | Deterministic file transfer, checksum verified |
| "Why did the deploy fail?"                         | A2A         | Requires log analysis and reasoning |
| After fix, broadcast "site is back up"             | TCP         | Push notification, all peers see it in <1ms |
| Peer auto-discovery (who else is in the mesh?)     | A2A         | Agent cards describe capabilities |
| Register peer IP and port after discovery          | TCP         | Deterministic registration, no LLM needed |
| "Show me the last 3 portfolio items"               | A2A         | Natural language understanding needed |
| Read portfolio JSON directly and return it         | TCP         | Simple file read, no reasoning |
| "Restart the nginx container"                      | TCP         | Single predefined command |
| Metrics streaming (CPU, memory, request counts)    | TCP         | High-frequency push, not polling |
| Rotate SSL certificates                            | TCP         | Fixed workflow, zero variability |
| Agent draining ("no new tasks, shutting down")     | TCP         | Must propagate in milliseconds, not 56 seconds |

### What a deterministic channel looks like

A simple framed protocol over TCP — about 200 lines of code:

```
4-byte length prefix (big-endian) + JSON payload

Commands:
  {"cmd":"ping"}                  → {"ack":"pong","ts":"...","load":0.3}
  {"cmd":"exec","task":"restart-container","args":{"name":"portfolio"}}
                                  → {"ack":"ok","pid":1234}
  {"cmd":"push-file","path":"projects.json","data":"<base64>"}
                                  → {"ack":"ok","sha256":"abc..."}
  {"cmd":"event","type":"deploy-done","data":{...}}
                                  → {"ack":"ok"}
  {"cmd":"drain"}                 → {"ack":"draining","queue_depth":2}
  {"cmd":"peer-update","peers":[...]}
                                  → {"ack":"ok"}
```

Each agent runs a small TCP sidecar (separate port, mutual TLS) that handles
these operational commands. The A2A server is only invoked when a task requires
reasoning. This gives you sub-millisecond coordination for routine operations
and LLM-powered flexibility for complex ones — without paying LLM costs for
every ping.

---

## 7. Quick Start

### Prerequisites
- Ubuntu 24.04+ (or any Linux with systemd)
- Python 3.11+
- pi v0.81.1+
- Node.js 20+

### 1. Clone and Run Setup

```bash
git clone https://github.com/tdk67/pi_a2a_setup.git
cd pi_a2a_setup
chmod +x setup.sh
./setup.sh
```

The script will:
- Install pi extensions (memory, a2a, browser, scheduler)
- Create the A2A server from template
- Configure systemd service
- Prompt for agent identity

### 2. Configure Agent Identity

Edit `/etc/pi-a2a-server/.env`:

```bash
A2A_HOST=0.0.0.0
A2A_PORT=8002                                    # choose a unique port
A2A_URL=http://YOUR_IP:8002                      # public URL
A2A_NAME="Your Agent Name"
A2A_TOKEN=your_64_char_hex_token                 # generate: openssl rand -hex 32
A2A_MODEL=google/gemma-3-12b-it                  # cheap model
A2A_CAPABLE_MODEL=deepseek/deepseek-v4-pro       # capable model
```

### 3. Add Peers

Edit `/etc/pi-a2a-server/peers.json`:

```json
{
  "PEER_IP": {
    "name": "PeerName",
    "description": "What this peer does",
    "url": "http://PEER_IP:PORT",
    "skills": ["skill1", "skill2"],
    "last_seen": null
  }
}
```

Or let auto-discovery handle it — the server probes unknown callers for their
agent card.

### 4. Start

```bash
systemctl enable --now pi-a2a-server
systemctl status pi-a2a-server
```

### 5. Test

```bash
# Health check (instant, no tokens)
curl http://YOUR_IP:8002/ping

# Agent card (public, no auth)
curl http://YOUR_IP:8002/.well-known/agent-card.json

# Send a handshake to a peer
curl -X POST http://PEER_IP:PORT/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"message/send",
    "params":{"message":{
      "role":"user",
      "parts":[{"kind":"text","text":"ping"}],
      "kind":"message",
      "messageId":"handshake-1"
    }}
  }'
```

---

## 8. Message Protocol (A2A)

### Send a Task

```json
POST /
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Your task here"}],
      "kind": "message",
      "messageId": "unique-message-id"
    }
  }
}
```

**Required fields:**
- `message.kind`: Must be `"message"`
- `message.messageId`: Any unique string — used for tracing
- `Content-Type: application/json`

### Poll for Result

```json
POST /
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tasks/get",
  "params": {"id": "task-id-from-send-response"}
}
```

States: `pending` → `working` → `completed` / `failed` / `canceled`

Result is in: `result.artifacts[0].parts[0].text`

### Keyword Routing

The server routes tasks to peer agents based on message content. Edit
`ROUTE_KEYWORDS` in `server.py`:

```python
ROUTE_KEYWORDS = {
    "supabase": "database-agent",   # DB-related → Agent B
    "sql": "database-agent",
    "n8n": "automation-agent",      # Workflow-related → Agent C
    "webhook": "automation-agent",
    "deploy": "pisti",              # Code/deploy → stay local
    "docker": "pisti",
    # ... customize per mesh
}
```

### Verification Endpoints

| Endpoint                          | Auth | Returns |
|-----------------------------------|------|---------|
| `/ping`                           | None | `{"pong": true, "agent": "...", "version": "..."}` |
| `/verify`                         | None | Server fingerprint: PID, token status, peer count, log count |
| `/cluster`                        | None | All peers with online/offline status |
| `/.well-known/agent-card.json`    | None | Full A2A agent card (skills, endpoints) |

---

## 9. Audit & Verification

Every interaction is logged to `audit.log` (JSONL format):

```json
{
  "timestamp": "2026-07-22T22:38:00.465821+00:00",
  "event": "task_completed",
  "task_id": "39ce0539-8c47-4730-9ab3-a36e0b957b40",
  "routed_to": "pisti (local)",
  "duration_ms": 3293.78,
  "response_length": 199
}
```

Events: `task_completed`, `task_failed`, `task_cancelled`, `invalid_json`,
`auth_failed`, `guard_rail_blocked`, `rate_limited`, `bad_signature`.

The audit log is append-only JSONL — each line is a self-contained JSON object.
You can tail it, search it with `grep`, or pipe it to any log aggregator.

---

## 10. Troubleshooting

### "invalid_json" in audit log
The sender is missing `"kind": "message"` or `"messageId"` in the message
object. Check the A2A format against the examples in section 8.

### "auth_failed" in audit log
Token mismatch or missing `Authorization: Bearer <token>` header. Verify
`A2A_TOKEN` matches on both sides — it must be identical across the mesh.

### "guard_rail_blocked" in audit log
The message text triggered a security pattern. This is usually a false positive
if someone sends legitimate code or configuration. Adjust the regex patterns in
`check_guard_rails()` in `server.py` if needed.

### Task stuck in "working" state
The pi process may be slow, especially with thinking models (DeepSeek can take
30–60 seconds for complex tasks). The polling schedule maxes out at 56 seconds
— if tasks consistently time out, consider: (a) using a faster model in
`A2A_CAPABLE_MODEL`, or (b) increasing the number of polling intervals in the
`delays` list in `server.py`.

### Circuit breaker open
3 consecutive pi failures triggered a 60-second cooldown. Check
`journalctl -u pi-a2a-server` for the root cause. The circuit auto-resets after
60 seconds.

### High token costs despite low traffic
Check that the fast path is correctly intercepting pings and handshakes. If your
ping message variant isn't matching, pi will spin up for every heartbeat. Add
your message to the `SIMPLE_RESPONSES` dict in `server.py`. Also verify that
the cheap model (not the capable one) is handling coordination traffic.

---

## 11. Files

```
pi_a2a_setup/
├── README.md              ← This file
├── setup.sh               ← One-command setup script
├── .gitignore
├── server.py              ← A2A server (~1300 lines)
├── server.env.example     ← Environment variables template
├── peers.example.json     ← Peer registry template
├── pi-a2a-server.service  ← systemd unit file
├── a2a-send.sh            ← CLI tool to send messages from bash
├── demo-proxy.py          ← Live demo proxy with web frontend
├── demo-frontend/         ← Live demo web UI (React)
└── extensions/
    └── mem0-memory.ts     ← Long-term memory extension
```

---

## Model Fallback Architecture

Pisti uses a **three-tier model fallback chain** that automatically switches providers when credit/quota errors are detected.

### Tier Structure

```
┌──────────────────────────────────────────────────────────────┐
│ CHEAP TIER — Simple/status messages (no tools)               │
│                                                              │
│   Tier 1 (Primary):                                          │
│     alibaba/qwen3.6-flash ← Default light model             │
│                                                              │
│   Tier 2 (Capable fallback — heavier reasoning needed):      │
│     alibaba/qwen3.7-pro → alibaba/qwen3.7-max               │
│                                                              │
│   Tier 3 (Last resort — provider credits exhausted):         │
│     openrouter/deepseek/deepseek-v4-flash                   │
│                                                              │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│ CAPABLE TIER — Complex coding/deployment tasks (full tools)  │
│                                                              │
│   Tier 1 (Primary):                                          │
│     deepseek/deepseek-v4-pro ← Strong reasoning model        │
│                                                              │
│   Tier 2 (Alternative capable):                              │
│     alibaba/qwen3.7-pro → alibaba/qwen3.7-max               │
│                                                              │
│   Tier 3 (Last resort — provider credits exhausted):         │
│     openrouter/deepseek/deepseek-v4-pro                     │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

### How It Works

1. **Task complexity detection** — `PiWorker._is_complex()` analyzes the message:
   - Length > 300 chars, or contains action keywords (`deploy`, `debug`, `docker`)
   - Routes to **capable client**; otherwise uses **cheap client**

2. **Credit error detection** — When the LLM response contains patterns like
   `insufficient_quota`, `out of credit`, `402`, `rate limit`, `billing`, etc.,
   the client automatically restarts with the next model in its fallback chain.

3. **Fallback progression** — Models are consumed in order:
   - Provider-native models first (Alibaba, DeepSeek) — best price/performance
   - Mid-tier capable models next (Qwen 3.7 Pro/Max) — strong reasoning
   - OpenRouter aggregator last — broader availability when primary providers run out of credit

4. **Circuit breaker** — After 3 consecutive non-credit failures, the client
   blocks requests for 60 seconds to prevent error loops. Credit-failure switches
   reset the breaker immediately.

### Required API Keys

| Variable | Purpose |
|----------|--------|
| `QWEN_API_KEY` | Alibaba Model Studio — powers `alibaba/*` models |
| `ANTHROPIC_API_KEY` | Anthropic — available for future fallback tiers |
| `OPENROUTER_API_KEY` | OpenRouter — last-resort fallback when above providers exhaust credit |

---

## Fallback Chains (Credit Protection)

When a model runs out of credit or gets rate-limited (HTTP 402/429/503), the server **automatically switches to the next model** in the fallback chain — no downtime, no error returned to the caller.

### How It Works

```
OpenRouter returns 402 "insufficient_quota"
  → PiRpcClient detects credit error in response text
  → Stops current pi process
  → Starts pi --mode rpc with next fallback model
  → Re-sends prompt
  → Audit log records: "model_chain": "deepseek-v4-pro → alibaba-plan/deepseek-v4-pro"
```

Both the **cheap** and **capable** tiers have independent fallback chains:

```bash
# .env

# Cheap tier fallback: Gemma → Alibaba Qwen 3.6 Flash
A2A_CHEAP_FALLBACKS=alibaba-plan/qwen3.6-flash

# Capable tier fallback: DS V4 Pro → Alibaba DS V4 Pro → Alibaba Qwen 3.7 Max
A2A_CAPABLE_FALLBACKS=alibaba-plan/deepseek-v4-pro,alibaba-plan/qwen3.7-max
```

### Setting Up Alibaba Fallback

You need an Alibaba Model Studio Coding Plan subscription (or the shared mesh key):

1. Install the provider: `pi install npm:pi-alibaba-models`
2. Log in via pi: `/login` → Plans → Alibaba Model Studio Coding Plan → paste your `sk-sp-...` token
3. The login is stored in `~/.pi/agent/auth.json` — the A2A server reads it from there
4. Add the fallback chains to your `.env` (see `server.env.example`)
5. Restart: `systemctl restart pi-a2a-server`

**Available Alibaba Plan models** (free within subscription):

| Model | ID for fallback | Best for |
|-------|-----------------|----------|
| DeepSeek V4 Pro | `alibaba-plan/deepseek-v4-pro` | Drop-in DS replacement |
| Qwen 3.7 Max | `alibaba-plan/qwen3.7-max` | Heavy coding/reasoning |
| Qwen 3.7 Plus | `alibaba-plan/qwen3.7-plus` | General purpose |
| Qwen 3.6 Flash | `alibaba-plan/qwen3.6-flash` | Fast/cheap fallback |
| Qwen 3.8 Max Preview | `alibaba-plan/qwen3.8-max-preview` | Cutting-edge (experimental) |
| GLM-5.2 | `alibaba-plan/glm-5.2` | Alternative reasoning |

### Credit Error Detection

The server detects credit/rater errors by scanning the model response for these patterns:
`insufficient_quota`, `out of credit`, `402`, `payment required`, `rate limit`, `429`, `billing`, `balance`, `top up`, `upgrade your plan`

Non-credit failures (timeouts, model crashes) also trigger fallback after exhausting retries.

### Audit Log Changes

Each task now logs `model_chain` and `tier`:

```json
{
  "event": "task_completed",
  "tier": "capable",
  "model_chain": "deepseek/deepseek-v4-pro → alibaba-plan/qwen3.7-max",
  "duration_ms": 8234.5,
  "response_length": 512
}
```

### Debugging Fallback

```bash
# Watch fallback in real time
journalctl -u pi-a2a-server -f | grep -E "CREDIT|fallback"

# Startup log shows active chains
journalctl -u pi-a2a-server | grep "fallback chain"
# Output:
# [server] Cheap fallback chain: google/gemma-3-12b-it → alibaba-plan/qwen3.6-flash
# [server] Capable fallback chain: deepseek/deepseek-v4-pro → alibaba-plan/deepseek-v4-pro → alibaba-plan/qwen3.7-max
```

---

## Model Fallback Architecture

Pisti uses a **three-tier model fallback chain** that automatically switches providers when credit/quota errors are detected.

### Tier Structure

```
┌──────────────────────────────────────────────────────────────┐
│ CHEAP TIER — Simple/status messages (no tools)               │
│                                                              │
│   Tier 1 (Primary):                                          │
│     alibaba/qwen3.6-flash ← Default light model             │
│                                                              │
│   Tier 2 (Capable fallback — heavier reasoning needed):      │
│     alibaba/qwen3.7-pro → alibaba/qwen3.7-max               │
│                                                              │
│   Tier 3 (Last resort — provider credits exhausted):         │
│     openrouter/deepseek/deepseek-v4-flash                   │
│                                                              │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│ CAPABLE TIER — Complex coding/deployment tasks (full tools)  │
│                                                              │
│   Tier 1 (Primary):                                          │
│     deepseek/deepseek-v4-pro ← Strong reasoning model        │
│                                                              │
│   Tier 2 (Alternative capable):                              │
│     alibaba/qwen3.7-pro → alibaba/qwen3.7-max               │
│                                                              │
│   Tier 3 (Last resort — provider credits exhausted):         │
│     openrouter/deepseek/deepseek-v4-pro                     │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

### How It Works

1. **Task complexity detection** — `PiWorker._is_complex()` analyzes the message:
   - Length > 300 chars, or contains action keywords (`deploy`, `debug`, `docker`)
   - Routes to **capable client**; otherwise uses **cheap client**

2. **Credit error detection** — When the LLM response contains patterns like
   `insufficient_quota`, `out of credit`, `402`, `rate limit`, `billing`, etc.,
   the client automatically restarts with the next model in its fallback chain.

3. **Fallback progression** — Models are consumed in order:
   - Provider-native models first (Alibaba, DeepSeek) — best price/performance
   - Mid-tier capable models next (Qwen 3.7 Pro/Max) — strong reasoning
   - OpenRouter aggregator last — broader availability when primary providers run out of credit

4. **Circuit breaker** — After 3 consecutive non-credit failures, the client
   blocks requests for 60 seconds to prevent error loops. Credit-failure switches
   reset the breaker immediately.

### Required API Keys

| Variable | Purpose |
|----------|--------|
| `QWEN_API_KEY` | Alibaba Model Studio — powers `alibaba/*` models |
| `ANTHROPIC_API_KEY` | Anthropic — available for future fallback tiers |
| `OPENROUTER_API_KEY` | OpenRouter — last-resort fallback when above providers exhaust credit |