from __future__ import annotations

import discord
from discord.ext import commands

from .config import AUTHOR_USER_ID
from .storage import _gdict, save_data

GUILD_OWNER_KEY = "guild_owner_user_id"


def get_tracked_owner_id(guild_id: int) -> int:
    g = _gdict(guild_id)
    try:
        return int(g.get(GUILD_OWNER_KEY, 0) or 0)
    except (TypeError, ValueError):
        return 0


def resolve_owner_id(guild: discord.Guild | None) -> int:
    if guild is None:
        return 0
    tracked = get_tracked_owner_id(guild.id)
    live = int(getattr(guild, "owner_id", 0) or 0)
    return tracked or live


def is_control_user_id(guild: discord.Guild | None, user_id: int) -> bool:
    if AUTHOR_USER_ID and user_id == AUTHOR_USER_ID:
        return True
    owner_id = resolve_owner_id(guild)
    return bool(owner_id and user_id == owner_id)


def owner_only():
    async def predicate(ctx: commands.Context):
        return is_control_user_id(ctx.guild, ctx.author.id)

    return commands.check(predicate)


async def sync_guild_owner(guild: discord.Guild) -> bool:
    owner_id = int(getattr(guild, "owner_id", 0) or 0)
    if owner_id <= 0:
        return False
    g = _gdict(guild.id)
    old_owner_id = int(g.get(GUILD_OWNER_KEY, 0) or 0)
    if old_owner_id == owner_id:
        return False
    g[GUILD_OWNER_KEY] = owner_id
    await save_data()
    return True


async def sync_all_guild_owners(guilds: list[discord.Guild]) -> int:
    changed = 0
    for guild in guilds:
        owner_id = int(getattr(guild, "owner_id", 0) or 0)
        if owner_id <= 0:
            continue
        g = _gdict(guild.id)
        old_owner_id = int(g.get(GUILD_OWNER_KEY, 0) or 0)
        if old_owner_id != owner_id:
            g[GUILD_OWNER_KEY] = owner_id
            changed += 1
    if changed > 0:
        await save_data()
    return changed
