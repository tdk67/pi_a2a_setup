"""
Pi A2A Server — pi coding agent exposed via A2A protocol.

Security layers:
  L1: Firewall — port 8002 whitelisted to trusted IPs only
  L2: Bearer token — required for non-whitelisted callers
  L3: Rate limiter — caps requests from untrusted IPs
  L4: Guard rails — prompt injection / command injection detection
  L5: Audit logging — every request logged to JSONL file

Features:
- Peer registry with auto-discovery
- Task router — keyword-based delegation to the right agent
- Periodic agent card refresh

URL: http://YOUR_IP:PORT
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import hashlib
import hmac
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import uvicorn
from fasta2a import FastA2A, Skill
from fasta2a.broker import Broker, InMemoryBroker
from fasta2a.schema import (
    Artifact,
    Message,
    TaskIdParams,
    TaskSendParams,
    TextPart,
)
from fasta2a.storage import InMemoryStorage, Storage
from fasta2a.worker import Worker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST = os.environ.get("A2A_HOST", "0.0.0.0")
PORT = int(os.environ.get("A2A_PORT", "8002"))
AGENT_URL = os.environ.get("A2A_URL", f"http://localhost:{PORT}")
AGENT_NAME = os.environ.get("A2A_NAME", "Pi A2A Agent")
A2A_TOKEN = os.environ.get("A2A_TOKEN", "")

# Model for A2A tasks — use cheap model to save tokens
A2A_MODEL = os.environ.get("A2A_MODEL", "google/gemma-3-12b-it")
A2A_CAPABLE_MODEL = os.environ.get("A2A_CAPABLE_MODEL", "deepseek/deepseek-v4-pro")
A2A_SYSTEM_PROMPT = os.environ.get(
    "A2A_SYSTEM_PROMPT",
    "You are a pi coding agent in the A2A agent mesh. "
    "Answer concisely. For simple status/handshake messages, respond in 1-2 sentences."
)
A2A_CAPABLE_SYSTEM_PROMPT = os.environ.get(
    "A2A_CAPABLE_SYSTEM_PROMPT",
    "You are a pi coding agent with full system access. "
    "You have full tools (read, bash, write, edit). Solve problems thoroughly."
)

# Complexity detection — keywords that trigger the capable model
COMPLEX_KEYWORDS = [
    "deploy", "fix", "debug", "create", "build", "analyze", "review",
    "refactor", "implement", "write", "edit", "docker", "restart",
    "configure", "migrate", "investigate", "error", "broken", "down",
    "failed", "crash", "security", "optimize", "refactor", "audit",
    "backup", "restore", "install", "update", "upgrade", "nginx",
    # Add domain-specific keywords for your services:
    "ssl", "tls", "domain", "dns",
]

TRUSTED_IPS = {
    # Add your peer agent IPs here:
    # "PEER_IP_1",  # Peer agent 1
    # "PEER_IP_2",  # Peer agent 2
    "127.0.0.1",     # localhost
}

PEERS_FILE = Path(__file__).parent / "peers.json"
AUDIT_LOG_FILE = Path(__file__).parent / "audit.log"


# ---------------------------------------------------------------------------
# HMAC message signing — cryptographic proof of agent identity
# ---------------------------------------------------------------------------

def sign_request(body: bytes) -> str:
    """Sign a request body with HMAC-SHA256 using the shared token."""
    if not A2A_TOKEN:
        return ""
    return hmac.new(A2A_TOKEN.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str) -> bool:
    """Verify an HMAC signature. Uses constant-time comparison."""
    if not A2A_TOKEN or not signature:
        return False
    expected = hmac.new(A2A_TOKEN.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def server_fingerprint() -> dict:
    """Return deterministic, verifiable server state."""
    return {
        "agent": AGENT_NAME,
        "version": "1.0.0",
        "url": AGENT_URL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "token_configured": bool(A2A_TOKEN),
        "peers_count": 0,  # populated at request time
        "audit_log_entries": 0,  # populated at request time
    }

# Rate limiting: non-whitelisted IPs
RATE_LIMIT_WINDOW = 60       # seconds
RATE_LIMIT_MAX_REQUESTS = 5  # max requests per window per IP
RATE_LIMIT_MAX_CONCURRENT = 3  # max concurrent tasks from untrusted IPs

# ---------------------------------------------------------------------------
# Audit Logger
# ---------------------------------------------------------------------------

class AuditLogger:
    """Structured JSONL audit logging for all A2A task requests."""

    def __init__(self, log_path: Path = AUDIT_LOG_FILE):
        self.log_path = log_path
        self._lock = asyncio.Lock()

    async def log(self, event: dict):
        """Append an audit event to the JSONL log file."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        async with self._lock:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")

audit = AuditLogger()

# ---------------------------------------------------------------------------
# Guard Rails — Prompt Injection & Command Injection Detection
# ---------------------------------------------------------------------------

