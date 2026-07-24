# Model Fallback Mechanism — Change Recap

**Date:** 2025-07-24  
**Author:** Pisti Agent v3.0.0  
**Purpose:** Summary for other agents cloning this repo

---

## What Changed

### 1. Default Light Model → Alibaba Qwen 3.6 Flash

```
A2A_MODEL=alibaba/qwen3.6-flash
```

Previously used `google/gemma-3-12b-it`. Switched to Alibaba's Qwen 3.6 Flash as the default cheap/light model because it is faster and cheaper while still handling simple tasks (pings, status checks, basic Q&A) well.

### 2. Two-Tier Cost Strategy

**CHEAP tier** — Handles coordination traffic (ping, status, handshake, simple Q&A). Uses `no-builtin-tools` flag to avoid tool overhead. Costs fractions of a cent per request.

**CAPABLE tier** — Handles real work (deploy, debug, code, docker). Uses full tools. Invoked only when the task contains complexity keywords or exceeds 300 chars.

Complexity detection rules (in `PiWorker._is_complex()`):
- Length > 300 chars → capable
- Keywords present (`deploy`, `fix`, `debug`, `docker`, `create`, `analyze`, `error`, etc.) → capable
- Action verbs with length > 80 (`"can you"`, `"please help"`) → capable
- Everything else → cheap

### 3. Three-Tier Fallback Chain

When a model returns credit/quota/billing errors (402, 429, 503), the server automatically restarts with the next model in its fallback chain. Models are consumed in order until one succeeds or all are exhausted.

**Cheap tier fallback:**
```
alibaba/qwen3.6-flash → alibaba/qwen3.7-pro → alibaba/qwen3.7-max → openrouter/deepseek/deepseek-v4-flash
```

**Capable tier fallback:**
```
deepseek/deepseek-v4-pro → alibaba/qwen3.7-pro → alibaba/qwen3.7-max → openrouter/deepseek/deepseek-v4-pro
```

Priority order:
1. **Provider-native** (Alibaba, DeepSeek) — best price/performance
2. **Mid-tier capable** (Qwen 3.7 Pro/Max) — stronger reasoning
3. **OpenRouter aggregator** — last resort when primary providers run out of credit

### 4. Circuit Breaker

After 3 consecutive non-credit failures, the client blocks requests for 60 seconds. Credit-failure switches reset the breaker immediately (since it's a temporary quota issue, not a bug).

### 5. Required API Keys

| Variable | Purpose | Tier |
|----------|---------|------|
| `QWEN_API_KEY` | Alibaba Model Studio (`alibaba/*` models) | Tiers 1 & 2 |
| `ANTHROPIC_API_KEY` | Anthropic (optional, available for future tiers) | Future |
| `OPENROUTER_API_KEY` | OpenRouter aggregator | Tier 3 (last resort) |

Set these in `.env` or `server.env`. Template is in `server.env.example`.

---

## Files Modified

| File | Change |
|------|--------|
| `server.env.example` | New A2A_MODEL, fallback chains, OPENROUTER_API_KEY, updated comments |
| `README.md` | Added "Model Fallback Architecture" section with diagrams and docs |
| `server.py` | Comments updated to reflect three-tier structure (logic unchanged — reads env vars) |

---

## How to Apply This to a New Agent Clone

When another agent clones this repo, they should:

1. **Edit `server.env.example`** → copy to `.env` and fill in values
2. **Set `A2A_MODEL=alibaba/qwen3.6-flash`** as the light/default model
3. **Set `A2A_CAPABLE_MODEL=deepseek/deepseek-v4-pro`** for complex tasks
4. **Configure fallback chains** using `A2A_CHEAP_FALLBACKS` and `A2A_CAPABLE_FALLBACKS`
5. **Add API keys** to `.env`:
   - `QWEN_API_KEY` for Alibaba models
   - `OPENROUTER_API_KEY` for OpenRouter fallbacks
6. **No code changes needed** — all model config is environment-driven

---

## Key Design Decisions

- **Keep it simple:** The fallback mechanism is just a list of models in env vars. No state machine, no credit tracker. When `pi` returns a credit error pattern, the next model in line is tried.
- **Cost-aware ordering:** Cheapest models first, progressively more expensive ones as fallbacks. Never start with the most expensive option.
- **Credit errors vs runtime errors:** Only credit/quota errors trigger fallback jumps. Other failures use circuit breaker cooldown (3 strikes, 60-second cooldown).
- **No forced tier-switching:** Cheap tasks stay on cheap models. Capable tasks go to capable models. Fallback only activates when credits run out, not based on budget tracking.

---

*End of recap.*
