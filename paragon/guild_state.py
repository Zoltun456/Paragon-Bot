from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

import discord

from .config import AUTHOR_USER_ID, LOCAL_TZ
from .guild_setup import (
    get_blackjack_channel_id,
    get_fishing_channel_id,
    get_log_channel_id,
    get_owner_channel_id,
)
from .include import _as_dict, _as_float, _as_int, _iso, _parse_iso, _utcnow
from .ownership import resolve_owner_id
from .storage import _gdict, save_data

BOT_ENABLED_KEY = "bot_enabled"
BOT_DISABLED_AT_KEY = "bot_disabled_at"
BOT_PAUSED_SECONDS_KEY = "bot_paused_seconds"
BOT_CHANNEL_SNAPSHOTS_KEY = "bot_channel_snapshots"


def guild_settings(guild_id: int) -> dict:
    g = _gdict(int(guild_id))
    st = g.get("settings")
    if not isinstance(st, dict):
        st = {}
        g["settings"] = st
    if "inactive_loss_enabled" not in st:
        st["inactive_loss_enabled"] = True
    if BOT_ENABLED_KEY not in st:
        st[BOT_ENABLED_KEY] = True
    if BOT_DISABLED_AT_KEY not in st:
        st[BOT_DISABLED_AT_KEY] = ""
    if BOT_PAUSED_SECONDS_KEY not in st:
        st[BOT_PAUSED_SECONDS_KEY] = 0.0
    snapshots = st.get(BOT_CHANNEL_SNAPSHOTS_KEY)
    if not isinstance(snapshots, dict):
        snapshots = {}
        st[BOT_CHANNEL_SNAPSHOTS_KEY] = snapshots
    return st


def _guild_id(guild_or_id) -> int:
    if isinstance(guild_or_id, discord.Guild):
        return int(guild_or_id.id)
    try:
        return int(guild_or_id)
    except Exception:
        return 0


def is_guild_enabled(guild_or_id) -> bool:
    gid = _guild_id(guild_or_id)
    if gid <= 0:
        return True
    st = guild_settings(gid)
    return bool(st.get(BOT_ENABLED_KEY, True))


def _disabled_at_dt(guild_id: int) -> Optional[datetime]:
    raw = str(guild_settings(guild_id).get(BOT_DISABLED_AT_KEY, "")).strip()
    if not raw:
        return None
    return _parse_iso(raw)


def paused_seconds(guild_or_id) -> float:
    gid = _guild_id(guild_or_id)
    if gid <= 0:
        return 0.0
    st = guild_settings(gid)
    total = max(0.0, _as_float(st.get(BOT_PAUSED_SECONDS_KEY, 0.0), 0.0))
    if bool(st.get(BOT_ENABLED_KEY, True)):
        return total
    disabled_at = _disabled_at_dt(gid)
    if disabled_at is None:
        return total
    elapsed = max(0.0, (_utcnow() - disabled_at).total_seconds())
    return total + elapsed


def effective_utcnow(guild_or_id) -> datetime:
    now = _utcnow()
    offset = paused_seconds(guild_or_id)
    if offset <= 0.0:
        return now
    return now - timedelta(seconds=offset)


def effective_unix_ts(guild_or_id) -> int:
    return int(effective_utcnow(guild_or_id).timestamp())


def effective_local_now(guild_or_id) -> datetime:
    return effective_utcnow(guild_or_id).astimezone(LOCAL_TZ)


def effective_date_key(guild_or_id) -> str:
    return effective_local_now(guild_or_id).strftime("%Y-%m-%d")


def effective_yesterday_key(guild_or_id) -> str:
    return (effective_local_now(guild_or_id) - timedelta(days=1)).strftime("%Y-%m-%d")


def current_disabled_elapsed_seconds(guild_or_id) -> int:
    gid = _guild_id(guild_or_id)
    if gid <= 0 or is_guild_enabled(gid):
        return 0
    disabled_at = _disabled_at_dt(gid)
    if disabled_at is None:
        return 0
    return max(0, int(round((_utcnow() - disabled_at).total_seconds())))


async def mark_guild_disabled(guild: discord.Guild) -> bool:
    st = guild_settings(guild.id)
    if not bool(st.get(BOT_ENABLED_KEY, True)):
        if not str(st.get(BOT_DISABLED_AT_KEY, "")).strip():
            st[BOT_DISABLED_AT_KEY] = _iso(_utcnow())
            await save_data()
        return False
    st[BOT_ENABLED_KEY] = False
    st[BOT_DISABLED_AT_KEY] = _iso(_utcnow())
    await save_data()
    return True


async def mark_guild_enabled(guild: discord.Guild) -> int:
    st = guild_settings(guild.id)
    if bool(st.get(BOT_ENABLED_KEY, True)):
        st[BOT_DISABLED_AT_KEY] = ""
        await save_data()
        return 0

    now = _utcnow()
    disabled_at = _disabled_at_dt(guild.id)
    elapsed = max(0.0, (now - disabled_at).total_seconds()) if disabled_at is not None else 0.0
    st[BOT_PAUSED_SECONDS_KEY] = max(0.0, _as_float(st.get(BOT_PAUSED_SECONDS_KEY, 0.0), 0.0)) + elapsed
    st[BOT_DISABLED_AT_KEY] = ""
    st[BOT_ENABLED_KEY] = True
    await save_data()
    return max(0, int(round(elapsed)))