# Patterns that indicate a prompt injection attempt
PROMPT_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"ignore\s+(all\s+)?(previous|above|prior|your)\s+(instructions?|prompts?|rules?)",
        r"(you\s+are|act\s+as|pretend\s+to\s+be|roleplay\s+as)\s+(now\s+)?(a\s+)?(different|another|new)",
        r"(system\s*prompt|system\s*message|developer\s*message)\s*[:=]",
        r"<\|im_start\|>",
        r"\[system\]\s*\(",
        r"\[INST\]",
        r"<system>",
        r"forget\s+(all\s+)?(previous|your)\s+(instructions?|training|guidelines)",
        r"(override|bypass|disable|skip)\s+(all\s+)?(safety|security|rules|restrictions|limits?)",
        r"(you\s+(must|have\s+to|are\s+required\s+to)|i\s+(command|order|demand)\s+you\s+to)",
    ]
]

# Patterns that indicate command injection
COMMAND_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\brm\s+-rf\b",
        r"\bmkfs\.",
        r"\bdd\s+if=",
        r">\s*/dev/sd",
        r"\bcurl\s+.*\|\s*(ba)?sh\b",
        r"\bwget\s+.*-O\s*-.*\|\s*(ba)?sh\b",
        r"\beval\s+",
        r"\bexec\s+",
        r"\bchmod\s+777\b",
        r"\bchmod\s+-R\b",
        r"\bnc\s+-[nl]",
        r"\bnetcat\b",
        r"`[^`]{20,}`",  # backtick command substitution with substantial content
        r"\$\([^)]{20,}\)",  # $(...) with substantial content
    ]
]

# Patterns indicating data exfiltration
EXFILTRATION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"cat\s+~?\/?\.(ssh|env|aws|gcloud|config)",
        r"(send|upload|post|transfer).*(/etc/(passwd|shadow)|/root/|\.pem|\.key)",
        r"base64\s+(-d|--decode)?\s*\|",
        r"xxd\s+-p\s+-r\s*\|",
    ]
]

