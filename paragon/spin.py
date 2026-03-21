from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord.ext import commands

from .config import SPIN_DISABLED_REWARDS, SPIN_RESET_HOUR, SPIN_RESET_MINUTE
from .ownership import owner_only
from .spin_support import (
    add_roulette_backfire_shield,
    add_blackjack_natural_charges,
    set_anagram_reward_multiplier,
    set_coinflip_win_edge,
    set_lotto_bonus_tickets_pct,
    set_lotto_jackpot_boost_multiplier,
    set_roulette_accuracy_bonus,
    set_wordle_reward_multiplier,
    wheel_buff_lines,
)
from .stats_store import record_game_fields
from .storage import _gdict, _udict, save_data
from .time_windows import LOCAL_TZ
from .xp import (
    apply_xp_change,
    grant_fixed_boost,
    prestige_cost,
    prestige_multiplier,
)
from .roles import enforce_level6_exclusive


RESET_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{1,2}))?(am|pm)?$")

WHEEL_REWARDS: dict[str, dict] = {
    "bj_natural_next": {
        "label": "Next blackjack hand is a guaranteed natural blackjack.",
        "weight": 8.0,
    },
    "wordle_x4_next": {
        "label": "Next Wordle clear is 4x final buff strength.",
        "weight": 8.0,
    },
    "anagram_x3_next": {
        "label": "Next anagram solve is 3x final buff strength.",
        "weight": 7.0,
    },
    "roulette_aim_next": {
        "label": "Next roulette shot gets +20% absolute success chance.",
        "weight": 6.0,
    },
    "roulette_shield_next": {
        "label": "Next roulette backfire is blocked.",
        "weight": 5.0,
    },
    "coinflip_edge_next": {
        "label": "Next coinflip gets a strong win edge.",
        "weight": 7.0,
    },
    "lotto_ticket_surge_next": {
        "label": "Next lotto buy grants +50% bonus tickets.",
        "weight": 5.0,
    },
    "lotto_jackpot_amp_next": {
        "label": "Next lotto jackpot win boost is amplified x1.75.",
        "weight": 3.5,
    },
    "xp_boost_minor": {
        "label": "+20% XP/min for 60m.",
        "weight": 14.0,
    },
    "xp_boost_major": {
        "label": "+40% XP/min for 90m.",
        "weight": 9.0,
    },
    "xp_boost_jackpot": {
        "label": "+100% XP/min for 45m.",
        "weight": 4.0,
    },
    "flat_xp_25pct": {
        "label": "Flat XP equal to 25% of your next prestige cost.",
        "weight": 12.0,
    },
    "flat_xp_60pct": {
        "label": "Flat XP equal to 60% of your next prestige cost.",
        "weight": 6.0,
    },
    "flat_xp_120pct": {
        "label": "Flat XP equal to 120% of your next prestige cost.",
        "weight": 2.0,
    },
    "prestige_plus_1": {
        "label": "+1 Prestige level instantly.",
        "weight": 2.0,
    },
    "prestige_plus_2": {
        "label": "+2 Prestige levels instantly.",
        "weight": 0.6,
    },
    "clear_debuffs": {
        "label": "Cleanse all active XP debuffs.",
        "weight": 7.0,
    },
}

_REWARD_SHORT_LABELS: dict[str, str] = {
    "bj_natural_next": "Natural BJ",
    "wordle_x4_next": "Wordle x4",
    "anagram_x3_next": "Anagram x3",
    "roulette_aim_next": "Roulette Aim",
    "roulette_shield_next": "Roulette Shield",
    "coinflip_edge_next": "Coinflip Edge",
    "lotto_ticket_surge_next": "Lotto Ticket Surge",
    "lotto_jackpot_amp_next": "Lotto Jackpot Amp",
    "xp_boost_minor": "XP Boost Minor",
    "xp_boost_major": "XP Boost Major",
    "xp_boost_jackpot": "XP Boost Jackpot",
    "flat_xp_25pct": "Flat XP 25%",
    "flat_xp_60pct": "Flat XP 60%",
    "flat_xp_120pct": "Flat XP 120%",
    "prestige_plus_1": "Prestige +1",
    "prestige_plus_2": "Prestige +2",
    "clear_debuffs": "Debuff Cleanse",
}


def _sanitize_reset_time(hour: int, minute: int) -> tuple[int, int]:
    try:
        h = int(hour)
    except Exception:
        h = int(SPIN_RESET_HOUR)
    try:
        m = int(minute)
    except Exception:
        m = int(SPIN_RESET_MINUTE)
    h = max(0, min(23, h))
    m = max(0, min(59, m))
    return h, m


