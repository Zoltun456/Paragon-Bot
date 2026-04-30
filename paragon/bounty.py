from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

from .config import (
    BOUNTY_CLAIM_BASE_MINUTES_EQ,
    BOUNTY_CLAIM_REWARD_MULTIPLIER,
    BOUNTY_CLAIM_SECONDS,
    BOUNTY_FAIL_DEBUFF_MINUTES,
    BOUNTY_FAIL_DEBUFF_PCT,
    BOUNTY_STOP_COOLDOWN_SECONDS,
    BOUNTY_SURVIVOR_BASE_MINUTES_EQ,
    BOUNTY_SURVIVOR_MAX_COMPANIONS,
    BOUNTY_SURVIVOR_MAX_MINUTES_EQ,
    BOUNTY_SURVIVOR_MIN_EXPOSURE_MINUTES,
    BOUNTY_SURVIVOR_PER_COMPANION_MINUTES_EQ,
    BOUNTY_SURVIVOR_PER_EXPOSURE_MINUTES_EQ,
    COMMAND_PREFIX,
    resolve_afk_channel_id,
)
from .guild_setup import get_log_channel
from .include import _as_dict, _as_float, _as_int, _iso, _parse_iso, _utcnow
from .roles import enforce_level6_exclusive
from .stats_store import record_game_fields
from .storage import _gdict, _udict, save_data
from .time_windows import _date_key, _today_local
from .xp import grant_bonus_xp_equivalent_boost, grant_fixed_debuff, prestige_passive_rate


BOUNTY_VERSION = 1

def _today_key() -> str:
    return _date_key(_today_local())


def _yesterday_key() -> str:
    return _date_key(_today_local() - timedelta(days=1))


def _fmt_duration_seconds(seconds: int | float) -> str:
    total = max(0, int(round(float(seconds))))
    minutes, secs = divmod(total, 60)
    if minutes <= 0:
        return f"{secs}s"
    if secs <= 0:
        return f"{minutes}m"
    return f"{minutes}m {secs:02d}s"


def _fmt_duration_minutes(minutes: int | float) -> str:
    total = max(0, int(round(float(minutes))))
    hours, mins = divmod(total, 60)
    if hours <= 0:
        return f"{mins}m"
    if mins <= 0:
        return f"{hours}h"
    return f"{hours}h {mins}m"


def _is_eligible_voice_channel(channel) -> bool:
    if not isinstance(channel, discord.VoiceChannel):
        return False
    afk_id = resolve_afk_channel_id(channel.guild)
    if afk_id and channel.id == afk_id:
        return False
    return True


def _human_members(channel: Optional[discord.VoiceChannel]) -> list[discord.Member]:
    if not _is_eligible_voice_channel(channel):
        return []
    return [m for m in channel.members if isinstance(m, discord.Member) and not m.bot]


def _message_channel(guild: discord.Guild, channel_id: int):
    if channel_id <= 0:
        return None
    getter = getattr(guild, "get_channel_or_thread", None)
    if callable(getter):
        return getter(channel_id)
    return guild.get_channel(channel_id)


def _bounty_state(gid: int) -> dict:
    g = _gdict(gid)
    st = g.get("bounty")
    if not isinstance(st, dict):
        st = {}
        g["bounty"] = st

    st.setdefault("date", "")
    st.setdefault("version", BOUNTY_VERSION)
    st.setdefault("target_user_id", 0)
    st.setdefault("announced", False)
    st.setdefault("resolved", False)
    st.setdefault("result", "")
    st.setdefault("winner_user_id", 0)
    st.setdefault("exposure_minutes", 0)
    st.setdefault("exposure_companion_total", 0.0)
    st.setdefault("cooldowns", {})
    st.setdefault("claimant_user_id", 0)
    st.setdefault("claim_started_at", "")
    st.setdefault("claim_expires_at", "")
    st.setdefault("claim_channel_id", 0)
    st.setdefault("claim_message_channel_id", 0)
    st.setdefault("stop_count", 0)
    st.setdefault("last_reward", {})
    st["cooldowns"] = {
        str(uid): _as_int(expires_at, 0)
        for uid, expires_at in _as_dict(st.get("cooldowns")).items()
        if str(uid).strip()
    }
    st["last_reward"] = _as_dict(st.get("last_reward"))
    return st


