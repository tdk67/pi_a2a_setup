# VPS Restart Recovery & System Upgrades — 2026-08-05

**Date:** 2026-08-05  
**Author:** Pisti Agent  
**Purpose:** Document recovery actions after VPS restart, mem0 memory optimization, telegram bot image processing, and scheduled architectural improvements.

---

## 1. VPS Restart Status

### Boot & System
- VPS restarted at **16:11 UTC** (Wed Aug 5), came up clean on kernel **6.8.0-90-generic**
- All Docker containers auto-started successfully (restart policies verified):
  - `tdeak67-website` (healthy), `bedtime-app`, `deutsch-linguist`, `taskmind-portfolio`, `user_service_api` (healthy), `user_service_db` (healthy), `diary-app-pocketbase`, `colosseum_engine`
- PM2 processes: `my-agent`, `telegram-bot`
- All systemd services: docker, nginx, mem0-service, pi-a2a-server

### Package Updates
- **Applied during reboot**: docker-ce, netplan, dbus, apport, plymouth, multipath-tools, kpartx, initramfs-tools
- **Held back** (intentionally pinned): cloud-init, linux-generic, linux-image-generic, linux-image-virtual, linux-virtual, linux-headers-generic, linux-headers-virtual
- Reasoning: kernel packages are pinned to 6.8.0-90 to avoid VPS instability

---

## 2. mem0 Memory Optimization

### Problem
mem0_service.py was consuming **921 MB RSS** despite only storing 107 vector entries (540KB on disk). The bulk was PyTorch with CUDA libraries loaded for the embedding model, even though the VPS has no GPU.

### Quick Fix: CPU-only PyTorch (2026-08-05)

```
pip uninstall torch -y
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

| Component | Before | After |
|---|---|---|
| PyTorch + CUDA libs | ~450 MB (libcublas, libnvrtc, libtriton, libnvJitLink) | ~150 MB (cpu-only) |
| sentence-transformers model | ~90 MB | ~90 MB |
| Python + Qdrant overhead | ~380 MB | ~364 MB |
| **Total RSS** | **921 MB** | **604 MB** |
| **Saved** | — | **317 MB (34%)** |

The service response time and accuracy are unchanged — embeddings are computed identically on CPU, just without the unused CUDA overhead.

### Scheduled Improvements (2026-08-06 @ 04:00 UTC)

Two jobs scheduled via `pi-schedule-prompt`:

| Job | Time | Goal |
|---|---|---|
| **mem0-onnx-migration** | 04:00 | Replace PyTorch sentence-transformers with ONNX runtime (~50MB instead of ~150MB for inference) |
| **mem0-tiered-architecture** | 04:30 | Add hot-cache layer (recent N memories in RAM, rest on disk), keyword search, stats endpoint |

---

## 3. Telegram Bot — Image Processing

### Architecture

The bot now handles **photo messages** with a dedicated vision pipeline:

```
User sends photo
    → sharp resize/compress (max 2048px, JPEG 85%)
    → base64 encode
    → Primary: alibaba-plan/qwen3.7-plus (free, vision-capable)
         ↓ fails/escalates
    → Fallback: openrouter/google/gemini-2.5-flash (cheap, reliable)
