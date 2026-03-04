from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class AIReply:
    text: str
    model: str
    latency_ms: int


class AIService:
    """
    Stub interface for future AI chat/call response integration.
    """

    async def generate_reply(self, *, guild_id: int, user_id: int, prompt: str) -> Optional[AIReply]:
        _ = guild_id, user_id, prompt
        return None
