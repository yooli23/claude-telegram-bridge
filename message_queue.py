"""Per-chat async message queue — buffers messages while Claude is busy."""

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class QueuedMessage:
    text: str
    notify: asyncio.Future | None = None  # resolved with queue position, or None


class ChatQueue:
    """Manages a per-chat message queue with a single worker processing messages."""

    def __init__(self):
        self._queues: dict[int, asyncio.Queue] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._processors: dict[int, object] = {}  # chat_id -> processor callback

    def _get_queue(self, chat_id: int) -> asyncio.Queue:
        if chat_id not in self._queues:
            self._queues[chat_id] = asyncio.Queue()
        return self._queues[chat_id]

    async def enqueue(self, chat_id: int, text: str, processor) -> int:
        """Add a message to the chat's queue. Returns 0 if processing immediately, else queue position."""
        q = self._get_queue(chat_id)
        already_busy = chat_id in self._workers and not self._workers[chat_id].done()

        q.put_nowait((text, processor))

        if not already_busy:
            self._workers[chat_id] = asyncio.create_task(self._worker(chat_id))
            return 0

        return q.qsize()

    async def _worker(self, chat_id: int):
        q = self._get_queue(chat_id)
        while not q.empty():
            text, processor = await q.get()
            try:
                await processor(text)
            except Exception as e:
                logger.error(f"Queue processor error for chat {chat_id}: {e}")
            finally:
                q.task_done()

    def pending_count(self, chat_id: int) -> int:
        if chat_id not in self._queues:
            return 0
        return self._queues[chat_id].qsize()

    def clear(self, chat_id: int):
        """Drop all pending messages for a chat."""
        if chat_id in self._queues:
            q = self._queues[chat_id]
            while not q.empty():
                try:
                    q.get_nowait()
                    q.task_done()
                except asyncio.QueueEmpty:
                    break
