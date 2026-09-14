from __future__ import annotations

import asyncio


_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def economy_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    """Serialize balance/inventory transactions for one guild member."""
    key = (int(guild_id), int(user_id))
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock
