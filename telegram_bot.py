"""Telegram bot that bridges to Claude Code sessions."""

import asyncio
import logging
import os
import tempfile
import time

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bridge import ClaudeBridge, PermissionRequest, wrap_channel_message
from sessions import list_sessions, get_session_by_id, get_last_assistant_message
from formatter import (
    format_telegram_html,
    split_message,
    TELEGRAM_MAX_LEN,
)
from message_queue import ChatQueue

load_dotenv()

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions")

active_sessions: dict[int, str] = {}  # chat_id -> session_id

bridge = ClaudeBridge(
    claude_bin=CLAUDE_BIN,
    permission_mode=CLAUDE_PERMISSION_MODE,
)

chat_queue = ChatQueue()

EDIT_INTERVAL_INITIAL = 0.8
EDIT_INTERVAL_STEADY = 2.0
EDIT_INTERVAL_RAMPUP_CHARS = 500


def auth(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
            logger.warning(f"Unauthorized access attempt from user {user_id}")
            await update.effective_message.reply_text("Unauthorized.")
            return
        return await func(update, context)
    return wrapper


# ── Commands ──────────────────────────────────────────────


@auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Claude Code Bridge\n\n"
        "Commands:\n"
        "/sessions - List and pick a session\n"
        "/current - Show current active session\n"
        "/detach - Detach from current session\n"
        "/new <cwd> - Start a new session\n"
        "/cancel - Cancel the current operation\n"
        "/cost - Show session cost so far\n\n"
        "Send text, photos, or files to interact with Claude."
    )


