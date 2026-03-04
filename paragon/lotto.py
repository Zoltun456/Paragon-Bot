# paragon/lotto.py
from __future__ import annotations
import random
import discord
from discord.ext import commands, tasks
from datetime import datetime
from .config import (
    LOTTO_TICKET_COST,
    LOTTO_DRAW_HOUR,
    LOTTO_DRAW_MINUTE,
    LOTTO_MAX_PER_USER,
)
from .guild_setup import get_log_channel
from .storage import _gdict, _udict, save_data
from .stats_store import record_game_fields
from .time_windows import LOCAL_TZ
from .xp import apply_xp_change, grant_reward_boost
from .roles import enforce_level6_exclusive
from .ownership import owner_only

def _lotto_state(gid: int) -> dict:
    g = _gdict(gid)
    st = g.get("lotto")
    if st is None:
        st = {"tickets": {}, "pot": 0, "last_draw": "", "enabled": True}
        g["lotto"] = st
    # add missing keys for older saves
    if "enabled" not in st: st["enabled"] = True
    if "tickets" not in st: st["tickets"] = {}
    if "pot" not in st: st["pot"] = 0
    if "last_draw" not in st: st["last_draw"] = ""
    return st

class LottoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not self.lotto_draw_loop.is_running():
            self.lotto_draw_loop.start()

    # -------- Commands --------

    @commands.command(name="lotto", aliases=["l"])
    async def lotto(self, ctx: commands.Context, arg: str = None):
        st = _lotto_state(ctx.guild.id)

        # If disabled, block usage for everyone (including pot checks)
        if not st.get("enabled", True):
            await ctx.reply("🎟️ The lottery is currently **disabled**.")
            return

        # Check pot / tickets (no arg or mention)
        if arg is None or (arg.startswith("<@") and ctx.message.mentions):
            target = ctx.author if arg is None else ctx.message.mentions[0]
            uid_s = str(target.id)
            tickets = st["tickets"].get(uid_s, 0)
            await ctx.reply(f"🎟️ Pot: **{st['pot']} XP** | {target.display_name} has **{tickets} ticket(s)**.")
            return

        # Buying tickets
        try:
            count = int(arg)
        except ValueError:
            await ctx.reply("Usage: `!l <tickets>` (number of tickets) or `!l @user` to check.")
            return
        if count <= 0:
            await ctx.reply("Ticket count must be positive.")
            return

        cost = count * LOTTO_TICKET_COST

        # ✅ Check per-user cap
        uid_s = str(ctx.author.id)
        already = st["tickets"].get(uid_s, 0) * LOTTO_TICKET_COST
        if already + cost > LOTTO_MAX_PER_USER:
            remaining = max(0, LOTTO_MAX_PER_USER - already)
            if remaining <= 0:
                await ctx.reply(f"🎟️ You’ve already reached the max of {LOTTO_MAX_PER_USER} XP in the pot.")
            else:
                await ctx.reply(f"🎟️ You can only add {remaining} more XP worth of tickets "
                                f"(current cap: {LOTTO_MAX_PER_USER} XP).")
            return

        # Deduct XP
        u = _udict(ctx.guild.id, ctx.author.id)
        cur_xp = int(u.get("xp_f", u.get("xp", 0)))
        if cur_xp < cost:
            await ctx.reply(f"You only have **{cur_xp} XP** but need **{cost} XP** for {count} ticket(s).")
            return
        await apply_xp_change(ctx.author, -cost, source="lotto ticket")
        record_game_fields(
            ctx.guild.id,
            ctx.author.id,
            "lotto",
            tickets_bought=count,
            xp_spent_total=cost,
        )
        await enforce_level6_exclusive(ctx.guild)

        # Update pot + tickets
        st["pot"] += cost
        st["tickets"][str(ctx.author.id)] = st["tickets"].get(str(ctx.author.id), 0) + count
        await save_data()
        await ctx.reply(f"🎟️ Bought {count} ticket(s)! Pot is now **{st['pot']} XP**.")

    @commands.command(name="poplatto")
    @owner_only()
    async def poplotto(self, ctx: commands.Context):
        """Force an immediate lottery draw + reset (owner/admin only)."""


        st = _lotto_state(ctx.guild.id)
        pot = st.get("pot", 0)
        if pot <= 0:
            await ctx.reply("The lottery pot is empty. Nothing to draw.")
            return

        pool = []
        for uid_s, t in st.get("tickets", {}).items():
            pool.extend([uid_s] * t)
        if not pool:
            await ctx.reply("No tickets in the pot. Nothing to draw.")
            st["tickets"] = {}
            st["pot"] = 0
            await save_data()
            return

        winner_id = int(random.choice(pool))
        winner = ctx.guild.get_member(winner_id)
        reward = pot

        # Reset pot
        st["tickets"] = {}
        st["pot"] = 0
        st["last_draw"] = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        await save_data()

        if winner:
            boost = await grant_reward_boost(winner, reward, source="lotto jackpot")
            record_game_fields(
                ctx.guild.id,
                winner.id,
                "lotto",
                jackpots_won=1,
                boost_seed_xp_total=reward,
                boost_percent_total=boost["percent"],
                boost_minutes_total=boost["minutes"],
            )
            await enforce_level6_exclusive(ctx.guild)
        else:
            boost = None

        msg = f"🎉 **Lottery Draw!** Pot was **{reward} XP**.\n"
        if winner and boost is not None:
            msg += (
                f"Winner: {winner.mention} 🎊\n"
                f"Boost: **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."
            )
        else:
            msg += "No winner (user left the server)."

        await ctx.reply(msg)
        ch = get_log_channel(ctx.guild)
        if ch and ch.permissions_for(ctx.guild.me).send_messages:
            await ch.send(msg)

    @commands.command(name="lottotoggle")
    @owner_only()
    async def lottotoggle(self, ctx: commands.Context):
        """Toggle the lottery on/off (owner/admin only)."""

        st = _lotto_state(ctx.guild.id)
        st["enabled"] = not st.get("enabled", True)
        await save_data()
        state = "ENABLED" if st["enabled"] else "DISABLED"
        msg = f"🔁 Lottery has been **{state}** by {ctx.author.mention}."
        await ctx.reply(msg)
        ch = get_log_channel(ctx.guild)
        if ch and ch.permissions_for(ctx.guild.me).send_messages:
            await ch.send(msg)

    # -------- Background draw loop --------
    @tasks.loop(minutes=1)
    async def lotto_draw_loop(self):
        now = datetime.now(LOCAL_TZ)
        if now.hour != LOTTO_DRAW_HOUR or now.minute != LOTTO_DRAW_MINUTE:
            return
        for guild in self.bot.guilds:
            st = _lotto_state(guild.id)
            # Respect toggle: skip drawing if disabled
            if not st.get("enabled", True):
                continue
            today_key = now.strftime("%Y-%m-%d")
            if st.get("last_draw") == today_key:
                continue  # already drew today

            pot = st["pot"]
            if pot <= 0:
                st["last_draw"] = today_key
                await save_data()
                continue

            pool = []
            for uid_s, t in st["tickets"].items():
                pool.extend([uid_s] * t)
            if not pool:
                st["last_draw"] = today_key
                await save_data()
                continue

            winner_id = int(random.choice(pool))
            winner = guild.get_member(winner_id)
            reward = pot

            # Reset pot
            st["tickets"] = {}
            st["pot"] = 0
            st["last_draw"] = today_key
            await save_data()

            if winner:
                boost = await grant_reward_boost(winner, reward, source="lotto jackpot")
                record_game_fields(
                    guild.id,
                    winner.id,
                    "lotto",
                    jackpots_won=1,
                    boost_seed_xp_total=reward,
                    boost_percent_total=boost["percent"],
                    boost_minutes_total=boost["minutes"],
                )
                await enforce_level6_exclusive(guild)
            else:
                boost = None

            # Announce result
            msg = f"🎉 **Lottery Draw!** Pot was **{reward} XP**.\n"
            if winner and boost is not None:
                msg += (
                    f"Winner: {winner.mention} 🎊\n"
                    f"Boost: **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."
                )
            else:
                msg += "No winner (user left the server)."
            ch = get_log_channel(guild)
            if ch and ch.permissions_for(guild.me).send_messages:
                await ch.send(msg)
