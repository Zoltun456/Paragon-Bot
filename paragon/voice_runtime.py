from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

import discord
from discord.ext import commands

from .voice_support import dave_4017_message, is_dave_close_4017


VoiceDebugFn = Callable[[str], None]


async def wait_voice_connected(
    client: discord.VoiceClient,
    *,
    channel_id: int,
    timeout_s: float,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if client.is_connected() and client.channel and client.channel.id == channel_id:
            return True
        await asyncio.sleep(0.1)
    return False


async def cleanup_voice_client(
    client: Optional[discord.VoiceClient],
    *,
    stop: bool = True,
    disconnect: bool = True,
) -> None:
    if client is None:
        return
    try:
        if stop and (client.is_playing() or client.is_paused()):
            client.stop()
    except Exception:
        pass
    if disconnect:
        try:
            await client.disconnect(force=True)
        except Exception:
            pass
    try:
        client.cleanup()
    except Exception:
        pass


async def ensure_voice_client(
    ctx: commands.Context,
    target_channel: discord.VoiceChannel,
    *,
    debug: Optional[VoiceDebugFn] = None,
) -> discord.VoiceClient:
    def _debug(msg: str) -> None:
        if debug is not None:
            debug(msg)

    vc = ctx.voice_client
    if vc is not None and not vc.is_connected():
        _debug("stale voice client detected; forcing disconnect")
        await cleanup_voice_client(vc)
        vc = None

    if vc is None:
        _debug(f"connecting to channel {target_channel.id}")
        try:
            vc = await target_channel.connect(
                timeout=30.0,
                reconnect=True,
                self_deaf=True,
                self_mute=False,
            )
        except discord.ConnectionClosed as e:
            if is_dave_close_4017(e):
                raise RuntimeError(dave_4017_message()) from e
            raise
    elif vc.channel is None or vc.channel.id != target_channel.id:
        _debug(f"moving from {getattr(vc.channel, 'id', None)} to {target_channel.id}")
        try:
            await vc.move_to(target_channel)
        except discord.ConnectionClosed as e:
            if is_dave_close_4017(e):
                raise RuntimeError(dave_4017_message()) from e
            raise

    ok = await wait_voice_connected(vc, channel_id=target_channel.id, timeout_s=15.0)
    if ok:
        _debug("voice client connected")
        return vc

    _debug("voice client not connected after wait; retrying reconnect")
    await cleanup_voice_client(vc)
    try:
        vc = await target_channel.connect(
            timeout=30.0,
            reconnect=True,
            self_deaf=True,
            self_mute=False,
        )
    except discord.ConnectionClosed as e:
        if is_dave_close_4017(e):
            raise RuntimeError(dave_4017_message()) from e
        raise

    ok = await wait_voice_connected(vc, channel_id=target_channel.id, timeout_s=15.0)
    if ok:
        _debug("voice client connected after retry")
        return vc

    await cleanup_voice_client(vc)
    raise RuntimeError("Voice client failed to connect after retry.")