```

### Model Selection & Cost

| Tier | Model | Input/Mtok | Output/Mtok | ~Cost/image |
|---|---|---|---|---|
| **Vision (primary)** | `alibaba-plan/qwen3.7-plus` | $0.32 | $1.28 | ~$0.001 |
| **Vision (fallback)** | `openrouter/google/gemini-2.5-flash` | $0.30 | $2.50 | ~$0.003 |
| ~~Previous fallback~~ | ~~openrouter/anthropic/claude-fable-latest~~ | ~~$10.00~~ | ~~$50.00~~ | ~~$0.15-0.50~~ |

The fallback was switched from Claude Fable to Gemini 2.5 Flash — **~30-50× cheaper**.

### Files Changed
- `/root/telegram-bot/index.js` — Added photo handler, vision pi process, sharp image processing
- `/root/telegram-bot/package.json` — Added `sharp` dependency
- `/root/telegram-bot/extensions/mem0-memory.ts` — Already HTTP-based

### Key Design Decisions
- Vision model stripped of edit/write tools (bash/read only) — it's for analysis, not code modification
- Images resized to max 2048px before encoding to keep prompt sizes manageable
- Same silence detection / timeout / escalation patterns as text processing
- Vision responses auto-escalate to gemini-2.5-flash if qwen fails
- Deepseek-v4-pro does NOT support images — the capable text model stays separate

### Full 4-Tier Pipeline

| Tier | Model | Cost | Tools | Handles |
|---|---|---|---|---|
| **Fast path** | None | $0 | — | `ping`, `status`, `handshake` |
| **Quick text** | `alibaba-plan/qwen3.6-flash` | Free | bash, read | Simple questions |
| **Vision** | `alibaba-plan/qwen3.7-plus` | ~Free | bash, read | Photo analysis |
| **Vision fallback** | `openrouter/google/gemini-2.5-flash` | ~$0.003/img | bash, read | Photo escalation |
| **Capable text** | `openrouter/deepseek/deepseek-v4-pro` | Paid | Full tools | Complex coding/debug/deploy |

---

## 4. mem0 Scalability Assessment

### Current State
- **107 memories** across all user_ids (pi-agent, telegram-user, global)
- Qdrant on-disk storage: 540KB in `/root/.pi/agent/qdrant_data/collection/pi_memory/storage.sqlite`
- mem0's `history.db`: 111 rows (internal tracking, not user memories)
- `mem0-store.json`: 47 entries (old JSON backup, kept for migration reference)

### Scalability Analysis

**What's fine:**
- Qdrant local mode with `on_disk: true` — uses mmap, vectors paged in/out by OS. 100K vectors @ 384 dims ≈ ~150MB on disk, well within limits
- The 604MB is a **fixed cost** for the embedding model, not proportional to data
- SQLite-based vector storage scales to millions of rows with proper indexing

**What needs attention:**
- Local Qdrant has no distributed indexing — search degrades beyond ~100K vectors
- `GET /memory/list` fetches ALL memories then paginates (top_k=999999) — needs real pagination
- No caching layer — every search goes through Qdrant
- `mem0.ai` library auto-loads history.db which grows unbounded

**Scale projections:**

| Memories | Qdrant DB | Search Latency | RAM (embedder) | Action needed |
|---|---|---|---|---|
| 1K | ~1.5MB | <100ms | 604MB | None |
| 10K | ~15MB | ~200ms | 604MB | Add pagination |
| 100K | ~150MB | ~500ms | 604MB | Add hot cache, switch to server Qdrant |
| 1M+ | ~1.5GB | >1s | 604MB | Dedicated Qdrant server, tiered storage |

The **tiered architecture** (scheduled for 04:30) addresses the "recent in memory, rest in DB" concern:
- Hot cache: last 1000 memories in a Python dict for instant access
- Cold storage: Qdrant on disk for everything else
- Keyword search: SQLite LIKE for fast exact matches (no embedding needed)
- Stats endpoint to monitor hit rates and size

---

## 5. System Services Summary

| Service | Type | Port | Status | Memory |
|---|---|---|---|---|
| `mem0-service` | systemd | 7011 | ✅ Active | 604 MB RSS |
| `pi-a2a-server` | systemd | 8002 | ✅ Active | ~30 MB |
| `telegram-bot` | PM2 | — | ✅ Online | ~80 MB |
| `my-agent` | PM2 | — | ✅ Online | ~80 MB |
| `demo-proxy` | systemd | — | ✅ Active | ~40 MB |
| `hermes-gateway` | systemd | — | ✅ Active | ~330 MB |
| Docker daemon | systemd | — | ✅ Active | ~105 MB |

---

## 6. Files Updated in This Session

| File | Change |
|---|---|
| `/root/telegram-bot/index.js` | Added photo handler, vision pipeline, sharp integration |
| `/root/telegram-bot/package.json` | Added `sharp` dependency |
| `torch` package | Replaced CUDA (2.13.0+cu130) with CPU (2.13.0+cpu) |
| `/root/pi_a2a_setup/extensions/mem0-memory.ts` | Synced from active HTTP-based version |
| `/root/pi_a2a_setup/VPS_RESTART_RECAP_2026-08-05.md` | **NEW** — This document |

### Scheduled (pending execution)
- `/root/.pi/agent/mem0_service.py` — ONNX migration (04:00 2026-08-06)
- `/root/.pi/agent/mem0_service.py` — Tiered architecture (04:30 2026-08-06)

---

*End of recap.*