# paragon/thanks.py
from __future__ import annotations
from typing import Optional
import discord
from discord.ext import commands

from .config import THANKS_GIFT_XP
from .storage import _udict, save_data
from .stats_store import record_game_fields
from .time_windows import _today_local, _date_key        # local date helpers:contentReference[oaicite:2]{index=2}
from .xp import grant_reward_boost
from .roles import enforce_level6_exclusive


def _thanks_state(gid: int, uid: int) -> dict:
    """Ensure per-user 'thanks' state exists and return it."""
    u = _udict(gid, uid)  # ensure user record:contentReference[oaicite:5]{index=5}
    st = u.get("thanks")
    if st is None:
        st = {"date": "", "used": False, "target": 0}
        u["thanks"] = st
    # fill any missing keys
    if "date" not in st: st["date"] = ""
    if "used" not in st: st["used"] = False
    if "target" not in st: st["target"] = 0
    return st


class ThanksCog(commands.Cog):
    """
    !thanks @user / !thx @user
    - Once per local day per user
    - Gifts THANKS_GIFT_XP XP to the target
    - No self-gifting, ignores bots
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="thanks", aliases=["thx"])
    async def thanks(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        if target is None:
            p = ctx.clean_prefix
            await ctx.reply(f"Usage: `{p}thanks @user` (or `{p}thx @user`)")
            return

        author: discord.Member = ctx.author  # type: ignore
        if target.bot:
            await ctx.reply("Bots don’t need XP love 🫶 — pick a real person.")
            return
        if target.id == author.id:
            await ctx.reply("Nice try, but you can’t gift XP to yourself.")
            return
        if target.guild.id != ctx.guild.id:
            await ctx.reply("Target must be a member of this server.")
            return

        # Check daily usage
        today = _date_key(_today_local())             # local day key:contentReference[oaicite:6]{index=6}
        st = _thanks_state(ctx.guild.id, author.id)   # per-user state:contentReference[oaicite:7]{index=7}
        if st.get("date") != today:
            st["date"] = today
            st["used"] = False
            st["target"] = 0
            await save_data()

        if st.get("used", False):
            prev = ctx.guild.get_member(int(st.get("target", 0))) if st.get("target") else None
            prev_name = f"**{prev.display_name}**" if prev else "someone"
            await ctx.reply(f"You already sent thanks today to {prev_name}. Try again tomorrow!")
            return

        boost = await grant_reward_boost(target, THANKS_GIFT_XP, source="thanks gift")
        record_game_fields(ctx.guild.id, author.id, "thanks", sent=1)
        record_game_fields(
            ctx.guild.id,
            target.id,
            "thanks",
            received=1,
            boost_seed_xp_total=THANKS_GIFT_XP,
            boost_percent_total=boost["percent"],
            boost_minutes_total=boost["minutes"],
        )
        await enforce_level6_exclusive(ctx.guild)

        # Persist daily usage
        st["used"] = True
        st["target"] = int(target.id)
        await save_data()

        await ctx.reply(
            f"🙏 {author.mention} thanked **{target.display_name}** — they gained "
            f"**+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**!"
        )
