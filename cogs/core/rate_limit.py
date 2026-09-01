import asyncio
import contextvars
import heapq
import itertools
from contextlib import asynccontextmanager

import discord


PRIORITY_USER = 0
PRIORITY_NORMAL = 10
PRIORITY_BACKGROUND = 20


class DiscordRequestScheduler:
    """Process-wide priority queue for bot-authenticated Discord REST calls."""

    def __init__(self, requests_per_second: float = 12.0):
        self._interval = 1.0 / max(1.0, requests_per_second)
        self._condition = asyncio.Condition()
        self._queue = []
        self._sequence = itertools.count()
        self._worker_task = None
        self._closed = False
        self._next_slot_at = 0.0
        self._cooldown_until = 0.0
        self._priority = contextvars.ContextVar(
            "discord_request_priority", default=PRIORITY_NORMAL
        )

    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._run(), name="discord-request-scheduler"
            )

    async def close(self) -> None:
        self._closed = True
        async with self._condition:
            self._condition.notify_all()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    def set_current_priority(self, value: int) -> None:
        self._priority.set(value)

    @asynccontextmanager
    async def priority(self, value: int):
        token = self._priority.set(value)
        try:
            yield
        finally:
            self._priority.reset(token)

    async def acquire(self, priority: int | None = None) -> None:
        if self._closed:
            raise RuntimeError("Discord request scheduler is closed")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        item = (
            self._priority.get() if priority is None else priority,
            next(self._sequence),
            future,
        )
        async with self._condition:
            heapq.heappush(self._queue, item)
            self._condition.notify()
        await future

    def report_rate_limit(self, error: Exception) -> float:
        """Pause the whole queue after an unexpected 429 and return the delay."""
        retry_after = None
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if headers:
            retry_after = headers.get("Retry-After")
        try:
            retry_after = float(retry_after)
        except (TypeError, ValueError):
            retry_after = None

        # discord.py normally absorbs ordinary bucket 429s. A raised code-0 429
        # is generally a temporary global/Cloudflare block, so use a safe floor.
        delay = max(retry_after or 0.0, 900.0 if getattr(error, "code", None) == 0 else 60.0)
        loop = asyncio.get_running_loop()
        self._cooldown_until = max(self._cooldown_until, loop.time() + delay)
        return delay

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - asyncio.get_running_loop().time())

    def queued_count(self) -> int:
        return len(self._queue)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._closed:
            async with self._condition:
                await self._condition.wait_for(lambda: self._queue or self._closed)
                if self._closed:
                    return
                _priority, _sequence, future = heapq.heappop(self._queue)

            delay = max(self._next_slot_at, self._cooldown_until) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_slot_at = loop.time() + self._interval
            if not future.cancelled():
                future.set_result(None)


def install_discord_http_scheduler(bot, scheduler: DiscordRequestScheduler) -> None:
    """Route every discord.py bot-token REST request through the scheduler."""
    if getattr(bot, "_scheduled_http_request_installed", False):
        return

    original_request = bot.http.request

    async def scheduled_request(route, *args, **kwargs):
        await scheduler.acquire()
        try:
            return await original_request(route, *args, **kwargs)
        except discord.HTTPException as error:
            if getattr(error, "status", None) == 429:
                delay = scheduler.report_rate_limit(error)
                print(
                    f"⚠️ [Discord API] 全局请求队列收到 429，暂停 {delay:.1f} 秒；"
                    f"当前排队 {scheduler.queued_count()}。"
                )
            raise

    bot.http.request = scheduled_request
    bot._scheduled_http_request_installed = True
