"""Discover and parse Claude Code sessions from ~/.claude/projects/."""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class SessionInfo:
    session_id: str
    cwd: str
    first_user_message: str
    timestamp: str
    file_path: str
    slug: str = ""

    @property
    def display_name(self) -> str:
        if self.slug:
            return self.slug
        msg = self.first_user_message
        if len(msg) > 80:
            msg = msg[:77] + "..."
        return msg

    @property
    def short_id(self) -> str:
        return self.session_id[:8]


def _decode_project_dir(dirname: str) -> str:
    """Convert encoded directory name back to a path.

    Claude Code encodes paths by replacing / with -,
    e.g. -export-home -> /export/home.
    This is a best-effort heuristic.
    """
    if dirname.startswith("-"):
        # Reconstruct path: leading - means /
        return "/" + dirname[1:].replace("-", "/")
    return dirname


def _parse_session_file(filepath: Path) -> SessionInfo | None:
    """Extract metadata from a session JSONL file."""
    session_id = filepath.stem
    first_user_msg = ""
    timestamp = ""
    cwd = ""
    slug = ""

    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type", "")

                # Pick up slug (session name) from any entry that has it
                if not slug and entry.get("slug"):
                    slug = entry["slug"]

                if entry_type == "user" and not first_user_msg:
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        # Extract text from content blocks
                        parts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                parts.append(block["text"])
                            elif isinstance(block, str):
                                parts.append(block)
                        content = " ".join(parts)

                    # Skip system/command messages
                    if content.startswith("<"):
                        continue

                    first_user_msg = content.strip()
                    timestamp = entry.get("timestamp", "")
                    cwd = entry.get("cwd", "")

                # Once we have what we need, stop reading
                if first_user_msg and slug:
                    break

    except (OSError, PermissionError):
        return None

    if not first_user_msg:
        return None

    return SessionInfo(
        session_id=session_id,
        cwd=cwd,
        first_user_message=first_user_msg,
        timestamp=timestamp,
        file_path=str(filepath),
        slug=slug,
    )


def _load_session_registry() -> dict[str, dict]:
    """Load session registry from ~/.claude/sessions/*.json.

    Returns a dict of session_id -> {name, cwd, live} for each registered session.
    Each file is named by PID; we check if the PID is still alive.
    """
    registry: dict[str, dict] = {}
    sessions_dir = Path(os.path.expanduser("~/.claude/sessions"))
    if not sessions_dir.exists():
        return registry

    for f in sessions_dir.glob("*.json"):
        try:
            with open(f) as fh:
                data = json.load(fh)
            sid = data.get("sessionId", "")
            if not sid:
                continue
            pid_str = f.stem
            live = False
            try:
                pid = int(pid_str)
                os.kill(pid, 0)
                live = True
            except (ValueError, ProcessLookupError, PermissionError):
                pass
            registry[sid] = {
                "name": data.get("name", ""),
                "cwd": data.get("cwd", ""),
                "live": live,
            }
        except (json.JSONDecodeError, OSError):
            continue
    return registry


def list_sessions(claude_dir: str | None = None, live_only: bool = True) -> list[SessionInfo]:
    """List Claude Code sessions.

    Args:
        claude_dir: Override the projects directory path.
        live_only: If True, only return sessions with a running process.

    Returns sessions sorted by most recent first.
    """
    if claude_dir is None:
        claude_dir = os.path.expanduser("~/.claude/projects")

    projects_path = Path(claude_dir)
    if not projects_path.exists():
        return []

    registry = _load_session_registry()
    live_ids = {sid for sid, info in registry.items() if info["live"]} if live_only else None

    sessions = []
    for project_dir in projects_path.iterdir():
        if not project_dir.is_dir():
            continue

        decoded_cwd = _decode_project_dir(project_dir.name)

        for session_file in project_dir.glob("*.jsonl"):
            session_id = session_file.stem
            if live_only and live_ids is not None and session_id not in live_ids:
                continue

            info = _parse_session_file(session_file)
            if info:
                if not info.cwd:
                    info.cwd = decoded_cwd
                reg = registry.get(info.session_id, {})
                if reg.get("name"):
                    info.slug = reg["name"]
                sessions.append(info)

    sessions.sort(key=lambda s: os.path.getmtime(s.file_path), reverse=True)
    return sessions


def get_session_by_id(session_id: str, claude_dir: str | None = None) -> SessionInfo | None:
    """Find a specific session by its ID (full or prefix)."""
    for session in list_sessions(claude_dir):
        if session.session_id == session_id or session.session_id.startswith(session_id):
            return session
    return None
