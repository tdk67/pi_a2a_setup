# mem0 Memory System Upgrade & Shared Memory Architecture

**Date:** 2026-07-28  
**Author:** Pisti Agent  
**Purpose:** Document the mem0 upgrade from JSON storage to semantic vector DB, shared memory architecture, and new applications (diary app, telegram bot integration)

---

## What Changed

### 1. mem0 Memory Upgrade — JSON → Vector DB

Previously, the `mem0-memory.ts` extension stored session memories in a flat JSON file (`~/.pi/agent/mem0-store.json`). This worked but had limitations:
- Only keyword matching for search
- No semantic understanding
- Slow with many entries
- No shared access between agents

**Now:** Uses the **mem0.ai library** with proper vector embeddings:

| Component | Old | New |
|-----------|-----|-----|
| Storage | `mem0-store.json` (flat JSON) | Qdrant vector DB (384-dim) |
| Search | Keyword match (`includes()`) | Semantic similarity (cosine) |
| Embeddings | None | sentence-transformers/all-MiniLM-L6-v2 (local) |
| API | File I/O | HTTP REST API on port 7011 |
| Shared | No (per-agent file) | Yes (all agents use same service) |
| Scale | ~50 entries before slowdown | Thousands of entries efficient |

### 2. New Service: mem0-service (systemd)

A Python HTTP service that wraps the mem0.ai library:

- **Location:** `/root/.pi/agent/mem0_service.py`
- **Service:** `mem0-service.service` (enabled, auto-starts on boot)
- **Port:** 7011
- **Memory:** ~544MB (embeddings model loaded in memory)
- **Vector DB:** Qdrant local at `/root/.pi/agent/qdrant_data`

**API Endpoints:**

```
GET  /health              → {"status": "ok", "service": "mem0"}
POST /memory/add          → Add a memory (text + metadata)
POST /memory/search       → Semantic search (query + limit)
GET  /memory/list         → List all memories (user_id + limit)
DELETE /memory/delete/<id> → Delete a memory by ID
```

**Example:**

```bash
# Add a memory
curl -X POST http://127.0.0.1:7011/memory/add \
  -H "Content-Type: application/json" \
  -d '{
    "text": "diary app is a flask sqlite app deployed at diary.tdeak67.com",
    "user_id": "pi-agent",
    "metadata": {"project": "/root/diary-app", "type": "project-info"}
  }'

# Semantic search
curl -X POST http://127.0.0.1:7011/memory/search \
  -H "Content-Type: application/json" \
  -d '{"query": "flask diary app", "user_id": "pi-agent", "limit": 3}'
```

### 3. Shared Memory Architecture

All three applications now share the **same** mem0 service:

```
┌──────────────────────────────────────────────────────────────┐
│                     SHARED MEMORY BUS                         │
│                                                               │
│   ┌────────────┐     ┌────────────┐     ┌────────────────┐   │
│   │ pi agent   │     │ telegram   │     │ diary app      │   │
│   │ (this TUI) │     │ bot        │     │ (diary.tdeak)  │   │
│   │            │     │            │     │                │   │
│   │ mem0-mem.ts│     │ mem0-mem.ts│     │ mem0_lookup.py │   │
│   └─────┬──────┘     └─────┬──────┘     └───────┬────────┘   │
│         │                   │                     │           │
│         └───────────┬───────┴─────────────────────┘           │
│                     │ HTTP (localhost:7011)                     │
│                     ▼                                         │
│   ┌─────────────────────────────────────────────────────┐     │
│   │              mem0-service (Python)                    │     │
│   │  • mem0.ai library (v2.0.12)                          │     │
│   │  • sentence-transformers (local embeddings)           │     │
│   │  • Qdrant vector DB (384-dim, on disk)               │     │
│   │  • Semantic search via cosine similarity              │     │
│   └─────────────────────────────────────────────────────┘     │
│                                                               │
└──────────────────────────────────────────────────────────────┘
```

**What this means in practice:**

