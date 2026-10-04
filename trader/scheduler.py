from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def run_minutely(cycle: Callable[[], Awaitable[None]], *, interval_seconds: float = 60,
                       stop: asyncio.Event | None = None) -> None:
    """Wall-clock aligned scheduler; catches and records at caller boundary, never overlaps cycles."""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        await cycle()
        delay = interval_seconds - (asyncio.get_running_loop().time() % interval_seconds)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass
