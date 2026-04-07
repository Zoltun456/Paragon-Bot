from __future__ import annotations

from datetime import date
from typing import List, Optional
import hashlib
import math
import os
import random
import re

import discord
from discord.ext import commands

from .config import (
    ANAGRAM_DAILY_LIMIT,
    ANAGRAM_FAIL_ADD_MINUTES,
    ANAGRAM_FAIL_ADD_PCT,
    ANAGRAM_FAIL_MAX_MINUTES,
    ANAGRAM_FAIL_MAX_PCT,
    ANAGRAM_PHRASES_PATH,
    ANAGRAM_SOLVE_ADD_MINUTES,
    ANAGRAM_SOLVE_ADD_PCT,
    ANAGRAM_SOLVE_MAX_MINUTES,
    ANAGRAM_SOLVE_MAX_PCT,
)
from .roles import enforce_level6_exclusive
from .spin_support import consume_anagram_reward_multiplier
from .stats_store import record_game_fields
from .storage import _udict, save_data
from .time_windows import _date_key, _today_local
from .xp import grant_stacked_fixed_boost, grant_stacked_fixed_debuff

_builtin_phrases = [
    "silver sword",
    "ocean breeze",
    "golden apple",
    "hidden path",
    "vast horizon",
    "midnight sun",
    "silent storm",
    "crystal river",
    "ancient ruins",
    "shadow dancer",
]
_phrases: List[str] = []

_word_re = re.compile(r"[A-Za-z]+")


def _normalize_phrase(raw: str) -> str:
    body = raw.split("#", 1)[0].strip()
    if not body:
        return ""
    return " ".join(_word_re.findall(body.lower()))


def _coprime_step(seed: bytes, mod: int) -> int:
    if mod <= 1:
        return 1
    n = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")
    step = (n % mod) or 1
    while math.gcd(step, mod) != 1:
        step = (step + 1) % mod
        if step == 0:
            step = 1
    return step


def _sequence_index(user_id: int, position: int, mod: int) -> int:
    if mod <= 1:
        return 0
    seed = f"anagram:{user_id}".encode("utf-8")
    start = int.from_bytes(hashlib.blake2b(seed, digest_size=8).digest(), "big") % mod
    step = _coprime_step(seed + b":step", mod)
    return (start + step * position) % mod


def _load_phrases():
    global _phrases
    if _phrases:
        return
    if os.path.exists(ANAGRAM_PHRASES_PATH):
        with open(ANAGRAM_PHRASES_PATH, "r", encoding="utf-8") as f:
            items = []
            seen = set()
            for line in f:
                s = _normalize_phrase(line)
                if not s or s in seen:
                    continue
                seen.add(s)
                items.append(s)
        _phrases = items if items else [_normalize_phrase(p) for p in _builtin_phrases]
    else:
        _phrases = [_normalize_phrase(p) for p in _builtin_phrases]


def _canonical(s: str) -> str:
    tokens = _word_re.findall(s.lower())
    return " ".join(tokens)


def _phrase_for(user_id: int, day_key: str, slot: int) -> str:
    _load_phrases()
    try:
        day_ordinal = date.fromisoformat(day_key).toordinal()
    except ValueError:
        day_ordinal = 0
    position = day_ordinal * ANAGRAM_DAILY_LIMIT + max(0, slot)
    idx = _sequence_index(user_id, position, len(_phrases))
    return _phrases[idx]


def _scramble_phrase(phrase: str) -> str:
    out_words = []
    for w in phrase.split():
        if len(w) <= 3:
            letters = list(w)
            random.shuffle(letters)
            out_words.append("".join(letters))
            continue
        letters = list(w)
        for _ in range(6):
            random.shuffle(letters)
            s = "".join(letters)
            if s.lower() != w.lower():
                break
        out_words.append("".join(letters))
    return " ".join(out_words)


def _state(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    st = u.get("anagram")
    if st is None:
        st = {"date": "", "used": 0, "solved": 0, "idx": 0, "awaiting": False, "scrambled": "", "answer": ""}
        u["anagram"] = st
    for k, v in [("used", 0), ("solved", 0), ("idx", 0), ("awaiting", False), ("scrambled", ""), ("answer", "")]:
        if k not in st:
            st[k] = v
    if "date" not in st:
        st["date"] = ""
    return st


class AnagramCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="anagram", aliases=["a"])
    async def anagram(self, ctx: commands.Context, *, guess: Optional[str] = None):
        today = _date_key(_today_local())
        st = _state(ctx.guild.id, ctx.author.id)

        if st.get("date") != today:
            st["date"] = today
            st["used"] = 0
            st["solved"] = 0
            st["idx"] = 0
            st["awaiting"] = False
            st["scrambled"] = ""
            st["answer"] = ""
            await save_data()

        used = int(st.get("used", 0))
        if used >= ANAGRAM_DAILY_LIMIT:
            await ctx.reply(
                f"You have used **{ANAGRAM_DAILY_LIMIT}/{ANAGRAM_DAILY_LIMIT}** anagrams today. Come back tomorrow!"
            )
            return

        if guess is None:
            p = ctx.clean_prefix
            if st.get("awaiting") and st.get("scrambled"):
                await ctx.reply(
                    f"**Anagram:** `{st['scrambled']}`\n"
                    f"Attempts left today: **{used}/{ANAGRAM_DAILY_LIMIT}**\n"
                    f"Reply with `{p}a <guess>`."
                )
                return
            phrase = _phrase_for(ctx.author.id, today, st["idx"])
            scrambled = _scramble_phrase(phrase)
            st["answer"] = phrase
            st["scrambled"] = scrambled
            st["awaiting"] = True
            record_game_fields(ctx.guild.id, ctx.author.id, "anagram", puzzles_started=1)
            await save_data()
            await ctx.reply(
                f"**Anagram:** `{scrambled}`\n"
                f"Attempts used today: **{used}/{ANAGRAM_DAILY_LIMIT}**\n"
                f"Reply with `{p}a <guess>`."
            )
            return

        if not st.get("awaiting"):
            await ctx.reply(f"No active puzzle. Type `{ctx.clean_prefix}a` to get one first.")
            return

        answer = st.get("answer", "")
        norm_guess = _canonical(guess)
        norm_answer = _canonical(answer)
        st["used"] = used + 1
        st["idx"] = int(st.get("idx", 0)) + 1
        st["awaiting"] = False
        st["scrambled"] = ""
        st["answer"] = ""
        await save_data()

        if norm_guess == norm_answer:
            solve_number = int(st.get("solved", 0)) + 1
            st["solved"] = solve_number
            wheel_mult = float(consume_anagram_reward_multiplier(ctx.guild.id, ctx.author.id))
            base_pct = float(ANAGRAM_SOLVE_ADD_PCT)
            base_minutes = int(ANAGRAM_SOLVE_ADD_MINUTES)
            final_pct = float(base_pct * wheel_mult)
            final_minutes = int(max(1, round(float(base_minutes) * wheel_mult)))
            boost = await grant_stacked_fixed_boost(
                ctx.author,
                pct_add=final_pct,
                minutes_add=final_minutes,
                pct_cap=ANAGRAM_SOLVE_MAX_PCT,
                minutes_cap=ANAGRAM_SOLVE_MAX_MINUTES,
                source="anagram solve",
                reward_seed_xp=int(round(base_pct * 100.0)),
            )
            record_game_fields(
                ctx.guild.id,
                ctx.author.id,
                "anagram",
                solves=1,
                boost_seed_xp_total=int(round(base_pct * 100.0)),
                boost_percent_total=boost["percent"],
                boost_minutes_total=boost["minutes"],
            )
            await enforce_level6_exclusive(ctx.guild)
            wheel_line = ""
            if wheel_mult > 1.0:
                wheel_line = (
                    f" Wheel buff: x{wheel_mult:.2f} final buff scaling "
                    f"(+{base_pct * 100.0:.1f}%/{base_minutes}m -> "
                    f"+{final_pct * 100.0:.1f}%/{final_minutes}m)."
                )
            await ctx.reply(
                f"Correct! Anagram stack is now **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**. "
                f"Solve **#{solve_number}** added **+{final_pct * 100.0:.1f}%** for **{final_minutes}m**. "
                f"Progress: **{st['used']}/{ANAGRAM_DAILY_LIMIT}**.{wheel_line}"
            )
            return

        debuff = await grant_stacked_fixed_debuff(
            ctx.author,
            pct_add=ANAGRAM_FAIL_ADD_PCT,
            minutes_add=ANAGRAM_FAIL_ADD_MINUTES,
            pct_cap=ANAGRAM_FAIL_MAX_PCT,
            minutes_cap=ANAGRAM_FAIL_MAX_MINUTES,
            source="anagram fail",
            reward_seed_xp=int(round(ANAGRAM_FAIL_ADD_PCT * 100.0)),
        )
        record_game_fields(ctx.guild.id, ctx.author.id, "anagram", fails=1)
        await enforce_level6_exclusive(ctx.guild)
        await ctx.reply(
            f"Not quite. The answer was **{answer}**. "
            f"Anagram debuff is now **-{debuff['percent']:.1f}% XP/min** for **{debuff['minutes']}m**.\n"
            f"Progress: **{st['used']}/{ANAGRAM_DAILY_LIMIT}**."
        )