- pi session works on diary app → saves memory about it
- Next telegram-bot session → retrieves that memory via semantic search
- Diary app can query memory for context (e.g., "what was I working on?")
- All three see the same knowledge base

### 4. mem0-memory.ts Extension — Rewritten

The extension was rewritten to use HTTP API instead of file I/O:

| Feature | Old (JSON) | New (HTTP) |
|---------|------------|------------|
| Load on start | Read JSON file, group by project | `POST /memory/search` (semantic) |
| Save on shutdown | Append to JSON, write file | `POST /memory/add` (HTTP) |
| Incremental save | No | Every 3 user messages |
| Search | `String.includes()` | Cosine similarity |
| Service dependency | None | mem0-service on :7011 |
| Graceful degradation | Always works | Falls back if service down |

**New features:**

1. **Incremental saves** — Saves every 3 messages (prevents data loss on crashes)
2. **Semantic context injection** — Top 5 most relevant memories injected into system prompt
3. **Relevance scores** — Each memory shows its cosine similarity score
4. **Service health check** — Detects if mem0-service is down, shows warning

**Commands remain the same:**
- `/mem0-search <query>` — Semantic search across all memories
- `/mem0-add <text>` — Add a memory manually
- `/mem0-stats` — Show service status and memory count

### 5. Data Migration

45 entries were migrated from the old `mem0-store.json` to Qdrant:

```python
# Migration script (conceptual)
import json, requests

store = json.load(open('/root/.pi/agent/mem0-store.json'))
for entry in store['entries']:
    requests.post('http://127.0.0.1:7011/memory/add', json={
        'text': entry['summary'],
        'user_id': 'pi-agent',
        'metadata': {
            'project': entry['project'],
            'type': 'migrated',
            'original_id': entry['id'],
            'timestamp': entry['timestamp']
        }
    })
```

All old entries are now searchable with semantic similarity.

### 6. Diary App (`/root/diary-app/`)

A personal diary/journal deployed at `diary.tdeak67.com`:

| Component | Tech | Notes |
|-----------|------|-------|
| App | Flask (Python) | Single `app.py`, ~15KB |
| Database | SQLite | `data/diary.db` (mounted volume) |
| Container | Docker | Port 8004 |
| Reverse proxy | Nginx | HTTPS via Let's Encrypt |
| Auth | Password gate | SHA-256 hash, session cookie |

**Features:**
- Create/edit/delete entries with title + body
- Basic markdown rendering (bold, italic, code, links)
- Auto-tags from `#keyword` mentions
- Full-text search with real-time filtering
- Tag-based filtering
- Export all entries as Markdown
- Dark theme, responsive design
- Previous entries shown on login

**Files:**
- `PRD.md` — Full product requirements document
- `app.py` — Flask application
- `Dockerfile` — Container build
- `docker-compose.yml` — Deployment config
- `mem0_lookup.py` — Query memory service from diary app
- `data/` — SQLite databases (mounted volume)

### 7. Telegram Bot Enhancements (`/root/telegram-bot/`)

The telegram bot was enhanced to share the same memory system:

**Changes:**
- `mem0-memory.ts` extension copied to `/root/telegram-bot/extensions/`
- Bot now connects to same mem0-service on port 7011
- Session context saved to shared memory
- Bot "remembers" what pi agent was working on

**Also added:**
- CV/cover-letter PDF generation (`pi-cv-cover-letter.pdf`)
- Presentation HTML (`pi-presentation.html`)

---

## Architecture Diagram

