"""Bridge to Claude Code CLI — send messages to sessions and stream responses."""

import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)


class ClaudeBridge:
    def __init__(
        self,
        claude_bin: str = "claude",
        permission_mode: str = "bypassPermissions",
    ):
        self.claude_bin = claude_bin
        self.permission_mode = permission_mode

    async def send_message(
        self,
        session_id: str,
        message: str,
        cwd: str | None = None,
        on_delta: callable = None,
    ) -> str:
        """Send a message to a Claude Code session and return the full response.

        Args:
            session_id: The session UUID to resume.
            message: The user message to send.
            cwd: Working directory to run claude from (must match session's original cwd).
            on_delta: Optional async callback(text_so_far) called on each stream chunk.

        Returns:
            The final assistant text response.
        """
        cmd = [
            self.claude_bin,
            "--resume", session_id,
            "--permission-mode", self.permission_mode,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--print",
            message,
        ]

        if self.permission_mode == "bypassPermissions":
            cmd.insert(1, "--allow-dangerously-skip-permissions")

        logger.info(f"Running: {' '.join(cmd[:6])}...")

        env = os.environ.copy()
        env["IS_SANDBOX"] = "1"

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )

        full_text = ""
        result_text = ""
        # Track what's happening for progress display
        status_prefix = ""

        try:
            async for line in process.stdout:
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                if event_type == "stream_event":
                    # Real-time streaming chunks
                    inner = event.get("event", {})
                    inner_type = inner.get("type", "")

                    if inner_type == "content_block_start":
                        block = inner.get("content_block", {})
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name", "tool")
                            status_prefix = f"[Running {tool_name}...]\n\n"
                            if on_delta:
                                await on_delta(status_prefix + full_text)

                    elif inner_type == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            full_text += delta["text"]
                            if on_delta:
                                await on_delta(status_prefix + full_text)

                elif event_type == "assistant":
                    # Complete assistant message — extract tool calls for status
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name", "tool")
                            tool_input = block.get("input", {})
                            # Show what tool is running
                            desc = tool_input.get("description", "")
                            cmd_str = tool_input.get("command", "")
                            detail = desc or cmd_str
                            if detail and len(detail) > 80:
                                detail = detail[:77] + "..."
                            status_prefix = f"[{tool_name}: {detail}]\n\n" if detail else f"[Running {tool_name}...]\n\n"
                            if on_delta:
                                await on_delta(status_prefix + full_text)
                        elif block.get("type") == "text":
                            # Complete text from assistant (non-streaming fallback)
                            text = block.get("text", "")
                            if text and not full_text:
                                full_text = text

                elif event_type == "user":
                    # Tool result came back — clear status prefix
                    status_prefix = ""
                    if on_delta and full_text:
                        await on_delta(full_text)

                elif event_type == "result":
                    result_text = event.get("result", full_text)
                    cost = event.get("cost_usd")
                    if cost is not None:
                        logger.info(f"Cost: ${cost:.4f}")

        except Exception as e:
            logger.error(f"Error reading stream: {e}")

        await process.wait()

        if process.returncode != 0:
            stderr = await process.stderr.read()
            err_msg = stderr.decode("utf-8", errors="replace").strip()
            if err_msg:
                logger.error(f"Claude stderr: {err_msg}")
            if not result_text and not full_text:
                return f"Error: Claude exited with code {process.returncode}\n{err_msg}"

        return result_text or full_text or "(no response)"
