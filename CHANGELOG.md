# Changelog — capes-relay

All notable changes to this project will be documented here.
Format: [Semantic Versioning](https://semver.org/)

---

## [1.0.0] — 2026-03-30

### Added
- Initial release
- FastAPI server (`relay.py`) bridging iPhone browser → `claude` CLI subprocess
- `/api/sessions` — lists recent claude CLI sessions from `~/.claude/projects/`
- `/api/chat` — accepts message + optional session_id, streams SSE response
- `/api/health` — health check with claude binary and session store status
- Mobile-first dark chat UI (`static/index.html`) with:
  - Session list drawer (shows title, message count, relative time)
  - New chat button
  - Streaming response with live text delta rendering
  - Minimal markdown rendering (code blocks, inline code, bold, italic)
  - Session ID tracking — new sessions captured from first stream event
  - Bearer token auth prompt stored in localStorage
- launchd plist (`com.capes.relay.plist`) for Mac Pro daemon
- Caddy vhost block (`caddy-block.txt`) for `relay.am180.us`
- `requirements.txt` (fastapi, uvicorn, aiofiles)
- `.gitignore`
- `README.md` with full setup instructions

### Notes
- Uses `claude --print --output-format stream-json --resume <id>` for existing sessions
- Uses `claude --print --output-format stream-json` for new sessions
- Sessions are stored by the claude CLI at `~/.claude/projects/` — shared with Claude Desktop
- LAN-only access enforced by Caddy `lan_only` snippet
- Compatible with Intel Mac + macOS 12 (Dispatch not involved)
