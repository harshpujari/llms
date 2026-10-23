"""A background worker's queue: one item at a time, and never dropping one.

Both workers -- extraction and indexing -- run through this. The database is
the record of what's unfinished; the queue only decides when to work on it.
So an item can't be lost, only delayed:

  - one already waiting isn't queued twice
  - a failure that isn't the item's own fault -- a busy database, a bug, a
    full disk -- retries that item later, on its own clock (30 s, 1 min, 2,
    5, then every 10), while the rest carry on
  - a pause (the model unreachable) holds the whole queue instead, since every
    item would fail the same way
  - every SWEEP_SECONDS the worker's scan of the database runs again, so
    anything that slipped out at runtime comes back without a restart
  - if the loop itself dies, it's started again

What *is* the item's fault -- a scanned PDF, a chunk the model can't
summarize -- is the process function's business: it records that and returns.
"""

# Custom libraries
from logger import configure_logging

# Default libraries
import asyncio
import time
from typing import Any, Callable, Optional

# Installed libraries
from starlette.concurrency import run_in_threadpool

logger = configure_logging(__name__)

# Waits before retrying an item that failed: per attempt, the last repeating.
RETRY_DELAYS = [30, 60, 120, 300, 600]
# Waits while paused: doubling from the first, up to the second.
PAUSE_START = 30
PAUSE_MAX = 300
SWEEP_SECONDS = 120


class RetryLater(Exception):
    """A step that has to succeed didn't -- a write the database refused, say.
    Not the item's fault, so it's retried rather than marked failed."""


def must(ok: bool, what: str) -> None:
    """For the repositories' True/False writes: a write that didn't happen is a
    reason to retry, never to carry on as if it had."""
    if not ok:
        raise RetryLater(f"couldn't save {what}")


class WorkQueue:
    def __init__(
        self,
        name: str,
        process: Callable[[int], Any],
        scan: Callable[[], list[int]],
        pause_on: tuple[type[BaseException], ...] = (),
        on_done: Optional[Callable[[int, Any], None]] = None,
    ):
        """process(item) runs on a worker thread; raising one of pause_on
        pauses the queue, anything else retries the item. scan() lists what
        the database says is unfinished. on_done(item, result) runs on the
        event loop after a success."""
        self.name = name
        self.process = process
        self.scan = scan
        self.pause_on = pause_on
        self.on_done = on_done

        self._queue: Optional[asyncio.Queue] = None
        self._tasks: list[asyncio.Task] = []
        # All touched only on the event loop: enqueue, the loop between its
        # awaits, and the call_later callbacks.
        self._waiting: set[int] = set()
        self._active: Optional[int] = None
        self._retries: dict[int, dict] = {}
        self._paused: Optional[dict] = None
        self._pause_wait = PAUSE_START

    # --- adding work ---------------------------------------------------------

    def enqueue(self, item: int) -> None:
        """Event loop only -- asyncio.Queue isn't thread-safe. Asked for
        explicitly, so a retry it was waiting on happens now instead."""
        if self._queue is None:
            logger.warning("%s worker not running, %s not queued", self.name, item)
            return
        retry = self._retries.get(item)
        if retry and retry["handle"]:
            retry["handle"].cancel()
            retry["handle"] = None
        self._add(item)

    def _add(self, item: int) -> bool:
        if item in self._waiting:
            return False
        self._waiting.add(item)
        self._queue.put_nowait(item)
        return True

    def _scheduled(self, item: int) -> bool:
        retry = self._retries.get(item)
        return bool(retry and retry["handle"])

    async def _sweep(self, startup: bool = False) -> None:
        """The scan, queued -- skipping what's waiting, in progress, or due a
        retry anyway, so a sweep never cuts a backoff short."""
        try:
            items = await run_in_threadpool(self.scan)
        except Exception:
            logger.exception("%s: scan for unfinished work failed", self.name)
            return
        added = sum(
            self._add(item)
            for item in items
            if item != self._active and not self._scheduled(item)
        )
        if added:
            how = "from a previous run" if startup else "that had slipped out of the queue"
            logger.info("%s: queued %d unfinished item(s) %s", self.name, added, how)

    # --- the loop ------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            self._waiting.discard(item)
            self._active = item
            try:
                result = await run_in_threadpool(self.process, item)
            except self.pause_on as exc:
                # Every item would fail the same way: keep this one, hold all.
                self._add(item)
                await self._pause(exc)
            except Exception as exc:
                self._retry_later(item, exc)
            else:
                self._retries.pop(item, None)
                self._pause_wait = PAUSE_START
                if self.on_done:
                    try:
                        self.on_done(item, result)
                    except Exception:
                        logger.exception("%s: after %s finished", self.name, item)
            finally:
                self._active = None
                self._queue.task_done()

    async def _pause(self, exc: Exception) -> None:
        wait = self._pause_wait
        logger.warning("%s paused, retrying in %ds: %s", self.name, wait, exc)
        self._paused = {"reason": str(exc), "retry_at": time.time() + wait}
        try:
            await asyncio.sleep(wait)
        finally:
            self._paused = None
        self._pause_wait = min(wait * 2, PAUSE_MAX)

    def _retry_later(self, item: int, exc: Exception) -> None:
        retry = self._retries.setdefault(item, {"attempts": 0, "handle": None})
        retry["attempts"] += 1
        delay = RETRY_DELAYS[min(retry["attempts"], len(RETRY_DELAYS)) - 1]
        retry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        retry["retry_at"] = time.time() + delay
        retry["handle"] = asyncio.get_running_loop().call_later(delay, self._retry_now, item)
        logger.error(
            "%s failed on %s (attempt %d), retrying in %ds",
            self.name, item, retry["attempts"], delay, exc_info=exc,
        )

    def _retry_now(self, item: int) -> None:
        retry = self._retries.get(item)
        if retry:
            retry["handle"] = None
        self._add(item)

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            await self._sweep()

    def _spawn(self, loop: Callable) -> None:
        task = asyncio.create_task(loop())
        task.add_done_callback(lambda t: self._watch(t, loop))
        self._tasks.append(task)

    def _watch(self, task: asyncio.Task, loop: Callable) -> None:
        """A loop that ended on its own is a bug -- but every later item would
        sit unprocessed until a restart, so it's logged loudly and restarted."""
        self._tasks.remove(task)
        if task.cancelled() or self._queue is None:
            return
        logger.critical("%s: %s stopped (%r), restarting it", self.name, loop.__name__, task.exception())
        self._spawn(loop)

    # --- lifecycle and status --------------------------------------------------

    async def start(self) -> None:
        self._queue = asyncio.Queue()
        self._waiting, self._retries, self._active = set(), {}, None
        self._spawn(self._run)
        self._spawn(self._sweep_forever)
        await self._sweep(startup=True)

    async def stop(self) -> None:
        self._queue = None  # first, so _watch doesn't restart what's cancelled
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        for retry in self._retries.values():
            if retry["handle"]:
                retry["handle"].cancel()
        self._paused = None

    def pending(self) -> int:
        return self._queue.qsize() if self._queue else 0

    def status(self) -> dict:
        now = time.time()
        paused = None
        if self._paused:
            paused = {
                "reason": self._paused["reason"],
                "retry_in": max(0, round(self._paused["retry_at"] - now)),
            }
        retrying = [
            {
                "id": item,
                "attempts": r["attempts"],
                "error": r["error"],
                "retry_in": max(0, round(r["retry_at"] - now)) if r["handle"] else 0,
            }
            for item, r in self._retries.items()
        ]
        return {"queued": self.pending(), "active": self._active, "paused": paused, "retrying": retrying}
