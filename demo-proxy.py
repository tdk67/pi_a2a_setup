"""
Live Demo Proxy — sits between the React UI and the A2A agent mesh.
Strips all secrets, enforces message whitelist, masks IPs.
"""

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse
from starlette.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

# ── Config ──
A2A_TOKEN = os.environ.get("A2A_TOKEN", "")
PISTI_URL = os.environ.get("PISTI_URL", "http://127.0.0.1:8002")
SISI_URL = os.environ.get("SISI_URL", "")
NORI_URL = os.environ.get("NORI_URL", "")
DEMO_PORT = int(os.environ.get("DEMO_PORT", "3001"))

# IP masking — real IP → label
IP_MASK: dict[str, str] = {}
if os.environ.get("PISTI_IP"):
    IP_MASK[os.environ["PISTI_IP"]] = "Agent A (Coordinator)"
if os.environ.get("SISI_IP"):
    IP_MASK[os.environ["SISI_IP"]] = "Agent B (Database)"
if os.environ.get("NORI_IP"):
    IP_MASK[os.environ["NORI_IP"]] = "Agent C (Automation)"

def mask_ip(text: str) -> str:
    for ip, label in IP_MASK.items():
        text = text.replace(ip, label)
    return text

def mask_token(text: str) -> str:
    if A2A_TOKEN:
        text = text.replace(A2A_TOKEN, "[REDACTED]")
    return text

def scrub(text: str) -> str:
    """Remove all sensitive info from response text."""
    text = mask_ip(text)
    text = mask_token(text)
    # Mask any remaining hex tokens (64 char hex strings)
    text = re.sub(r'\b[a-f0-9]{64}\b', '[REDACTED]', text)
    return text

# ── Message whitelist ──
MESSAGE_WHITELIST: dict[str, str] = {
    "ping": "ping",
    "roster": "List all agents in the cluster with their versions and skills. Reply concisely.",
    "portfolio": "Read /root/taskmind-portfolio/data/projects.json and list the 3 most recent items by date with their titles only. Reply concisely.",
    "audit": "Show the last 8 entries from /root/pi-a2a-server/audit.log. Mask the IPs. Reply concisely.",
    # "edit" is built dynamically below
}

# ── Rate limiter (simple in-memory) ──
_rate_store: dict[str, float] = {}
RATE_LIMIT_S = 10

def check_rate(ip: str) -> bool:
    now = time.time()
    last = _rate_store.get(ip, 0)
    if now - last < RATE_LIMIT_S:
        return False
    _rate_store[ip] = now
    return True

# ── A2A Helpers ──

async def a2a_send_and_poll(url: str, message_text: str) -> dict:
    """Send a message via A2A and poll with relaxed backoff for the result."""
    headers = {"Content-Type": "application/json"}
    if A2A_TOKEN:
        headers["Authorization"] = f"Bearer {A2A_TOKEN}"

    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        # Send
        send_body = {
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": message_text}],
                "kind": "message",
                "messageId": f"demo-{uuid.uuid4().hex[:8]}",
            }},
        }
        t0 = time.time()
        resp = await client.post(f"{url}/", json=send_body)
        if resp.status_code != 200:
            return {"error": f"Agent returned HTTP {resp.status_code}: {resp.text[:200]}"}

        data = resp.json()
        task_id = data.get("result", {}).get("id")
        if not task_id:
            return {"error": "No task ID in response"}

        # Poll with relaxed backoff
        delays = [4, 4, 8, 8, 16, 16]
        steps = []
        for i, delay in enumerate(delays):
            await asyncio.sleep(delay)
            poll_resp = await client.post(f"{url}/", json={
                "jsonrpc": "2.0", "id": 2, "method": "tasks/get",
                "params": {"id": task_id},
            })
            if poll_resp.status_code != 200:
                steps.append({"poll": i+1, "state": f"HTTP {poll_resp.status_code}", "delay": delay})
                continue

            poll_data = poll_resp.json()
            state = poll_data.get("result", {}).get("status", {}).get("state", "unknown")
            steps.append({"poll": i+1, "state": state, "delay": delay})

            if state == "completed":
                # Extract response text
                response_text = ""
                for artifact in poll_data.get("result", {}).get("artifacts", []):
                    for part in artifact.get("parts", []):
                        if part.get("kind") == "text":
                            response_text += part.get("text", "")
                if not response_text:
                    history = poll_data.get("result", {}).get("history", [])
                    for msg in history:
                        if msg.get("role") == "agent":
                            for part in msg.get("parts", []):
                                if part.get("kind") == "text":
                                    response_text += part.get("text", "")
                return {
                    "response": scrub(response_text) if response_text else "(empty response)",
                    "duration_ms": (time.time() - t0) * 1000,
                    "steps": steps,
                    "task_id": task_id,
                }
            elif state in ("failed", "canceled"):
                return {"error": f"Task {state}", "steps": steps}

        return {"error": "Timeout after 56s", "steps": steps}


