#!/usr/bin/env python3
"""
capes-relay — v1.0.0
Thin HTTP bridge between a mobile browser and the claude CLI.
Lets Paco open, resume, and chat with Claude Code sessions from iPhone.

Architecture:
  iPhone Safari → relay.am180.us (Caddy TLS) → this server (port 7823, Mac Pro)
  → claude CLI subprocess (--continue / --resume / new session)

Sessions are stored by the claude CLI under ~/.claude/ and are
shared with Claude Desktop — open a chat here, see it in Desktop too.

Usage:
  python3 relay.py [--port 7823] [--token <bearer-token>]

Author: Paco / The Capes homelab
Repo:   mm333rr/capes-relay
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
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

CLAUDE_BIN = shutil.which("claude") or "/Users/mProAdmin/.npm-global/bin/claude"
SESSIONS_DIR = Path.home() / ".claude" / "projects"

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="capes-relay", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Bearer token — loaded from env or CLI arg (set at startup)
RELAY_TOKEN: str = ""


def _check_auth(request: Request) -> None:
    """Raise 401 if bearer token doesn't match."""
    if not RELAY_TOKEN:
        return  # no auth configured — LAN-only mode
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != RELAY_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def _list_sessions() -> list[dict]:
    """
    Enumerate claude CLI sessions from ~/.claude/projects/<hash>/<session>.jsonl
    Returns list of dicts: {id, project, title, updated_at, message_count}
    """
    sessions: list[dict] = []
    if not SESSIONS_DIR.exists():
        return sessions

    for proj_dir in sorted(SESSIONS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        for jsonl_path in sorted(proj_dir.glob("*.jsonl"), key=lambda p: -p.stat().st_mtime):
            try:
                session = _parse_session_meta(jsonl_path, proj_dir.name)
                if session:
                    sessions.append(session)
            except Exception as exc:
                log.debug("Skipping %s: %s", jsonl_path, exc)

    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions[:50]  # cap at 50 most recent


def _parse_session_meta(path: Path, proj_hash: str) -> Optional[dict]:
    """Read first/last lines of a session .jsonl to extract metadata."""
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        return None

    first = json.loads(lines[0])
    last  = json.loads(lines[-1])

    session_id = first.get("sessionId") or path.stem
    # Title = first user message, truncated
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
    """
    Spawn claude CLI, stream output as server-sent events.
    Yields SSE lines: data: <json>\n\n
    """
    cmd = [CLAUDE_BIN, "--print", "--output-format", "stream-json"]
    if session_id:
        cmd += ["--resume", session_id]

    log.info("Spawning: %s (session=%s)", " ".join(cmd[:3]), session_id)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"},
    )

    # Write the message to stdin and close it
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

            # Capture session ID from first result object
            if not new_session_id and obj.get("session_id"):
                new_session_id = obj["session_id"]

            # Stream text deltas
            if obj.get("type") == "assistant":
                content = obj.get("message", {}).get("content", [])
                for block in (content if isinstance(content, list) else []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        yield f"data: {json.dumps({'type': 'text', 'text': block['text'], 'session_id': new_session_id})}\n\n"

            # Stream result (final message)
            elif obj.get("type") == "result":
                sid = obj.get("session_id") or new_session_id
                yield f"data: {json.dumps({'type': 'result', 'session_id': sid, 'cost_usd': obj.get('cost_usd')})}\n\n"

    await proc.wait()
    stderr = await proc.stderr.read()
    if proc.returncode != 0:
        log.error("claude exited %d: %s", proc.returncode, stderr.decode()[:500])
        yield f"data: {json.dumps({'type': 'error', 'text': stderr.decode()[:300]})}\n\n"

    yield "data: [DONE]\n\n"

# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None  # None = new conversation


@app.get("/api/sessions")
async def get_sessions(request: Request):
    """List recent claude CLI sessions."""
    _check_auth(request)
    return {"sessions": _list_sessions()}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    """
    Send a message to claude CLI and stream the response as SSE.
    Pass session_id to resume a session; omit for a new conversation.
    """
    _check_auth(request)
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")

    return StreamingResponse(
        _run_claude(req.message, req.session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "claude_bin": CLAUDE_BIN,
        "claude_bin_exists": Path(CLAUDE_BIN).exists(),
        "sessions_dir": str(SESSIONS_DIR),
        "sessions_dir_exists": SESSIONS_DIR.exists(),
        "session_count": len(_list_sessions()),
        "ts": int(time.time()),
    }


# ---------------------------------------------------------------------------
# Frontend — serve static HTML from ./static/
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
    parser.add_argument("--port",  type=int, default=7823, help="Port to listen on (default: 7823)")
    parser.add_argument("--host",  default="0.0.0.0",    help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--token", default=os.environ.get("RELAY_TOKEN", ""),
                        help="Bearer token for auth (or set RELAY_TOKEN env var)")
    args = parser.parse_args()

    RELAY_TOKEN = args.token
    if not RELAY_TOKEN:
        log.warning("No bearer token set — relay is unauthenticated (LAN-only via Caddy)")

    log.info("capes-relay v1.0.0 starting on %s:%d", args.host, args.port)
    log.info("Claude binary: %s", CLAUDE_BIN)
    log.info("Sessions dir:  %s", SESSIONS_DIR)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
