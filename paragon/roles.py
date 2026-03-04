from __future__ import annotations
import discord

from .storage import _gdict

PODIUM_ROLE_SPECS: list[tuple[str, discord.Color]] = [
    ("Gold", discord.Color.gold()),
    ("Silver", discord.Color.light_grey()),
    ("Bronze", discord.Color.from_rgb(205, 127, 50)),
]


async def _ensure_podium_roles(guild: discord.Guild) -> dict[str, discord.Role]:
    out: dict[str, discord.Role] = {}
    me = guild.me
    can_manage = bool(me and guild.me.guild_permissions.manage_roles)

    for name, color in PODIUM_ROLE_SPECS:
        role = discord.utils.get(guild.roles, name=name)
        if role is None and can_manage:
            try:
                role = await guild.create_role(
                    name=name,
                    color=color,
                    mentionable=False,
                    hoist=False,
                    reason="Paragon podium role auto-create",
                )
            except (discord.Forbidden, discord.HTTPException):
                role = None
        if role is not None:
            out[name] = role
    return out


def _top_three_user_ids(guild_id: int) -> list[int]:
    g = _gdict(guild_id)
    users = g.get("users", {})
    rows = []
    for uid_str, u in users.items():
        try:
            uid = int(uid_str)
        except Exception:
            continue
        total = int(u.get("xp_f", u.get("xp", 0)))
        rows.append((uid, total))
    rows.sort(key=lambda t: (-t[1], t[0]))
    return [uid for uid, _ in rows[:3]]


async def sync_podium_roles(guild: discord.Guild):
    roles = await _ensure_podium_roles(guild)
    if not roles:
        return

    top_ids_all = _top_three_user_ids(guild.id)
    top_members = []
    for uid in top_ids_all:
        m = guild.get_member(uid)
        if m and not m.bot:
            top_members.append(m)
        if len(top_members) >= 3:
            break

    order = [name for name, _ in PODIUM_ROLE_SPECS]
    target_for_role: dict[str, int] = {}
    for i, m in enumerate(top_members):
        if i < len(order):
            target_for_role[order[i]] = m.id

    for role_name in order:
        role = roles.get(role_name)
        if role is None:
            continue
        target_id = target_for_role.get(role_name)

        # Remove role from everyone except the target.
        for member in list(role.members):
            if target_id is None or member.id != target_id:
                try:
                    await member.remove_roles(role, reason="Paragon podium refresh")
                except (discord.Forbidden, discord.HTTPException):
                    pass

        # Ensure target has the role.
        if target_id is not None:
            target = guild.get_member(target_id)
            if target and role not in target.roles:
                try:
                    await target.add_roles(role, reason="Paragon podium refresh")
                except (discord.Forbidden, discord.HTTPException):
                    pass


async def get_level_role_map(guild: discord.Guild) -> dict[int, discord.Role]:
    # Legacy API retained for compatibility; level roles are retired.
    return {}


async def sync_level_roles(member: discord.Member, level: int):
    # Level roles are retired for now.
    return


async def enforce_level6_exclusive(guild: discord.Guild):
    # Repurposed: keep Gold/Silver/Bronze in sync with top 3 total XP.
    await sync_podium_roles(guild)


async def announce_level_up(member: discord.Member, new_level: int):
    # Level-up log messages are intentionally disabled.
    return


async def sync_all_roles(guild: discord.Guild):
    await sync_podium_roles(guild)
