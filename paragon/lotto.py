# paragon/lotto.py
from __future__ import annotations

import math
import random
import re
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks

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
from .xp import apply_xp_change, grant_fixed_boost
from .roles import enforce_level6_exclusive
from .ownership import owner_only


def _sanitize_draw_time(hour: int, minute: int) -> tuple[int, int]:
    try:
        h = int(hour)
    except Exception:
        h = int(LOTTO_DRAW_HOUR)
    try:
        m = int(minute)
    except Exception:
        m = int(LOTTO_DRAW_MINUTE)
    h = max(0, min(23, h))
    m = max(0, min(59, m))
    return h, m


def _draw_time_label(hour: int, minute: int) -> str:
    ampm = "AM" if hour < 12 else "PM"
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{minute:02d} {ampm} ET"


def _next_draw_dt(hour: int, minute: int, *, now: Optional[datetime] = None) -> datetime:
    now_local = now or datetime.now(LOCAL_TZ)
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_local >= target:
        target = target + timedelta(days=1)
    return target


def _parse_draw_time(raw: str) -> Optional[tuple[int, int]]:
    s = (raw or "").strip().lower()
    if not s:
        return None
    s = s.replace(" ", "")

    # "6:30pm", "18:00", "6pm"
    m = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?(am|pm)?", s)
    if m:
        h = int(m.group(1))
        mins = int(m.group(2) or "0")
        ap = m.group(3)
    else:
        # "1830", "630pm"
        m = re.fullmatch(r"(\d{3,4})(am|pm)?", s)
        if not m:
            return None
        digits = m.group(1)
        h = int(digits[:-2])
        mins = int(digits[-2:])
        ap = m.group(2)

    if mins < 0 or mins > 59:
        return None

    if ap:
        if h < 1 or h > 12:
            return None
        if h == 12:
            h = 0
        if ap == "pm":
            h += 12
    else:
        if h < 0 or h > 23:
            return None

    return h, mins


def _lotto_boost_profile(total_tickets: int, winner_tickets: int, prestige: int) -> dict:
    """
    Balance goals:
    - more pool tickets => longer buff duration
    - lower prestige => much larger buff
    - tiny ticket share (underdog) => larger buff
    """
    pool = max(1, int(total_tickets))
    won = max(1, min(int(winner_tickets), pool))
    p = max(0, int(prestige))

    share = won / float(pool)
    underdog_factor = min(2.5, max(1.0, (1.0 / max(share, 1e-9)) ** 0.20))

    # Hard prestige dampening: high prestige gets much smaller buffs.
    prestige_factor = 1.0 / ((1.0 + (p / 8.0)) ** 1.2)

    pool_scale = 0.10 + (0.18 * math.log10(pool + 1.0))
    pct = pool_scale * underdog_factor * prestige_factor
    pct = max(0.03, min(1.75, pct))  # +3% .. +175%

    minutes = int(round(25 + (40.0 * math.log2(pool + 1.0))))
    minutes = max(30, min(720, minutes))  # 30m .. 12h

    return {
        "pct": float(pct),
        "percent": float(pct * 100.0),
        "minutes": int(minutes),
        "share_pct": float(share * 100.0),
        "prestige": int(p),
    }


def _lotto_state(gid: int) -> dict:
    g = _gdict(gid)
    st = g.get("lotto")
    if st is None:
        st = {
            "tickets": {},
            "pot": 0,
            "last_draw": "",
            "enabled": True,
            "draw_hour": int(LOTTO_DRAW_HOUR),
            "draw_minute": int(LOTTO_DRAW_MINUTE),
        }
        g["lotto"] = st
    # add missing keys for older saves
    if "enabled" not in st:
        st["enabled"] = True
    if "tickets" not in st or not isinstance(st.get("tickets"), dict):
        st["tickets"] = {}
    if "pot" not in st:
        st["pot"] = 0
    if "last_draw" not in st:
        st["last_draw"] = ""
    if "draw_hour" not in st:
        st["draw_hour"] = int(LOTTO_DRAW_HOUR)
    if "draw_minute" not in st:
        st["draw_minute"] = int(LOTTO_DRAW_MINUTE)
    st["draw_hour"], st["draw_minute"] = _sanitize_draw_time(
        st.get("draw_hour", LOTTO_DRAW_HOUR),
        st.get("draw_minute", LOTTO_DRAW_MINUTE),
    )
    return st


def _ticket_totals(st: dict) -> tuple[int, dict[int, int]]:
    by_user: dict[int, int] = {}
    total = 0
    for uid_s, raw_t in st.get("tickets", {}).items():
        try:
            uid = int(uid_s)
            t = int(raw_t)
        except Exception:
            continue
        if t <= 0:
            continue
        by_user[uid] = by_user.get(uid, 0) + t
        total += t
    return total, by_user


class LottoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not self.lotto_draw_loop.is_running():
            self.lotto_draw_loop.start()

    async def _resolve_draw(self, guild: discord.Guild, *, now: Optional[datetime] = None, forced: bool = False) -> tuple[bool, str]:
        st = _lotto_state(guild.id)
        now_local = now or datetime.now(LOCAL_TZ)
        today_key = now_local.strftime("%Y-%m-%d")

        if (not forced) and st.get("last_draw") == today_key:
            return False, ""

        pot = int(st.get("pot", 0) or 0)
        total_tickets, by_user = _ticket_totals(st)
        if pot <= 0 or total_tickets <= 0 or not by_user:
            st["tickets"] = {}
            st["pot"] = 0
            if not forced:
                st["last_draw"] = today_key
            await save_data()
            if forced:
                return False, "The lottery pot has no valid tickets. Nothing to draw."
            return False, ""

        # Anti-farm rule: if only one participant entered, refund and grant no boost.
        if len(by_user) == 1:
            solo_id = next(iter(by_user.keys()))
            refund = pot
            st["tickets"] = {}
            st["pot"] = 0
            st["last_draw"] = today_key
            await save_data()

            solo_member = guild.get_member(solo_id)
            if solo_member:
                await apply_xp_change(solo_member, refund, source="lotto refund")
                await enforce_level6_exclusive(guild)
                record_game_fields(
                    guild.id,
                    solo_id,
                    "lotto",
                    refunds=1,
                    solo_refunds=1,
                    xp_refunded_total=refund,
                )
                msg = (
                    "🎟️ **Lottery Draw Cancelled**\n"
                    "Only one participant entered, so no boost was awarded.\n"
                    f"Refunded **{refund} XP** to {solo_member.mention}."
                )
            else:
                msg = (
                    "🎟️ **Lottery Draw Cancelled**\n"
                    "Only one participant entered, so no boost was awarded.\n"
                    f"Could not refund user `{solo_id}` because they are no longer in this server."
                )
            return True, msg

        uids = list(by_user.keys())
        weights = list(by_user.values())
        winner_id = int(random.choices(uids, weights=weights, k=1)[0])
        winner_tickets = int(by_user.get(winner_id, 0))
        winner = guild.get_member(winner_id)

        # Reset pot immediately.
        st["tickets"] = {}
        st["pot"] = 0
        st["last_draw"] = today_key
        await save_data()

        if winner:
            wu = _udict(guild.id, winner.id)
            prestige = int(wu.get("prestige", 0))
            profile = _lotto_boost_profile(total_tickets, winner_tickets, prestige)
            boost = await grant_fixed_boost(
                winner,
                pct=profile["pct"],
                minutes=profile["minutes"],
                source="lotto jackpot",
                reward_seed_xp=pot,
            )
            record_game_fields(
                guild.id,
                winner.id,
                "lotto",
                jackpots_won=1,
                boost_seed_xp_total=pot,
                boost_percent_total=boost["percent"],
                boost_minutes_total=boost["minutes"],
                winning_tickets_total=winner_tickets,
                pool_tickets_total=total_tickets,
            )
            await enforce_level6_exclusive(guild)
        else:
            profile = None
            boost = None

        msg = (
            f"🎉 **Lottery Draw!** Pool: **{pot} XP** from **{total_tickets} ticket(s)**.\n"
            "Jackpot reward is a temporary XP-rate boost (no direct XP payout)."
        )
        if winner and boost is not None and profile is not None:
            msg += (
                f"\nWinner: {winner.mention} 🎊 "
                f"({winner_tickets} ticket(s), Prestige {profile['prestige']})"
                f"\nBoost: **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."
            )
        else:
            msg += f"\nWinning ticket belonged to user `{winner_id}` who is no longer in this server."
        return True, msg

    # -------- Commands --------
    @commands.command(name="lotto", aliases=["l"])
    async def lotto(self, ctx: commands.Context, arg: str = None):
        st = _lotto_state(ctx.guild.id)

        # If disabled, block usage for everyone (including pot checks)
        if not st.get("enabled", True):
            await ctx.reply("🎟️ The lottery is currently **disabled**.")
            return

        draw_h, draw_m = _sanitize_draw_time(st.get("draw_hour", LOTTO_DRAW_HOUR), st.get("draw_minute", LOTTO_DRAW_MINUTE))
        next_draw = _next_draw_dt(draw_h, draw_m)

        # Check pot / tickets (no arg or mention)
        if arg is None or (arg.startswith("<@") and ctx.message.mentions):
            target = ctx.author if arg is None else ctx.message.mentions[0]
            uid_s = str(target.id)
            tickets = int(st["tickets"].get(uid_s, 0))
            total_tickets, _ = _ticket_totals(st)
            await ctx.reply(
                f"🎟️ Pot: **{st['pot']} XP** from **{total_tickets} ticket(s)** "
                f"| {target.display_name} has **{tickets} ticket(s)**"
                f"\nNext draw: **{_draw_time_label(draw_h, draw_m)}** "
                f"({next_draw.strftime('%Y-%m-%d %I:%M %p ET')})."
            )
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

        # Per-user cap (in XP spent into current pot).
        uid_s = str(ctx.author.id)
        already = int(st["tickets"].get(uid_s, 0)) * LOTTO_TICKET_COST
        if already + cost > LOTTO_MAX_PER_USER:
            remaining = max(0, LOTTO_MAX_PER_USER - already)
            if remaining <= 0:
                await ctx.reply(f"🎟️ You’ve already reached the max of {LOTTO_MAX_PER_USER} XP in the current pot.")
            else:
                max_more_tickets = max(0, remaining // LOTTO_TICKET_COST)
                await ctx.reply(
                    f"🎟️ You can only add **{remaining} XP** more to this pot "
                    f"(~{max_more_tickets} ticket(s) at current ticket cost)."
                )
            return

        # Deduct XP to buy tickets.
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
        st["pot"] = int(st.get("pot", 0)) + cost
        st["tickets"][uid_s] = int(st["tickets"].get(uid_s, 0)) + count
        total_tickets, _ = _ticket_totals(st)
        await save_data()
        await ctx.reply(
            f"🎟️ Bought {count} ticket(s)! Pot is now **{st['pot']} XP** "
            f"from **{total_tickets} ticket(s)**."
        )

    @commands.command(name="poplatto", aliases=["poplotto"])
    @owner_only()
    async def poplotto(self, ctx: commands.Context):
        """Force an immediate lottery draw + reset (owner/admin only)."""
        drew, msg = await self._resolve_draw(ctx.guild, forced=True)
        await ctx.reply(msg)
        if drew:
            ch = get_log_channel(ctx.guild)
            if ch and ch.permissions_for(ctx.guild.me).send_messages:
                await ch.send(msg)

    @commands.command(name="lottotime")
    @owner_only()
    async def lottotime(self, ctx: commands.Context, *, when: Optional[str] = None):
        """
        View or set daily lottery draw time (US Eastern).
        Examples: !lottotime 18:00, !lottotime 6pm, !lottotime 6:30pm
        """
        st = _lotto_state(ctx.guild.id)
        cur_h, cur_m = _sanitize_draw_time(st.get("draw_hour", LOTTO_DRAW_HOUR), st.get("draw_minute", LOTTO_DRAW_MINUTE))

        if not when:
            nxt = _next_draw_dt(cur_h, cur_m)
            await ctx.reply(
                f"🕕 Lottery draw time is **{_draw_time_label(cur_h, cur_m)}**.\n"
                f"Next draw: **{nxt.strftime('%Y-%m-%d %I:%M %p ET')}**."
            )
            return

        parsed = _parse_draw_time(when)
        if not parsed:
            await ctx.reply(
                "Usage: `!lottotime <time>` where time is `HH:MM` (24h) or `h[:mm]am/pm`.\n"
                "Examples: `!lottotime 18:00`, `!lottotime 6pm`, `!lottotime 6:30pm`."
            )
            return

        h, m = parsed
        st["draw_hour"] = int(h)
        st["draw_minute"] = int(m)
        await save_data()

        nxt = _next_draw_dt(h, m)
        msg = (
            f"🕕 Lottery draw time set to **{_draw_time_label(h, m)}** by {ctx.author.mention}.\n"
            f"Next draw: **{nxt.strftime('%Y-%m-%d %I:%M %p ET')}**."
        )
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
        today_key = now.strftime("%Y-%m-%d")

        for guild in self.bot.guilds:
            st = _lotto_state(guild.id)
            # Respect toggle: skip drawing if disabled
            if not st.get("enabled", True):
                continue

            draw_h, draw_m = _sanitize_draw_time(
                st.get("draw_hour", LOTTO_DRAW_HOUR),
                st.get("draw_minute", LOTTO_DRAW_MINUTE),
            )
            if now.hour != draw_h or now.minute != draw_m:
                continue
            if st.get("last_draw") == today_key:
                continue  # already drew today

            drew, msg = await self._resolve_draw(guild, now=now, forced=False)
            if not drew or not msg:
                continue
            ch = get_log_channel(guild)
            if ch and ch.permissions_for(guild.me).send_messages:
                await ch.send(msg)
