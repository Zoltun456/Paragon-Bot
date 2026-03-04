# paragon/xpshield.py
from __future__ import annotations
from typing import Optional
from datetime import timedelta

import discord
from discord.ext import commands, tasks
from discord.utils import get as dget

from .config import (
    XP_SHIELD_ROLE_NAME,
    XP_SHIELD_CHECK_MINUTES,
    MAX_XP_SHIELD_MINUTES,
)
from .storage import _udict, _gdict, save_data
from .xp import apply_xp_change
from .roles import announce_level_up, sync_level_roles, enforce_level6_exclusive


def _get_role(guild: discord.Guild) -> Optional[discord.Role]:
    return dget(guild.roles, name=XP_SHIELD_ROLE_NAME)


def _format_minutes(mins: int) -> str:
    mins = max(0, int(mins))
    d, rem = divmod(mins, 60 * 24)
    h, m = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def _xs_state(gid: int, uid: int) -> dict:
    """
    Per-user XP Shield state under user record:
      { "xpshield": { "mins": <int remaining minutes> } }

    Migration:
      If older 'until' timestamp exists, convert to remaining minutes once.
    """
    u = _udict(gid, uid)
    st = u.get("xpshield")
    # Create fresh state
    if st is None:
        st = {"mins": 0}
        u["xpshield"] = st
        return st
    # Migration from old time-based model
    if "mins" not in st:
        st["mins"] = 0
    if "until" in st:
        # Best-effort migration: compute remaining minutes, clamp to cap, then drop 'until'
        try:
            from datetime import datetime, timezone
            def _parse_iso(s: Optional[str]):
                if not s: return None
                try:
                    dt = datetime.fromisoformat(s)
                    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                except Exception:
                    return None
            now = datetime.now(timezone.utc)
            until = _parse_iso(st.get("until"))
            if until and until > now:
                remain = int((until - now).total_seconds() // 60)
                st["mins"] = min(MAX_XP_SHIELD_MINUTES, max(st.get("mins", 0), remain))
        except Exception:
            pass
        st.pop("until", None)
    return st


# -------- Used by core.award_loop() to absorb inactive penalties --------
async def consume_xps_minutes(member: discord.Member, penalty_minutes: int) -> int:
    """
    Consume up to `penalty_minutes` from the member's XP Shield balance.
    Returns the number of minutes absorbed (0..penalty_minutes).
    If balance drops to 0, removes the XP Shield role.
    """
    if penalty_minutes <= 0:
        return 0
    st = _xs_state(member.guild.id, member.id)
    have = int(st.get("mins", 0))
    if have <= 0:
        return 0
    use = min(have, penalty_minutes)
    st["mins"] = have - use
    await save_data()

    # Remove role if balance is 0
    if st["mins"] <= 0:
        role = _get_role(member.guild)
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason="XP Shield consumed")
            except (discord.Forbidden, discord.HTTPException):
                pass
    return use


class XPShieldCog(commands.Cog):
    """
    !xpshield <minutes> / !xs <minutes>  -> spend XP (1 XP = 1 minute) to start/extend XP Shield (caps at 24h)
    !xs                                   -> show your remaining minutes
    !xs @user                             -> show someone else's remaining minutes

    XP Shield minutes are only consumed when an inactive XP penalty would apply.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not self._sweeper.is_running():
            self._sweeper.start()

    @tasks.loop(minutes=XP_SHIELD_CHECK_MINUTES)
    async def _sweeper(self):
        """
        Keep the XP Shield role in sync with whether mins > 0.
        No countdown here — minutes are only consumed during penalties.
        """
        for guild in self.bot.guilds:
            role = _get_role(guild)
            if not role:
                continue
            g = _gdict(guild.id)
            for uid_str, u in g.get("users", {}).items():
                st = u.get("xpshield", {})
                mins = int(st.get("mins", 0)) if isinstance(st, dict) else 0
                m = guild.get_member(int(uid_str))
                if not m:
                    continue
                has_role = role in m.roles
                if mins > 0 and not has_role:
                    try:
                        await m.add_roles(role, reason="XP Shield active")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                if mins <= 0 and has_role:
                    try:
                        await m.remove_roles(role, reason="XP Shield expired")
                    except (discord.Forbidden, discord.HTTPException):
                        pass

    @commands.command(name="xpshield", aliases=["xs"])
    async def xpshield(self, ctx: commands.Context, arg: Optional[str] = None):
        guild = ctx.guild
        author: discord.Member = ctx.author  # type: ignore
        role = _get_role(guild)
        if not role:
            await ctx.reply(f"I can’t find a **{XP_SHIELD_ROLE_NAME}** role. Please create it first.")
            return

        # View mode: !xs  OR  !xs @user
        if arg is None or (arg.startswith("<@") and ctx.message.mentions):
            target = author if arg is None else ctx.message.mentions[0]
            st = _xs_state(guild.id, target.id)
            mins = int(st.get("mins", 0))
            if mins > 0:
                await ctx.reply(f"🛡️ **{target.display_name}** has **{_format_minutes(mins)}** of {XP_SHIELD_ROLE_NAME} remaining.")
            else:
                await ctx.reply(f"🛡️ **{target.display_name}** does not have an active {XP_SHIELD_ROLE_NAME}.")
            return

        # Spend mode: !xs <minutes>
        try:
            minutes = int(arg.replace(",", "").strip())
        except ValueError:
            await ctx.reply(
                "Usage:\n"
                "`!xs <minutes>` to buy/extend (1 XP = 1 minute, up to 24h total)\n"
                "`!xs` to view your time\n"
                "`!xs @user` to view another user’s time."
            )
            return
        if minutes <= 0:
            await ctx.reply("Minutes must be a positive number.")
            return

        st = _xs_state(guild.id, author.id)
        have = int(st.get("mins", 0))
        # Cap so total does not exceed MAX_XP_SHIELD_MINUTES
        addable = min(minutes, max(0, MAX_XP_SHIELD_MINUTES - have))
        if addable <= 0:
            await ctx.reply(f"You’re already at the **{XP_SHIELD_ROLE_NAME}** cap of {_format_minutes(MAX_XP_SHIELD_MINUTES)}.")
            return

        # Check XP funds
        u = _udict(guild.id, author.id)
        cur_xp = int(u.get("xp_f", u.get("xp", 0)))
        cost = addable
        if cur_xp < cost:
            await ctx.reply(f"You only have **{cur_xp} XP** but need **{cost} XP** to buy {addable} minute(s).")
            return

        # Deduct XP and sync roles
        changed = await apply_xp_change(author, -cost, source="xp shield purchase")
        if changed:
            old_lvl, new_lvl = changed
            if new_lvl > old_lvl:
                await announce_level_up(author, new_lvl)
            await sync_level_roles(author, new_lvl)
        else:
            await sync_level_roles(author, int(u.get("level", 1)))
        await enforce_level6_exclusive(guild)

        # Add minutes and persist
        st["mins"] = have + addable
        await save_data()

        # Ensure role present
        if role not in author.roles:
            try:
                await author.add_roles(role, reason=f"{XP_SHIELD_ROLE_NAME} purchased: +{addable}m")
            except (discord.Forbidden, discord.HTTPException):
                await ctx.reply("I couldn’t add the role (check my role hierarchy/permissions).")
                return

        await ctx.reply(
            f"🛡️ Added **{_format_minutes(addable)}** to your {XP_SHIELD_ROLE_NAME}. "
            f"New remaining time: **{_format_minutes(st['mins'])}**."
        )