def check_guard_rails(message_text: str) -> str | None:
    """
    Check message for security violations.
    Returns error message if a violation is found, None if clean.
    """
    # Length limit
    if len(message_text) > 50_000:
        return "Message too long (max 50000 characters)"

    # Prompt injection
    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(message_text):
            return f"Blocked: potential prompt injection detected"

    # Command injection
    for pattern in COMMAND_INJECTION_PATTERNS:
        if pattern.search(message_text):
            return f"Blocked: potential command injection detected"

    # Exfiltration
    for pattern in EXFILTRATION_PATTERNS:
        if pattern.search(message_text):
            return f"Blocked: potential data exfiltration detected"

    return None


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Tracks request rates per IP. Only applies to non-whitelisted IPs."""

    def __init__(self, window: int = RATE_LIMIT_WINDOW, max_requests: int = RATE_LIMIT_MAX_REQUESTS,
                 max_concurrent: int = RATE_LIMIT_MAX_CONCURRENT):
        self.window = window
        self.max_requests = max_requests
        self.max_concurrent = max_concurrent
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._concurrent: dict[str, int] = defaultdict(int)

    def check(self, ip: str) -> str | None:
        """Check rate limit. Returns error message or None if allowed."""
        now = time.time()

        # Clean old entries
        self._requests[ip] = [t for t in self._requests[ip] if now - t < self.window]

        # Check request count in window
        if len(self._requests[ip]) >= self.max_requests:
            return f"Rate limit exceeded: {self.max_requests} requests per {self.window}s"

        # Check concurrent tasks
        if self._concurrent[ip] >= self.max_concurrent:
            return f"Too many concurrent tasks (max {self.max_concurrent})"

        return None

    def record(self, ip: str):
        """Record a request from this IP."""
        self._requests[ip].append(time.time())
        self._concurrent[ip] += 1

    def release(self, ip: str):
        """Release a concurrent slot."""
        self._concurrent[ip] = max(0, self._concurrent[ip] - 1)


rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Auth Middleware
# ---------------------------------------------------------------------------

class AuthMiddleware(BaseHTTPMiddleware):
    """Enforces Bearer token + IP whitelist + rate limiting + guard rails."""

    def __init__(self, app, token: str, trusted_ips: set[str]):
        super().__init__(app)
        self.token = token
        self.trusted_ips = trusted_ips

    async def dispatch(self, request: Request, call_next):
        # Public endpoints — always allow
        if request.url.path in ("/.well-known/agent-card.json", "/cluster", "/ping", "/verify"):
            return await call_next(request)

        # Only protect POST /
        if request.method != "POST" or request.url.path != "/":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        is_trusted = client_ip in self.trusted_ips

        # ---- L3: Rate limiting (untrusted IPs only) ----
        if not is_trusted:
            rate_error = rate_limiter.check(client_ip)
            if rate_error:
                await audit.log({
                    "event": "rate_limited",
                    "ip": client_ip,
                    "reason": rate_error,
                })
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32004, "message": rate_error}},
                    status_code=429,
                )

        # ---- L2: Bearer token (untrusted IPs only) ----
        if not is_trusted:
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                await audit.log({"event": "auth_failed", "ip": client_ip, "reason": "no_token"})
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32001, "message": "Authentication required. Use Bearer token."}},
                    status_code=401,
                )
            provided_token = auth_header[7:]
            if self.token and provided_token != self.token:
                await audit.log({"event": "auth_failed", "ip": client_ip, "reason": "bad_token"})
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32002, "message": "Invalid token."}},
                    status_code=403,
                )

        # ---- HMAC signature verification ----
        body = await request.body()
        sig_header = request.headers.get("X-A2A-Signature", "")
        sig_valid = verify_signature(body, sig_header) if sig_header else None
        # sig_valid=None means no signature provided (ok for trusted IPs)
        # sig_valid=False means bad signature
        # sig_valid=True means good signature
        if sig_valid is False:
            await audit.log({"event": "bad_signature", "ip": client_ip})
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32003, "message": "Invalid HMAC signature."}},
                status_code=403,
            )

        # ---- L4: Guard rails — check message content ----
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raw_preview = body.decode(errors="replace")[:500] if body else "(empty body)"
            await audit.log({
                "event": "invalid_json",
                "ip": client_ip,
                "raw_body_preview": raw_preview,
                "content_type": request.headers.get("content-type", "none"),
            })
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32600, "message": "Invalid JSON"}},
                status_code=400,
            )

        message = data.get("params", {}).get("message", {})
        message_text = ""
        for part in message.get("parts", []):
            if part.get("kind") == "text":
                message_text += part.get("text", "")
            elif part.get("kind") == "data":
                message_text += json.dumps(part.get("data", {}))

        violation = check_guard_rails(message_text)
        if violation:
            await audit.log({
                "event": "guard_rail_blocked",
                "ip": client_ip,
                "is_trusted": is_trusted,
                "reason": violation,
                "message_preview": message_text[:200],
            })
            return JSONResponse(
                {"jsonrpc": "2.0", "id": data.get("id"),
                 "error": {"code": -32006, "message": violation}},
                status_code=400,
            )

        # Record rate limit for untrusted IPs
        if not is_trusted:
            rate_limiter.record(client_ip)

        # Store attributes for logging later
        request.state.audit = {
            "ip": client_ip,
            "is_trusted": is_trusted,
            "message_preview": message_text[:500],
            "message_length": len(message_text),
        }

        try:
            response = await call_next(request)
            return response
        finally:
            if not is_trusted:
                rate_limiter.release(client_ip)


# ---------------------------------------------------------------------------
# Peer Registry
# ---------------------------------------------------------------------------

@dataclass
class PeerRegistry:
    """Manages known A2A peers."""

    peers: dict[str, dict] = field(default_factory=dict)

    def load(self):
        if PEERS_FILE.exists():
            self.peers = json.loads(PEERS_FILE.read_text())
            print(f"[registry] Loaded {len(self.peers)} peers: {[p['name'] for p in self.peers.values()]}")
        else:
            print("[registry] No peers file found, starting empty")

    def save(self):
        PEERS_FILE.write_text(json.dumps(self.peers, indent=2))

    def get_by_ip(self, ip: str) -> dict | None:
        return self.peers.get(ip)

    def get_by_name(self, name: str) -> dict | None:
        for peer in self.peers.values():
            if peer["name"].lower() == name.lower():
                return peer
        return None

    def add_or_update(self, ip: str, name: str, description: str, url: str,
                      skills: list[str] | None = None):
        now = datetime.now(timezone.utc).isoformat()
        if ip in self.peers:
            self.peers[ip]["last_seen"] = now
            if skills:
                self.peers[ip]["skills"] = skills
        else:
            self.peers[ip] = {
                "name": name,
                "description": description,
                "url": url,
                "skills": skills or [],
                "last_seen": now,
            }
            print(f"[registry] New peer discovered: {name} @ {url}")
        self.save()

    def list_peers(self) -> list[dict]:
        return [{"ip": ip, **data} for ip, data in self.peers.items()]


# ---------------------------------------------------------------------------
# Caller Tracking Middleware
# ---------------------------------------------------------------------------

class CallerTrackingMiddleware(BaseHTTPMiddleware):
    """Auto-discovers A2A callers by probing their IP for agent cards."""

    def __init__(self, app, registry: PeerRegistry, our_ip: str = "127.0.0.1"):  # Override with your server IP
        super().__init__(app)
        self.registry = registry
        self.our_ip = our_ip
        self._known_ips: set[str] = set()

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else None
        if client_ip and client_ip != "127.0.0.1" and client_ip != self.our_ip:
            if client_ip not in self._known_ips:
                self._known_ips.add(client_ip)
                asyncio.create_task(self._discover_caller(client_ip))
        return await call_next(request)

    async def _discover_caller(self, ip: str):
        for port in [9090, 8080, 8002, 8000, 3000]:
            try:
                async with _make_peer_client(timeout=5) as client:
                    resp = await client.get(
                        f"http://{ip}:{port}/.well-known/agent-card.json"
                    )
                    if resp.status_code == 200:
                        card = resp.json()
                        name = card.get("name", "Unknown")
                        description = card.get("description", "")
                        url = card.get("url", f"http://{ip}:{port}")
                        skills = [s["id"] for s in card.get("skills", [])]
                        self.registry.add_or_update(ip, name, description, url, skills)
                        return
            except Exception:
                continue


# ---------------------------------------------------------------------------
# Keyword routing
# ---------------------------------------------------------------------------

ROUTE_KEYWORDS: dict[str, str] = {
    # Customize keyword → agent mapping for your mesh:
    # "keyword": "agent-name",
    # "docker": "devops-agent",
    # "supabase": "database-agent",
}


def _make_peer_client(timeout: int = 30) -> httpx.AsyncClient:
    """Create an httpx client with the shared auth token for peer calls."""
    headers = {}
    if A2A_TOKEN:
        headers["Authorization"] = f"Bearer {A2A_TOKEN}"
    return httpx.AsyncClient(timeout=timeout, headers=headers)


class SignedClient:
    """httpx client wrapper that HMAC-signs POST bodies automatically."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    async def post(self, url: str, json_data: dict) -> httpx.Response:
        body = json.dumps(json_data).encode()
        headers = {"Content-Type": "application/json"}
        if A2A_TOKEN:
            headers["Authorization"] = f"Bearer {A2A_TOKEN}"
            headers["X-A2A-Signature"] = sign_request(body)
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as c:
            return await c.post(url, content=body)

    async def get(self, url: str) -> httpx.Response:
        headers = {}
        if A2A_TOKEN:
            headers["Authorization"] = f"Bearer {A2A_TOKEN}"
        async with httpx.AsyncClient(timeout=5, headers=headers) as c:
            return await c.get(url)