async def get_cluster_agents() -> list[dict]:
    """Fetch agent info from Pisti's /cluster, return masked list."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{PISTI_URL}/cluster")
            if resp.status_code != 200:
                return []
            data = resp.json()
            agents = []
            # Add Pisti
            agents.append({
                "id": "pisti",
                "name": "Pisti",
                "version": data.get("version", "3.0.0"),
                "skills": 5,
                "status": "online",
                "masked_ip": "Agent A (Coordinator)",
            })
            # Add peers
            for peer in data.get("peers", []):
                name = peer.get("name", "Unknown")
                peer_ip = peer.get("ip", "")
                agents.append({
                    "id": name.lower(),
                    "name": name,
                    "version": "1.0.0",
                    "skills": len(peer.get("skills", [])),
                    "status": "online" if peer.get("status") == "online" else "offline",
                    "masked_ip": IP_MASK.get(peer_ip, f"Agent ({peer_ip[:8]}...)"),
                })
            return agents
    except Exception:
        return []


# ── App ──
app = Starlette()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def api_agents(request: Request):
    agents = await get_cluster_agents()
    return JSONResponse({"agents": agents})


async def api_send(request: Request):
    client_ip = request.client.host if request.client else "unknown"

    if not check_rate(client_ip):
        return JSONResponse({"error": "Rate limit — please wait 10s between requests"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    msg_id = body.get("id", "")

    # Handle dynamic "edit" message
    if msg_id == "edit":
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        message_text = (
            f"Edit the pi-a2a-mesh entry in /root/taskmind-portfolio/data/projects.json: "
            f"if the fullDescription does NOT already contain the exact phrase 'Live demo run at:', "
            f"append exactly this text to the end of fullDescription: ' Live demo run at: {now}'. "
            f"If it already contains 'Live demo run at:', update ONLY the ISO timestamp after it "
            f"to {now}. Then rebuild and redeploy the Docker container with: "
            f"cd /root/taskmind-portfolio && docker compose up -d --build. Reply concisely with what you did."
        )
    elif msg_id in MESSAGE_WHITELIST:
        message_text = MESSAGE_WHITELIST[msg_id]
    else:
        return JSONResponse({"error": f"Unknown message ID: {msg_id}. Allowed: {list(MESSAGE_WHITELIST.keys())}"}, status_code=400)

    result = await a2a_send_and_poll(PISTI_URL, message_text)
    return JSONResponse(result)


async def api_audit(request: Request):
    """Return last 20 masked audit log lines."""
    try:
        audit_path = "/root/pi-a2a-server/audit.log"
        proc = await asyncio.create_subprocess_exec(
            "tail", "-20", audit_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        lines = []
        for line in stdout.decode().strip().split("\n"):
            if line.strip():
                try:
                    entry = json.loads(line)
                    entry_str = json.dumps(entry)
                    entry_str = mask_ip(entry_str)
                    entry_str = mask_token(entry_str)
                    lines.append(json.loads(entry_str))
                except json.JSONDecodeError:
                    lines.append({"raw": scrub(line)})
        return JSONResponse({"entries": lines})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# Routes
app.add_route("/api/agents", api_agents, methods=["GET"])
app.add_route("/api/send", api_send, methods=["POST"])
app.add_route("/api/audit", api_audit, methods=["GET"])


# Serve React build in production
import os as _os
_frontend_dist = _os.path.join(_os.path.dirname(__file__), "frontend", "dist")
if _os.path.exists(_frontend_dist):
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="static")


if __name__ == "__main__":
    print(f"[demo] Starting live demo proxy on port {DEMO_PORT}")
    print(f"[demo] Pisti: {PISTI_URL}")
    print(f"[demo] Sisi: {SISI_URL or 'not configured'}")
    print(f"[demo] Nori: {NORI_URL or 'not configured'}")
    print(f"[demo] Message whitelist: {list(MESSAGE_WHITELIST.keys())}")
    uvicorn.run(app, host="0.0.0.0", port=DEMO_PORT, log_level="info")