def _draw_time_label(hour: int, minute: int) -> str:
    ampm = "AM" if hour < 12 else "PM"
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{minute:02d} {ampm} ET"


def _next_reset_dt(hour: int, minute: int, *, now: Optional[datetime] = None) -> datetime:
    now_local = now or datetime.now(LOCAL_TZ)
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_local >= target:
        target = target + timedelta(days=1)
    return target


def _cycle_key(hour: int, minute: int, *, now: Optional[datetime] = None) -> str:
    now_local = now or datetime.now(LOCAL_TZ)
    boundary = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_local < boundary:
        use_date = (now_local - timedelta(days=1)).date()
    else:
        use_date = now_local.date()
    return use_date.isoformat()


def _parse_reset_time(raw: str) -> Optional[tuple[int, int]]:
    s = (raw or "").strip().lower().replace(" ", "")
    if not s:
        return None
    m = RESET_TIME_RE.fullmatch(s)
    if m:
        h = int(m.group(1))
        mins = int(m.group(2) or "0")
        ap = m.group(3)
    else:
        m = re.fullmatch(r"(\d{3,4})(am|pm)?$", s)
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


def _wheel_state(gid: int) -> dict:
    g = _gdict(gid)
    st = g.get("spin_wheel")
    if not isinstance(st, dict):
        st = {}
        g["spin_wheel"] = st
    st.setdefault("reset_hour", int(SPIN_RESET_HOUR))
    st.setdefault("reset_minute", int(SPIN_RESET_MINUTE))
    st.setdefault("reward_overrides", {})
    st["reset_hour"], st["reset_minute"] = _sanitize_reset_time(
        st.get("reset_hour", SPIN_RESET_HOUR),
        st.get("reset_minute", SPIN_RESET_MINUTE),
    )
    if not isinstance(st.get("reward_overrides"), dict):
        st["reward_overrides"] = {}
    return st