```
                          ┌─────────────────────────────────────────┐
                          │          AGENT MESH (existing)           │
                          │                                         │
  ┌───────────────┐       │  ┌──────────┐    A2A     ┌──────────┐  │
  │ diary app     │       │  │ Agent A  │◄──────────►│ Agent B  │  │
  │ diary.tdeak67 │       │  │ (pisti)  │            │ (peer)   │  │
  │ .com          │       │  └────┬─────┘            └──────────┘  │
  │               │       │       │                                 │
  │ Flask+SQLite  │       │       │ mem0 HTTP                       │
  │ Docker :8004  │       │  ┌────▼──────────────────────────────┐ │
  │               │       │  │     mem0-service :7011             │ │
  │ mem0_lookup.py├───────┼──┤  (shared semantic memory)          │ │
  └───────────────┘       │  │  • Qdrant vector DB                │ │
                          │  │  • sentence-transformers            │ │
  ┌───────────────┐       │  │  • Semantic search                  │ │
  │ telegram bot  │       │  └────────────────────────────────────┘ │
  │               │       │       ▲                                  │
  │ mem0-memory.ts├───────┼───────┤                                  │
  └───────────────┘       │       │                                  │
                          │  ┌────┴──────────────────────────────┐  │
                          │  │ pi agent (this TUI)                │  │
                          │  │ mem0-memory.ts                     │  │
                          │  └───────────────────────────────────┘  │
                          └─────────────────────────────────────────┘
```

---

## How to Set Up on a New Agent Clone

### Prerequisites

- Python 3.11+
- pip packages: `mem0ai`, `sentence-transformers`, `qdrant-client`
- ~600MB disk for embeddings model + vector DB

### 1. Install mem0 Dependencies

```bash
pip install mem0ai sentence-transformers qdrant-client
```

### 2. Deploy mem0 Service

```bash
# Copy service file
cp /root/.pi/agent/mem0_service.py /etc/pi-a2a-server/mem0_service.py

# Create systemd unit
cat > /etc/systemd/system/mem0-service.service << 'EOF'
[Unit]
Description=mem0 Memory Service for Pi Agent
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /etc/pi-a2a-server/mem0_service.py
WorkingDirectory=/etc/pi-a2a-server
Restart=always
RestartSec=10
Environment=HF_HOME=/root/.cache/huggingface

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
systemctl daemon-reload
systemctl enable --now mem0-service
systemctl status mem0-service
```

### 3. Update mem0-memory.ts Extension

Replace the old file-based extension with the new HTTP-based one:

```bash
# The new extension is already at:
# /root/pi_a2a_setup/extensions/mem0-memory.ts

# Copy to active extensions directory
cp /root/pi_a2a_setup/extensions/mem0-memory.ts ~/.pi/agent/extensions/

# Also copy to telegram-bot if using it
cp /root/pi_a2a_setup/extensions/mem0-memory.ts /root/telegram-bot/extensions/
```

### 4. Migrate Old Data (Optional)

If you have an old `mem0-store.json`:

```bash
python3 << 'EOF'
import json, requests

try:
    store = json.load(open('/root/.pi/agent/mem0-store.json'))
except:
    print("No old data to migrate")
    exit()

count = 0
for entry in store.get('entries', []):
    resp = requests.post('http://127.0.0.1:7011/memory/add', json={
        'text': entry.get('summary', ''),
        'user_id': 'pi-agent',
        'metadata': {
            'project': entry.get('project', 'unknown'),
            'type': 'migrated',
            'original_id': entry.get('id', ''),
            'timestamp': entry.get('timestamp', '')
        }
    })
    if resp.status_code == 200:
        count += 1

print(f"Migrated {count} entries")
EOF
```

### 5. Verify

```bash
# Check service is running
systemctl status mem0-service

# Test health endpoint
curl http://127.0.0.1:7011/health

# Test adding a memory
curl -X POST http://127.0.0.1:7011/memory/add \
  -H "Content-Type: application/json" \
  -d '{"text":"test memory","user_id":"pi-agent"}'

# Test semantic search
curl -X POST http://127.0.0.1:7011/memory/search \
  -H "Content-Type: application/json" \
  -d '{"query":"test","user_id":"pi-agent","limit":3}'

# Start pi and check extension loads
pi
# Should see: "🧠 mem0: Loaded N relevant memories (semantic search)"
```

---

## Key Design Decisions

### Why HTTP service instead of library import?

