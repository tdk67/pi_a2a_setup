#!/bin/bash
# Pi A2A Agent Mesh — Setup Script
# Run this on a fresh VPS to set up an A2A agent node
# Usage: curl -fsSL https://raw.github.../setup.sh | bash
#    or: chmod +x setup.sh && ./setup.sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════╗"
echo "║   Pi A2A Agent Mesh — Setup             ║"
echo "║   v1.0.0                                ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Prerequisites check ──────────────────────────────────────────
echo -e "\n${YELLOW}[1/7] Checking prerequisites...${NC}"

if ! command -v python3 &>/dev/null; then
    echo -e "${RED}✗ python3 not found. Install with: apt install python3${NC}"
    exit 1
fi
echo "  ✓ python3 $(python3 --version)"

if ! command -v pip &>/dev/null && ! command -v pip3 &>/dev/null; then
    echo -e "${RED}✗ pip not found. Install with: apt install python3-pip${NC}"
    exit 1
fi
echo "  ✓ pip available"

if ! command -v pi &>/dev/null; then
    echo -e "${RED}✗ pi not found. Install pi coding agent first.${NC}"
    exit 1
fi
echo "  ✓ pi $(pi --version 2>/dev/null || echo 'installed')"

if ! command -v node &>/dev/null; then
    echo -e "${RED}✗ node not found. Install Node.js 20+.${NC}"
    exit 1
fi
echo "  ✓ node $(node --version)"

# ── Install Python dependencies ──────────────────────────────────
echo -e "\n${YELLOW}[2/7] Installing Python dependencies...${NC}"
pip3 install fasta2a uvicorn httpx 2>&1 | tail -1
echo "  ✓ fasta2a, uvicorn, httpx installed"

# ── Install pi extensions ────────────────────────────────────────
echo -e "\n${YELLOW}[3/7] Installing pi extensions...${NC}"

# A2A Adaptor
if ! pi list 2>/dev/null | grep -q "pi-a2a-adaptor"; then
    pi install npm:pi-a2a-adaptor 2>&1 || echo "  ⚠ A2A adaptor may already be installed"
fi
echo "  ✓ pi-a2a-adaptor"

# Agent Browser Native (web access)
if ! pi list 2>/dev/null | grep -q "pi-agent-browser-native"; then
    pi install npm:pi-agent-browser-native 2>&1 || echo "  ⚠ Browser extension may already be installed"
fi
echo "  ✓ pi-agent-browser-native"

# Schedule Prompt
if ! pi list 2>/dev/null | grep -q "pi-schedule-prompt"; then
    pi install npm:pi-schedule-prompt 2>&1 || echo "  ⚠ Schedule extension may already be installed"
fi
echo "  ✓ pi-schedule-prompt"

# ── Long-term memory extension ───────────────────────────────────
echo -e "\n${YELLOW}[4/7] Setting up long-term memory...${NC}"
mkdir -p ~/.pi/agent/extensions/
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/extensions/mem0-memory.ts" ]; then
    cp "$SCRIPT_DIR/extensions/mem0-memory.ts" ~/.pi/agent/extensions/
    echo "  ✓ mem0-memory.ts installed"
else
    echo "  ⚠ mem0-memory.ts not found in repo — skipping"
    echo "    (get it from https://github.com/tdk67/pi_a2a_setup/extensions/mem0-memory.ts)"
fi

# Register mem0 in pi settings if not already there
if [ -f ~/.pi/agent/settings.json ]; then
    if ! grep -q "file:mem0-memory" ~/.pi/agent/settings.json; then
        python3 -c "
import json
with open('$HOME/.pi/agent/settings.json') as f:
    s = json.load(f)
if 'file:mem0-memory' not in s.get('packages', []):
    s.setdefault('packages', []).append('file:mem0-memory')
    with open('$HOME/.pi/agent/settings.json', 'w') as f:
        json.dump(s, f, indent=2)
    print('  ✓ mem0-memory registered in settings.json')
else:
    print('  ✓ mem0-memory already in settings.json')
"
    fi
fi

# ── Deploy A2A server ────────────────────────────────────────────
echo -e "\n${YELLOW}[5/7] Deploying A2A server...${NC}"