# Task Router
# ---------------------------------------------------------------------------

@dataclass
class TaskRouter:
    registry: PeerRegistry
    our_name: str = "pisti"
    our_url: str = AGENT_URL

    def route(self, message: Message) -> tuple[str | None, str | None]:
        text = self._extract_text(message).lower()
        for part in message.get("parts", []):
            if part.get("kind") == "data":
                route_to = part.get("data", {}).get("route_to", "").lower()
                if route_to:
                    peer = self.registry.get_by_name(route_to)
                    if peer:
                        return route_to, peer["url"]
                    if route_to == self.our_name:
                        return None, None

        scores: dict[str, int] = {}
        for keyword, agent in ROUTE_KEYWORDS.items():
            if keyword in text:
                scores[agent] = scores.get(agent, 0) + 1

        if scores:
            best_agent = max(scores, key=scores.get)
            if best_agent != self.our_name:
                peer = self.registry.get_by_name(best_agent)
                if peer:
                    print(f"[router] Routing to {best_agent} based on keywords: {scores}")
                    return best_agent, peer["url"]

        return None, None

    async def delegate_task(self, message: Message) -> dict:
        agent_name, agent_url = self.route(message)
        if agent_url is None:
            return {"local": True}

        print(f"[router] Delegating to {agent_name} at {agent_url}")
        try:
            clean_message = {
                "role": message["role"],
                "parts": message["parts"],
                "kind": "message",
                "messageId": str(uuid.uuid4()),
            }

            async with _make_peer_client(timeout=30) as client:
                resp = await client.post(
                    f"{agent_url}/",
                    json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "message/send",
                        "params": {"message": clean_message},
                    },
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                result = resp.json()
                task_id = result.get("result", {}).get("id")

                if task_id:
                    for _ in range(60):
                        await asyncio.sleep(2)
                        poll_resp = await client.post(
                            f"{agent_url}/",
                            json={
                                "jsonrpc": "2.0", "id": 2,
                                "method": "tasks/get",
                                "params": {"id": task_id},
                            },
                            headers={"Content-Type": "application/json"},
                        )
                        poll_result = poll_resp.json()
                        task = poll_result.get("result", {})
                        state = task.get("status", {}).get("state")
                        if state == "completed":
                            return {"local": False, "agent": agent_name,
                                    "agent_url": agent_url, "task_id": task_id,
                                    "result": task}
                        elif state in ("failed", "canceled"):
                            return {"local": False, "agent": agent_name,
                                    "agent_url": agent_url, "task_id": task_id,
                                    "error": f"Task {state}"}
                    return {"local": False, "agent": agent_name,
                            "task_id": task_id, "error": "timeout after 120s"}
                else:
                    return {"local": False, "agent": agent_name,
                            "error": "No task ID returned"}
        except Exception as e:
            print(f"[router] Delegation to {agent_name} failed: {e}")
            return {"local": False, "agent": agent_name, "error": str(e)}

    @staticmethod
    def _extract_text(message: Message) -> str:
        parts = message.get("parts", [])
        texts = []
        for part in parts:
            if part.get("kind") == "text":
                texts.append(part.get("text", ""))
            elif part.get("kind") == "data":
                texts.append(json.dumps(part.get("data", {})))
        return " ".join(texts)


# ---------------------------------------------------------------------------
# Pi RPC client
# ---------------------------------------------------------------------------