@auth
async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions = list_sessions()
    if not sessions:
        await update.message.reply_text("No Claude Code sessions found.")
        return

    sessions = sessions[:20]

    keyboard = []
    text_lines = ["Pick a session:\n"]
    for i, s in enumerate(sessions):
        label = f"{s.short_id} | {s.display_name}"
        if len(label) > 60:
            label = label[:57] + "..."
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"pick:{s.session_id}")]
        )
        cwd_short = s.cwd
        ts = s.timestamp[:16].replace("T", " ") if s.timestamp else "?"
        text_lines.append(f"{i+1}. [{s.short_id}] {cwd_short}\n   {s.display_name}\n   {ts}")

    await update.message.reply_text(
        "\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@auth
async def cmd_current(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sid = active_sessions.get(chat_id)
    if not sid:
        await update.message.reply_text("No active session. Use /sessions to pick one.")
        return

    session = get_session_by_id(sid)
    cost = bridge.cost_tracker.get(sid)
    if session:
        await update.message.reply_text(
            f"Active session: {session.short_id}\n"
            f"Directory: {session.cwd}\n"
            f"Topic: {session.display_name}\n"
            f"Session cost: ${cost:.4f}"
        )
    else:
        await update.message.reply_text(
            f"Active session: {sid[:8]} (details unavailable)\n"
            f"Session cost: ${cost:.4f}"
        )


@auth
async def cmd_detach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_sessions:
        old = active_sessions.pop(chat_id)
        await update.message.reply_text(f"Detached from session {old[:8]}.")
    else:
        await update.message.reply_text("No active session.")


@auth
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cancelled = await bridge.cancel(chat_id)
    chat_queue.clear(chat_id)
    if cancelled:
        await update.message.reply_text("Cancelled.")
        await _set_reaction(update.message, "🛑")
    else:
        await update.message.reply_text("Nothing running to cancel.")


@auth
async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sid = active_sessions.get(chat_id)
    if not sid:
        await update.message.reply_text("No active session.")
        return

    cost = bridge.cost_tracker.get(sid)
    await update.message.reply_text(f"Session cost so far: ${cost:.4f}")


@auth
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import json

    args = context.args
    cwd = " ".join(args) if args else os.path.expanduser("~")

    if not os.path.isdir(cwd):
        await update.message.reply_text(f"Directory not found: {cwd}")
        return

    msg = await update.message.reply_text(f"Starting new session in {cwd}...")

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
            active_sessions[update.effective_chat.id] = session_id
            await msg.edit_text(
                f"New session created: {session_id[:8]}\n"
                f"Directory: {cwd}\n\n"
                "Send messages to start working."
            )
            return
    except (json.JSONDecodeError, KeyError):
        pass

    await msg.edit_text(
        f"Session started in {cwd} but couldn't capture session ID.\n"
        "Use /sessions to find and select it."
    )


@auth
async def callback_pick_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("pick:"):
        return

    session_id = data[5:]
    chat_id = update.effective_chat.id
    active_sessions[chat_id] = session_id

    session = get_session_by_id(session_id)
    if session:
        text = (
            f"Connected to session {session.short_id}\n"
            f"Directory: {session.cwd}\n"
            f"Topic: {session.display_name}\n\n"
        )
        last_msg = get_last_assistant_message(session)
        if last_msg:
            text += f"Last message:\n\n{last_msg}"
        else:
            text += "Send messages to continue this session."
        await query.edit_message_text(text)
    else:
        await query.edit_message_text(
            f"Connected to session {session_id[:8]}.\nSend messages to continue."
        )


# ── Permission handling ────────────────────────────────────


@auth
async def callback_permission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle permission approve/deny inline button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data  # "perm:allow:<request_id>" or "perm:deny:<request_id>"
    parts = data.split(":", 2)
    if len(parts) != 3:
        return

    _, action, request_id = parts
    chat_id = update.effective_chat.id
    allow = action == "allow"

    success = await bridge.respond_permission(
        chat_id=chat_id,
        request_id=request_id,
        allow=allow,
    )

    if success:
        verdict = "Approved" if allow else "Denied"
        await query.edit_message_text(f"{verdict}: {query.message.text}")
    else:
        await query.edit_message_text("Permission request expired or already handled.")


# ── Message handling ──────────────────────────────────────


@auth
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session_id = active_sessions.get(chat_id)

    if not session_id:
        await update.message.reply_text(
            "No active session. Use /sessions to pick one, or /new <dir> to start fresh."
        )
        return

    session = get_session_by_id(session_id)
    session_cwd = session.cwd if session else None

    user_text = update.message.text or update.message.caption or ""

    file_paths = await _download_attachments(update, context, session_cwd)
    if file_paths:
        file_refs = " ".join(f'@"{p}"' for p in file_paths)
        user_text = f"{file_refs}\n\n{user_text}" if user_text else file_refs

    if not user_text:
        await update.message.reply_text("Send text, a photo, or a file.")
        return

    # Wrap in <channel> XML for source awareness
    username = update.effective_user.username or update.effective_user.first_name or ""
    wrapped = wrap_channel_message(
        content=user_text,
        source="telegram",
        user=username,
        chat_id=str(chat_id),
    )

    # Enqueue — if Claude is busy, the message waits
    async def process_message(msg_text: str):
        await _process_single_message(update, context, session_id, session_cwd, msg_text)

    depth = await chat_queue.enqueue(chat_id, wrapped, process_message)
    if depth > 0:
        await update.message.reply_text(f"Queued (position {depth}). Claude is still working...")


async def _process_single_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: str,
    session_cwd: str | None,
    message_text: str,
):
    """Process a single message through Claude with full streaming UX."""
    chat_id = update.effective_chat.id

    await update.effective_chat.send_action(ChatAction.TYPING)

    reply = await update.message.reply_text("...")
    await _set_reaction(update.message, "⏳")

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
            display = tool_status + "\n\n" + display

        if len(display) > TELEGRAM_MAX_LEN:
            display = display[: TELEGRAM_MAX_LEN - 30] + "\n\n...(streaming)"

        if display == last_edit_text:
            return

        try:
            await reply.edit_text(display)
            last_edit_time = now
            last_edit_text = display
        except Exception:
            pass

        if tool_status and not tool_active:
            tool_active = True
            await _set_reaction(update.message, "🔧")
        elif not tool_status and tool_active:
            tool_active = False
            await _set_reaction(update.message, "⏳")

    async def on_cost_threshold(total: float, threshold: float):
        await update.effective_chat.send_message(
            f"Cost alert: session has spent ${total:.2f} (crossed ${threshold:.0f} threshold)"
        )

    async def on_compaction():
        await _set_reaction(update.message, "📦")

    async def on_permission_request(perm: PermissionRequest):
        preview = perm.preview
        text = f"Permission needed: {perm.tool_name}\n{preview}"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Allow", callback_data=f"perm:allow:{perm.request_id}"),
                InlineKeyboardButton("Deny", callback_data=f"perm:deny:{perm.request_id}"),
            ]
        ])
        await update.effective_chat.send_message(text, reply_markup=keyboard)
        await _set_reaction(update.message, "🔐")

    typing_task = asyncio.create_task(_keep_typing(update.effective_chat))

    try:
        response = await bridge.send_message(
            session_id=session_id,
            message=message_text,
            cwd=session_cwd,
            chat_id=chat_id,
            on_delta=on_delta,
            on_cost_threshold=on_cost_threshold,
            on_compaction=on_compaction,
            on_permission_request=on_permission_request,
        )
    finally:
        typing_task.cancel()

    if response == "(cancelled)":
        try:
            await reply.edit_text("(cancelled)")
        except Exception:
            pass
        await _set_reaction(update.message, "🛑")
        return

    # Format and send the final response as properly formatted HTML chunks
    chunks = format_telegram_html(response)

    if not chunks:
        await _set_reaction(update.message, "❌")
        return

    try:
        await reply.edit_text(chunks[0].text, parse_mode=chunks[0].parse_mode)
    except Exception:
        chunks = [
            type(chunks[0])(text=c)
            for c in split_message(response, TELEGRAM_MAX_LEN)
        ]
        try:
            await reply.edit_text(chunks[0].text)
        except Exception:
            await update.message.reply_text(chunks[0].text)

    for chunk in chunks[1:]:
        try:
            await update.message.reply_text(chunk.text, parse_mode=chunk.parse_mode)
        except Exception:
            await update.message.reply_text(chunk.text)

    await _set_reaction(update.message, "✅")


