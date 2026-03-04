# paragon/permshield.py
from __future__ import annotations
from typing import Optional
from datetime import datetime, timedelta, timezone
from .roles import announce_level_up, sync_level_roles, enforce_level6_exclusive

import discord
from discord.ext import commands, tasks
from discord.utils import get as dget

from .config import (
    PERM_SHIELD_ROLE_NAME,
    PERM_SHIELD_CHECK_MINUTES,
    MAX_PERM_SHIELD_MINUTES,
)
from .storage import _udict, _gdict, save_data
from .xp import apply_xp_change
from .ownership import owner_only


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _ps_state(gid: int, uid: int) -> dict:
    """
    Per-user Perm Shield state stored under user record:
      { "permshield": { "until": "<iso8601 or ''>" } }
    """
    u = _udict(gid, uid)  # ensure user exists
    st = u.get("permshield")
    if st is None:
        st = {"until": ""}
        u["permshield"] = st
    if "until" not in st:
        st["until"] = ""
    return st


def _get_role(guild: discord.Guild) -> Optional[discord.Role]:
    return dget(guild.roles, name=PERM_SHIELD_ROLE_NAME)


def _remaining_str(remaining: timedelta) -> str:
    secs = int(max(0, remaining.total_seconds()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


class PermShieldCog(commands.Cog):
    """
    !permshield <minutes> / !ps <minutes>  -> spend XP (1 XP = 1 minute) to start/extend Perm Shield
    !ps                                    -> show your remaining time
    !ps @user                              -> show another user's remaining time

    Role named PERM_SHIELD_ROLE_NAME must exist on the server.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not self._sweeper.is_running():
            self._sweeper.start()

    # --------- Background: remove role when expired ----------
    @tasks.loop(minutes=PERM_SHIELD_CHECK_MINUTES)
    async def _sweeper(self):
        now = _utcnow()
        for guild in self.bot.guilds:
            role = _get_role(guild)
            if not role:  # role missing; nothing to manage
                continue
            g = _gdict(guild.id)
            users = g.get("users", {})
            for uid_str, u in list(users.items()):
                st = u.get("permshield", {})
                until = _parse_iso(st.get("until"))
                if not until:
                    continue
                if now >= until:
                    # Expired—clear state and remove role if present
                    st["until"] = ""
                    await save_data()
                    m = guild.get_member(int(uid_str))
                    if m and role in m.roles:
                        try:
                            await m.remove_roles(role, reason="Perm Shield expired")
                        except (discord.Forbidden, discord.HTTPException):
                            pass

    # ------------- Admin Set Command -------------
    @commands.command(name="psset")
    @owner_only()
    async def psset(self, ctx: commands.Context, target: discord.Member = None, minutes: int = None):
        """
        Admin/Owner: Set a user's Perm Shield remaining time to <minutes>.
        - minutes > 0: sets shield to exactly <minutes> from now (replacing any existing time)
        - minutes = 0: clears shield and removes the role
        Usage: !psset @user <minutes>
        """


        role = _get_role(ctx.guild)
        if target is None or minutes is None:
            await ctx.reply("Usage: `!psset @user <minutes>` (use 0 to clear).")
            return
        if minutes < 0:
            await ctx.reply("Minutes must be zero or a positive number.")
            return

        st = _ps_state(ctx.guild.id, target.id)
        now = _utcnow()

        if minutes == 0:
            # Clear shield and remove role if present
            st["until"] = ""
            await save_data()
            if role and role in target.roles:
                try:
                    await target.remove_roles(role, reason="Perm Shield cleared by admin")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            await ctx.reply(f"🛡️ Cleared {PERM_SHIELD_ROLE_NAME} for **{target.display_name}**.")
            return

        # ✅ Apply cap from config
        capped_minutes = min(minutes, MAX_PERM_SHIELD_MINUTES)
        new_until = now + timedelta(minutes=capped_minutes)
        st["until"] = _iso(new_until)
        await save_data()

        # Ensure role present
        if role and role not in target.roles:
            try:
                await target.add_roles(role, reason=f"Perm Shield set by admin: {capped_minutes}m")
            except (discord.Forbidden, discord.HTTPException):
                await ctx.reply("I couldn’t add the role (check my role hierarchy/permissions).")
                return

        await ctx.reply(
            f"🛡️ Set **{target.display_name}** {PERM_SHIELD_ROLE_NAME} "
            f"to **{capped_minutes}m** remaining (capped at {MAX_PERM_SHIELD_MINUTES} minutes)."
        )


    # ------------- User Command -------------
    @commands.command(name="permshield", aliases=["ps"])
    async def permshield(self, ctx: commands.Context, arg: Optional[str] = None):
        guild = ctx.guild
        author: discord.Member = ctx.author  # type: ignore
        role = _get_role(guild)
        if not role:
            await ctx.reply(f"I can’t find a **{PERM_SHIELD_ROLE_NAME}** role. Please create it first.")
            return

        # View modes: !ps  OR  !ps @user
        if arg is None or (arg.startswith("<@") and ctx.message.mentions):
            target = author if arg is None else ctx.message.mentions[0]
            st = _ps_state(guild.id, target.id)
            until = _parse_iso(st.get("until"))
            now = _utcnow()
            if until and until > now:
                rem = _remaining_str(until - now)
                await ctx.reply(f"🛡️ **{target.display_name}** has **{rem}** of {PERM_SHIELD_ROLE_NAME} remaining.")
            else:
                await ctx.reply(f"🛡️ **{target.display_name}** does not have an active {PERM_SHIELD_ROLE_NAME}.")
            return

        # Spend mode: expect an integer amount of XP (minutes)
        try:
            minutes = int(arg.replace(",", "").strip())
        except ValueError:
            await ctx.reply(
                "Usage:\n"
                "`!ps <minutes>` to buy/extend (1 XP = 1 minute)\n"
                "`!ps` to view your time\n"
                "`!ps @user` to view another user’s time."
            )
            return
        if minutes <= 0:
            await ctx.reply("Minutes must be a positive number.")
            return

        # Verify the caller has enough XP before charging (1 XP = 1 minute)
        u = _udict(guild.id, author.id)
        cur_xp = int(u.get("xp_f", u.get("xp", 0)))
        cost = minutes
        if cur_xp < cost:
            await ctx.reply(f"You only have **{cur_xp} XP** but need **{cost} XP** to buy {minutes} minute(s).")
            return

        # Deduct XP once
        changed = await apply_xp_change(author, -cost, source="perm shield purchase")

        # ✅ Sync roles after any XP change
        if changed:
            old_lvl, new_lvl = changed
            if new_lvl > old_lvl:
                await announce_level_up(author, new_lvl)
            await sync_level_roles(author, new_lvl)
        else:
            # Keep roles tidy even if no level change
            u = _udict(guild.id, author.id)
            await sync_level_roles(author, int(u.get("level", 1)))

        await enforce_level6_exclusive(guild)

        # Extend timer: from existing expiry if active, else from now
        st = _ps_state(guild.id, author.id)
        now = _utcnow()
        current_until = _parse_iso(st.get("until"))
        start_from = current_until if (current_until and current_until > now) else now
        new_until = start_from + timedelta(minutes=minutes)
        # ✅ Clamp to max duration
        max_until = now + timedelta(minutes=MAX_PERM_SHIELD_MINUTES)
        if new_until > max_until:
            new_until = max_until
        st["until"] = _iso(new_until)
        await save_data()

        # Ensure role present
        if role not in author.roles:
            try:
                await author.add_roles(role, reason=f"Perm Shield purchased: +{minutes}m")
            except (discord.Forbidden, discord.HTTPException):
                await ctx.reply("I couldn’t add the role (check my role hierarchy/permissions).")
                return

        rem = _remaining_str(new_until - now)
        await ctx.reply(
            f"🛡️ Added **{minutes}m** to your {PERM_SHIELD_ROLE_NAME}. "
            f"New remaining time: **{rem}**."
        )
