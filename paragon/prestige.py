# paragon/prestige.py
from __future__ import annotations
from typing import Optional, List, Tuple

import discord
from discord.ext import commands

from .config import PRESTIGE_BOARD_LIMIT
from .stats_store import record_xp_change
from .storage import _udict, _gdict, save_data
from .roles import enforce_level6_exclusive
from .ownership import owner_only
from .xp import prestige_cost, prestige_multiplier, get_gain_state


def _fmt_eta(minutes: Optional[int]) -> str:
    if minutes is None:
        return "n/a"
    m = max(0, int(minutes))
    days, rem = divmod(m, 24 * 60)
    hours, mins = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


class PrestigeCog(commands.Cog):
    """
    Prestige model:
      - Cost depends on current prestige p: cost = prestige_cost(p)
      - On prestige:
          xp = xp - cost
          prestige = prestige + 1
      - Temporary boosts persist through prestige.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="setp")
    @owner_only()
    async def set_prestige(self, ctx: commands.Context, amount: int, target: discord.Member):
        """Admin command to set a user's prestige level manually."""
        guild = ctx.guild
        if amount < 0:
            await ctx.reply("Prestige amount cannot be negative.")
            return

        u = _udict(guild.id, target.id)
        u["prestige"] = int(amount)
        await save_data()
        await enforce_level6_exclusive(guild)

        nxt = prestige_cost(int(amount))
        mult = prestige_multiplier(int(amount))
        await ctx.reply(
            f"Set prestige for **{target.display_name}** to **{amount}**. "
            f"Rate bonus now **+{(mult - 1.0) * 100.0:.1f}%**, next prestige cost **{nxt} XP**."
        )

    @commands.command(name="prestige", aliases=["p"])
    async def prestige(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        guild = ctx.guild
        author: discord.Member = ctx.author  # type: ignore

        if target is None:
            await self._show_scoreboard(ctx)
            return

        if target.id != author.id:
            await ctx.reply(f"You can only prestige **yourself**. Usage: `{ctx.clean_prefix}prestige @self`.")
            return

        u = _udict(guild.id, author.id)
        p = int(u.get("prestige", 0))
        total_xp = int(u.get("xp_f", u.get("xp", 0)))
        cost = prestige_cost(p)

        if total_xp < cost:
            st = await get_gain_state(author)
            need = max(0, cost - total_xp)
            eta = _fmt_eta(st.get("prestige_progress_eta_minutes"))
            await ctx.reply(
                f"You need **{need} more XP** to prestige.\n"
                f"Current: **{total_xp} / {cost} XP** | "
                f"Estimated time at current pace: **{eta}**."
            )
            return

        # Prestige action: spend cost, increment prestige, keep active boosts.
        new_xp = float(max(0, total_xp - cost))
        delta = new_xp - float(total_xp)
        u["xp_f"] = new_xp
        u["xp"] = int(u["xp_f"])
        u["level"] = 1
        u["prestige"] = p + 1
        if delta != 0.0:
            record_xp_change(guild.id, author.id, delta, source="prestige cost")
        await save_data()
        await enforce_level6_exclusive(guild)

        new_p = int(u["prestige"])
        next_cost = prestige_cost(new_p)
        new_mult = prestige_multiplier(new_p)
        await ctx.reply(
            f"🌟 **Prestiged!** You are now **Prestige {new_p}**.\n"
            f"Spent **{cost} XP**. Remaining XP: **{u['xp']}**.\n"
            f"Passive prestige bonus: **+{(new_mult - 1.0) * 100.0:.1f}%**. "
            f"Next cost: **{next_cost} XP**."
        )

    async def _show_scoreboard(self, ctx: commands.Context):
        g = _gdict(ctx.guild.id)
        users = g.get("users", {})
        rows: List[Tuple[int, int, int]] = []
        for uid_s, u in users.items():
            p = int(u.get("prestige", 0))
            xp = int(u.get("xp_f", u.get("xp", 0)))
            if p > 0 or xp > 0:
                rows.append((int(uid_s), p, xp))
        rows.sort(key=lambda t: (-t[1], -t[2], t[0]))

        me = _udict(ctx.guild.id, ctx.author.id)
        my_p = int(me.get("prestige", 0))
        my_xp = int(me.get("xp_f", me.get("xp", 0)))
        my_cost = prestige_cost(my_p)
        my_need = max(0, my_cost - my_xp)
        my_mult = prestige_multiplier(my_p)

        if not rows:
            await ctx.reply(
                "🌟 **Prestige Board**\n"
                "No prestige data yet.\n"
                f"Your prestige: **{my_p}** | XP: **{my_xp}/{my_cost}** | Need: **{my_need}**"
            )
            return

        top = rows[: max(1, PRESTIGE_BOARD_LIMIT)]
        lines = ["🌟 **Prestige Board**"]
        for i, (uid, p, xp) in enumerate(top, start=1):
            m = ctx.guild.get_member(uid)
            name = m.display_name if m else f"User {uid}"
            lines.append(f"`{i:>2}.` **{name}** — P{p} | {xp} XP")

        lines.append(
            f"\nYou: **P{my_p}** | Rate bonus **+{(my_mult - 1.0) * 100.0:.1f}%** | "
            f"Next prestige: **{my_xp}/{my_cost} XP** (need **{my_need}**)"
        )
        await ctx.reply("\n".join(lines))
