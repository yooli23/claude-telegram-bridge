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
        on_delta: callable = None,
    ) -> str:
        """Send a message to a Claude Code session and return the full response.

        Args:
            session_id: The session UUID to resume.
            message: The user message to send.
            on_delta: Optional async callback(text_so_far) called on each stream chunk.

        Returns:
            The final assistant text response.
        """
        cmd = [
            self.claude_bin,
            "--resume", session_id,
            "--permission-mode", self.permission_mode,
            "--output-format", "stream-json",
            "--print",
            message,
        ]

        if self.permission_mode == "bypassPermissions":
            cmd.insert(1, "--allow-dangerously-skip-permissions")

        logger.info(f"Running: {' '.join(cmd[:6])}...")

        env = os.environ.copy()
        env["CLAUDE_CODE_IS_SANDBOX"] = "1"

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        full_text = ""
        result_text = ""

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

                if event_type == "assistant" and event.get("subtype") == "text":
                    chunk = event.get("text", "")
                    full_text += chunk
                    if on_delta:
                        await on_delta(full_text)

                elif event_type == "result":
                    result_text = event.get("result", full_text)
                    # Also capture cost info
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