def _bounty_activity_state(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    st = u.get("bounty_activity")
    if not isinstance(st, dict):
        st = {}
        u["bounty_activity"] = st
    st.setdefault("last_date", "")
    st.setdefault("prev_date", "")
    return st


def _note_daily_activity(gid: int, uid: int, day_key: str) -> bool:
    st = _bounty_activity_state(gid, uid)
    current = str(st.get("last_date", "")).strip()
    previous = str(st.get("prev_date", "")).strip()
    today = str(day_key).strip()

    if current == today:
        return False

    if current and current != previous:
        st["prev_date"] = current
    st["last_date"] = today
    return True


def _was_active_on_date(gid: int, uid: int, day_key: str) -> bool:
    st = _bounty_activity_state(gid, uid)
    target = str(day_key).strip()
    return str(st.get("last_date", "")).strip() == target or str(st.get("prev_date", "")).strip() == target


def _clear_claim_state(st: dict) -> None:
    st["claimant_user_id"] = 0
    st["claim_started_at"] = ""
    st["claim_expires_at"] = ""
    st["claim_channel_id"] = 0
    st["claim_message_channel_id"] = 0


def _prune_cooldowns(st: dict, *, now_ts: Optional[int] = None) -> bool:
    current = int(_utcnow().timestamp()) if now_ts is None else int(now_ts)
    raw = _as_dict(st.get("cooldowns"))
    kept = {
        str(uid): _as_int(expires_at, 0)
        for uid, expires_at in raw.items()
        if _as_int(expires_at, 0) > current
    }
    changed = kept != raw
    if changed:
        st["cooldowns"] = kept
    return changed


def _target_member(guild: discord.Guild, st: dict) -> Optional[discord.Member]:
    uid = _as_int(st.get("target_user_id", 0), 0)
    if uid <= 0:
        return None
    member = guild.get_member(uid)
    return member if isinstance(member, discord.Member) and not member.bot else None


def _claimant_member(guild: discord.Guild, st: dict) -> Optional[discord.Member]:
    uid = _as_int(st.get("claimant_user_id", 0), 0)
    if uid <= 0:
        return None
    member = guild.get_member(uid)
    return member if isinstance(member, discord.Member) and not member.bot else None


def _claim_remaining_seconds(st: dict) -> int:
    expires_at = _parse_iso(st.get("claim_expires_at"))
    if expires_at is None:
        return 0
    return max(0, int((expires_at - _utcnow()).total_seconds()))


def _projected_survivor_minutes_equivalent(st: dict) -> int:
    exposure_minutes = max(0, _as_int(st.get("exposure_minutes", 0), 0))
    if exposure_minutes < max(0, int(BOUNTY_SURVIVOR_MIN_EXPOSURE_MINUTES)):
        return 0

    companion_total = max(0.0, _as_float(st.get("exposure_companion_total", 0.0), 0.0))
    reward = (
        float(BOUNTY_SURVIVOR_BASE_MINUTES_EQ)
        + (float(BOUNTY_SURVIVOR_PER_EXPOSURE_MINUTES_EQ) * float(exposure_minutes))
        + (float(BOUNTY_SURVIVOR_PER_COMPANION_MINUTES_EQ) * companion_total)
    )
    return max(0, min(int(BOUNTY_SURVIVOR_MAX_MINUTES_EQ), int(round(reward))))


def _projected_claim_minutes_equivalent(st: dict) -> int:
    projected = max(int(BOUNTY_CLAIM_BASE_MINUTES_EQ), _projected_survivor_minutes_equivalent(st))
    projected = max(0.0, float(projected) * max(0.0, float(BOUNTY_CLAIM_REWARD_MULTIPLIER)))
    hard_cap = max(int(BOUNTY_CLAIM_BASE_MINUTES_EQ), int(round(float(BOUNTY_SURVIVOR_MAX_MINUTES_EQ) * max(1.0, float(BOUNTY_CLAIM_REWARD_MULTIPLIER)))))
    return max(0, min(hard_cap, int(round(projected))))


def _member_bonus_xp_for_minutes_equivalent(member: discord.Member, minutes_equivalent: int | float) -> float:
    u = _udict(member.guild.id, member.id)
    prestige = max(0, int(u.get("prestige", 0)))
    rate = max(0.01, float(prestige_passive_rate(prestige)))
    return rate * max(0.0, float(minutes_equivalent))


class BountyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._claim_tasks: dict[int, asyncio.Task] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def cog_unload(self):
        if self.bounty_loop.is_running():
            self.bounty_loop.cancel()
        for task in self._claim_tasks.values():
            task.cancel()
        self._claim_tasks.clear()
        self._locks.clear()

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(int(guild_id))
        if lock is None:
            lock = asyncio.Lock()
            self._locks[int(guild_id)] = lock
        return lock

    def _cancel_claim_task(self, guild_id: int) -> None:
        task = self._claim_tasks.pop(int(guild_id), None)
        current = asyncio.current_task()
        if task is not None and task is not current:
            task.cancel()

    def _eligible_user_ids(self, guild: discord.Guild, day_key: str) -> list[int]:
        out: list[int] = []
        for member in guild.members:
            if member.bot:
                continue
            if _was_active_on_date(guild.id, member.id, day_key):
                out.append(int(member.id))
        return sorted(set(out))

    def _pick_daily_target(self, guild: discord.Guild, *, today_key: str, eligible_ids: list[int]) -> int:
        ids = sorted(int(uid) for uid in eligible_ids if int(uid) > 0)
        if not ids:
            return 0
        seed_input = f"bounty:v{BOUNTY_VERSION}:{guild.id}:{today_key}:{','.join(str(uid) for uid in ids)}".encode("utf-8")
        seed = int.from_bytes(hashlib.sha256(seed_input).digest()[:8], "big")
        return ids[seed % len(ids)]

    def _reset_for_today(self, st: dict, today_key: str, target_user_id: int) -> None:
        st["date"] = today_key
        st["version"] = BOUNTY_VERSION
        st["target_user_id"] = int(target_user_id)
        st["announced"] = False
        st["resolved"] = False
        st["result"] = ""
        st["winner_user_id"] = 0
        st["exposure_minutes"] = 0
        st["exposure_companion_total"] = 0.0
        st["cooldowns"] = {}
        st["stop_count"] = 0
        st["last_reward"] = {}
        _clear_claim_state(st)

    async def _grant_minutes_equivalent_boost(
        self,
        member: discord.Member,
        *,
        minutes_equivalent: int,
        source: str,
    ) -> dict:
        bonus_xp = _member_bonus_xp_for_minutes_equivalent(member, minutes_equivalent)
        reward = await grant_bonus_xp_equivalent_boost(
            member,
            bonus_xp,
            source=source,
            reward_seed_xp=bonus_xp,
        )
        reward["bonus_xp"] = float(bonus_xp)
        reward["minutes_equivalent"] = int(minutes_equivalent)
        return reward

    async def _ensure_today_state(self, guild: discord.Guild) -> list[str]:
        notices: list[str] = []
        async with self._lock_for(guild.id):
            st = _bounty_state(guild.id)
            changed = _prune_cooldowns(st)
            today_key = _today_key()
            current_date = str(st.get("date", "")).strip()

            if current_date == today_key:
                if _as_int(st.get("claimant_user_id", 0), 0) > 0 and guild.id not in self._claim_tasks:
                    _clear_claim_state(st)
                    changed = True
                if changed:
                    await save_data()
                return notices

            old_target_id = _as_int(st.get("target_user_id", 0), 0)
            old_resolved = bool(st.get("resolved", False))
            old_exposure_minutes = max(0, _as_int(st.get("exposure_minutes", 0), 0))
            old_companion_total = max(0.0, _as_float(st.get("exposure_companion_total", 0.0), 0.0))
            survivor_reward = None
            survivor_member = None

            if current_date and not old_resolved and old_target_id > 0:
                self._cancel_claim_task(guild.id)
                old_member = guild.get_member(old_target_id)
                if isinstance(old_member, discord.Member) and not old_member.bot:
                    survivor_member = old_member
                    projected_minutes = _projected_survivor_minutes_equivalent(st)
                    if projected_minutes > 0:
                        survivor_reward = await self._grant_minutes_equivalent_boost(
                            old_member,
                            minutes_equivalent=projected_minutes,
                            source="bounty survive",
                        )
                        record_game_fields(
                            guild.id,
                            old_member.id,
                            "bounty",
                            survives=1,
                            exposure_minutes_total=old_exposure_minutes,
                            exposure_companion_total=old_companion_total,
                            reward_minutes_equivalent_total=projected_minutes,
                            boost_seed_xp_total=survivor_reward["bonus_xp"],
                            boost_percent_total=survivor_reward["percent"],
                            boost_minutes_total=survivor_reward["minutes"],
                        )
                    else:
                        record_game_fields(
                            guild.id,
                            old_member.id,
                            "bounty",
                            survives=1,
                            survives_unpaid=1,
                            exposure_minutes_total=old_exposure_minutes,
                            exposure_companion_total=old_companion_total,
                        )
                    await enforce_level6_exclusive(guild)

            eligible_ids = self._eligible_user_ids(guild, _yesterday_key())
            target_user_id = self._pick_daily_target(guild, today_key=today_key, eligible_ids=eligible_ids)
            self._reset_for_today(st, today_key, target_user_id)
            changed = True

            if target_user_id > 0:
                record_game_fields(guild.id, target_user_id, "bounty", assigned=1)

            if changed:
                await save_data()

            if survivor_member is not None:
                if survivor_reward is not None:
                    notices.append(
                        f"{survivor_member.mention} survived yesterday's bounty and earned "
                        f"**+{float(survivor_reward.get('percent', 0.0)):.1f}% XP/min** for **{int(survivor_reward.get('minutes', 0))}m** "
                        f"(about **{_fmt_duration_minutes(int(survivor_reward.get('minutes_equivalent', 0)))}** of passive gain)."
                    )
                else:
                    notices.append(
                        f"{survivor_member.mention} survived yesterday's bounty, but banked no reward because they did not spend enough eligible time in voice."
                    )

            if target_user_id > 0:
                target = guild.get_member(target_user_id)
                if isinstance(target, discord.Member) and not target.bot:
                    notices.append(
                        f"Today's bounty target is **{target.display_name}**. "
                        f"Use `{COMMAND_PREFIX}b` to check status and `{COMMAND_PREFIX}b @user` to start a claim from voice."
                    )
            else:
                notices.append("No bounty target was assigned today because nobody qualified from yesterday's activity.")

        return notices

    async def _send_rollover_notices(self, guild: discord.Guild, notices: list[str]) -> None:
        if not notices:
            return
        channel = get_log_channel(guild)
        if channel is None:
            return
        for line in notices:
            try:
                await channel.send(line)
            except Exception:
                return

    async def _maybe_announce_target(self, guild: discord.Guild, member: discord.Member, *, channel=None) -> None:
        if member.bot:
            return

        maybe_channel = channel or get_log_channel(guild)
        if maybe_channel is None:
            return

        async with self._lock_for(guild.id):
            st = _bounty_state(guild.id)
            if str(st.get("date", "")) != _today_key():
                return
            if bool(st.get("announced", False)):
                return
            if bool(st.get("resolved", False)):
                return
            if _as_int(st.get("target_user_id", 0), 0) != member.id:
                return

            st["announced"] = True
            record_game_fields(guild.id, member.id, "bounty", reveals=1)
            await save_data()

        try:
            await maybe_channel.send(
                f"{member.mention} has today's bounty on their head. "
                f"Hunters can use `{COMMAND_PREFIX}b {member.mention}` from the same voice channel, "
                f"and the target can shut down an active claim with `{COMMAND_PREFIX}b stop`."
            )
        except Exception:
            return

    def _claim_is_valid(self, guild: discord.Guild, st: dict) -> bool:
        claimant = _claimant_member(guild, st)
        target = _target_member(guild, st)
        channel_id = _as_int(st.get("claim_channel_id", 0), 0)
        if claimant is None or target is None or channel_id <= 0:
            return False
        claimant_channel = getattr(getattr(claimant, "voice", None), "channel", None)
        target_channel = getattr(getattr(target, "voice", None), "channel", None)
        if not _is_eligible_voice_channel(claimant_channel):
            return False
        if claimant_channel is None or target_channel is None:
            return False
        if claimant_channel.id != channel_id or target_channel.id != channel_id:
            return False
        return True

    async def _cancel_active_claim(
        self,
        guild: discord.Guild,
        *,
        reason: str,
        stopped_by_target: bool = False,
    ) -> Optional[dict]:
        message = None
        async with self._lock_for(guild.id):
            st = _bounty_state(guild.id)
            claimant_id = _as_int(st.get("claimant_user_id", 0), 0)
            target_id = _as_int(st.get("target_user_id", 0), 0)
            if claimant_id <= 0 or target_id <= 0:
                return None

            claimant = guild.get_member(claimant_id)
            target = guild.get_member(target_id)
            message_channel_id = _as_int(st.get("claim_message_channel_id", 0), 0)
            if stopped_by_target:
                st["cooldowns"][str(claimant_id)] = int(_utcnow().timestamp()) + max(1, int(BOUNTY_STOP_COOLDOWN_SECONDS))
                st["stop_count"] = _as_int(st.get("stop_count", 0), 0) + 1
                if claimant is not None:
                    record_game_fields(guild.id, claimant.id, "bounty", failed_claims=1, stopped_claims=1)
                if target is not None:
                    record_game_fields(guild.id, target.id, "bounty", stops_used=1)
            elif claimant is not None:
                record_game_fields(guild.id, claimant.id, "bounty", claims_canceled=1)

            _clear_claim_state(st)
            await save_data()

            claimant_name = claimant.display_name if claimant is not None else f"User {claimant_id}"
            target_name = target.display_name if target is not None else f"User {target_id}"
            if stopped_by_target:
                message = (
                    f"**{target_name}** shut down the bounty claim. "
                    f"**{claimant_name}** is on cooldown for **{_fmt_duration_seconds(BOUNTY_STOP_COOLDOWN_SECONDS)}**."
                )
            elif reason == "movement":
                message = (
                    f"The bounty claim on **{target_name}** was interrupted because the voice lock broke."
                )
            elif reason == "restart":
                message = "The active bounty claim was reset because the bot restarted."
            elif reason == "rollover":
                message = "The active bounty claim expired at daily rollover."
            else:
                message = "The active bounty claim was canceled."

        self._cancel_claim_task(guild.id)
        return {"message": message, "channel_id": int(message_channel_id)}

    async def _complete_claim(self, guild: discord.Guild) -> Optional[str]:
        async with self._lock_for(guild.id):
            st = _bounty_state(guild.id)
            if bool(st.get("resolved", False)):
                return None
            claimant = _claimant_member(guild, st)
            target = _target_member(guild, st)
            if claimant is None or target is None or not self._claim_is_valid(guild, st):
                return None

            claim_minutes_eq = _projected_claim_minutes_equivalent(st)
            reward = await self._grant_minutes_equivalent_boost(
                claimant,
                minutes_equivalent=claim_minutes_eq,
                source="bounty claim",
            )
            debuff = await grant_fixed_debuff(
                target,
                pct=BOUNTY_FAIL_DEBUFF_PCT,
                minutes=BOUNTY_FAIL_DEBUFF_MINUTES,
                source="bounty failure",
                reward_seed_xp=int(round(float(BOUNTY_FAIL_DEBUFF_PCT) * 100.0 * max(1, int(BOUNTY_FAIL_DEBUFF_MINUTES)))),
            )

            exposure_minutes = max(0, _as_int(st.get("exposure_minutes", 0), 0))
            exposure_companion_total = max(0.0, _as_float(st.get("exposure_companion_total", 0.0), 0.0))

            record_game_fields(
                guild.id,
                claimant.id,
                "bounty",
                wins=1,
                claims_completed=1,
                reward_minutes_equivalent_total=claim_minutes_eq,
                boost_seed_xp_total=reward["bonus_xp"],
                boost_percent_total=reward["percent"],
                boost_minutes_total=reward["minutes"],
            )
            record_game_fields(
                guild.id,
                target.id,
                "bounty",
                losses=1,
                exposure_minutes_total=exposure_minutes,
                exposure_companion_total=exposure_companion_total,
                debuff_percent_total=debuff["percent"],
                debuff_minutes_total=debuff["minutes"],
            )

            st["resolved"] = True
            st["result"] = "claimed"
            st["winner_user_id"] = int(claimant.id)
            claim_message_channel_id = _as_int(st.get("claim_message_channel_id", 0), 0)
            st["last_reward"] = {
                "winner_user_id": int(claimant.id),
                "claim_message_channel_id": int(claim_message_channel_id),
                "claim_minutes_equivalent": int(claim_minutes_eq),
                "claim_percent": float(reward["percent"]),
                "claim_minutes": int(reward["minutes"]),
                "target_debuff_percent": float(debuff["percent"]),
                "target_debuff_minutes": int(debuff["minutes"]),
            }
            _clear_claim_state(st)
            await save_data()
            await enforce_level6_exclusive(guild)

            target_debuff_line = (
                f"**-{float(debuff.get('percent', 0.0)):.1f}% XP/min** for **{int(debuff.get('minutes', 0))}m**"
                if not debuff.get("blocked", False)
                else "a blocked debuff (mulligan consumed)"
            )
            return (
                f"{claimant.mention} collected the bounty on {target.mention} and earned "
                f"**+{float(reward.get('percent', 0.0)):.1f}% XP/min** for **{int(reward.get('minutes', 0))}m** "
                f"(about **{_fmt_duration_minutes(claim_minutes_eq)}** of passive gain). "
                f"{target.mention} took {target_debuff_line}."
            )

    async def _claim_countdown(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(max(1, int(BOUNTY_CLAIM_SECONDS)))
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return

            text = await self._complete_claim(guild)
            if text is None:
                async with self._lock_for(guild.id):
                    st = _bounty_state(guild.id)
                    should_cancel = (
                        _as_int(st.get("claimant_user_id", 0), 0) > 0
                        and not bool(st.get("resolved", False))
                        and not self._claim_is_valid(guild, st)
                    )
                if should_cancel:
                    payload = await self._cancel_active_claim(guild, reason="movement", stopped_by_target=False)
                    if payload is not None:
                        channel = _message_channel(guild, _as_int(payload.get("channel_id", 0), 0)) or get_log_channel(guild)
                        if channel is not None:
                            try:
                                await channel.send(str(payload.get("message", "")))
                            except Exception:
                                pass
                return

            async with self._lock_for(guild.id):
                st = _bounty_state(guild.id)
                if not bool(st.get("resolved", False)):
                    return
                reward_row = _as_dict(st.get("last_reward"))
                if str(st.get("result", "")) != "claimed":
                    return
                channel = _message_channel(guild, _as_int(reward_row.get("claim_message_channel_id", 0), 0)) or get_log_channel(guild)
            if channel is not None:
                try:
                    await channel.send(text)
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            current = self._claim_tasks.get(int(guild_id))
            if current is asyncio.current_task():
                self._claim_tasks.pop(int(guild_id), None)

    async def _start_claim(self, ctx: commands.Context, target: discord.Member) -> None:
        guild = ctx.guild
        if guild is None:
            return

        notices = await self._ensure_today_state(guild)
        await self._send_rollover_notices(guild, notices)

        async with self._lock_for(guild.id):
            st = _bounty_state(guild.id)
            if str(st.get("date", "")) != _today_key():
                await ctx.reply("Bounty state is still syncing. Try again in a moment.")
                return
            if bool(st.get("resolved", False)):
                winner = guild.get_member(_as_int(st.get("winner_user_id", 0), 0))
                if str(st.get("result", "")) == "claimed" and winner is not None:
                    await ctx.reply(f"Today's bounty is already over. **{winner.display_name}** already collected it.")
                else:
                    await ctx.reply("Today's bounty is already over.")
                return
            if _as_int(st.get("target_user_id", 0), 0) != target.id:
                current = _target_member(guild, st)
                if current is None:
                    await ctx.reply("There is no active bounty target right now.")
                else:
                    await ctx.reply(f"Today's bounty is on **{current.display_name}**, not **{target.display_name}**.")
                return
            if ctx.author.id == target.id:
                await ctx.reply("You cannot claim your own bounty.")
                return

            changed = _prune_cooldowns(st)
            cooldown_expires = _as_int(_as_dict(st.get("cooldowns")).get(str(ctx.author.id), 0), 0)
            now_ts = int(_utcnow().timestamp())
            if cooldown_expires > now_ts:
                if changed:
                    await save_data()
                await ctx.reply(
                    f"You cannot start another bounty claim yet. Cooldown remaining: **{_fmt_duration_seconds(cooldown_expires - now_ts)}**."
                )
                return

            claimant_channel = getattr(getattr(ctx.author, "voice", None), "channel", None)
            target_channel = getattr(getattr(target, "voice", None), "channel", None)
            if not _is_eligible_voice_channel(claimant_channel):
                await ctx.reply("Join a normal voice channel with the target before trying to claim the bounty.")
                return
            if target_channel is None or claimant_channel.id != target_channel.id:
                await ctx.reply(f"You need to be in the same voice channel as **{target.display_name}** to start the claim.")
                return

            active_claimant_id = _as_int(st.get("claimant_user_id", 0), 0)
            if active_claimant_id > 0:
                claimant = guild.get_member(active_claimant_id)
                remaining = _claim_remaining_seconds(st)
                name = claimant.display_name if claimant is not None else "Someone"
                await ctx.reply(
                    f"A bounty claim is already in progress by **{name}**. "
                    f"Time remaining: **{_fmt_duration_seconds(remaining)}**."
                )
                return

            expires_at = _utcnow() + timedelta(seconds=max(1, int(BOUNTY_CLAIM_SECONDS)))
            st["claimant_user_id"] = int(ctx.author.id)
            st["claim_started_at"] = _iso(_utcnow())
            st["claim_expires_at"] = _iso(expires_at)
            st["claim_channel_id"] = int(claimant_channel.id)
            st["claim_message_channel_id"] = int(ctx.channel.id)
            record_game_fields(guild.id, ctx.author.id, "bounty", claims_started=1)
            record_game_fields(guild.id, target.id, "bounty", claims_against=1)
            await save_data()

        self._claim_tasks[guild.id] = asyncio.create_task(self._claim_countdown(guild.id))
        projected_claim = _projected_claim_minutes_equivalent(_bounty_state(guild.id))
        await ctx.reply(
            f"{ctx.author.mention} started a bounty claim on {target.mention}. "
            f"Stay in **{claimant_channel.name}** together for **{_fmt_duration_seconds(BOUNTY_CLAIM_SECONDS)}** to collect it. "
            f"{target.mention} can cancel the attempt with `{ctx.clean_prefix}b stop`. "
            f"Projected payout is about **{_fmt_duration_minutes(projected_claim)}** of passive gain."
        )

    async def _stop_claim(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return

        notices = await self._ensure_today_state(guild)
        await self._send_rollover_notices(guild, notices)

        async with self._lock_for(guild.id):
            st = _bounty_state(guild.id)
            target_id = _as_int(st.get("target_user_id", 0), 0)
            claimant_id = _as_int(st.get("claimant_user_id", 0), 0)
            if claimant_id <= 0:
                await ctx.reply("There is no active bounty claim to stop.")
                return
            if ctx.author.id != target_id:
                await ctx.reply("Only the current bounty target can use `stop`.")
                return

        payload = await self._cancel_active_claim(guild, reason="stopped", stopped_by_target=True)
        if payload is None:
            await ctx.reply("There is no active bounty claim to stop.")
            return
        text = str(payload.get("message", ""))
        await ctx.reply(text)
        channel = _message_channel(guild, _as_int(payload.get("channel_id", 0), 0))
        if channel is not None and _as_int(getattr(channel, "id", 0), 0) != _as_int(ctx.channel.id, 0):
            try:
                await channel.send(text)
            except Exception:
                return

    def _status_lines(self, guild: discord.Guild, viewer_id: int) -> list[str]:
        st = _bounty_state(guild.id)
        lines: list[str] = ["**Today's Bounty**"]

        if str(st.get("date", "")) != _today_key():
            lines.append("Status is syncing. Try again in a moment.")
            return lines

        target_id = _as_int(st.get("target_user_id", 0), 0)
        if target_id <= 0:
            lines.append("No bounty target was assigned today.")
            return lines
        target = guild.get_member(target_id)
        target_label = target.mention if target is not None else f"<@{target_id}>"

        lines.append(f"Target: {target_label}")
        lines.append(f"Revealed: **{'yes' if bool(st.get('announced', False)) else 'not yet'}**")

        exposure_minutes = max(0, _as_int(st.get("exposure_minutes", 0), 0))
        companion_total = max(0.0, _as_float(st.get("exposure_companion_total", 0.0), 0.0))
        avg_companions = (companion_total / exposure_minutes) if exposure_minutes > 0 else 0.0
        projected_survivor = _projected_survivor_minutes_equivalent(st)
        projected_claim = _projected_claim_minutes_equivalent(st)

        lines.append(
            f"Exposure banked: **{_fmt_duration_minutes(exposure_minutes)}** "
            f"(avg companions **{avg_companions:.1f}**)."
        )
        if projected_survivor > 0:
            lines.append(
                f"Current survivor reward pace: about **{_fmt_duration_minutes(projected_survivor)}** of passive gain."
            )
        else:
            lines.append(
                f"Current survivor reward pace: **0** until **{int(BOUNTY_SURVIVOR_MIN_EXPOSURE_MINUTES)}m** of eligible voice exposure."
            )

        if bool(st.get("resolved", False)):
            if str(st.get("result", "")) == "claimed":
                winner = guild.get_member(_as_int(st.get("winner_user_id", 0), 0))
                if winner is not None:
                    lines.append(f"Outcome: collected by **{winner.display_name}**.")
                else:
                    lines.append("Outcome: collected.")
            else:
                lines.append("Outcome: the target survived the day.")
            return lines

        claimant = _claimant_member(guild, st)
        if claimant is not None:
            lines.append(
                f"Active claim: **{claimant.display_name}** is holding the bounty. "
                f"Time remaining: **{_fmt_duration_seconds(_claim_remaining_seconds(st))}**."
            )
            lines.append(
                f"If the hold finishes, projected claimant payout is about **{_fmt_duration_minutes(projected_claim)}** of passive gain."
            )
        else:
            lines.append("Active claim: none.")

        cooldown_expires = _as_int(_as_dict(st.get("cooldowns")).get(str(viewer_id), 0), 0)
        remaining = max(0, cooldown_expires - int(_utcnow().timestamp()))
        if remaining > 0:
            lines.append(f"Your claim cooldown: **{_fmt_duration_seconds(remaining)}**.")

        lines.append(
            f"Use `{COMMAND_PREFIX}b @user` from the same voice channel to start a claim, or `{COMMAND_PREFIX}b stop` if the bounty is on you."
        )
        return lines

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            async with self._lock_for(guild.id):
                st = _bounty_state(guild.id)
                if _as_int(st.get("claimant_user_id", 0), 0) > 0:
                    _clear_claim_state(st)
                    await save_data()
        if not self.bounty_loop.is_running():
            self.bounty_loop.start()

    @tasks.loop(minutes=1)
    async def bounty_loop(self):
        for guild in self.bot.guilds:
            notices = await self._ensure_today_state(guild)
            await self._send_rollover_notices(guild, notices)

            async with self._lock_for(guild.id):
                st = _bounty_state(guild.id)
                if str(st.get("date", "")) != _today_key():
                    continue
                if bool(st.get("resolved", False)):
                    continue
                target = _target_member(guild, st)
                if target is None:
                    continue
                channel = getattr(getattr(target, "voice", None), "channel", None)
                humans = _human_members(channel)
                if target not in humans:
                    continue
                companions = max(0, len(humans) - 1)
                if companions <= 0:
                    continue
                st["exposure_minutes"] = _as_int(st.get("exposure_minutes", 0), 0) + 1
                st["exposure_companion_total"] = _as_float(st.get("exposure_companion_total", 0.0), 0.0) + float(
                    min(max(0, int(BOUNTY_SURVIVOR_MAX_COMPANIONS)), companions)
                )
                await save_data()

    @bounty_loop.before_loop
    async def _before_bounty_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        member = message.author if isinstance(message.author, discord.Member) else message.guild.get_member(message.author.id)
        if member is None or member.bot:
            return

        notices = await self._ensure_today_state(message.guild)
        await self._send_rollover_notices(message.guild, notices)

        if _note_daily_activity(message.guild.id, member.id, _today_key()):
            await save_data()

        await self._maybe_announce_target(message.guild, member, channel=message.channel)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot or member.guild is None:
            return

        notices = await self._ensure_today_state(member.guild)
        await self._send_rollover_notices(member.guild, notices)

        before_channel = before.channel
        after_channel = after.channel
        if not _is_eligible_voice_channel(before_channel) and _is_eligible_voice_channel(after_channel):
            if _note_daily_activity(member.guild.id, member.id, _today_key()):
                await save_data()
            await self._maybe_announce_target(member.guild, member, channel=get_log_channel(member.guild))

        async with self._lock_for(member.guild.id):
            st = _bounty_state(member.guild.id)
            tracked_ids = {
                _as_int(st.get("target_user_id", 0), 0),
                _as_int(st.get("claimant_user_id", 0), 0),
            }
            is_active_claim = _as_int(st.get("claimant_user_id", 0), 0) > 0 and not bool(st.get("resolved", False))

        if is_active_claim and member.id in tracked_ids:
            async with self._lock_for(member.guild.id):
                st = _bounty_state(member.guild.id)
                if _as_int(st.get("claimant_user_id", 0), 0) > 0 and not self._claim_is_valid(member.guild, st):
                    pass
                else:
                    return
            payload = await self._cancel_active_claim(member.guild, reason="movement", stopped_by_target=False)
            if payload:
                channel = _message_channel(member.guild, _as_int(payload.get("channel_id", 0), 0))
                channel = channel or get_log_channel(member.guild)
                if channel is not None:
                    try:
                        await channel.send(str(payload.get("message", "")))
                    except Exception:
                        return

    @commands.command(name="bounty", aliases=["b"])
    async def bounty(self, ctx: commands.Context, *args: str):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        notices = await self._ensure_today_state(ctx.guild)
        await self._send_rollover_notices(ctx.guild, notices)

        if not args:
            await ctx.reply("\n".join(self._status_lines(ctx.guild, ctx.author.id)))
            return

        joined = " ".join(args).strip()
        if joined.lower() == "stop":
            await self._stop_claim(ctx)
            return

        if ctx.message.mentions:
            target = next((m for m in ctx.message.mentions if isinstance(m, discord.Member) and not m.bot), None)
            if target is None:
                await ctx.reply(f"Usage: `{ctx.clean_prefix}b @user` or `{ctx.clean_prefix}b stop`")
                return
            await self._start_claim(ctx, target)
            return

        try:
            target = await commands.MemberConverter().convert(ctx, joined)
        except commands.BadArgument:
            await ctx.reply(f"Usage: `{ctx.clean_prefix}b` | `{ctx.clean_prefix}b @user` | `{ctx.clean_prefix}b stop`")
            return

        if target.bot:
            await ctx.reply("Bots do not carry bounties.")
            return
        await self._start_claim(ctx, target)
