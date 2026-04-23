"""Discord bot that bridges to Claude Code sessions."""

import asyncio
import logging
import os
import tempfile
import time

import discord
from discord import app_commands
from dotenv import load_dotenv

from bridge import ClaudeBridge, PermissionRequest, wrap_channel_message
from sessions import list_sessions, get_session_by_id, get_last_assistant_message
from formatter import format_discord, split_message, DISCORD_MAX_LEN
from message_queue import ChatQueue

load_dotenv()

logger = logging.getLogger(__name__)

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("DISCORD_ALLOWED_USER_ID", "0"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions")

active_sessions: dict[int, str] = {}  # channel_id -> session_id

bridge = ClaudeBridge(
    claude_bin=CLAUDE_BIN,
    permission_mode=CLAUDE_PERMISSION_MODE,
)

chat_queue = ChatQueue()

EDIT_INTERVAL_INITIAL = 0.8
EDIT_INTERVAL_STEADY = 2.0
EDIT_INTERVAL_RAMPUP_CHARS = 500


def is_allowed(interaction: discord.Interaction) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return interaction.user.id == ALLOWED_USER_ID


def is_allowed_message(message: discord.Message) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return message.author.id == ALLOWED_USER_ID


class ClaudeBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self._register_commands()
        await self.tree.sync()
        logger.info("Slash commands synced")

    def _register_commands(self):

        @self.tree.command(name="sessions", description="List available Claude Code sessions")
        async def cmd_sessions(interaction: discord.Interaction):
            if not is_allowed(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            sessions = list_sessions()
            if not sessions:
                await interaction.response.send_message("No Claude Code sessions found.")
                return

            sessions = sessions[:20]
            view = SessionSelectView(sessions)

            lines = ["**Pick a session:**\n"]
            for i, s in enumerate(sessions):
                ts = s.timestamp[:16].replace("T", " ") if s.timestamp else "?"
                lines.append(f"`{i+1}.` `{s.short_id}` {s.cwd}\n> {s.display_name} — {ts}")

            text = "\n".join(lines)
            if len(text) > DISCORD_MAX_LEN:
                text = text[:DISCORD_MAX_LEN - 20] + "\n...(truncated)"

            await interaction.response.send_message(text, view=view)

        @self.tree.command(name="current", description="Show current active session")
        async def cmd_current(interaction: discord.Interaction):
            if not is_allowed(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            channel_id = interaction.channel_id
            sid = active_sessions.get(channel_id)
            if not sid:
                await interaction.response.send_message(
                    "No active session. Use `/sessions` to pick one."
                )
                return

            session = get_session_by_id(sid)
            cost = bridge.cost_tracker.get(sid)
            if session:
                embed = discord.Embed(
                    title=f"Session {session.short_id}",
                    color=discord.Color.blue(),
                )
                embed.add_field(name="Directory", value=f"`{session.cwd}`", inline=False)
                embed.add_field(name="Topic", value=session.display_name, inline=False)
                embed.add_field(name="Cost", value=f"${cost:.4f}", inline=True)
                await interaction.response.send_message(embed=embed)
            else:
                await interaction.response.send_message(
                    f"Active session: `{sid[:8]}` (details unavailable)\nCost: ${cost:.4f}"
                )

        @self.tree.command(name="detach", description="Detach from current session")
        async def cmd_detach(interaction: discord.Interaction):
            if not is_allowed(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            channel_id = interaction.channel_id
            if channel_id in active_sessions:
                old = active_sessions.pop(channel_id)
                await interaction.response.send_message(f"Detached from session `{old[:8]}`.")
            else:
                await interaction.response.send_message("No active session.")

        @self.tree.command(name="cancel", description="Cancel the current operation")
        async def cmd_cancel(interaction: discord.Interaction):
            if not is_allowed(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            channel_id = interaction.channel_id
            cancelled = await bridge.cancel(channel_id)
            chat_queue.clear(channel_id)
            if cancelled:
                await interaction.response.send_message("Cancelled.")
            else:
                await interaction.response.send_message("Nothing running to cancel.")

        @self.tree.command(name="cost", description="Show session cost so far")
        async def cmd_cost(interaction: discord.Interaction):
            if not is_allowed(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            channel_id = interaction.channel_id
            sid = active_sessions.get(channel_id)
            if not sid:
                await interaction.response.send_message("No active session.")
                return

            cost = bridge.cost_tracker.get(sid)
            await interaction.response.send_message(f"Session cost so far: **${cost:.4f}**")

        @self.tree.command(name="new", description="Start a new Claude Code session")
        @app_commands.describe(directory="Working directory for the new session")
        async def cmd_new(interaction: discord.Interaction, directory: str = "~"):
            if not is_allowed(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            import json

            cwd = os.path.expanduser(directory)
            if not os.path.isdir(cwd):
                await interaction.response.send_message(f"Directory not found: `{cwd}`")
                return

            await interaction.response.defer()

            cmd = [
                CLAUDE_BIN,
                "--print",
                "--permission-mode", CLAUDE_PERMISSION_MODE,
                "--output-format", "json",
                "-p", "Say 'Session started. Ready for instructions.' and nothing else.",
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await process.communicate()

            try:
                result = json.loads(stdout.decode())
                session_id = result.get("session_id", "")
                if session_id:
                    active_sessions[interaction.channel_id] = session_id
                    embed = discord.Embed(
                        title="New Session Created",
                        color=discord.Color.green(),
                    )
                    embed.add_field(name="Session", value=f"`{session_id[:8]}`", inline=True)
                    embed.add_field(name="Directory", value=f"`{cwd}`", inline=True)
                    await interaction.followup.send(embed=embed)
                    return
            except (json.JSONDecodeError, KeyError):
                pass

            await interaction.followup.send(
                f"Session started in `{cwd}` but couldn't capture session ID.\n"
                "Use `/sessions` to find and select it."
            )

    async def on_ready(self):
        logger.info(f"Discord bot logged in as {self.user}")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        logger.info(f"Message from {message.author} (id={message.author.id}): {message.content[:50]}")
        if not is_allowed_message(message):
            logger.warning(f"Rejected: user {message.author.id} not in allowed list (expected {ALLOWED_USER_ID})")
            return

        channel_id = message.channel.id
        session_id = active_sessions.get(channel_id)

        if not session_id:
            await message.reply(
                "No active session in this channel. Use `/sessions` to pick one."
            )
            return

        session = get_session_by_id(session_id)
        session_cwd = session.cwd if session else None

        content = message.content
        if self.user:
            content = content.replace(f"<@{self.user.id}>", "").strip()

        file_paths = await _download_discord_attachments(message, session_cwd)
        if file_paths:
            file_refs = " ".join(f'@"{p}"' for p in file_paths)
            content = f"{file_refs}\n\n{content}" if content else file_refs

        if not content:
            return

        wrapped = wrap_channel_message(
            content=content,
            source="discord",
            user=message.author.display_name,
            chat_id=str(channel_id),
        )

        bot_ref = self

        async def process_message(msg_text: str):
            await _process_single_message(bot_ref, message, session_id, session_cwd, msg_text)

        depth = await chat_queue.enqueue(channel_id, wrapped, process_message)
        if depth > 0:
            await message.reply(f"Queued (position {depth}). Claude is still working...")


async def _process_single_message(
    bot: ClaudeBot,
    message: discord.Message,
    session_id: str,
    session_cwd: str | None,
    message_text: str,
):
    """Process a single message through Claude with full streaming UX."""
    channel_id = message.channel.id

    await message.add_reaction("⏳")
    reply = await message.reply("...")

    last_edit_time = 0.0
    last_edit_text = ""
    tool_active = False

    async def on_delta(text_so_far: str, tool_status: str = ""):
        nonlocal last_edit_time, last_edit_text, tool_active
        now = time.time()

        interval = (
            EDIT_INTERVAL_INITIAL
            if len(text_so_far) < EDIT_INTERVAL_RAMPUP_CHARS
            else EDIT_INTERVAL_STEADY
        )
        if now - last_edit_time < interval:
            return

        display = text_so_far
        if tool_status:
            display = f"*{tool_status}*\n\n{display}"

        if len(display) > DISCORD_MAX_LEN:
            display = display[: DISCORD_MAX_LEN - 30] + "\n\n...(streaming)"

        if display == last_edit_text:
            return

        try:
            await reply.edit(content=display)
            last_edit_time = now
            last_edit_text = display
        except Exception:
            pass

        if tool_status and not tool_active:
            tool_active = True
            try:
                await message.remove_reaction("⏳", bot.user)
                await message.add_reaction("🔧")
            except Exception:
                pass
        elif not tool_status and tool_active:
            tool_active = False
            try:
                await message.remove_reaction("🔧", bot.user)
                await message.add_reaction("⏳")
            except Exception:
                pass

    async def on_cost_threshold(total: float, threshold: float):
        await message.channel.send(
            f"**Cost alert:** session has spent ${total:.2f} (crossed ${threshold:.0f} threshold)"
        )

    async def on_compaction():
        try:
            await message.remove_reaction("⏳", bot.user)
            await message.remove_reaction("🔧", bot.user)
        except Exception:
            pass
        await message.add_reaction("📦")

    async def on_permission_request(perm: PermissionRequest):
        view = PermissionView(channel_id, perm.request_id)
        preview = perm.preview
        text = f"**Permission needed: {perm.tool_name}**\n```\n{preview}\n```"
        if len(text) > DISCORD_MAX_LEN:
            text = text[:DISCORD_MAX_LEN - 10] + "\n```"
        await message.channel.send(text, view=view)
        await message.add_reaction("🔐")

    try:
        async with message.channel.typing():
            response = await bridge.send_message(
                session_id=session_id,
                message=message_text,
                cwd=session_cwd,
                chat_id=channel_id,
                on_delta=on_delta,
                on_cost_threshold=on_cost_threshold,
                on_compaction=on_compaction,
                on_permission_request=on_permission_request,
            )
    except Exception as e:
        logger.error(f"Bridge error: {e}")
        await reply.edit(content=f"Error: {e}")
        await _swap_reaction(message, bot.user, "❌")
        return

    if response == "(cancelled)":
        try:
            await reply.edit(content="(cancelled)")
        except Exception:
            pass
        await _swap_reaction(message, bot.user, "🛑")
        return

    chunks = format_discord(response)

    if not chunks:
        await _swap_reaction(message, bot.user, "❌")
        return

    try:
        await reply.edit(content=chunks[0].text)
    except Exception:
        await message.reply(chunks[0].text)

    for chunk in chunks[1:]:
        await message.reply(chunk.text)

    await _swap_reaction(message, bot.user, "✅")


# ── Helpers ───────────────────────────────────────────────


async def _download_discord_attachments(
    message: discord.Message, session_cwd: str | None
) -> list[str]:
    if not message.attachments:
        return []

    dest_dir = session_cwd or tempfile.gettempdir()
    paths = []
    for att in message.attachments:
        try:
            dest = os.path.join(dest_dir, att.filename)
            await att.save(dest)
            paths.append(dest)
            logger.info(f"Downloaded attachment to {dest}")
        except Exception as e:
            logger.error(f"Failed to download attachment: {e}")
    return paths


async def _swap_reaction(message: discord.Message, bot_user, new_emoji: str):
    try:
        for reaction in message.reactions:
            if reaction.me:
                await message.remove_reaction(reaction.emoji, bot_user)
        await message.add_reaction(new_emoji)
    except Exception:
        pass


class PermissionView(discord.ui.View):
    """Approve/deny buttons for a permission request."""

    def __init__(self, channel_id: int, request_id: str):
        super().__init__(timeout=300)
        self.channel_id = channel_id
        self.request_id = request_id

    @discord.ui.button(label="Allow", style=discord.ButtonStyle.green)
    async def allow(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        success = await bridge.respond_permission(self.channel_id, self.request_id, allow=True)
        if success:
            await interaction.response.edit_message(content=f"Approved: {interaction.message.content}", view=None)
        else:
            await interaction.response.edit_message(content="Permission request expired.", view=None)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        success = await bridge.respond_permission(self.channel_id, self.request_id, allow=False)
        if success:
            await interaction.response.edit_message(content=f"Denied: {interaction.message.content}", view=None)
        else:
            await interaction.response.edit_message(content="Permission request expired.", view=None)


class SessionSelectView(discord.ui.View):

    def __init__(self, sessions):
        super().__init__(timeout=120)
        self.sessions = sessions
        options = []
        for i, s in enumerate(sessions[:25]):
            label = f"{s.short_id} | {s.display_name}"
            if len(label) > 100:
                label = label[:97] + "..."
            desc = s.cwd
            if len(desc) > 100:
                desc = "..." + desc[-97:]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=s.session_id,
                    description=desc,
                )
            )
        select = discord.ui.Select(
            placeholder="Choose a session...",
            options=options,
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return

        session_id = interaction.data["values"][0]
        channel_id = interaction.channel_id
        active_sessions[channel_id] = session_id

        session = get_session_by_id(session_id)
        if session:
            embed = discord.Embed(
                title=f"Connected to session {session.short_id}",
                color=discord.Color.green(),
            )
            embed.add_field(name="Directory", value=f"`{session.cwd}`", inline=False)
            embed.add_field(name="Topic", value=session.display_name, inline=False)
            last_msg = get_last_assistant_message(session)
            if last_msg:
                embed.add_field(name="Last message", value=last_msg, inline=False)
            else:
                embed.set_footer(text="Send messages to continue this session.")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                f"Connected to session `{session_id[:8]}`. Send messages to continue."
            )


# ── Entry points ──────────────────────────────────────────


def create_discord_client() -> ClaudeBot:
    if not DISCORD_TOKEN:
        raise ValueError("Set DISCORD_BOT_TOKEN in .env")
    return ClaudeBot()


def main():
    client = create_discord_client()
    logger.info("Discord bot starting...")
    client.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    main()