def _spin_user_state(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    st = u.get("spin_daily")
    if not isinstance(st, dict):
        st = {"cycle_key": "", "spun": False, "last_reward": "", "last_spin_ts": 0}
        u["spin_daily"] = st
    st.setdefault("cycle_key", "")
    st.setdefault("spun", False)
    st.setdefault("last_reward", "")
    st.setdefault("last_spin_ts", 0)
    return st


def _reward_enabled(st: dict, reward_key: str) -> bool:
    key = str(reward_key).strip().lower()
    overrides = st.get("reward_overrides")
    if isinstance(overrides, dict) and key in overrides:
        return bool(overrides[key])
    return key not in SPIN_DISABLED_REWARDS


class SpinCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _eligible_rewards(self, st: dict) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for key, meta in WHEEL_REWARDS.items():
            if not _reward_enabled(st, key):
                continue
            weight = float(meta.get("weight", 0.0))
            if weight <= 0:
                continue
            out.append((key, weight))
        return out

    def _short_label(self, reward_key: str) -> str:
        key = str(reward_key).strip().lower()
        return _REWARD_SHORT_LABELS.get(key, key.replace("_", " ").title())

    def _pick_reward(self, st: dict) -> Optional[str]:
        rewards = self._eligible_rewards(st)
        keys = [k for k, _ in rewards]
        weights = [w for _, w in rewards]
        if not keys:
            return None
        return str(random.choices(keys, weights=weights, k=1)[0])

    async def _animate_spin(self, ctx: commands.Context, st: dict, final_key: str) -> None:
        rewards = self._eligible_rewards(st)
        keys = [k for k, _ in rewards]
        if not keys:
            return

        seq_len = 12
        lead = [random.choice(keys) for _ in range(max(0, seq_len - 3))]
        sequence = lead + [random.choice(keys), final_key, final_key]
        delays = [0.08, 0.08, 0.10, 0.10, 0.12, 0.14, 0.16, 0.19, 0.23, 0.27, 0.32, 0.42]
        spinner = ["◜", "◠", "◝", "◞", "◡", "◟"]

        def _frame(i: int, key: str) -> str:
            slots = [
                self._short_label(sequence[max(0, i - 1)]),
                self._short_label(key),
                self._short_label(sequence[min(len(sequence) - 1, i + 1)]),
            ]
            pointer = f"`{spinner[i % len(spinner)]}`"
            fill = max(0, min(12, i + 1))
            bar = f"[{'=' * fill}{'.' * (12 - fill)}]"
            return (
                "🎡 **Daily Wheel Spin**\n"
                f"{bar} {pointer}\n"
                f"`{slots[0]}`  **`{slots[1]}`**  `{slots[2]}`\n"
                "_Spinning..._"
            )

        try:
            msg = await ctx.reply(_frame(0, sequence[0]))
        except Exception:
            return

        for i in range(1, len(sequence)):
            await asyncio.sleep(delays[min(i, len(delays) - 1)])
            try:
                await msg.edit(content=_frame(i, sequence[i]))
            except Exception:
                return

        await asyncio.sleep(0.18)
        try:
            await msg.edit(
                content=(
                    "🎡 **Daily Wheel Spin**\n"
                    f"**Landed on:** `{self._short_label(final_key)}`"
                )
            )
        except Exception:
            return

    async def _apply_reward(self, ctx: commands.Context, reward_key: str) -> str:
        gid = int(ctx.guild.id)
        uid = int(ctx.author.id)
        key = str(reward_key).strip().lower()

        if key == "bj_natural_next":
            charges = add_blackjack_natural_charges(gid, uid, charges=1)
            return f"Guaranteed natural blackjack added. Charges now: **{charges}**."

        if key == "wordle_x4_next":
            state = set_wordle_reward_multiplier(gid, uid, multiplier=4.0, charges=1)
            return (
                f"Next Wordle clear buff multiplier set to **x{state['multiplier']:.2f}** "
                f"for **{state['charges']}** clear(s)."
            )

        if key == "anagram_x3_next":
            state = set_anagram_reward_multiplier(gid, uid, multiplier=3.0, charges=1)
            return (
                f"Next anagram solve buff multiplier set to **x{state['multiplier']:.2f}** "
                f"for **{state['charges']}** solve(s)."
            )

        if key == "roulette_aim_next":
            state = set_roulette_accuracy_bonus(gid, uid, bonus=0.20, charges=1)
            return (
                f"Next roulette shot aim boosted by **+{state['bonus'] * 100.0:.1f}%** "
                f"for **{state['charges']}** use(s)."
            )

        if key == "roulette_shield_next":
            charges = add_roulette_backfire_shield(gid, uid, charges=1)
            return f"Roulette backfire shield granted. Charges now: **{charges}**."

        if key == "coinflip_edge_next":
            state = set_coinflip_win_edge(gid, uid, bonus=0.22, charges=1)
            return (
                f"Next coinflip gets **+{state['bonus'] * 100.0:.1f}%** win edge "
                f"for **{state['charges']}** match(es)."
            )

        if key == "lotto_ticket_surge_next":
            state = set_lotto_bonus_tickets_pct(gid, uid, pct=0.50, charges=1)
            return (
                f"Next lotto buy gains **+{state['pct'] * 100.0:.0f}%** bonus tickets "
                f"for **{state['charges']}** purchase(s)."
            )

        if key == "lotto_jackpot_amp_next":
            state = set_lotto_jackpot_boost_multiplier(gid, uid, multiplier=1.75, charges=1)
            return (
                f"Next lotto jackpot boost amp set to **x{state['multiplier']:.2f}** "
                f"for **{state['charges']}** jackpot(s)."
            )

        if key == "xp_boost_minor":
            boost = await grant_fixed_boost(
                ctx.author,
                pct=0.20,
                minutes=60,
                source="wheel xp_boost_minor",
                persist=False,
            )
            return f"XP boost granted: **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."

        if key == "xp_boost_major":
            boost = await grant_fixed_boost(
                ctx.author,
                pct=0.40,
                minutes=90,
                source="wheel xp_boost_major",
                persist=False,
            )
            return f"XP boost granted: **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."

        if key == "xp_boost_jackpot":
            boost = await grant_fixed_boost(
                ctx.author,
                pct=1.00,
                minutes=45,
                source="wheel xp_boost_jackpot",
                persist=False,
            )
            return f"XP boost jackpot: **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."

        if key in {"flat_xp_25pct", "flat_xp_60pct", "flat_xp_120pct"}:
            u = _udict(gid, uid)
            p = int(u.get("prestige", 0))
            cost = max(1, int(prestige_cost(p)))
            pct_map = {
                "flat_xp_25pct": 0.25,
                "flat_xp_60pct": 0.60,
                "flat_xp_120pct": 1.20,
            }
            pct = float(pct_map[key])
            gain = max(1, int(round(cost * pct)))
            await apply_xp_change(ctx.author, gain, source=f"wheel {key}")
            return f"Flat XP awarded: **+{gain} XP** ({int(round(pct * 100.0))}% of prestige cost **{cost}**)."

        if key in {"prestige_plus_1", "prestige_plus_2"}:
            add_levels = 1 if key == "prestige_plus_1" else 2
            u = _udict(gid, uid)
            old_p = int(u.get("prestige", 0))
            new_p = max(0, old_p + add_levels)
            u["prestige"] = int(new_p)
            await enforce_level6_exclusive(ctx.guild)
            mult = prestige_multiplier(new_p)
            return (
                f"Prestige increased by **+{add_levels}** to **P{new_p}**. "
                f"Passive prestige bonus now **+{(mult - 1.0) * 100.0:.1f}%**."
            )

        if key == "clear_debuffs":
            now = int(time.time())
            u = _udict(gid, uid)
            raw = u.get("xp_debuffs")
            debuffs = raw if isinstance(raw, list) else []
            active = 0
            for d in debuffs:
                if not isinstance(d, dict):
                    continue
                try:
                    until = int(d.get("until", 0))
                except Exception:
                    until = 0
                if until > now:
                    active += 1
            u["xp_debuffs"] = []
            return f"Debuffs cleansed. Removed **{active}** active debuff(s)."

        # Should not happen when reward table is valid.
        return "No effect was applied."

    @commands.command(name="spin", aliases=["wheel"])
    async def spin(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        gid = int(ctx.guild.id)
        uid = int(ctx.author.id)
        st = _wheel_state(gid)
        h, m = _sanitize_reset_time(st.get("reset_hour", SPIN_RESET_HOUR), st.get("reset_minute", SPIN_RESET_MINUTE))
        cycle = _cycle_key(h, m)
        nxt = _next_reset_dt(h, m)

        ust = _spin_user_state(gid, uid)
        if str(ust.get("cycle_key", "")) != cycle:
            ust["cycle_key"] = cycle
            ust["spun"] = False

        if bool(ust.get("spun", False)):
            buffs = wheel_buff_lines(gid, uid)
            lines = [
                f"You already used your daily spin for cycle **{cycle}**.",
                f"Next reset: **{nxt.strftime('%Y-%m-%d %I:%M %p ET')}**.",
            ]
            if buffs:
                lines.append("Active wheel buffs:")
                lines.extend(f"- {line}" for line in buffs)
            await ctx.reply("\n".join(lines))
            return

        reward_key = self._pick_reward(st)
        if not reward_key:
            await ctx.reply("Spin wheel has no enabled rewards. Ask an admin to enable rewards.")
            return

        await self._animate_spin(ctx, st, reward_key)

        reward_meta = WHEEL_REWARDS.get(reward_key, {})
        effect_text = await self._apply_reward(ctx, reward_key)

        ust["spun"] = True
        ust["cycle_key"] = cycle
        ust["last_reward"] = reward_key
        ust["last_spin_ts"] = int(time.time())

        record_game_fields(gid, uid, "spin", spins=1)
        record_game_fields(gid, uid, "spin", **{f"reward_{reward_key}": 1})
        await save_data()

        buffs = wheel_buff_lines(gid, uid)
        lines = [
            f"Daily spin result: **{reward_key}**",
            f"{reward_meta.get('label', '').strip()}",
            effect_text,
            f"Next reset: **{nxt.strftime('%Y-%m-%d %I:%M %p ET')}**.",
        ]
        if buffs:
            lines.append("Active wheel buffs:")
            lines.extend(f"- {line}" for line in buffs)
        await ctx.reply("\n".join(lines))

    @commands.command(name="spinstatus", aliases=["wheelstatus"])
    async def spin_status(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        gid = int(ctx.guild.id)
        uid = int(ctx.author.id)
        st = _wheel_state(gid)
        h, m = _sanitize_reset_time(st.get("reset_hour", SPIN_RESET_HOUR), st.get("reset_minute", SPIN_RESET_MINUTE))
        cycle = _cycle_key(h, m)
        nxt = _next_reset_dt(h, m)
        ust = _spin_user_state(gid, uid)
        spun = bool(ust.get("spun", False)) and str(ust.get("cycle_key", "")) == cycle

        lines = [
            f"Daily wheel reset: **{_draw_time_label(h, m)}**",
            f"Current cycle: **{cycle}**",
            f"Spin used this cycle: **{'yes' if spun else 'no'}**",
            f"Next reset: **{nxt.strftime('%Y-%m-%d %I:%M %p ET')}**",
        ]
        buffs = wheel_buff_lines(gid, uid)
        if buffs:
            lines.append("Active wheel buffs:")
            lines.extend(f"- {line}" for line in buffs)
        await ctx.reply("\n".join(lines))

    @commands.command(name="spintime")
    @owner_only()
    async def spin_time(self, ctx: commands.Context, *, when: Optional[str] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        st = _wheel_state(ctx.guild.id)
        cur_h, cur_m = _sanitize_reset_time(st.get("reset_hour", SPIN_RESET_HOUR), st.get("reset_minute", SPIN_RESET_MINUTE))
        if when is None or not when.strip():
            nxt = _next_reset_dt(cur_h, cur_m)
            await ctx.reply(
                f"Spin reset time is **{_draw_time_label(cur_h, cur_m)}**.\n"
                f"Next reset: **{nxt.strftime('%Y-%m-%d %I:%M %p ET')}**."
            )
            return

        parsed = _parse_reset_time(when)
        if not parsed:
            p = ctx.clean_prefix
            await ctx.reply(
                f"Usage: `{p}spintime <time>` where time is `HH:MM` (24h) or `h[:mm]am/pm`.\n"
                f"Examples: `{p}spintime 00:00`, `{p}spintime 6pm`, `{p}spintime 6:30pm`."
            )
            return

        h, m = parsed
        st["reset_hour"] = int(h)
        st["reset_minute"] = int(m)
        await save_data()
        nxt = _next_reset_dt(h, m)
        await ctx.reply(
            f"Spin reset time set to **{_draw_time_label(h, m)}** by {ctx.author.mention}.\n"
            f"Next reset: **{nxt.strftime('%Y-%m-%d %I:%M %p ET')}**."
        )

    @commands.command(name="spinrewards")
    @owner_only()
    async def spin_rewards(self, ctx: commands.Context, reward_key: Optional[str] = None, mode: Optional[str] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        st = _wheel_state(ctx.guild.id)
        overrides = st.setdefault("reward_overrides", {})

        if reward_key is None:
            lines = ["**Spin rewards**"]
            for key, meta in WHEEL_REWARDS.items():
                enabled = _reward_enabled(st, key)
                source = "override" if key in overrides else "default"
                lines.append(
                    f"- `{key}`: **{'ON' if enabled else 'OFF'}** "
                    f"(weight={float(meta.get('weight', 0.0)):g}, {source})"
                )
            lines.append(
                f"Use `{ctx.clean_prefix}spinrewards <reward_key> <on|off|toggle|default>` to change."
            )
            await ctx.reply("\n".join(lines))
            return

        key = str(reward_key).strip().lower()
        if key not in WHEEL_REWARDS:
            await ctx.reply(f"Unknown reward key: `{key}`.")
            return

        token = (mode or "toggle").strip().lower()
        if token in {"on", "enable", "enabled", "true", "1"}:
            overrides[key] = True
            action = "ON"
        elif token in {"off", "disable", "disabled", "false", "0"}:
            overrides[key] = False
            action = "OFF"
        elif token in {"toggle", "t"}:
            overrides[key] = not _reward_enabled(st, key)
            action = "ON" if overrides[key] else "OFF"
        elif token in {"default", "reset", "clear"}:
            overrides.pop(key, None)
            action = "DEFAULT"
        else:
            await ctx.reply(f"Usage: `{ctx.clean_prefix}spinrewards <reward_key> <on|off|toggle|default>`")
            return

        await save_data()
        await ctx.reply(f"Spin reward `{key}` is now **{action}**.")

    @commands.command(name="spinreset", aliases=["wheelreset"])
    @owner_only()
    async def spin_reset(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        target = member
        if target is None:
            await ctx.reply(f"Usage: `{ctx.clean_prefix}spinreset @user`")
            return
        if target.bot:
            await ctx.reply("Target must be a non-bot member.")
            return

        gid = int(ctx.guild.id)
        st = _wheel_state(gid)
        h, m = _sanitize_reset_time(st.get("reset_hour", SPIN_RESET_HOUR), st.get("reset_minute", SPIN_RESET_MINUTE))
        cycle = _cycle_key(h, m)

        ust = _spin_user_state(gid, target.id)
        ust["cycle_key"] = cycle
        ust["spun"] = False
        ust["last_reward"] = ""
        ust["last_spin_ts"] = 0
        await save_data()

        await ctx.reply(
            f"Spin reset for {target.mention}. They can spin again in cycle **{cycle}**."
        )
