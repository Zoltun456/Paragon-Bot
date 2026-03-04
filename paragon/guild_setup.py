from __future__ import annotations

from typing import Optional

import discord
from discord.utils import get as dget

from .config import AUTHOR_USER_ID
from .ownership import resolve_owner_id, sync_guild_owner
from .storage import _gdict, save_data

CHANNELS_KEY = "channels"
LOG_CHANNEL_KEY = "log_channel_id"
BLACKJACK_CHANNEL_KEY = "blackjack_channel_id"
OWNER_CHANNEL_KEY = "owner_channel_id"

LOG_CHANNEL_NAME = "paragon-log"
BLACKJACK_CHANNEL_NAME = "paragon-blackjack"
OWNER_CHANNEL_NAME = "paragon-owner"


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _channels_dict(guild_id: int) -> dict:
    g = _gdict(guild_id)
    channels = g.get(CHANNELS_KEY)
    if not isinstance(channels, dict):
        channels = {}
        g[CHANNELS_KEY] = channels
    return channels


def get_log_channel_id(guild_id: int) -> int:
    channels = _channels_dict(guild_id)
    return _as_int(channels.get(LOG_CHANNEL_KEY), 0)


def get_blackjack_channel_id(guild_id: int) -> int:
    channels = _channels_dict(guild_id)
    return _as_int(channels.get(BLACKJACK_CHANNEL_KEY), 0)


def get_owner_channel_id(guild_id: int) -> int:
    channels = _channels_dict(guild_id)
    return _as_int(channels.get(OWNER_CHANNEL_KEY), 0)


def get_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch = guild.get_channel(get_log_channel_id(guild.id))
    return ch if isinstance(ch, discord.TextChannel) else None


def get_blackjack_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch = guild.get_channel(get_blackjack_channel_id(guild.id))
    return ch if isinstance(ch, discord.TextChannel) else None


def get_owner_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch = guild.get_channel(get_owner_channel_id(guild.id))
    return ch if isinstance(ch, discord.TextChannel) else None


def _owner_channel_overwrites(guild: discord.Guild) -> dict:
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }

    me = guild.me
    if me is not None:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )

    owner_id = resolve_owner_id(guild) or _as_int(getattr(guild, "owner_id", 0), 0)
    owner_member = guild.get_member(owner_id) if owner_id > 0 else None
    if owner_member is not None:
        overwrites[owner_member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )

    if AUTHOR_USER_ID > 0:
        author_member = guild.get_member(AUTHOR_USER_ID)
        if author_member is not None:
            overwrites[author_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

    return overwrites


async def _get_or_create_text_channel(
    guild: discord.Guild,
    *,
    stored_id: int,
    name: str,
    overwrites: Optional[dict] = None,
) -> Optional[discord.TextChannel]:
    channel: Optional[discord.TextChannel] = None

    if stored_id > 0:
        by_id = guild.get_channel(stored_id)
        if isinstance(by_id, discord.TextChannel):
            channel = by_id

    if channel is None:
        by_name = dget(guild.text_channels, name=name)
        if isinstance(by_name, discord.TextChannel):
            channel = by_name

    try:
        if channel is None:
            kwargs = {"name": name}
            if overwrites is not None:
                kwargs["overwrites"] = overwrites
            channel = await guild.create_text_channel(**kwargs)
        elif overwrites is not None:
            await channel.edit(overwrites=overwrites, reason="Paragon owner access sync")
    except (discord.Forbidden, discord.HTTPException):
        return None

    return channel


async def ensure_guild_setup(guild: discord.Guild) -> bool:
    changed = await sync_guild_owner(guild)
    channels = _channels_dict(guild.id)

    log_channel = await _get_or_create_text_channel(
        guild,
        stored_id=get_log_channel_id(guild.id),
        name=LOG_CHANNEL_NAME,
    )
    if log_channel and _as_int(channels.get(LOG_CHANNEL_KEY), 0) != log_channel.id:
        channels[LOG_CHANNEL_KEY] = int(log_channel.id)
        changed = True

    blackjack_channel = await _get_or_create_text_channel(
        guild,
        stored_id=get_blackjack_channel_id(guild.id),
        name=BLACKJACK_CHANNEL_NAME,
    )
    if blackjack_channel and _as_int(channels.get(BLACKJACK_CHANNEL_KEY), 0) != blackjack_channel.id:
        channels[BLACKJACK_CHANNEL_KEY] = int(blackjack_channel.id)
        changed = True

    owner_channel = await _get_or_create_text_channel(
        guild,
        stored_id=get_owner_channel_id(guild.id),
        name=OWNER_CHANNEL_NAME,
        overwrites=_owner_channel_overwrites(guild),
    )
    if owner_channel and _as_int(channels.get(OWNER_CHANNEL_KEY), 0) != owner_channel.id:
        channels[OWNER_CHANNEL_KEY] = int(owner_channel.id)
        changed = True

    if changed:
        await save_data()

    return changed