# Circuit breaker — prevent error loops
CIRCUIT_BREAKER_THRESHOLD = 3  # consecutive failures before opening
CIRCUIT_BREAKER_COOLDOWN = 60  # seconds before trying again

class PiRpcClient:
    def __init__(self, pi_bin: str = "pi", model: str = A2A_MODEL,
                 system_prompt: str = A2A_SYSTEM_PROMPT,
                 no_tools: bool = True):
        self.pi_bin = pi_bin
        self.model = model
        self.system_prompt = system_prompt
        self.no_tools = no_tools
        self.process: asyncio.subprocess.Process | None = None
        self._response_event = asyncio.Event()
        self._accumulated_text: str = ""
        self._lock = asyncio.Lock()
        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open_until: float = 0.0

    async def start(self):
        args = [
            self.pi_bin, "--mode", "rpc",
            "--model", self.model,
            "--system-prompt", self.system_prompt,
        ]
        if self.no_tools:
            args.append("--no-builtin-tools")
        self.process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._read_stderr())

    async def stop(self):
        if self.process:
            try:
                self.process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            except ProcessLookupError:
                pass

    async def _read_stderr(self):
        if not self.process or not self.process.stderr:
            return
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            text = line.decode().rstrip()
            if text:
                print(f"[pi stderr] {text}")

    def _send_cmd(self, cmd: dict):
        if not self.process or not self.process.stdin:
            raise RuntimeError("Pi process not running")
        self.process.stdin.write((json.dumps(cmd) + "\n").encode())

    def _circuit_breaker_check(self) -> str | None:
        """Check circuit breaker. Returns error string if circuit is open."""
        if self._consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
            now = time.time()
            if now < self._circuit_open_until:
                remaining = int(self._circuit_open_until - now)
                return f"[circuit open] Too many failures ({self._consecutive_failures}). " \
                       f"Try again in {remaining}s."
            else:
                # Cooldown expired, half-open — allow one attempt
                print(f"[circuit] Half-open after {CIRCUIT_BREAKER_COOLDOWN}s cooldown")
                self._consecutive_failures = 0
        return None

    async def send_prompt(self, message: str, max_retries: int = 2) -> str:
        # Circuit breaker check
        cb_error = self._circuit_breaker_check()
        if cb_error:
            return cb_error

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                async with self._lock:
                    self._accumulated_text = ""
                    self._response_event.clear()
                    read_task = asyncio.create_task(self._read_response())
                    self._send_cmd({"type": "prompt", "message": message})
                    try:
                        await asyncio.wait_for(self._response_event.wait(), timeout=120)
                    except asyncio.TimeoutError:
                        read_task.cancel()
                        raise asyncio.TimeoutError(f"Pi did not respond in 120s")
                    await read_task
                    result = self._accumulated_text
                # Success — reset circuit breaker
                self._consecutive_failures = 0
                return result
            except Exception as e:
                last_error = e
                self._consecutive_failures += 1
                print(f"[worker] Attempt {attempt+1}/{max_retries+1} failed: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(1.5 * (attempt + 1))  # backoff: 1.5s, 3s

        # All retries exhausted — open circuit breaker
        self._circuit_open_until = time.time() + CIRCUIT_BREAKER_COOLDOWN
        print(f"[circuit] OPEN — {self._consecutive_failures} consecutive failures")
        return f"[error] All {max_retries+1} attempts failed. Last error: {last_error}. " \
               f"Circuit breaker open for {CIRCUIT_BREAKER_COOLDOWN}s."

    async def send_abort(self):
        async with self._lock:
            self._send_cmd({"type": "abort"})
            self._response_event.set()

    async def _read_response(self) -> None:
        if not self.process or not self.process.stdout:
            return
        while True:
            try:
                line = await self.process.stdout.readline()
            except Exception:
                break
            if not line:
                break
            try:
                event = json.loads(line.decode().rstrip())
            except json.JSONDecodeError:
                continue
            if event.get("type") == "message_update":
                delta_event = event.get("assistantMessageEvent", {})
                if delta_event.get("type") == "text_delta":
                    self._accumulated_text += delta_event.get("delta", "")
            elif event.get("type") == "agent_end":
                self._response_event.set()
                break


# ---------------------------------------------------------------------------
# Custom Worker
# ---------------------------------------------------------------------------

# Fast responses for simple messages — skip pi entirely, save tokens
SIMPLE_RESPONSES = {
    "ping": "🟢 Pong! Pi A2A Agent — online and healthy. All 5 security layers active.",
    "status": "🟢 Pi A2A Agent — server running. Check /cluster for peers.",
    "handshake": "🤝 Handshake confirmed! Pi A2A Agent here. Connected to mesh.",
}

@dataclass
class PiWorker(Worker[dict]):
    broker: Broker
    storage: Storage[dict]
    cheap_client: PiRpcClient = field(default_factory=PiRpcClient)
    capable_client: PiRpcClient | None = None  # set in lifespan
    router: TaskRouter | None = None

    # ── Complexity detection ──
    @staticmethod
    def _is_complex(text: str) -> bool:
        """Detect if a message needs the capable model."""
        text_lower = text.lower()
        # Length heuristic: > 300 chars usually means real work
        if len(text) > 300:
            return True
        # Keyword match
        for kw in COMPLEX_KEYWORDS:
            if kw in text_lower:
                return True
        # Multi-sentence with action verbs
        action_verbs = ["i need", "can you", "please", "help me", "check why",
                       "find out", "look into", "tell me how"]
        if any(v in text_lower for v in action_verbs) and len(text) > 80:
            return True
        return False

    # ── Pick the right model ──
    def _pick_client(self, text: str) -> PiRpcClient:
        """Choose cheap or capable client based on task complexity."""
        if self._is_complex(text) and self.capable_client is not None:
            print(f"[worker] ⚡ Routing to CAPABLE model ({A2A_CAPABLE_MODEL})")
            return self.capable_client
        print(f"[worker] 💤 Using cheap model ({A2A_MODEL})")
        return self.cheap_client

    async def run_task(self, params: TaskSendParams) -> None:
        task_id = params["id"]
        context_id = params["context_id"]
        message = params["message"]
        t_start = time.time()

        await self.storage.update_task(task_id, state="working")

        # Try routing to a peer
        if self.router:
            route_result = await self.router.delegate_task(message)
            is_local = route_result.get("local", True)

            if not is_local:
                agent_name = route_result.get("agent", "unknown")
                if "error" in route_result:
                    response_text = f"[Routed to {agent_name}] Error: {route_result['error']}"
                else:
                    peer_task = route_result.get("result", {})
                    response_text = ""
                    for artifact in peer_task.get("artifacts", []):
                        for part in artifact.get("parts", []):
                            if part.get("kind") == "text":
                                response_text += part.get("text", "")
                    if not response_text:
                        for msg in peer_task.get("history", []):
                            if msg.get("role") == "agent":
                                for part in msg.get("parts", []):
                                    if part.get("kind") == "text":
                                        response_text += part.get("text", "")
                    response_text = response_text or f"[Routed to {agent_name} — task {route_result.get('task_id')} completed]"

                response_msg = Message(
                    role="agent",
                    parts=[TextPart(kind="text",
                           text=f"🤖 Router: delegated to {agent_name}\n\n{response_text}")],
                    kind="message", message_id=str(uuid.uuid4()),
                    context_id=context_id, task_id=task_id,
                )
                artifact = Artifact(
                    artifact_id=str(uuid.uuid4()),
                    name=f"delegated-to-{agent_name}",
                    description=f"Task delegated to {agent_name}",
                    parts=[TextPart(kind="text", text=response_text)],
                )
                await self.storage.update_task(
                    task_id, state="completed",
                    new_messages=[response_msg], new_artifacts=[artifact],
                )

                await audit.log({
                    "event": "task_completed", "task_id": task_id,
                    "routed_to": agent_name, "duration_ms": (time.time() - t_start) * 1000,
                    "response_length": len(response_text),
                })
                return

        # ---- Fast path: simple messages don't need pi ----
        user_prompt = self._extract_text_from_message(message)
        fast_response = self._check_fast_path(user_prompt)
        if fast_response:
            duration = (time.time() - t_start) * 1000
            response_msg = Message(
                role="agent",
                parts=[TextPart(kind="text", text=fast_response)],
                kind="message", message_id=str(uuid.uuid4()),
                context_id=context_id, task_id=task_id,
            )
            artifact = Artifact(
                artifact_id=str(uuid.uuid4()),
                name="fast-response",
                description="Instant response for simple message",
                parts=[TextPart(kind="text", text=fast_response)],
            )
            await self.storage.update_task(
                task_id, state="completed",
                new_messages=[response_msg], new_artifacts=[artifact],
            )
            await audit.log({
                "event": "task_completed", "task_id": task_id,
                "routed_to": "pisti (local, fast-path)",
                "duration_ms": duration,
                "response_length": len(fast_response),
            })
            print(f"[worker] Fast-path response ({len(fast_response)} chars, {duration:.0f}ms)")
            return

        # ---- Full path: use pi for complex messages ----
        context = await self.storage.load_context(context_id) or {}
        conversation_history = context.get("messages", [])

        if conversation_history:
            history_text = "\n---\n".join(
                f"[{m['role']}]: {self._extract_text_from_message(m)}"
                for m in conversation_history[-10:]
            )
            full_prompt = (
                f"Previous conversation:\n{history_text}\n---\n"
                f"New message from user:\n{user_prompt}"
            )
        else:
            full_prompt = user_prompt

        try:
            client = self._pick_client(user_prompt)
            print(f"[worker] Task {task_id[:8]} locally... prompt: {user_prompt[:100]}...")
            response_text = await client.send_prompt(full_prompt)
            duration = (time.time() - t_start) * 1000
            print(f"[worker] Task {task_id[:8]} done ({len(response_text)} chars, {duration:.0f}ms)")

            response_msg = Message(
                role="agent",
                parts=[TextPart(kind="text", text=response_text)],
                kind="message", message_id=str(uuid.uuid4()),
                context_id=context_id, task_id=task_id,
            )
            artifact = Artifact(
                artifact_id=str(uuid.uuid4()),
                name="pi-response",
                description="Response from pi coding agent",
                parts=[TextPart(kind="text", text=response_text)],
            )
            await self.storage.update_task(
                task_id, state="completed",
                new_messages=[response_msg], new_artifacts=[artifact],
            )

            conversation_history.append(message)
            conversation_history.append(response_msg)
            context["messages"] = conversation_history
            await self.storage.update_context(context_id, context)

            await audit.log({
                "event": "task_completed", "task_id": task_id,
                "routed_to": "pisti (local)", "duration_ms": duration,
                "response_length": len(response_text),
            })

        except Exception as e:
            print(f"[worker] Task {task_id[:8]} failed: {e}")
            await audit.log({
                "event": "task_failed", "task_id": task_id,
                "error": str(e),
                "duration_ms": (time.time() - t_start) * 1000,
            })
            error_msg = Message(
                role="agent",
                parts=[TextPart(kind="text", text=f"Error: {str(e)}")],
                kind="message", message_id=str(uuid.uuid4()),
                context_id=context_id, task_id=task_id,
            )
            await self.storage.update_task(
                task_id, state="failed", new_messages=[error_msg],
            )

    async def cancel_task(self, params: TaskIdParams) -> None:
        await audit.log({"event": "task_cancelled", "task_id": params["id"]})
        await self.cheap_client.send_abort()
        if self.capable_client:
            await self.capable_client.send_abort()
        await self.storage.update_task(params["id"], state="canceled")

    def build_message_history(self, history: list[Message]) -> list[Any]:
        return [
            {"role": m["role"], "text": self._extract_text_from_message(m)}
            for m in history
        ]

    def build_artifacts(self, result: Any) -> list[Artifact]:
        text = str(result) if result else ""
        return [Artifact(
            artifact_id=str(uuid.uuid4()), name="pi-response",
            parts=[TextPart(kind="text", text=text)],
        )]

    @staticmethod
    def _check_fast_path(text: str) -> str | None:
        """Check if this is a simple message that can be answered without pi."""
        text_lower = text.strip().lower()
        # Exact keyword matches
        for keyword, response in SIMPLE_RESPONSES.items():
            if text_lower == keyword:
                return response
        # Short handshake-like messages (≤30 chars)
        if len(text) <= 30:
            handshake_words = {"ping", "hello", "hi", "hey", "handshake", "test",
                               "status", "you there?", "are you there", "who are you"}
            if text_lower in handshake_words:
                return f"👋 Hello from Pi A2A Agent! Online and ready. Mesh: check /cluster for peers."
            if "ping" in text_lower and len(text) <= 50:
                return f"🟢 Pong! Agent here. {text[:50]}"
        return None

    @staticmethod
    def _extract_text_from_message(msg: Message) -> str:
        parts = msg.get("parts", [])
        texts = []
        for part in parts:
            if part.get("kind") == "text":
                texts.append(part.get("text", ""))
            elif part.get("kind") == "data":
                texts.append(json.dumps(part.get("data", {})))
        return "\n".join(texts) if texts else "(empty message)"


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> FastA2A:
    registry = PeerRegistry()
    registry.load()

    storage: Storage[dict] = InMemoryStorage[dict]()
    broker = InMemoryBroker()
    cheap_client = PiRpcClient(model=A2A_MODEL, system_prompt=A2A_SYSTEM_PROMPT, no_tools=True)
    capable_client = PiRpcClient(model=A2A_CAPABLE_MODEL, system_prompt=A2A_CAPABLE_SYSTEM_PROMPT, no_tools=False)
    router = TaskRouter(registry=registry)

    app = FastA2A(
        storage=storage,
        broker=broker,
        name=AGENT_NAME,
        url=AGENT_URL,
        version="3.0.0",
        description=(
            "A pi coding agent in the A2A agent mesh. "
            "Hardened with 5-layer security (firewall, auth, rate limit, guard rails, audit). "
            "Configure agent description via env vars."
        ),
        provider={"organization": "A2A Mesh", "url": "https://github.com/tdk67/pi_a2a_setup"},
        skills=[
            Skill(
                id="code-generation", name="Code Generation & Editing",
                description="Write, edit, and refactor code in any language.",
                tags=["code", "editing", "development"],
                examples=["Write a Python script to parse JSON files"],
                input_modes=["application/json"], output_modes=["application/json"],
            ),
            Skill(
                id="system-operations", name="System Operations",
                description="Execute bash commands, manage files, inspect directories.",
                tags=["bash", "system", "devops"],
                examples=["List all running Docker containers"],
                input_modes=["application/json"], output_modes=["application/json"],
            ),
            Skill(
                id="custom-task", name="Custom Task Processing",
                description="Handle domain-specific tasks for this agent.",
                tags=["custom", "automation"],
                examples=["Process a custom task"],
                input_modes=["application/json"], output_modes=["application/json"],
            ),
            Skill(
                id="code-review", name="Code Review & Analysis",
                description="Review code and analyze architecture.",
                tags=["review", "analysis", "architecture"],
                examples=["Review this PR for security issues"],
                input_modes=["application/json"], output_modes=["application/json"],
            ),
            Skill(
                id="peer-routing", name="Agent Mesh Routing",
                description="Route tasks to peer agents in the mesh.",
                tags=["routing", "coordinator", "mesh"],
                examples=["route_to: agent-name — Delegate a task"],
                input_modes=["application/json"], output_modes=["application/json"],
            ),
        ],
        docs_url=None,
    )

    # ---- Security middleware (outermost) ----
    if A2A_TOKEN:
        app.add_middleware(AuthMiddleware, token=A2A_TOKEN, trusted_ips=TRUSTED_IPS)
        print(f"[server] Auth: token + IP whitelist + rate limiter + guard rails enabled")
        print(f"[server] Trusted IPs: {sorted(TRUSTED_IPS)}")
    else:
        print("[server] ⚠️  WARNING: No A2A_TOKEN — running without auth!")

    app.add_middleware(CallerTrackingMiddleware, registry=registry)

    # ---- Cluster status endpoint ----
    async def cluster_status(request: Request) -> JSONResponse:
        peers_status = []
        for ip, peer in registry.peers.items():
            status = "unknown"
            try:
                async with _make_peer_client(timeout=5) as client:
                    resp = await client.get(
                        f"{peer['url']}/.well-known/agent-card.json"
                    )
                    status = "online" if resp.status_code == 200 else f"error({resp.status_code})"
            except Exception as e:
                status = f"offline: {str(e)[:50]}"
            peers_status.append({**peer, "ip": ip, "status": status})

        return JSONResponse({
            "agent": AGENT_NAME, "url": AGENT_URL, "peers": peers_status,
            "security": {
                "layers": [
                    "L1: Firewall — IP whitelisted",
                    "L2: Bearer token — required for non-whitelisted IPs",
                    "L3: Rate limiter — 5 req/60s for untrusted IPs",
                    "L4: Guard rails — prompt/command injection detection",
                    "L5: Audit logging — all requests logged",
                ],
                "token_required": bool(A2A_TOKEN),
                "trusted_ips": sorted(TRUSTED_IPS),
            },
            "route_keywords": {k: v for k, v in sorted(ROUTE_KEYWORDS.items())},
        })

    app.add_route("/cluster", cluster_status)

    # ---- Ping endpoint (no AI, instant, verifiable) ----
    async def ping(request: Request) -> JSONResponse:
        return JSONResponse({
            "pong": True,
            "agent": AGENT_NAME,
            "version": "1.0.0",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    app.add_route("/ping", ping)

    # ---- Verify endpoint (deterministic server state) ----
    async def verify(request: Request) -> JSONResponse:
        fp = server_fingerprint()
        fp["peers_count"] = len(registry.peers)
        try:
            fp["audit_log_entries"] = sum(1 for _ in open(AUDIT_LOG_FILE)) if AUDIT_LOG_FILE.exists() else 0
        except Exception:
            fp["audit_log_entries"] = -1
        return JSONResponse(fp)

    app.add_route("/verify", verify)

    # ---- Worker with router (dual-model pool) ----
    worker = PiWorker(
        broker=broker, storage=storage,
        cheap_client=cheap_client, capable_client=capable_client,
        router=router,
    )

    @asynccontextmanager
    async def custom_lifespan(app: FastA2A) -> AsyncIterator[None]:
        print(f"[server] Starting cheap model ({A2A_MODEL})...")
        await cheap_client.start()
        print(f"[server] Starting capable model ({A2A_CAPABLE_MODEL})...")
        await capable_client.start()
        print(f"[server] Audit log: {AUDIT_LOG_FILE}")

        async with app.task_manager:
            async with worker.run():
                print(f"[server] A2A agent ready at {AGENT_URL}")
                print(f"[server] 5-layer security active")

                async def refresh_peers():
                    while True:
                        await asyncio.sleep(300)
                        for ip, peer in list(registry.peers.items()):
                            try:
                                async with _make_peer_client(timeout=5) as client:
                                    resp = await client.get(
                                        f"{peer['url']}/.well-known/agent-card.json"
                                    )
                                    if resp.status_code == 200:
                                        card = resp.json()
                                        skills = [s["id"] for s in card.get("skills", [])]
                                        registry.add_or_update(
                                            ip, peer["name"],
                                            card.get("description", ""),
                                            peer["url"], skills,
                                        )
                            except Exception:
                                pass

                refresh_task = asyncio.create_task(refresh_peers())
                try:
                    yield
                finally:
                    refresh_task.cancel()
                    print("[server] Shutting down...")
                    await cheap_client.stop()
                    await capable_client.stop()

    app.router.lifespan_context = custom_lifespan
    return app


def main():
    app = create_app()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()