# ── Helpers ───────────────────────────────────────────────


async def _download_attachments(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_cwd: str | None
) -> list[str]:
    """Download photos/documents from a Telegram message and return local paths."""
    msg = update.message
    files_to_download = []

    if msg.photo:
        files_to_download.append((msg.photo[-1].file_id, "photo.jpg"))
    if msg.document:
        name = msg.document.file_name or "document"
        files_to_download.append((msg.document.file_id, name))

    if not files_to_download:
        return []

    dest_dir = session_cwd or tempfile.gettempdir()
    paths = []
    for file_id, filename in files_to_download:
        try:
            tg_file = await context.bot.get_file(file_id)
            dest = os.path.join(dest_dir, filename)
            await tg_file.download_to_drive(dest)
            paths.append(dest)
            logger.info(f"Downloaded attachment to {dest}")
        except Exception as e:
            logger.error(f"Failed to download attachment: {e}")

    return paths


async def _set_reaction(message, emoji: str):
    try:
        from telegram import ReactionTypeEmoji
        await message.set_reaction(ReactionTypeEmoji(emoji))
    except Exception:
        pass


async def _keep_typing(chat, interval: float = 8.0):
    try:
        while True:
            await chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass


# ── App setup ─────────────────────────────────────────────


def create_telegram_app() -> Application:
    if not BOT_TOKEN or BOT_TOKEN == "your-bot-token-here":
        raise ValueError("Set TELEGRAM_BOT_TOKEN in .env")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("current", cmd_current))
    app.add_handler(CommandHandler("detach", cmd_detach))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CallbackQueryHandler(callback_pick_session, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(callback_permission, pattern=r"^perm:"))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
        handle_message,
    ))

    return app


def main():
    app = create_telegram_app()
    logger.info("Telegram bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    main()
