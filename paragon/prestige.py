from __future__ import annotations

from typing import List, Optional, Tuple

import discord
from discord.ext import commands

from .config import PRESTIGE_BOARD_LIMIT
from .ownership import is_control_user_id, owner_only
from .roles import enforce_level6_exclusive
from .stats_store import record_xp_change
from .storage import _gdict, _udict, save_data
from .xp import (
    get_gain_state,
    prestige_base_rate,
    prestige_cost,
    prestige_multiplier,
    prestige_passive_rate,
)


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

    def _can_prestige_others(self, ctx: commands.Context) -> bool:
        if is_control_user_id(ctx.guild, ctx.author.id):
            return True
        perms = getattr(ctx.author, "guild_permissions", None)
        if not perms:
            return False
        return bool(perms.manage_guild or perms.administrator)

    def _max_affordable_prestiges(self, total_xp: int, prestige_level: int) -> Tuple[int, int, int]:
        remaining_xp = max(0, int(total_xp))
        current_prestige = max(0, int(prestige_level))
        count = 0
        spent = 0

        while True:
            cost = prestige_cost(current_prestige)
            if cost <= 0 or remaining_xp < cost:
                break
            remaining_xp -= cost
            spent += cost
            current_prestige += 1
            count += 1

        return count, spent, remaining_xp

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
        base_rate = prestige_base_rate(int(amount))
        mult = prestige_multiplier(int(amount))
        passive_rate = prestige_passive_rate(int(amount))
        await ctx.reply(
            f"Set prestige for **{target.display_name}** to **{amount}**. "
            f"Passive rate now **{passive_rate:.2f} XP/min** "
            f"(base **{base_rate:.2f}**, prestige x**{mult:.3f}**). "
            f"Next prestige cost **{nxt} XP**."
        )

    @commands.command(name="prestige", aliases=["p"], usage="[@user] [all]")
    async def prestige(self, ctx: commands.Context, first: Optional[str] = None, second: Optional[str] = None):
        guild = ctx.guild
        author: discord.Member = ctx.author  # type: ignore
        usage = f"`{ctx.clean_prefix}prestige [@user] [all]`"

        if first is None:
            await self._show_scoreboard(ctx)
            return

        if first.lower() == "all" and second is not None:
            await ctx.reply(f"Usage: {usage}")
            return

        target = author
        do_all = False

        if first.lower() == "all":
            do_all = True
        else:
            try:
                target = await commands.MemberConverter().convert(ctx, first)
            except commands.BadArgument:
                await ctx.reply(f"Usage: {usage}")
                return

            if second is not None:
                if second.lower() != "all":
                    await ctx.reply(f"Usage: {usage}")
                    return
                do_all = True

        if target.id != author.id and not self._can_prestige_others(ctx):
            await ctx.reply(
                f"You can only prestige **yourself**. Usage: `{ctx.clean_prefix}prestige @self [all]`."
            )
            return

        u = _udict(guild.id, target.id)
        p = int(u.get("prestige", 0))
        total_xp = int(u.get("xp_f", u.get("xp", 0)))
        cost = prestige_cost(p)

        if do_all:
            prestige_count, spent_xp, remaining_xp = self._max_affordable_prestiges(total_xp, p)
        else:
            prestige_count = 1 if total_xp >= cost else 0
            spent_xp = cost if prestige_count else 0
            remaining_xp = max(0, total_xp - spent_xp)

        if prestige_count <= 0:
            st = await get_gain_state(target)
            need = max(0, cost - total_xp)
            eta = _fmt_eta(st.get("prestige_progress_eta_minutes"))
            lead = "You need" if target.id == author.id else f"**{target.display_name}** needs"
            await ctx.reply(
                f"{lead} **{need} more XP** to prestige.\n"
                f"Current: **{total_xp} / {cost} XP** | "
                f"Estimated time at current pace: **{eta}**."
            )
            return

        # Prestige action: spend cost, increment prestige, keep active boosts.
        new_xp = float(max(0, remaining_xp))
        delta = new_xp - float(total_xp)
        u["xp_f"] = new_xp
        u["xp"] = int(u["xp_f"])
        u["level"] = 1
        u["prestige"] = p + prestige_count
        if delta != 0.0:
            record_xp_change(guild.id, target.id, delta, source="prestige cost")
        await save_data()
        await enforce_level6_exclusive(guild)

        new_p = int(u["prestige"])
        next_cost = prestige_cost(new_p)
        new_base = prestige_base_rate(new_p)
        new_mult = prestige_multiplier(new_p)
        new_rate = prestige_passive_rate(new_p)
        if target.id == author.id:
            if prestige_count == 1:
                headline = f"Prestiged! You are now **Prestige {new_p}**."
            else:
                headline = f"Prestiged **{prestige_count}** times! You are now **Prestige {new_p}**."
        else:
            if prestige_count == 1:
                headline = f"Prestiged **{target.display_name}** to **Prestige {new_p}**."
            else:
                headline = (
                    f"Prestiged **{target.display_name}** **{prestige_count}** times. "
                    f"They are now **Prestige {new_p}**."
                )
        await ctx.reply(
            f"{headline}\n"
            f"Spent **{spent_xp} XP**. Remaining XP: **{u['xp']}**.\n"
            f"Passive rate now **{new_rate:.2f} XP/min** "
            f"(base **{new_base:.2f}**, prestige x**{new_mult:.3f}**). "
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
        my_base = prestige_base_rate(my_p)
        my_mult = prestige_multiplier(my_p)
        my_rate = prestige_passive_rate(my_p)

        if not rows:
            await ctx.reply(
                "Prestige Board\n"
                "No prestige data yet.\n"
                f"Your prestige: **{my_p}** | Passive rate: **{my_rate:.2f} XP/min** "
                f"(base **{my_base:.2f}**, prestige x**{my_mult:.3f}**) | "
                f"XP: **{my_xp}/{my_cost}** | Need: **{my_need}**"
            )
            return

        top = rows[: max(1, PRESTIGE_BOARD_LIMIT)]
        lines = ["Prestige Board"]
        for i, (uid, p, xp) in enumerate(top, start=1):
            m = ctx.guild.get_member(uid)
            name = m.display_name if m else f"User {uid}"
            lines.append(f"`{i:>2}.` **{name}** - P{p} | {xp} XP")

        lines.append(
            f"\nYou: **P{my_p}** | Passive rate **{my_rate:.2f} XP/min** "
            f"(base {my_base:.2f}, prestige x{my_mult:.3f}) | "
            f"Next prestige: **{my_xp}/{my_cost} XP** (need **{my_need}**)"
        )
        await ctx.reply("\n".join(lines))