SERVER_DIR="/etc/pi-a2a-server"
mkdir -p "$SERVER_DIR"

# Copy server
if [ -f "$SCRIPT_DIR/server.py" ]; then
    cp "$SCRIPT_DIR/server.py" "$SERVER_DIR/server.py"
    chmod +x "$SERVER_DIR/server.py"
    echo "  ✓ server.py → $SERVER_DIR/"
else
    echo -e "${RED}  ✗ server.py not found in repo${NC}"
    exit 1
fi

# Copy env template if .env doesn't exist
if [ ! -f "$SERVER_DIR/.env" ]; then
    if [ -f "$SCRIPT_DIR/server.env.example" ]; then
        cp "$SCRIPT_DIR/server.env.example" "$SERVER_DIR/.env"
        echo "  ✓ .env created from template (EDIT THIS FILE!)"
    fi
else
    echo "  ✓ .env already exists (not overwritten)"
fi

# Copy peers template if peers.json doesn't exist
if [ ! -f "$SERVER_DIR/peers.json" ]; then
    if [ -f "$SCRIPT_DIR/peers.example.json" ]; then
        cp "$SCRIPT_DIR/peers.example.json" "$SERVER_DIR/peers.json"
        echo "  ✓ peers.json created from template (EDIT THIS FILE!)"
    fi
else
    echo "  ✓ peers.json already exists (not overwritten)"
fi

# Copy a2a-send.sh
if [ -f "$SCRIPT_DIR/a2a-send.sh" ]; then
    cp "$SCRIPT_DIR/a2a-send.sh" /usr/local/bin/a2a-send
    chmod +x /usr/local/bin/a2a-send
    echo "  ✓ a2a-send → /usr/local/bin/a2a-send"
fi

# ── Install systemd service ──────────────────────────────────────
echo -e "\n${YELLOW}[6/7] Installing systemd service...${NC}"

if [ -f "$SCRIPT_DIR/pi-a2a-server.service" ]; then
    cp "$SCRIPT_DIR/pi-a2a-server.service" /etc/systemd/system/
    systemctl daemon-reload
    echo "  ✓ systemd service installed"
else
    # Create from embedded template
    cat > /etc/systemd/system/pi-a2a-server.service << 'SYSTEMDEOF'
[Unit]
Description=Pi A2A Agent Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/etc/pi-a2a-server
EnvironmentFile=/etc/pi-a2a-server/.env
ExecStart=/usr/bin/python3 /etc/pi-a2a-server/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SYSTEMDEOF
    systemctl daemon-reload
    echo "  ✓ systemd service created"
fi

# ── Final instructions ───────────────────────────────────────────
echo -e "\n${YELLOW}[7/7] Setup complete!${NC}"
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   NEXT STEPS                            ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "1. ${CYAN}EDIT YOUR IDENTITY:${NC}"
echo -e "   nano /etc/pi-a2a-server/.env"
echo ""
echo -e "   Required: A2A_NAME, A2A_TOKEN, A2A_URL"
echo -e "   Generate token: ${YELLOW}openssl rand -hex 32${NC}"
echo ""
echo -e "2. ${CYAN}CONFIGURE PEERS:${NC}"
echo -e "   nano /etc/pi-a2a-server/peers.json"
echo ""
echo -e "3. ${CYAN}START THE SERVICE:${NC}"
echo -e "   systemctl enable --now pi-a2a-server"
echo -e "   systemctl status pi-a2a-server"
echo ""
echo -e "4. ${CYAN}VERIFY:${NC}"
echo -e "   curl http://localhost:8002/ping"
echo -e "   curl http://localhost:8002/.well-known/agent-card.json"
echo ""
echo -e "5. ${CYAN}SEND A TEST MESSAGE:${NC}"
echo -e "   a2a-send http://PEER_IP:PORT TOKEN \"ping\""
echo ""
echo -e "${YELLOW}⚠  IMPORTANT: Add each agent's IP to TRUSTED_IPS in server.py!${NC}"
echo -e "${YELLOW}   Edit /etc/pi-a2a-server/server.py and add peer IPs to the set.${NC}"
echo ""