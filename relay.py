#!/usr/bin/env python3
"""
capes-relay — v1.1.0
Thin HTTP bridge between a mobile browser and the claude CLI.
Lets Paco open, resume, and chat with Claude Code sessions from iPhone.

Architecture:
  iPhone Safari → relay.am180.us (Caddy TLS) → this server (port 7823, Mac Pro)
  → claude CLI subprocess (--resume / new session)

Security model (v1.1.0):
  - Bearer token auth (RELAY_TOKEN env var / --token flag)
  - In-process rate limiting: 30 req/min per IP (blunts token brute-force)
  - Auth bypass for LAN/Tailscale IPs (192.168.x, 100.64.x) — no token needed on WiFi

Sessions stored by claude CLI at ~/.claude/projects/ — shared with Claude Desktop.

Usage:
  python3 relay.py [--port 7823] [--token <bearer-token>]

Author: Paco / The Capes homelab  v1.1.0
Repo:   mm333rr/capes-relay
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import ipaddress
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOG_DIR = Path.home() / "Library" / "Logs" / "capes-relay"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "relay.log"),
    ],
)
log = logging.getLogger("capes-relay")

CLAUDE_BIN   = shutil.which("claude") or "/Users/mProAdmin/.npm-global/bin/claude"
SESSIONS_DIR = Path.home() / ".claude" / "projects"

# Networks that bypass token auth (LAN + Tailscale CGNAT + Docker bridge + localhost)
_TRUSTED_NETS = [
    ipaddress.ip_network("192.168.0.0/16"),   # home LAN
    ipaddress.ip_network("100.64.0.0/10"),    # Tailscale CGNAT
    ipaddress.ip_network("172.16.0.0/12"),    # Docker bridge
    ipaddress.ip_network("10.0.0.0/8"),       # VPN / internal
    ipaddress.ip_network("127.0.0.0/8"),      # localhost
]

# Simple sliding-window rate limiter: 30 requests per 60s per IP
_RATE_WINDOW   = 60   # seconds
_RATE_LIMIT    = 30   # requests per window
_rate_log: dict[str, collections.deque] = {}

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="capes-relay", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

RELAY_TOKEN: str = ""  # set at startup from env / --token


def _client_ip(request: Request) -> str:
    """Extract real client IP, respecting X-Forwarded-For from Caddy."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_trusted(ip: str) -> bool:
    """Return True if IP is on LAN, Tailscale, Docker, or localhost."""
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _TRUSTED_NETS)
    except ValueError:
        return False


def _check_rate(ip: str) -> None:
    """Raise 429 if IP exceeds rate limit. Sliding window."""
    now = time.time()
    if ip not in _rate_log:
        _rate_log[ip] = collections.deque()
    dq = _rate_log[ip]
    # Evict entries outside window
    while dq and dq[0] < now - _RATE_WINDOW:
        dq.popleft()
    if len(dq) >= _RATE_LIMIT:
        log.warning("Rate limit hit from %s (%d reqs in %ds)", ip, len(dq), _RATE_WINDOW)
        raise HTTPException(status_code=429, detail="Rate limit exceeded — slow down")
    dq.append(now)


def _check_auth(request: Request) -> str:
    """
    Auth check with layered security:
    1. Always rate-limit by IP.
    2. Trusted IPs (LAN/Tailscale) bypass token check.
    3. External IPs require valid Bearer token.
    Returns client IP string.
    """
    ip = _client_ip(request)
    _check_rate(ip)

    if _is_trusted(ip):
        return ip  # LAN / Tailscale — no token needed

    if not RELAY_TOKEN:
        return ip  # no token configured — open (LAN-only scenario)

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != RELAY_TOKEN:
        log.warning("Auth failure from external IP %s", ip)
        raise HTTPException(status_code=401, detail="Unauthorized")

    return ip

# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def _list_sessions() -> list[dict]:
    """Enumerate claude CLI sessions from ~/.claude/projects/<hash>/<session>.jsonl"""
    sessions: list[dict] = []
    if not SESSIONS_DIR.exists():
        return sessions
    for proj_dir in sorted(SESSIONS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        for jsonl_path in sorted(proj_dir.glob("*.jsonl"), key=lambda p: -p.stat().st_mtime):
            try:
                s = _parse_session_meta(jsonl_path, proj_dir.name)
                if s:
                    sessions.append(s)
            except Exception as exc:
                log.debug("Skipping %s: %s", jsonl_path, exc)
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions[:50]


def _parse_session_meta(path: Path, proj_hash: str) -> Optional[dict]:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        return None
    first = json.loads(lines[0])
    session_id = first.get("sessionId") or path.stem
    title = "(empty)"
    for line in lines:
        obj = json.loads(line)
        if obj.get("type") == "user":
            content = obj.get("message", {}).get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        title = block["text"][:80].replace("\n", " ")
                        break
            elif isinstance(content, str):
                title = content[:80].replace("\n", " ")
            if title != "(empty)":
                break
    return {
        "id":            session_id,
        "project":       proj_hash,
        "title":         title,
        "updated_at":    int(path.stat().st_mtime),
        "message_count": len(lines),
    }


# ---------------------------------------------------------------------------
# Claude CLI subprocess streaming
# ---------------------------------------------------------------------------

async def _run_claude(message: str, session_id: Optional[str] = None) -> AsyncGenerator[str, None]:
    """Spawn claude CLI, stream SSE. Yields data: <json>\n\n lines."""
    cmd = [CLAUDE_BIN, "--print", "--verbose", "--output-format", "stream-json"]
    if session_id:
        cmd += ["--resume", session_id]
    log.info("Spawning claude (session=%s)", session_id or "new")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"},
    )
    proc.stdin.write(message.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    buffer = b""
    new_session_id: Optional[str] = None

    async for chunk in proc.stdout:
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not new_session_id and obj.get("session_id"):
                new_session_id = obj["session_id"]

            if obj.get("type") == "assistant":
                for block in (obj.get("message", {}).get("content", []) or []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        yield f"data: {json.dumps({'type':'text','text':block['text'],'session_id':new_session_id})}\n\n"

            elif obj.get("type") == "result":
                sid = obj.get("session_id") or new_session_id
                yield f"data: {json.dumps({'type':'result','session_id':sid,'cost_usd':obj.get('cost_usd')})}\n\n"

    await proc.wait()
    stderr = await proc.stderr.read()
    if proc.returncode != 0:
        log.error("claude exited %d: %s", proc.returncode, stderr.decode()[:500])
        yield f"data: {json.dumps({'type':'error','text':stderr.decode()[:300]})}\n\n"
    yield "data: [DONE]\n\n"

# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


@app.get("/api/sessions")
async def get_sessions(request: Request):
    _check_auth(request)
    return {"sessions": _list_sessions()}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    ip = _check_auth(request)
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    log.info("Chat from %s (session=%s): %s…", ip, req.session_id or "new", req.message[:60])
    return StreamingResponse(
        _run_claude(req.message, req.session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
async def health(request: Request):
    ip = _client_ip(request)
    return {
        "status":              "ok",
        "version":             "1.1.0",
        "claude_bin":          CLAUDE_BIN,
        "claude_bin_exists":   Path(CLAUDE_BIN).exists(),
        "sessions_dir":        str(SESSIONS_DIR),
        "sessions_dir_exists": SESSIONS_DIR.exists(),
        "session_count":       len(_list_sessions()),
        "client_ip":           ip,
        "client_trusted":      _is_trusted(ip),
        "ts":                  int(time.time()),
    }


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>capes-relay</h1><p>static/index.html not found</p>")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global RELAY_TOKEN

    parser = argparse.ArgumentParser(description="capes-relay — Claude CLI web bridge")
    parser.add_argument("--port",  type=int, default=7823)
    parser.add_argument("--host",  default="0.0.0.0")
    parser.add_argument("--token", default=os.environ.get("RELAY_TOKEN", ""),
                        help="Bearer token for external auth (LAN/Tailscale bypass this)")
    args = parser.parse_args()

    RELAY_TOKEN = args.token

    log.info("capes-relay v1.1.0 starting on %s:%d", args.host, args.port)
    log.info("Claude binary:  %s (exists=%s)", CLAUDE_BIN, Path(CLAUDE_BIN).exists())
    log.info("Sessions dir:   %s", SESSIONS_DIR)
    log.info("Auth:           token=%s, trusted nets=%d",
             "set" if RELAY_TOKEN else "NOT SET", len(_TRUSTED_NETS))
    log.info("Rate limit:     %d req/%ds per IP", _RATE_LIMIT, _RATE_WINDOW)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
