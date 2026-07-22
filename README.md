# Pi A2A Agent Mesh — Setup & Operations

Production-grade agent-to-agent communication mesh built on **pi coding agent** (v0.81.1+) and the **[fasta2a](https://pypi.org/project/fasta2a/)** Python library (A2A protocol v0.3.0). Includes long-term memory, web access, guard rails, scheduling, and dual-model switching.

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                     AGENT MESH                           │
│                                                          │
│   ┌─────────┐    A2A (JSON-RPC)    ┌─────────┐          │
│   │ Agent A │◄───────────────────►│ Agent B │          │
│   │ :8002   │    HMAC-signed       │ :9090   │          │
│   └────┬────┘                      └────┬────┘          │
│        │                                │                │
│   ┌────┴────┐                      ┌────┴────┐          │
│   │ cheap   │                      │ custom  │          │
│   │ model   │                      │ backend │          │
│   └─────────┘                      └─────────┘          │
│                                                          │
│   Message Flow: Send → Poll → Verify (no hallucination)  │
│   Security: 5-layer defense (firewall → audit)          │
│   Memory: Automatic session persistence across restarts  │
└──────────────────────────────────────────────────────────┘
```

### Why Two Pi Instances? (Dual-Model Pool)

A single pi process is locked to one model at startup. Instead of restarting the process to switch models (slow, loses state), the server runs **two processes in parallel**:

| Process | Model | Tools | Cost | Purpose |
|---------|-------|-------|------|---------|
| **Cheap** | gemma-3-12b (or similar) | None (`--no-builtin-tools`) | ~$0.10/M tokens | Chat, handshakes, coordination, simple Q&A |
| **Capable** | deepseek-v4-pro (or similar) | Full (read/bash/write/edit) | Varies | Code generation, deployment, debugging, real work |

**Automatic switching**: The server detects task complexity before dispatching. Simple pings never touch the expensive model. Complex tasks automatically route to the capable one. Both processes are always warm — no cold-start delay.

**If you don't need dual-model**: Set `A2A_CAPABLE_MODEL=$A2A_MODEL` in `.env` and the server will only start one process.

### Verified Message Exchange (No Hallucination)

Every A2A message follows a **send → poll → verify** cycle:

```
Sender                    Receiver
  │                          │
  │── POST message/send ───►│  (returns { "result": { "id": "task-uuid" } })
  │                          │  (pi processes in background)
  │                          │  (audit log: task_started)
  │                          │
  │── POST tasks/get ◄──────│  (poll: state = "working")
  │   {"id": "task-uuid"}    │
  │                          │
  │── POST tasks/get ◄──────│  (poll: state = "completed")
  │   {"id": "task-uuid"}    │  artifacts[0].parts[0].text = result
  │                          │  (audit log: task_completed + duration + length)
```

**Poll details**: The client polls `tasks/get` every 2 seconds, up to 60 times (120s max timeout). Most cheap-model tasks complete in 1-5s (1-3 polls). Capable-model tasks with DeepSeek thinking can take 30-60s (15-30 polls).

**Why polling instead of streaming?** The standard A2A protocol supports both polling (`tasks/get`) and SSE streaming (`message/stream`). This implementation uses polling because:
- Simpler to debug and audit (every state transition is logged)
- Works through any HTTP proxy or firewall
- Task results are immutable artifacts, not partial stream chunks
- The `a2a-send.sh` CLI tool can work with plain `curl`

Peers that support streaming can use the `message/stream` method instead — this server is compatible with standard A2A clients.

**Key properties:**
- **Task IDs are UUIDs** — every message traceable end-to-end
- **Audit log is JSONL** — immutable append-only record of every interaction
- **State machine**: `pending → working → completed/failed/canceled`
- **No "trust me bro"** — responses are verified artifacts, not model hallucinations
- **HMAC signatures** — cryptographic proof of sender identity

## A2A Protocol & fasta2a

This server is built on **[fasta2a](https://pypi.org/project/fasta2a/)** (Python), which implements the [Google A2A protocol](https://a2a-protocol.org/) (Agent-to-Agent, v0.3.0).

### What fasta2a Provides

| Component | Role |
|-----------|------|
| `FastA2A` app | HTTP server with agent card, task endpoints, lifespan |
| `Broker` + `Storage` | Task queue and state persistence (InMemory by default) |
| `Worker` | Task execution loop (we subclass this for pi integration) |
| `Skill` | Declarative agent capabilities in the agent card |
| `Message`, `Artifact`, `TextPart` | A2A data model types |

### How This Server Extends Standard A2A

| Feature | Standard A2A | Our Implementation |
|---------|-------------|-------------------|
| Transport | JSON-RPC 2.0 over HTTP | Same — fully compatible |
| Task lifecycle | `tasks/get`, `tasks/cancel` | Same + **circuit breaker** (3 failures → 60s cooldown) |
| Auth | Not specified (bring your own) | **5-layer**: IP whitelist, Bearer token, rate limiter, guard rails, audit |
| Model selection | Not specified | **Dual-model pool** with automatic complexity detection |
| Peer discovery | Agent card polling | Same + **auto-discovery** of unknown callers |
| Routing | Not specified | **Keyword-based routing** to peer agents |
| Signing | Not specified | **HMAC-SHA256** request signing for cryptographic identity verification |
| Fast path | Not specified | **Instant responses** for ping/status/handshake (0ms, 0 tokens) |

### Why Not Pure A2A?

Standard A2A is a protocol spec — it defines message formats and endpoints but leaves auth, routing, model selection, security, and reliability as implementation details. This server fills those gaps with production-hardened defaults while remaining **fully compatible** with any standard A2A client.

## Security: 5-Layer Defense

```
Internet → [L1: Firewall] → [L2: Bearer Token] → [L3: Rate Limiter]
                                              → [L4: Guard Rails] → [L5: Audit Log] → pi
```

| Layer | What | How |
|-------|------|-----|
| **L1: Firewall** | IP whitelist | Only trusted peer IPs + localhost |
| **L2: Bearer Token** | Auth for non-whitelisted IPs | 64-char hex token in `Authorization` header |
| **L3: Rate Limiter** | Abuse prevention | 5 req/60s, 3 concurrent max for untrusted IPs |
| **L4: Guard Rails** | Injection detection | Blocks prompt injection, command injection, exfiltration |
| **L5: Audit Log** | Full traceability | Every request logged to JSONL with timestamp, IP, task_id, duration |

### Guard Rail Patterns Detected

- **Prompt injection**: "ignore previous instructions", "act as a different", system prompt overrides
- **Command injection**: `rm -rf`, `curl | sh`, `chmod 777`, backtick substitution
- **Data exfiltration**: `cat ~/.ssh`, `base64 -d |`, pattern-based file access

## Pi Extensions Included

### 1. Long-Term Memory (`mem0-memory`)
- Auto-saves session summaries on shutdown
- Injects memory context on startup
- Commands: `/mem0-search`, `/mem0-stats`, `/mem0-clear`
- Storage: `~/.pi/agent/mem0-store.json`

### 2. A2A Adaptor (`pi-a2a-adaptor`)
- Tools: `a2a_call`, `a2a_parallel`
- Enables agents to delegate tasks to peers
- Supports short names and full URLs
- Auto-includes bearer token on peer calls

### 3. Web Browser (`pi-agent-browser-native`)
- Full browser automation via `agent_browser` tool
- Navigate sites, take snapshots, fill forms, click links
- Semantic actions: click by text/label/role
- QA presets, screenshots, Electron app support

### 4. Scheduling (`pi-schedule-prompt`)
- Cron-based and one-shot scheduled prompts
- Natural language: "check deployment every hour"
- Subagent support for background execution
- Auto-cleanup of disabled jobs

## Quick Start

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

Or let auto-discovery handle it — the server probes unknown callers for their agent card.

### 4. Start

```bash
systemctl enable --now pi-a2a-server
systemctl status pi-a2a-server
```

### 5. Test

```bash
# Health check
curl http://YOUR_IP:8002/ping

# Agent card
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

## Message Protocol (A2A)

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

**Critical fields:**
- `message.kind`: Must be `"message"`
- `message.messageId`: Required — any unique string
- `Content-Type: application/json`: Required header

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

The server auto-routes tasks based on message content:

```python
ROUTE_KEYWORDS = {
    "supabase": "database-agent",
    "docker": "devops-agent",
    "deploy": "devops-agent",
    # ... customize per mesh
}
```

## Audit & Verification

Every interaction is logged to `audit.log` (JSONL format):

```json
{
  "timestamp": "2026-07-22T22:38:00.465821+00:00",
  "event": "task_completed",
  "task_id": "39ce0539-8c47-4730-9ab3-a36e0b957b40",
  "routed_to": "agent-name (local)",
  "duration_ms": 3293.78,
  "response_length": 199
}
```

Events: `task_completed`, `task_failed`, `task_cancelled`, `invalid_json`, `auth_failed`, `guard_rail_blocked`, `rate_limited`, `bad_signature`

### Verification Endpoints

| Endpoint | Auth | Returns |
|----------|------|---------|
| `/ping` | None | `{"pong": true, "agent": "...", "version": "..."}` |
| `/verify` | None | Server fingerprint: PID, token status, peer count, log count |
| `/cluster` | None | All peers with online/offline status |
| `/.well-known/agent-card.json` | None | Full A2A agent card (skills, endpoints) |

## Files

```
pi_a2a_setup/
├── README.md              ← This file
├── setup.sh               ← One-command setup script
├── .gitignore
├── server.py              ← A2A server (template — no hardcoded agent info)
├── server.env.example     ← Environment variables template
├── peers.example.json     ← Peer registry template
├── pi-a2a-server.service  ← systemd unit file
├── a2a-send.sh            ← CLI tool to send messages from bash
└── extensions/
    └── mem0-memory.ts     ← Long-term memory extension
```

## Adding a New Agent to the Mesh

1. Clone this repo on the new VPS
2. Run `./setup.sh`
3. Configure `.env` with unique NAME, PORT, TOKEN
4. Add the new agent's IP to every existing agent's `TRUSTED_IPS`
5. Add existing agents to the new agent's `peers.json`
6. Copy the shared `A2A_TOKEN` to all agents
7. Start the service
8. Verify: `curl http://NEW_IP:PORT/ping`
9. Verify mesh: `curl http://ANY_AGENT:PORT/cluster`

## Troubleshooting

### "invalid_json" in audit log
→ The sender is missing `"kind": "message"` or `"messageId"` in the message object. Check the A2A format.

### "auth_failed" in audit log
→ Token mismatch or missing `Authorization: Bearer <token>` header. Verify `.env` on both sides.

### "guard_rail_blocked" in audit log
→ The message text triggered a security pattern. Check for command injection or prompt injection keywords.

### Task stuck in "working" state
→ The pi process may be slow with the capable model (DeepSeek thinking can take 30-60s). Poll with longer intervals.

### Circuit breaker open
→ 3 consecutive failures triggered cooldown. Check `journalctl -u pi-a2a-server` for the root cause. Circuit auto-resets after 60s.

## Model Switching Details

The complexity detector checks (in order):

1. **Fast path**: Exact matches for "ping", "status", "handshake" → instant response (0ms, 0 tokens)
2. **Short messages** (≤30 chars): "hello", "hi", "hey", "test" → instant response
3. **Length > 300 chars** → capable model
4. **Keywords**: deploy, fix, debug, create, build, docker, review, analyze, error, broken, etc. → capable model
5. **Action verbs** with length > 80: "can you", "please help", "investigate" → capable model
6. **Everything else** → cheap model

Override by setting `A2A_MODEL=A2A_CAPABLE_MODEL` in `.env` to always use the capable model.

## License

MIT