def _managed_channel_ids(guild: discord.Guild) -> list[int]:
    ids = {
        int(get_log_channel_id(guild.id) or 0),
        int(get_blackjack_channel_id(guild.id) or 0),
        int(get_fishing_channel_id(guild.id) or 0),
        int(get_owner_channel_id(guild.id) or 0),
    }

    g = _gdict(guild.id)
    boss = _as_dict(g.get("boss"))
    current = _as_dict(boss.get("current"))
    boss_channel_id = _as_int(current.get("channel_id", 0), 0)
    if boss_channel_id > 0:
        ids.add(boss_channel_id)

    snapshots = _as_dict(guild_settings(guild.id).get(BOT_CHANNEL_SNAPSHOTS_KEY))
    for channel_id in snapshots.keys():
        parsed = _as_int(channel_id, 0)
        if parsed > 0:
            ids.add(parsed)

    return sorted(channel_id for channel_id in ids if channel_id > 0)


def managed_text_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    channels: list[discord.TextChannel] = []
    for channel_id in _managed_channel_ids(guild):
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            channels.append(channel)
    return channels


def _snapshot_store(guild_id: int) -> dict:
    return _as_dict(guild_settings(guild_id).get(BOT_CHANNEL_SNAPSHOTS_KEY))


def _clone_overwrite(overwrite: discord.PermissionOverwrite) -> discord.PermissionOverwrite:
    allow, deny = overwrite.pair()
    return discord.PermissionOverwrite.from_pair(allow, deny)


def _serialize_overwrites(channel: discord.TextChannel) -> list[dict[str, int | str]]:
    rows: list[dict[str, int | str]] = []
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Role):
            kind = "role"
        elif isinstance(target, discord.Member):
            kind = "member"
        else:
            continue
        allow, deny = overwrite.pair()
        rows.append(
            {
                "kind": kind,
                "id": int(target.id),
                "allow": int(allow.value),
                "deny": int(deny.value),
            }
        )
    return rows


def _deserialize_overwrites(guild: discord.Guild, payload: Iterable[dict]) -> dict:
    overwrites: dict = {}
    for raw in payload:
        row = _as_dict(raw)
        target_id = _as_int(row.get("id", 0), 0)
        if target_id <= 0:
            continue
        kind = str(row.get("kind", "")).strip().lower()
        if kind == "role":
            target = guild.get_role(target_id)
        elif kind == "member":
            target = guild.get_member(target_id)
        else:
            target = None
        if target is None:
            continue
        allow = discord.Permissions(_as_int(row.get("allow", 0), 0))
        deny = discord.Permissions(_as_int(row.get("deny", 0), 0))
        overwrites[target] = discord.PermissionOverwrite.from_pair(allow, deny)
    return overwrites


def _protected_members(guild: discord.Guild) -> list[discord.Member]:
    ids = set()
    me = guild.me
    if me is not None:
        ids.add(int(me.id))
    owner_id = resolve_owner_id(guild)
    if owner_id > 0:
        ids.add(int(owner_id))
    if AUTHOR_USER_ID > 0:
        ids.add(int(AUTHOR_USER_ID))

    out: list[discord.Member] = []
    for user_id in sorted(ids):
        member = guild.get_member(user_id)
        if member is not None:
            out.append(member)
    return out


def _hidden_overwrites(guild: discord.Guild, channel: discord.TextChannel) -> dict:
    current = {target: _clone_overwrite(overwrite) for target, overwrite in channel.overwrites.items()}
    protected = _protected_members(guild)
    protected_ids = {int(member.id) for member in protected}

    for role in guild.roles:
        overwrite = _clone_overwrite(current.get(role, discord.PermissionOverwrite()))
        overwrite.view_channel = False
        overwrite.send_messages = False
        overwrite.read_message_history = False
        current[role] = overwrite

    for target in list(current.keys()):
        if not isinstance(target, discord.Member) or int(target.id) in protected_ids:
            continue
        overwrite = _clone_overwrite(current.get(target, discord.PermissionOverwrite()))
        overwrite.view_channel = False
        overwrite.send_messages = False
        overwrite.read_message_history = False
        current[target] = overwrite

    for member in protected:
        overwrite = _clone_overwrite(current.get(member, discord.PermissionOverwrite()))
        overwrite.view_channel = True
        overwrite.send_messages = True
        overwrite.read_message_history = True
        current[member] = overwrite

    return current


async def hide_managed_channels(guild: discord.Guild) -> int:
    changed = 0
    snapshots = _snapshot_store(guild.id)
    for channel in managed_text_channels(guild):
        snapshots[str(channel.id)] = _serialize_overwrites(channel)
        try:
            await channel.edit(
                overwrites=_hidden_overwrites(guild, channel),
                reason="Paragon disabled for this guild",
            )
            changed += 1
        except (discord.Forbidden, discord.HTTPException):
            continue
    await save_data()
    return changed


async def restore_managed_channels(guild: discord.Guild) -> int:
    changed = 0
    snapshots = _snapshot_store(guild.id)
    for channel_id in list(snapshots.keys()):
        channel = guild.get_channel(_as_int(channel_id, 0))
        if not isinstance(channel, discord.TextChannel):
            snapshots.pop(channel_id, None)
            continue
        try:
            await channel.edit(
                overwrites=_deserialize_overwrites(guild, snapshots.get(channel_id, [])),
                reason="Paragon re-enabled for this guild",
            )
            changed += 1
        except (discord.Forbidden, discord.HTTPException):
            continue
        snapshots.pop(channel_id, None)
    await save_data()
    return changed
