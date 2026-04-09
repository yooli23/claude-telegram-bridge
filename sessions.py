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


def _load_session_names() -> dict[str, str]:
    """Load user-assigned session names from ~/.claude/sessions/*.json."""
    names: dict[str, str] = {}
    sessions_dir = Path(os.path.expanduser("~/.claude/sessions"))
    if not sessions_dir.exists():
        return names

    for f in sessions_dir.glob("*.json"):
        try:
            with open(f) as fh:
                data = json.load(fh)
            sid = data.get("sessionId", "")
            name = data.get("name", "")
            if sid and name:
                names[sid] = name
        except (json.JSONDecodeError, OSError):
            continue
    return names


def list_sessions(claude_dir: str | None = None) -> list[SessionInfo]:
    """List all Claude Code sessions across all project directories.

    Returns sessions sorted by most recent first.
    """
    if claude_dir is None:
        claude_dir = os.path.expanduser("~/.claude/projects")

    projects_path = Path(claude_dir)
    if not projects_path.exists():
        return []

    # Load user-assigned names
    user_names = _load_session_names()

    sessions = []
    for project_dir in projects_path.iterdir():
        if not project_dir.is_dir():
            continue

        decoded_cwd = _decode_project_dir(project_dir.name)

        for session_file in project_dir.glob("*.jsonl"):
            info = _parse_session_file(session_file)
            if info:
                if not info.cwd:
                    info.cwd = decoded_cwd
                # User-assigned name takes priority over slug
                if info.session_id in user_names:
                    info.slug = user_names[info.session_id]
                sessions.append(info)

    # Sort by file modification time (most recent first)
    sessions.sort(key=lambda s: os.path.getmtime(s.file_path), reverse=True)
    return sessions


def get_session_by_id(session_id: str, claude_dir: str | None = None) -> SessionInfo | None:
    """Find a specific session by its ID (full or prefix)."""
    for session in list_sessions(claude_dir):
        if session.session_id == session_id or session.session_id.startswith(session_id):
            return session
    return None
