"""Discord bot that bridges to Claude Code sessions.

Supports two modes:
- Single-user: regular channel, /sessions to attach existing sessions
- Multi-user: forum channel bound to a project via /setup, /spawn creates tasks
"""

import asyncio
import json
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
from project_config import ProjectConfigStore, TaskInfo, UserRegistration
from project_scaffold import scaffold_project
from worktree import create_worktree, remove_worktree

load_dotenv()

logger = logging.getLogger(__name__)

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("DISCORD_ALLOWED_USER_ID", "0"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions")

# Single-user mode: channel_id -> session_id (legacy flow)
active_sessions: dict[int, str] = {}

bridge = ClaudeBridge(
    claude_bin=CLAUDE_BIN,
    permission_mode=CLAUDE_PERMISSION_MODE,
)

chat_queue = ChatQueue()
config_store = ProjectConfigStore()

EDIT_INTERVAL_INITIAL = 0.8
EDIT_INTERVAL_STEADY = 2.0
EDIT_INTERVAL_RAMPUP_CHARS = 500

STATUS_TAGS = {
    "active": "\U0001f527 Active",
    "done": "✅ Done",
    "review": "\U0001f4cb Review",
    "error": "❌ Error",
}


def is_admin(interaction: discord.Interaction) -> bool:
    """Check if user is an admin (server owner, has Administrator perm, or is ALLOWED_USER_ID)."""
    if ALLOWED_USER_ID and interaction.user.id == ALLOWED_USER_ID:
        return True
    if interaction.guild and interaction.guild.owner_id == interaction.user.id:
        return True
    if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator:
        return True
    return False


def is_allowed(interaction: discord.Interaction) -> bool:
    """Check if user is allowed. Admins always pass. Members pass in project channels."""
    if is_admin(interaction):
        return True
    channel = interaction.channel
    parent_id = getattr(channel, "parent_id", None) or interaction.channel_id
    if config_store.get_binding(parent_id):
        return True
    # Check if there's a project in this server (for commands run from text channels)
    if interaction.guild:
        for cid in config_store._bindings:
            if interaction.guild.get_channel(cid) is not None:
                return True
    return False


def is_allowed_message(message: discord.Message) -> bool:
    """Check if message author is allowed. Forum thread members are always allowed."""
    channel = message.channel
    if isinstance(channel, discord.Thread) and channel.parent:
        parent_id = channel.parent_id
        if config_store.get_binding(parent_id):
            return True
    if not ALLOWED_USER_ID:
        return True
    return message.author.id == ALLOWED_USER_ID


def _git_env_for_user(user_id: int) -> dict[str, str] | None:
    reg = config_store.get_user(user_id)
    if not reg:
        return None
    return {
        "GIT_AUTHOR_NAME": reg.git_name,
        "GIT_AUTHOR_EMAIL": reg.git_email,
        "GIT_COMMITTER_NAME": reg.git_name,
        "GIT_COMMITTER_EMAIL": reg.git_email,
    }


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
        self._register_legacy_commands()
        self._register_project_commands()

    # ── Legacy single-user commands ──────────────────────────

    def _register_legacy_commands(self):

        @self.tree.command(name="help", description="Show available commands")
        async def cmd_help(interaction: discord.Interaction):
            embed = discord.Embed(
                title="Claude Code Bridge — Commands",
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="Project Commands",
                value=(
                    "`/register <name> <email>` — Set your git identity for commits\n"
                    "`/spawn <task>` — Create a new agent task (own branch + thread)\n"
                    "`/status` — List all active agent tasks\n"
                    "`/board` — Full dashboard: status, tasks, PRs, notes\n"
                    "`/note <text>` — Add a note to NOTES.md and push\n"
                    "`/notes` — Display project NOTES.md\n"
                    "`/current` — Show current task info\n"
                    "`/cost` — Show session cost"
                ),
                inline=False,
            )
            embed.add_field(
                name="Admin Commands",
                value=(
                    "`/setup` — Bind a forum channel to a project\n"
                    "`/sessions` — List and pick a live session\n"
                    "`/new <dir>` — Start a new raw session\n"
                    "`/cancel` — Cancel a running operation\n"
                    "`/detach` — Disconnect from session"
                ),
                inline=False,
            )
            embed.add_field(
                name="Status Reactions",
                value="⏳ thinking | \U0001f527 tool | \U0001f510 permission | \U0001f4e6 compact | ✅ done | ❌ error | \U0001f6d1 cancelled",
                inline=False,
            )
            embed.set_footer(text="Docs: github.com/yooli23/claude-code-bridge")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="sessions", description="List available Claude Code sessions (admin)")
        async def cmd_sessions(interaction: discord.Interaction):
            if not is_admin(interaction):
                await interaction.response.send_message("Admin only.", ephemeral=True)
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

            # Check if this is a forum thread with a task
            task = config_store.get_task(channel_id)
            if task:
                embed = discord.Embed(
                    title=f"Task: {task.description}",
                    color=discord.Color.blue(),
                )
                embed.add_field(name="Session", value=f"`{task.session_id[:8]}`", inline=True)
                embed.add_field(name="Branch", value=f"`{task.branch_name}`", inline=True)
                embed.add_field(name="Status", value=task.status, inline=True)
                embed.add_field(name="Spawned by", value=task.user_name, inline=True)
                embed.add_field(name="Worktree", value=f"`{task.worktree_path}`", inline=False)
                cost = bridge.cost_tracker.get(task.session_id)
                embed.add_field(name="Cost", value=f"${cost:.4f}", inline=True)
                await interaction.response.send_message(embed=embed)
                return

            sid = active_sessions.get(channel_id)
            if not sid:
                await interaction.response.send_message(
                    "No active session. Use `/sessions` to pick one or `/spawn` to create a task."
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

        @self.tree.command(name="detach", description="Detach from current session (admin)")
        async def cmd_detach(interaction: discord.Interaction):
            if not is_admin(interaction):
                await interaction.response.send_message("Admin only.", ephemeral=True)
                return

            channel_id = interaction.channel_id
            if channel_id in active_sessions:
                old = active_sessions.pop(channel_id)
                await interaction.response.send_message(f"Detached from session `{old[:8]}`.")
            else:
                await interaction.response.send_message("No active session.")

        @self.tree.command(name="cancel", description="Cancel the current operation (admin)")
        async def cmd_cancel(interaction: discord.Interaction):
            if not is_admin(interaction):
                await interaction.response.send_message("Admin only.", ephemeral=True)
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

            task = config_store.get_task(channel_id)
            if task:
                cost = bridge.cost_tracker.get(task.session_id)
                await interaction.response.send_message(f"Task cost so far: **${cost:.4f}**")
                return

            sid = active_sessions.get(channel_id)
            if not sid:
                await interaction.response.send_message("No active session.")
                return

            cost = bridge.cost_tracker.get(sid)
            await interaction.response.send_message(f"Session cost so far: **${cost:.4f}**")

        @self.tree.command(name="new", description="Start a new Claude Code session (admin)")
        @app_commands.describe(directory="Working directory for the new session")
        async def cmd_new(interaction: discord.Interaction, directory: str = "~"):
            if not is_admin(interaction):
                await interaction.response.send_message("Admin only.", ephemeral=True)
                return

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

    # ── Multi-user project commands ──────────────────────────

    def _register_project_commands(self):

        @self.tree.command(name="setup", description="Bind a forum channel to a project (admin)")
        @app_commands.describe(
            forum_channel="The forum channel to bind",
            project_dir="Local project directory path",
            code_repo="GitHub code repo (org/repo)",
            paper_repo="GitHub paper repo (org/repo, optional)",
        )
        async def cmd_setup(
            interaction: discord.Interaction,
            forum_channel: discord.ForumChannel,
            project_dir: str,
            code_repo: str = "",
            paper_repo: str = "",
        ):
            if not is_admin(interaction):
                await interaction.response.send_message("Admin only.", ephemeral=True)
                return

            expanded = os.path.expanduser(project_dir)
            if not os.path.isdir(expanded):
                await interaction.response.send_message(
                    f"Directory not found: `{expanded}`", ephemeral=True
                )
                return

            await interaction.response.defer()

            binding = config_store.bind(
                channel_id=forum_channel.id,
                project_dir=expanded,
                code_repo=code_repo,
                paper_repo=paper_repo,
            )

            # Scaffold CLAUDE.md, STATUS.md, NOTES.md
            created = scaffold_project(expanded, code_repo, paper_repo)
            if created:
                proc = await asyncio.create_subprocess_exec(
                    "git", "add", *created,
                    cwd=expanded,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                proc = await asyncio.create_subprocess_exec(
                    "git", "commit", "-m", f"Add project scaffold: {', '.join(created)}",
                    cwd=expanded,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                proc = await asyncio.create_subprocess_exec(
                    "git", "push",
                    cwd=expanded,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()

            embed = discord.Embed(
                title=f"Project bound to #{forum_channel.name}",
                color=discord.Color.green(),
            )
            embed.add_field(name="Forum channel", value=forum_channel.mention, inline=False)
            embed.add_field(name="Directory", value=f"`{expanded}`", inline=False)
            if code_repo:
                embed.add_field(name="Code repo", value=f"`{code_repo}`", inline=True)
            if paper_repo:
                embed.add_field(name="Paper repo", value=f"`{paper_repo}`", inline=True)
            if created:
                embed.add_field(name="Created", value=", ".join(f"`{f}`" for f in created), inline=False)
            embed.set_footer(text="Use /spawn <task> to create tasks.")
            await interaction.followup.send(embed=embed)

        @self.tree.command(name="register", description="Register your Git identity for commits")
        @app_commands.describe(
            git_name="Your name for git commits (e.g. 'Jane Doe')",
            git_email="Your email for git commits (e.g. 'jane@example.com')",
        )
        async def cmd_register(
            interaction: discord.Interaction,
            git_name: str,
            git_email: str,
        ):
            if not is_allowed(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            config_store.register_user(interaction.user.id, git_name, git_email)
            await interaction.response.send_message(
                f"Registered git identity: **{git_name}** <{git_email}>\n"
                "Your spawned tasks will commit under this name.",
                ephemeral=True,
            )

        @self.tree.command(name="spawn", description="Spawn a new agent task (creates a forum post)")
        @app_commands.describe(
            task="Description of the task for the agent",
            forum_channel="Target forum channel (optional if used inside one)",
        )
        async def cmd_spawn(
            interaction: discord.Interaction,
            task: str,
            forum_channel: discord.ForumChannel | None = None,
        ):
            if not is_allowed(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            if not forum_channel:
                channel = interaction.channel
                if isinstance(channel, discord.ForumChannel):
                    forum_channel = channel
                elif isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.ForumChannel):
                    forum_channel = channel.parent

            if not forum_channel:
                await interaction.response.send_message(
                    "Specify a forum channel or use this command inside one.",
                    ephemeral=True,
                )
                return

            binding = config_store.get_binding(forum_channel.id)
            if not binding:
                await interaction.response.send_message(
                    "This channel has no project binding. Use `/setup` first.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()

            # Create git worktree
            try:
                worktree_path, branch_name = await create_worktree(
                    binding.project_dir, task, interaction.user.display_name
                )
            except RuntimeError as e:
                await interaction.followup.send(f"Failed to create worktree: {e}")
                return

            # Start a new Claude Code session in the worktree
            git_env = _git_env_for_user(interaction.user.id)

            cmd = [
                CLAUDE_BIN,
                "--print",
                "--permission-mode", CLAUDE_PERMISSION_MODE,
                "--output-format", "json",
                "-p", "Say 'Session started. Ready for instructions.' and nothing else.",
            ]
            if CLAUDE_PERMISSION_MODE == "bypassPermissions":
                cmd.insert(1, "--allow-dangerously-skip-permissions")

            spawn_env = os.environ.copy()
            spawn_env["IS_SANDBOX"] = "1"
            if git_env:
                spawn_env.update(git_env)

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=worktree_path,
                env=spawn_env,
            )
            stdout, stderr = await process.communicate()

            session_id = ""
            try:
                result = json.loads(stdout.decode())
                session_id = result.get("session_id", "")
            except (json.JSONDecodeError, KeyError):
                logger.error(f"Failed to parse session output: {stdout.decode()[:500]}")

            if not session_id:
                err = stderr.decode().strip() if stderr else "unknown error"
                out = stdout.decode().strip()[:300] if stdout else "no output"
                logger.error(f"Session creation failed. stderr: {err}, stdout: {out}")
                await remove_worktree(binding.project_dir, worktree_path)
                await interaction.followup.send(f"Failed to create Claude session.\n```\n{err[:500]}\n```")
                return

            # Create the forum post (thread)
            user_name = interaction.user.display_name
            thread_content = (
                f"**Task:** {task}\n"
                f"**Spawned by:** {user_name}\n"
                f"**Branch:** `{branch_name}`\n"
                f"**Session:** `{session_id[:8]}`"
            )

            # Find or create status tags
            available_tags = {t.name: t for t in forum_channel.available_tags}
            active_tag = available_tags.get(STATUS_TAGS["active"])
            tags_to_apply = [active_tag] if active_tag else []

            thread_with_message = await forum_channel.create_thread(
                name=f"{task[:90]}",
                content=thread_content,
                applied_tags=tags_to_apply,
            )
            thread = thread_with_message.thread

            # Register the task
            task_info = TaskInfo(
                thread_id=thread.id,
                session_id=session_id,
                worktree_path=worktree_path,
                branch_name=branch_name,
                user_id=interaction.user.id,
                user_name=user_name,
                description=task,
                status="active",
                project_dir=binding.project_dir,
            )
            config_store.add_task(task_info)

            # Also register in active_sessions so message handling works
            active_sessions[thread.id] = session_id

            embed = discord.Embed(
                title="Task spawned",
                color=discord.Color.green(),
            )
            embed.add_field(name="Thread", value=thread.mention, inline=True)
            embed.add_field(name="Branch", value=f"`{branch_name}`", inline=True)
            embed.set_footer(text="Send messages in the thread to work with the agent.")
            await interaction.followup.send(embed=embed)

            # Send the initial task to Claude in the thread
            initial_instructions = (
                f"Your task: {task}\n\n"
                f"You are working on branch `{branch_name}` in a git worktree.\n\n"
                "Before you start:\n"
                "1. Read CLAUDE.md for project workflow rules\n"
                "2. Read STATUS.md for current project state and active tasks\n"
                "3. Read NOTES.md for related work and shared context\n\n"
                "When you finish:\n"
                "1. Commit your changes\n"
                f"2. Push: `git push -u origin {branch_name}`\n"
                f"3. Create a PR: `gh pr create --title '<title>' --body '<description>'`"
                f"{' --repo ' + binding.code_repo if binding.code_repo else ''}\n"
                "4. Update STATUS.md with your completed task and PR link\n"
                "5. Commit and push the STATUS.md update\n\n"
                "Now start working on the task."
            )
            wrapped = wrap_channel_message(
                content=initial_instructions,
                source="discord",
                user=user_name,
                chat_id=str(thread.id),
            )

            bot_ref = self

            # Create a synthetic message-like context for the thread
            initial_msg = await thread.send("Starting work on this task...")
            await initial_msg.add_reaction("⏳")

            spawn_user_id = interaction.user.id

            async def process_initial(msg_text: str):
                await _process_thread_message(bot_ref, thread, initial_msg, session_id, worktree_path, msg_text, task_user_id=spawn_user_id)

            await chat_queue.enqueue(thread.id, wrapped, process_initial)

        @self.tree.command(name="status", description="Show all active tasks in this project")
        async def cmd_status(interaction: discord.Interaction):
            if not is_allowed(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            channel = interaction.channel
            parent_id = None

            if isinstance(channel, discord.ForumChannel):
                parent_id = channel.id
            elif isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.ForumChannel):
                parent_id = channel.parent_id

            if not parent_id:
                await interaction.response.send_message(
                    "No project found. Run `/setup` first or use this in a project forum thread.", ephemeral=True
                )
                return

            binding = config_store.get_binding(parent_id)
            if not binding:
                await interaction.response.send_message("No project binding found.", ephemeral=True)
                return

            tasks = config_store.get_tasks_for_channel(parent_id)

            if not tasks:
                await interaction.response.send_message("No active tasks.")
                return

            embed = discord.Embed(
                title="Project Tasks",
                description=f"`{binding.project_dir}`",
                color=discord.Color.blue(),
            )

            for t in tasks:
                status_icon = {
                    "active": "\U0001f527",
                    "done": "✅",
                    "review": "\U0001f4cb",
                    "error": "❌",
                }.get(t.status, "❓")

                cost = bridge.cost_tracker.get(t.session_id)
                value = (
                    f"By: {t.user_name} | Branch: `{t.branch_name}`\n"
                    f"Cost: ${cost:.4f} | <#{t.thread_id}>"
                )
                embed.add_field(
                    name=f"{status_icon} {t.description[:50]}",
                    value=value,
                    inline=False,
                )

            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="note", description="Add a note to the project's NOTES.md")
        @app_commands.describe(content="Note to add (related work, discussion point, resource)")
        async def cmd_note(interaction: discord.Interaction, content: str):
            if not is_allowed(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            binding = _get_project_binding(interaction)
            if not binding:
                await interaction.response.send_message(
                    "No project found. Run `/setup` first.", ephemeral=True
                )
                return

            await interaction.response.defer()

            from datetime import date
            today = date.today().isoformat()
            user_name = interaction.user.display_name

            notes_path = os.path.join(binding.project_dir, "NOTES.md")
            entry = f"- {content}\n  - Added by: {user_name}, {today}\n"

            if not os.path.exists(notes_path):
                header = "# Project Notes\n\n## Notes\n\n"
                with open(notes_path, "w") as f:
                    f.write(header + entry)
            else:
                with open(notes_path, "a") as f:
                    f.write(entry)

            # Git add and commit
            proc = await asyncio.create_subprocess_exec(
                "git", "add", "NOTES.md",
                cwd=binding.project_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            proc = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", f"Add note from {user_name}: {content[:60]}",
                cwd=binding.project_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            proc = await asyncio.create_subprocess_exec(
                "git", "push",
                cwd=binding.project_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            await interaction.followup.send(f"Added to NOTES.md and pushed:\n> {content}")

        @self.tree.command(name="notes", description="Show project NOTES.md")
        async def cmd_notes(interaction: discord.Interaction):
            if not is_allowed(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            binding = _get_project_binding(interaction)
            if not binding:
                await interaction.response.send_message(
                    "No project found. Run `/setup` first.", ephemeral=True
                )
                return

            notes_path = os.path.join(binding.project_dir, "NOTES.md")
            if not os.path.exists(notes_path):
                await interaction.response.send_message("No NOTES.md found. Use `/note` to add the first one.")
                return

            with open(notes_path, "r") as f:
                content = f.read()

            if not content.strip():
                await interaction.response.send_message("NOTES.md is empty.")
                return

            chunks = split_message(content, DISCORD_MAX_LEN)
            await interaction.response.send_message(chunks[0])
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk)

        @self.tree.command(name="board", description="Full project dashboard: status, tasks, PRs, notes")
        async def cmd_board(interaction: discord.Interaction):
            if not is_allowed(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            binding = _get_project_binding(interaction)
            if not binding:
                await interaction.response.send_message(
                    "No project found. Run `/setup` first.", ephemeral=True
                )
                return

            await interaction.response.defer()

            parent_id = _get_forum_parent_id(interaction)
            sections = []

            # Header
            sections.append(f"## Project: `{binding.project_dir}`")
            if binding.code_repo:
                sections.append(f"Code: `{binding.code_repo}` | Paper: `{binding.paper_repo or 'none'}`")

            # STATUS.md summary
            status_path = os.path.join(binding.project_dir, "STATUS.md")
            if os.path.exists(status_path):
                with open(status_path, "r") as f:
                    status_content = f.read().strip()
                if status_content:
                    if len(status_content) > 600:
                        status_content = status_content[:597] + "..."
                    sections.append(f"\n**Status:**\n{status_content}")

            # Active tasks
            tasks = config_store.get_tasks_for_channel(parent_id) if parent_id else []
            if tasks:
                task_lines = []
                for t in tasks:
                    icon = {"active": "\U0001f527", "done": "✅", "review": "\U0001f4cb", "error": "❌"}.get(t.status, "❓")
                    cost = bridge.cost_tracker.get(t.session_id)
                    task_lines.append(f"{icon} **{t.description[:50]}** — {t.user_name} (${cost:.2f}) <#{t.thread_id}>")
                sections.append("\n**Active Tasks:**\n" + "\n".join(task_lines))
            else:
                sections.append("\n**Active Tasks:** none")

            # Recent PRs from GitHub
            if binding.code_repo:
                pr_text = await _fetch_recent_prs(binding.code_repo)
                if pr_text:
                    sections.append(f"\n**Recent PRs ({binding.code_repo}):**\n{pr_text}")

            # Recent notes
            notes_path = os.path.join(binding.project_dir, "NOTES.md")
            if os.path.exists(notes_path):
                with open(notes_path, "r") as f:
                    lines = f.readlines()
                note_entries = [l.rstrip() for l in lines if l.startswith("- ")]
                if note_entries:
                    recent = note_entries[-5:]
                    sections.append("\n**Recent Notes:**\n" + "\n".join(recent))

            full_text = "\n".join(sections)
            chunks = split_message(full_text, DISCORD_MAX_LEN)
            await interaction.followup.send(chunks[0])
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk)

    # ── Message handling ─────────────────────────────────────

    async def on_ready(self):
        logger.info(f"Discord bot logged in as {self.user}")

        # Restore active sessions from persisted tasks
        restored = 0
        for task in list(config_store._tasks.values()):
            if task.status == "active" and os.path.isdir(task.worktree_path):
                active_sessions[task.thread_id] = task.session_id
                restored += 1
            elif not os.path.isdir(task.worktree_path):
                config_store.update_task_status(task.thread_id, "done")
        if restored:
            logger.info(f"Restored {restored} active task(s) from previous session")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        logger.info(f"Message from {message.author} (id={message.author.id}): {message.content[:50]}")
        if not is_allowed_message(message):
            logger.warning(f"Rejected: user {message.author.id} not in allowed list")
            return

        channel = message.channel
        channel_id = channel.id

        # Determine session: check forum task first, then legacy active_sessions
        session_id = None
        session_cwd = None

        task = config_store.get_task(channel_id)
        if task:
            session_id = task.session_id
            session_cwd = task.worktree_path
        else:
            session_id = active_sessions.get(channel_id)
            if session_id:
                session = get_session_by_id(session_id)
                session_cwd = session.cwd if session else None

        if not session_id:
            # In a forum thread without a task? Guide them
            if isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.ForumChannel):
                binding = config_store.get_binding(channel.parent_id)
                if binding:
                    await message.reply(
                        "This thread has no active task. Use `/spawn` in the channel to create one."
                    )
                    return

            await message.reply(
                "No active session. Use `/sessions` to pick one, or `/spawn` in a project channel."
            )
            return

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

        if task:
            async def process_message(msg_text: str):
                await _process_thread_message(bot_ref, channel, message, task.session_id, task.worktree_path, msg_text, task_user_id=task.user_id)
        else:
            async def process_message(msg_text: str):
                await _process_single_message(bot_ref, message, session_id, session_cwd, msg_text)

        depth = await chat_queue.enqueue(channel_id, wrapped, process_message)
        if depth > 0:
            await message.reply(f"Queued (position {depth}). Claude is still working...")


# ── Message processing ───────────────────────────────────


async def _process_thread_message(
    bot: ClaudeBot,
    thread: discord.Thread | discord.abc.Messageable,
    message: discord.Message,
    session_id: str,
    worktree_path: str,
    message_text: str,
    task_user_id: int | None = None,
):
    """Process a message in a forum thread task."""
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
            display = display[:DISCORD_MAX_LEN - 30] + "\n\n...(streaming)"

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
                await message.add_reaction("\U0001f527")
            except Exception:
                pass
        elif not tool_status and tool_active:
            tool_active = False
            try:
                await message.remove_reaction("\U0001f527", bot.user)
                await message.add_reaction("⏳")
            except Exception:
                pass

    async def on_cost_threshold(total: float, threshold: float):
        await message.channel.send(
            f"**Cost alert:** task has spent ${total:.2f} (crossed ${threshold:.0f} threshold)"
        )

    async def on_compaction():
        try:
            await message.remove_reaction("⏳", bot.user)
            await message.remove_reaction("\U0001f527", bot.user)
        except Exception:
            pass
        await message.add_reaction("\U0001f4e6")

    async def on_permission_request(perm: PermissionRequest):
        view = PermissionView(channel_id, perm.request_id)
        preview = perm.preview
        text = f"**Permission needed: {perm.tool_name}**\n```\n{preview}\n```"
        if len(text) > DISCORD_MAX_LEN:
            text = text[:DISCORD_MAX_LEN - 10] + "\n```"
        await message.channel.send(text, view=view)
        await message.add_reaction("\U0001f510")

    git_env = _git_env_for_user(task_user_id) if task_user_id else None

    try:
        async with message.channel.typing():
            response = await bridge.send_message(
                session_id=session_id,
                message=message_text,
                cwd=worktree_path,
                chat_id=channel_id,
                on_delta=on_delta,
                on_cost_threshold=on_cost_threshold,
                on_compaction=on_compaction,
                on_permission_request=on_permission_request,
                git_env=git_env,
            )
    except Exception as e:
        logger.error(f"Bridge error: {e}")
        await reply.edit(content=f"Error: {e}")
        await _swap_reaction(message, bot.user, "❌")
        task = config_store.get_task(channel_id)
        if task:
            config_store.update_task_status(channel_id, "error")
        return

    if response == "(cancelled)":
        try:
            await reply.edit(content="(cancelled)")
        except Exception:
            pass
        await _swap_reaction(message, bot.user, "\U0001f6d1")
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


async def _process_single_message(
    bot: ClaudeBot,
    message: discord.Message,
    session_id: str,
    session_cwd: str | None,
    message_text: str,
):
    """Process a single message through Claude with full streaming UX (legacy single-user)."""
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
                await message.add_reaction("\U0001f527")
            except Exception:
                pass
        elif not tool_status and tool_active:
            tool_active = False
            try:
                await message.remove_reaction("\U0001f527", bot.user)
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
            await message.remove_reaction("\U0001f527", bot.user)
        except Exception:
            pass
        await message.add_reaction("\U0001f4e6")

    async def on_permission_request(perm: PermissionRequest):
        view = PermissionView(channel_id, perm.request_id)
        preview = perm.preview
        text = f"**Permission needed: {perm.tool_name}**\n```\n{preview}\n```"
        if len(text) > DISCORD_MAX_LEN:
            text = text[:DISCORD_MAX_LEN - 10] + "\n```"
        await message.channel.send(text, view=view)
        await message.add_reaction("\U0001f510")

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
        await _swap_reaction(message, bot.user, "\U0001f6d1")
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


def _get_forum_parent_id(interaction: discord.Interaction) -> int | None:
    channel = interaction.channel
    if isinstance(channel, discord.ForumChannel):
        return channel.id
    if isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.ForumChannel):
        return channel.parent_id
    # Not in a forum — try to find the only project in this server
    if interaction.guild:
        matches = [
            cid for cid in config_store._bindings
            if interaction.guild.get_channel(cid) is not None
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def _get_project_binding(interaction: discord.Interaction):
    parent_id = _get_forum_parent_id(interaction)
    if parent_id:
        return config_store.get_binding(parent_id)
    return None


async def _fetch_recent_prs(repo: str, limit: int = 5) -> str:
    """Fetch recent PRs from GitHub using gh CLI."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "list", "--repo", repo, "--limit", str(limit),
            "--json", "number,title,state,author,url",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return ""

        prs = json.loads(stdout.decode())
        if not prs:
            return "No open PRs"

        lines = []
        for pr in prs:
            author = pr.get("author", {}).get("login", "?")
            lines.append(f"- #{pr['number']} {pr['title']} ({author})")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Failed to fetch PRs: {e}")
        return ""


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
            embed.set_footer(text="Send messages to continue this session.")
            await interaction.response.send_message(embed=embed)
            last_msg = get_last_assistant_message(session)
            if last_msg:
                chunks = split_message(last_msg, DISCORD_MAX_LEN)
                for chunk in chunks:
                    await interaction.followup.send(chunk)
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