- **Shared access** — Multiple agents (pi, telegram-bot, diary app) can all use the same memory
- **Isolation** — If mem0 crashes, agents keep running (graceful degradation)
- **Independence** — Agents don't need Python or mem0 installed (just HTTP client)
- **Observability** — Easy to monitor, debug, and restart independently

### Why local embeddings instead of API?

- **Zero cost** — No per-query embedding fees
- **Privacy** — Data never leaves the server
- **Speed** — Local inference, no network round-trip
- **Offline** — Works without internet (after initial model download)

### Why incremental saves every 3 messages?

- **Data loss prevention** — If connection drops or crashes, max 2 messages lost
- **Cost balance** — Not too frequent (overhead), not too sparse (data loss)
- **Tunable** — Change `SAVE_INTERVAL` constant in extension

### Why Qdrant instead of Chroma/Faiss?

- **Production-ready** — Battle-tested, good docs
- **On-disk storage** — Doesn't need to fit in RAM
- **HTTP API** — Easy to inspect and debug
- **mem0.ai native** — Best integration with the library

---

## Current Status

| Component | Status | Notes |
|-----------|--------|-------|
| mem0-service | ✅ Running | Active since Jul 28 15:56 |
| Qdrant vector DB | ✅ Active | 384-dim, on-disk |
| mem0-memory.ts (pi) | ✅ Active | HTTP-based, semantic search |
| mem0-memory.ts (telegram) | ✅ Active | Same extension, shared memory |
| diary app | ✅ Deployed | diary.tdeak67.com, port 8004 |
| mem0_lookup.py | ✅ Working | Diary → mem0 bridge |
| Old JSON data | ✅ Migrated | 45 entries → Qdrant |

---

## Troubleshooting

### "⚠️ mem0: Service unavailable at localhost:7011"

The mem0-service is not running:

```bash
systemctl status mem0-service
journalctl -u mem0-service -n 50
systemctl restart mem0-service
```

### High memory usage (~544MB)

This is normal — the sentence-transformers model is loaded in RAM for fast embeddings. The service is designed to run 24/7 as a background daemon.

### Search returns no results

Check if memories exist:

```bash
curl http://127.0.0.1:7011/memory/list?user_id=pi-agent&limit=10
```

If empty, the extension hasn't saved anything yet. Memories are saved:
- Every 3 user messages (incremental)
- On session shutdown (final)

### Slow semantic search

First search after service start may be slow (model warm-up). Subsequent searches are fast (<100ms).

---

## Files Modified/Created

| File | Change |
|------|--------|
| `/root/.pi/agent/mem0_service.py` | **NEW** — Python HTTP service wrapping mem0.ai |
| `/etc/systemd/system/mem0-service.service` | **NEW** — systemd unit for auto-start |
| `/root/.pi/agent/extensions/mem0-memory.ts` | **REWRITTEN** — HTTP-based, semantic search |
| `/root/telegram-bot/extensions/mem0-memory.ts` | **NEW** — Copy for telegram bot |
| `/root/diary-app/mem0_lookup.py` | **NEW** — Diary → mem0 bridge |
| `/root/diary-app/app.py` | **NEW** — Full Flask diary application |
| `/root/diary-app/PRD.md` | **NEW** — Product requirements document |
| `/root/diary-app/Dockerfile` | **NEW** — Container build |
| `/root/diary-app/docker-compose.yml` | **NEW** — Deployment config |
| `/root/pi_a2a_setup/extensions/mem0-memory.ts` | **UPDATED** — New version for distribution |

---

## Summary

The memory system evolved from a simple JSON file to a full semantic memory service shared across multiple agents. This enables:

1. **True cross-agent memory** — pi, telegram-bot, and diary app all see the same knowledge
2. **Semantic search** — Find memories by meaning, not just keywords
3. **Incremental saves** — Prevent data loss with periodic saves during sessions
4. **Local embeddings** — Zero cost, full privacy, works offline
5. **Production-ready** — systemd service, auto-start, graceful degradation

The architecture is now: **agents → HTTP → mem0-service → Qdrant → semantic search**

All components are running and operational as of 2026-07-28.

---

*End of recap.*
