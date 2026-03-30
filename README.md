# capes-relay

**Version:** 1.0.0  
**Repo:** mm333rr/capes-relay  
**Machine:** Mac Pro (`mMacPro`, 192.168.1.30)  
**URL:** https://relay.am180.us (LAN-only via Caddy)

Thin HTTP bridge between a mobile browser (iPhone Safari) and the `claude` CLI on the Mac Pro. Lets Paco open, resume, and chat with Claude Code sessions from his phone — using the **same session storage** as Claude Desktop, so conversations are shared between both interfaces.

---

## Architecture

```
iPhone Safari
    │ HTTPS
    ▼
relay.am180.us  (Caddy TLS termination on mbuntu, lan_only)
    │ HTTP → 192.168.1.30:7823
    ▼
capes-relay (FastAPI, port 7823, launchd daemon on Mac Pro)
    │ subprocess
    ▼
claude CLI  (~/.npm-global/bin/claude, v2.0.76+)
    │ shared session storage at ~/.claude/projects/
    ▼
Claude Desktop  (same sessions — Desktop and relay are fully in sync)
```

**Sessions created on the relay appear in Claude Desktop and vice versa.**  
Claude Desktop does NOT need to be open for the relay to work.

---

## Quick Start

### 1. Install deps
```bash
cd ~/Claude\ Scripts\ and\ Venvs/capes-relay
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Test locally
```bash
source venv/bin/activate
python3 relay.py --port 7823
# open http://localhost:7823 in browser
```

### 3. Install launchd daemon
```bash
cp com.capes.relay.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.capes.relay.plist
```

### 4. Add Caddy route (on mbuntu)
```bash
# Append caddy-block.txt content to /srv/docker/caddy/Caddyfile
ssh mbuntu
cat >> /srv/docker/caddy/Caddyfile << 'EOF'

relay.am180.us {
  import tls_wildcard
  import lan_only
  import log_access
  reverse_proxy 192.168.1.30:7823
}
EOF
cd /srv/docker/caddy-stack && docker compose restart caddy
```

---

## CLI Flags

| Flag | Default | Description |
|---|---|---|
| `--port` | `7823` | Port to bind on Mac Pro |
| `--host` | `0.0.0.0` | Bind address |
| `--token` | env `RELAY_TOKEN` | Bearer token for auth (optional — Caddy `lan_only` handles access control) |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Mobile chat UI (HTML) |
| `GET` | `/api/health` | Health check + claude binary status |
| `GET` | `/api/sessions` | List recent claude CLI sessions (last 50) |
| `POST` | `/api/chat` | Send message, stream SSE response |

### POST /api/chat body
```json
{
  "message": "Add Severance season 3 to Sonarr",
  "session_id": "abc123..."   // optional — omit to start new session
}
```

### SSE stream format
```
data: {"type": "text", "text": "...", "session_id": "abc123"}
data: {"type": "result", "session_id": "abc123", "cost_usd": 0.002}
data: [DONE]
```

---

## Launchd Management

```bash
# Start
launchctl load ~/Library/LaunchAgents/com.capes.relay.plist

# Stop
launchctl unload ~/Library/LaunchAgents/com.capes.relay.plist

# Restart
launchctl unload ~/Library/LaunchAgents/com.capes.relay.plist
launchctl load ~/Library/LaunchAgents/com.capes.relay.plist

# Logs
tail -f ~/Library/Logs/capes-relay/relay.log
tail -f ~/Library/Logs/capes-relay/stdout.log
tail -f ~/Library/Logs/capes-relay/stderr.log

# Check status
launchctl list | grep capes.relay
```

---

## Auth

The relay uses Caddy's `lan_only` snippet for access control — only LAN, Tailscale, and Docker subnet IPs can reach `relay.am180.us`. No bearer token is required by default.

To add a bearer token (optional extra layer):
1. Set `RELAY_TOKEN` in the plist `EnvironmentVariables`
2. On first visit, the UI prompts for the token and stores it in `localStorage`

---

## Known Quirks

- **Session listing requires claude CLI session format v2** — sessions live at `~/.claude/projects/<hash>/<session>.jsonl`. If the directory is empty (no claude CLI sessions yet), the session list returns empty.
- **Claude Desktop does not need to be open** — the CLI has its own auth via macOS Keychain (`security find-generic-password -s "Claude Code"`).
- **Dispatch is NOT involved** — this is a direct CLI subprocess call, completely independent of Dispatch or Cowork.
- **Intel Mac + macOS 12** — fully compatible. The CLI works fine; only Cowork/Dispatch require Apple Silicon + Sonoma.

---

## Stack Integration

- **Caddy:** `relay.am180.us` → `192.168.1.30:7823`, LAN-only, wildcard TLS
- **launchd label:** `com.capes.relay`
- **Log dir:** `~/Library/Logs/capes-relay/`
- **Project dir:** `~/Claude Scripts and Venvs/capes-relay/`
- **venv:** `~/Claude Scripts and Venvs/capes-relay/venv/`
