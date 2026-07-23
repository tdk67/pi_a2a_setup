"""
Live Demo Proxy — sits between the React UI and the A2A agent mesh.
Strips all secrets, enforces message whitelist, masks IPs.
Supports real-time polling: frontend polls /api/poll every 2s.
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
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

# ── Config ──
A2A_TOKEN = os.environ.get("A2A_TOKEN", "")
PISTI_URL = os.environ.get("PISTI_URL", "http://127.0.0.1:8002")
SISI_URL = os.environ.get("SISI_URL", "")
NORI_URL = os.environ.get("NORI_URL", "")
DEMO_PORT = int(os.environ.get("DEMO_PORT", "3002"))

IP_MASK: dict[str, str] = {}
if os.environ.get("PISTI_IP"): IP_MASK[os.environ["PISTI_IP"]] = "Agent A (Coordinator)"
if os.environ.get("SISI_IP"): IP_MASK[os.environ["SISI_IP"]] = "Agent B (Database)"
if os.environ.get("NORI_IP"): IP_MASK[os.environ["NORI_IP"]] = "Agent C (Automation)"

def mask_ip(text: str) -> str:
    for ip, label in IP_MASK.items():
        text = text.replace(ip, label)
    return text

def mask_token(text: str) -> str:
    if A2A_TOKEN: text = text.replace(A2A_TOKEN, "[REDACTED]")
    return text

def scrub(text: str) -> str:
    text = mask_ip(text)
    text = mask_token(text)
    text = re.sub(r'\b[a-f0-9]{64}\b', '[REDACTED]', text)
    return text

# ── Message whitelist ──
MESSAGE_WHITELIST: dict[str, str] = {
    "ping": "ping",
    "roster": "List all agents in the cluster with their versions and skills. Reply concisely.",
    "portfolio": "Read /root/taskmind-portfolio/data/projects.json and list the 3 most recent items by date with their titles only. Reply concisely.",
    "audit": "Show the last 8 entries from /root/pi-a2a-server/audit.log. Mask the IPs. Reply concisely.",
    "offtopic": "What's the color of the sky?",
}

# ── Rate limiter ──
_rate_store: dict[str, float] = {}
RATE_LIMIT_S = 10

def check_rate(ip: str) -> bool:
    now = time.time()
    if now - _rate_store.get(ip, 0) < RATE_LIMIT_S:
        return False
    _rate_store[ip] = now
    return True

# ── In-flight tasks store ──
_pending_tasks: dict[str, dict] = {}  # task_uuid → {url, poll_index, t_start, headers, a2a_task_id}


def get_message_text(msg_id: str) -> str | None:
    if msg_id == "edit":
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return (
            f"Edit the pi-a2a-mesh entry in /root/taskmind-portfolio/data/projects.json: "
            f"if the fullDescription does NOT already contain the exact phrase 'Live demo run at:', "
            f"append exactly this text to the end of fullDescription: ' Live demo run at: {now}'. "
            f"If it already contains 'Live demo run at:', update ONLY the ISO timestamp after it "
            f"to {now}. Then rebuild and redeploy the Docker container with: "
            f"cd /root/taskmind-portfolio && docker compose up -d --build. Reply concisely with what you did."
        )
    return MESSAGE_WHITELIST.get(msg_id)


async def get_cluster_agents() -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{PISTI_URL}/cluster")
            if resp.status_code != 200: return []
            data = resp.json()
            agents = [{
                "id": "pisti", "name": "Pisti", "version": data.get("version", "3.0.0"),
                "skills": 5, "status": "online", "masked_ip": "Agent A (Coordinator)",
            }]
            for peer in data.get("peers", []):
                peer_ip = peer.get("ip", "")
                agents.append({
                    "id": peer.get("name", "").lower(),
                    "name": peer.get("name", "Unknown"),
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

TARGET_URLS = {"pisti": PISTI_URL, "sisi": SISI_URL, "nori": NORI_URL}
TARGET_LABELS = {"pisti": "Pisti (Coordinator)", "sisi": "Sisi (Database)", "nori": "Nori (Automation)"}
DELAYS = [4, 4, 8, 8, 16, 16]


async def api_agents(request: Request):
    return JSONResponse({"agents": await get_cluster_agents()})


async def api_send(request: Request):
    """Send a message — returns task_uuid immediately. Frontend then polls /api/poll."""
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate(client_ip):
        return JSONResponse({"error": "Rate limit — 10s between requests"}, status_code=429)

    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    msg_id = body.get("id", "")
    target = body.get("target", "pisti")
    agent_url = TARGET_URLS.get(target, PISTI_URL)
    agent_label = TARGET_LABELS.get(target, target)

    if not agent_url:
        return JSONResponse({"error": f"Agent '{target}' not configured"}, status_code=400)

    message_text = get_message_text(msg_id)
    if message_text is None:
        return JSONResponse({"error": f"Unknown message: {msg_id}"}, status_code=400)

    # Send to A2A agent
    headers = {"Content-Type": "application/json"}
    if A2A_TOKEN: headers["Authorization"] = f"Bearer {A2A_TOKEN}"

    send_body = {
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {
            "role": "user", "parts": [{"kind": "text", "text": message_text}],
            "kind": "message", "messageId": f"demo-{uuid.uuid4().hex[:8]}",
        }},
    }

    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.post(f"{agent_url}/", json=send_body)
            if resp.status_code != 200:
                return JSONResponse({"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}, status_code=500)
            data = resp.json()
            a2a_task_id = data.get("result", {}).get("id")
            if not a2a_task_id:
                return JSONResponse({"error": "No task ID in response"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    task_uuid = uuid.uuid4().hex
    _pending_tasks[task_uuid] = {
        "url": agent_url, "a2a_task_id": a2a_task_id,
        "poll_index": 0, "t_start": time.time(),
        "headers": headers, "agent_label": agent_label, "target": target,
    }

    return JSONResponse({
        "task_uuid": task_uuid,
        "agent_label": agent_label,
        "message_id": msg_id,
    })


async def api_poll(request: Request):
    """Poll the A2A agent once. Returns current state + text if completed."""
    task_uuid = request.query_params.get("task_uuid", "")
    task = _pending_tasks.get(task_uuid)
    if not task:
        return JSONResponse({"state": "unknown", "error": "Task not found"}, status_code=404)

    poll_index = task["poll_index"]
    if poll_index >= len(DELAYS):
        # Last poll didn't complete — timeout
        return JSONResponse({"state": "timeout", "error": "Timeout after 56s", "steps": poll_index})

    delay = DELAYS[poll_index]
    elapsed_since_start = time.time() - task["t_start"]
    wait = max(0, delay - elapsed_since_start)
    if wait > 0:
        await asyncio.sleep(wait)

    task["poll_index"] += 1

    try:
        async with httpx.AsyncClient(timeout=30, headers=task["headers"]) as client:
            poll_resp = await client.post(f"{task['url']}/", json={
                "jsonrpc": "2.0", "id": 2, "method": "tasks/get",
                "params": {"id": task["a2a_task_id"]},
            })
            if poll_resp.status_code != 200:
                return JSONResponse({"state": "error", "error": f"HTTP {poll_resp.status_code}", "poll": poll_index, "delay": delay})

            poll_data = poll_resp.json()
            state = poll_data.get("result", {}).get("status", {}).get("state", "unknown")

            if state == "completed":
                response_text = ""
                for a in poll_data.get("result", {}).get("artifacts", []):
                    for p in a.get("parts", []):
                        if p.get("kind") == "text": response_text += p.get("text", "")
                if not response_text:
                    for m in poll_data.get("result", {}).get("history", []):
                        if m.get("role") == "agent":
                            for p in m.get("parts", []):
                                if p.get("kind") == "text": response_text += p.get("text", "")
                duration = (time.time() - task["t_start"]) * 1000
                return JSONResponse({
                    "state": "completed",
                    "response": scrub(response_text) if response_text else "(empty)",
                    "duration_ms": duration,
                    "poll": poll_index,
                    "delay": delay,
                    "total_polls": poll_index,
                })
            elif state in ("failed", "canceled"):
                return JSONResponse({"state": state, "error": f"Task {state}", "poll": poll_index, "delay": delay})
            else:
                return JSONResponse({"state": state, "poll": poll_index, "delay": delay})
    except Exception as e:
        return JSONResponse({"state": "error", "error": str(e), "poll": poll_index, "delay": delay})


async def api_audit(request: Request):
    try:
        proc = await asyncio.create_subprocess_exec(
            "tail", "-20", "/root/pi-a2a-server/audit.log",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        lines = []
        for line in stdout.decode().strip().split("\n"):
            if line.strip():
                try:
                    entry = json.loads(line)
                    lines.append(json.loads(mask_ip(mask_token(json.dumps(entry)))))
                except json.JSONDecodeError:
                    lines.append({"raw": scrub(line)})
        return JSONResponse({"entries": lines})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


app.add_route("/api/agents", api_agents, methods=["GET"])
app.add_route("/api/send", api_send, methods=["POST"])
app.add_route("/api/poll", api_poll, methods=["GET"])
app.add_route("/api/audit", api_audit, methods=["GET"])

# Serve React build
import os as _os
_frontend_dist = _os.path.join(_os.path.dirname(__file__), "frontend", "dist")
if _os.path.exists(_frontend_dist):
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="static")


if __name__ == "__main__":
    print(f"[demo] Live demo proxy on port {DEMO_PORT}")
    print(f"[demo] Targets: pisti={PISTI_URL}, sisi={SISI_URL}, nori={NORI_URL}")
    uvicorn.run(app, host="0.0.0.0", port=DEMO_PORT, log_level="info")