# blackjack.py
from __future__ import annotations
from typing import Dict, List, Optional, Tuple
import random
import time
import asyncio
import re
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

# --- your project-specific imports (unchanged) ---
from .config import (
    BJ_MAX_PLAYERS,  # if unused, safe to remove
    BJ_DAILY_RESET_HOUR,
    BJ_DAILY_RESET_MINUTE,
    BJ_TURN_TIMEOUT_SECONDS,
    BJ_SEAT_IDLE_TIMEOUT_SECONDS,
    BJ_COOLDOWN_ENABLED,
)
from .emojis import (
    EMOJI_BANKNOTE_WITH_DOLLAR_SIGN,
    EMOJI_BLACK_CLUB_SUIT,
    EMOJI_BLACK_DIAMOND_SUIT,
    EMOJI_BLACK_HEART_SUIT,
    EMOJI_BLACK_RIGHT_POINTING_TRIANGLE,
    EMOJI_BLACK_SCISSORS,
    EMOJI_BLACK_SPADE_SUIT,
    EMOJI_COLLISION_SYMBOL,
    EMOJI_HAMMER_AND_WRENCH,
    EMOJI_KEYCAP_2,
    EMOJI_LARGE_GREEN_SQUARE,
    EMOJI_LARGE_RED_SQUARE,
    EMOJI_OCTAGONAL_SIGN,
    EMOJI_RAISED_HAND,
    EMOJI_RECYCLING_SYMBOL,
    EMOJI_REGIONAL_INDICATOR_SYMBOL_LETTER_D,
    EMOJI_REGIONAL_INDICATOR_SYMBOL_LETTER_H,
    EMOJI_REGIONAL_INDICATOR_SYMBOL_LETTER_R,
    EMOJI_REGIONAL_INDICATOR_SYMBOL_LETTER_S,
    EMOJI_STANDING_PERSON,
    EMOJI_VICTORY_HAND,
    EMOJI_WAVING_WHITE_FLAG,
    EMOJI_WHITE_RIGHT_POINTING_BACKHAND_INDEX,
)
from .guild_setup import get_blackjack_channel_id
from .storage import _gdict, _udict, save_data
from .stats_store import record_game_fields, record_xp_change
from .spin_support import consume_blackjack_natural_charge
from .xp import apply_xp_change
from .roles import announce_level_up, sync_level_roles, enforce_level6_exclusive
from .ownership import owner_only
from .time_windows import LOCAL_TZ


# =========================
# Cards & Emoji UI
# =========================
SUITS = [
    EMOJI_BLACK_SPADE_SUIT,
    EMOJI_BLACK_HEART_SUIT,
    EMOJI_BLACK_DIAMOND_SUIT,
    EMOJI_BLACK_CLUB_SUIT,
]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

EMOJI_DEAL = EMOJI_BLACK_RIGHT_POINTING_TRIANGLE
EMOJI_ALL_IN = EMOJI_BANKNOTE_WITH_DOLLAR_SIGN
EMOJI_HIT = EMOJI_LARGE_GREEN_SQUARE
EMOJI_STAND = EMOJI_LARGE_RED_SQUARE
EMOJI_DD = EMOJI_KEYCAP_2
EMOJI_SURRENDER = EMOJI_WAVING_WHITE_FLAG
EMOJI_SPLIT = EMOJI_BLACK_SCISSORS

# Accept both unicode and the Discord alias for the play arrow
DEAL_EMOJIS = {EMOJI_DEAL.replace("\ufe0f", ""), EMOJI_DEAL, "arrow_forward"}
ALL_IN_EMOJIS = {EMOJI_ALL_IN, "dollar"}

EMOJI_JOIN = EMOJI_ALL_IN
EMOJI_LEAVE = EMOJI_OCTAGONAL_SIGN
JOIN_EMOJIS = set(ALL_IN_EMOJIS)
LEAVE_EMOJIS = {EMOJI_LEAVE, "octagonal_sign"}

RESET_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{1,2}))?(am|pm)?$")

MESSAGE_RETENTION_SECONDS = 30 * 60 # Messages deleted from channel after 30 min
MESSAGE_CLEANUP_INTERVAL_SECONDS = 5 * 60

# Map normalized emoji -> action
ACTION_EMOJI_MAP = {
    EMOJI_HIT: "hit",
    EMOJI_STAND: "stand",
    EMOJI_DD: "dd",
    EMOJI_SURRENDER: "surrender",
    EMOJI_SPLIT: "split",
    # Optional alternates:
    EMOJI_REGIONAL_INDICATOR_SYMBOL_LETTER_H: "hit",
    EMOJI_REGIONAL_INDICATOR_SYMBOL_LETTER_S: "stand",
    EMOJI_REGIONAL_INDICATOR_SYMBOL_LETTER_D: "dd",
    EMOJI_REGIONAL_INDICATOR_SYMBOL_LETTER_R: "surrender",
}

def _norm_emoji_name(e: discord.PartialEmoji | str) -> str:
    """Normalize for comparison: strip VS16 so EMOJI_DEAL variants compare equal."""
    try:
        name = e.name if isinstance(e, discord.PartialEmoji) else str(e)
    except Exception:
        name = str(e)
    return name.replace("\ufe0f", "")


# =========================
# Card helpers
# =========================
def new_shoe(decks: int = 4) -> List[str]:
    cards = [f"{r}{s}" for s in SUITS for r in RANKS]
    shoe = cards * decks
    random.shuffle(shoe); random.shuffle(shoe)
    return shoe

def value_of_hand(cards: List[str]) -> Tuple[int, bool]:
    """Return (best_total, is_blackjack_on_first_two)."""
    vals = []; aces = 0
    for c in cards:
        r = c[:-1] if c[:-1] else c[0]
        if r == "A": aces += 1
        elif r in ("K", "Q", "J"): vals.append(10)
        else: vals.append(int(r))
    total = sum(vals)
    for i in range(aces):
        if total + 11 <= 21 - (aces - 1 - i): total += 11
        else: total += 1
    return total, (len(cards) == 2 and total == 21)

def pretty(cards: List[str]) -> str:
    return " ".join(cards)

def now_ts() -> int:
    return int(time.time())

def rank_of(card: str) -> str:
    r = card[:-1] if card[:-1] else card[0]
    return r


def _pop_card_with_rank(st: dict, ranks: set[str]) -> Optional[str]:
    shoe = st.get("shoe")
    if not isinstance(shoe, list):
        shoe = []
        st["shoe"] = shoe
    for i, c in enumerate(list(shoe)):
        if rank_of(str(c)) in ranks:
            try:
                return str(shoe.pop(i))
            except Exception:
                return str(c)
    return None


def _natural_hand_from_shoe(st: dict) -> list[str]:
    ace = _pop_card_with_rank(st, {"A"}) or f"A{random.choice(SUITS)}"
    ten = _pop_card_with_rank(st, {"10", "J", "Q", "K"}) or f"K{random.choice(SUITS)}"
    if random.random() < 0.5:
        return [ace, ten]
    return [ten, ace]


def _opening_hand_snapshot(cards: object) -> list[str]:
    if not isinstance(cards, list) or len(cards) < 2:
        return []
    return [str(cards[0]), str(cards[1])]


def _sanitize_reset_time(hour: int, minute: int) -> tuple[int, int]:
    try:
        h = int(hour)
    except Exception:
        h = int(BJ_DAILY_RESET_HOUR)
    try:
        m = int(minute)
    except Exception:
        m = int(BJ_DAILY_RESET_MINUTE)
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


def _cycle_key(hour: int, minute: int, *, now: Optional[datetime] = None) -> str:
    now_local = now or datetime.now(LOCAL_TZ)
    boundary = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_local < boundary:
        use_date = (now_local - timedelta(days=1)).date()
    else:
        use_date = now_local.date()
    return use_date.isoformat()


# =========================
# Table State
# =========================
def _table(gid: int) -> dict:
    g = _gdict(gid)
    st = g.setdefault("blackjack", {})
    st.setdefault("dealing_lock", False)
    st.setdefault("active", True)  # legacy flag; table is always available
    st.setdefault("players", {})  # uid -> per-player dict (see below)
    st.setdefault("shoe", [])
    st.setdefault("dealer", [])
    st.setdefault("phase", "betting")  # betting|dealing|acting|dealer|payout
    st.setdefault("turn_idx", 0)
    st.setdefault("channel_id", 0)
    st.setdefault("last_action_ts", 0)
    st.setdefault("last_cleanup_ts", 0)
    st.setdefault("turn_started_ts", 0)
    st.setdefault("deal_msg_id", 0)     # current deal-button message id
    st.setdefault("action_msg_id", 0)   # current action prompt id
    st.setdefault("reset_hour", int(BJ_DAILY_RESET_HOUR))
    st.setdefault("reset_minute", int(BJ_DAILY_RESET_MINUTE))
    st.setdefault("cooldown_enabled", bool(BJ_COOLDOWN_ENABLED))
    st.setdefault("xp_betting_migrated_v1", False)
    st.setdefault("last_cycle_key", "")
    st["reset_hour"], st["reset_minute"] = _sanitize_reset_time(
        st.get("reset_hour", BJ_DAILY_RESET_HOUR),
        st.get("reset_minute", BJ_DAILY_RESET_MINUTE),
    )
    if not bool(st.get("xp_betting_migrated_v1", False)):
        st["cooldown_enabled"] = False
        st["xp_betting_migrated_v1"] = True
    return st

def in_right_channel(ctx: commands.Context) -> bool:
    if ctx.guild is None:
        return True
    st = _table(ctx.guild.id)
    configured_channel_id = int(get_blackjack_channel_id(ctx.guild.id) or 0)
    if configured_channel_id > 0:
        st["channel_id"] = configured_channel_id
    channel_id = int(st.get("channel_id", 0) or 0)
    if channel_id <= 0:
        return True
    return bool(ctx.channel and ctx.channel.id == channel_id)


# =========================
# Blackjack Cog
# =========================
class BlackjackCog(commands.Cog):
    """
    Blackjack table flow:
      - Bet with `!bj <amount>` or use the all-in reaction on the deal prompt.
      - Use the cancel reaction to remove a pending bet before the hand starts.
      - Use the deal reaction to start the hand.
      - Wagers settle directly in XP using standard blackjack payouts.
      - Optional daily loss lockout can be enabled by admin, but defaults off.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.debug_enabled: Dict[int, bool] = {}  # guild_id -> bool
        if not self.guard_loop.is_running():
            self.guard_loop.start()

    # -------- Debug helpers --------
    def _dbg(self, guild_id: int) -> bool:
        return bool(self.debug_enabled.get(guild_id, False))

    async def _dprint(self, guild: discord.Guild, channel: discord.TextChannel, msg: str):
        if self._dbg(guild.id):
            await channel.send(f"{EMOJI_HAMMER_AND_WRENCH} **BJ DEBUG**: {msg}")

    def _cooldown_enabled(self, gid: int) -> bool:
        st = _table(gid)
        return bool(st.get("cooldown_enabled", BJ_COOLDOWN_ENABLED))

    def _current_cycle_key(self, gid: int) -> str:
        st = _table(gid)
        h, m = _sanitize_reset_time(st.get("reset_hour", BJ_DAILY_RESET_HOUR), st.get("reset_minute", BJ_DAILY_RESET_MINUTE))
        return _cycle_key(h, m)

    def _daily_state(self, gid: int, uid: int, cycle_key: str) -> dict:
        u = _udict(gid, uid)
        d = u.get("blackjack_daily")
        if d is None or not isinstance(d, dict):
            d = {"cycle_key": cycle_key, "locked": False, "locked_ts": 0, "last_result": "", "streak": 0}
            u["blackjack_daily"] = d
        d.setdefault("cycle_key", cycle_key)
        d.setdefault("locked", False)
        d.setdefault("locked_ts", 0)
        d.setdefault("last_result", "")
        d.setdefault("streak", 0)
        if d.get("cycle_key") != cycle_key:
            d["cycle_key"] = cycle_key
            d["locked"] = False
            d["locked_ts"] = 0
            d["last_result"] = ""
            d["streak"] = 0
        return d

    def _is_locked(self, gid: int, uid: int) -> tuple[bool, dict]:
        d = self._daily_state(gid, uid, self._current_cycle_key(gid))
        if not self._cooldown_enabled(gid):
            return False, d
        return bool(d.get("locked", False)), d

    async def _roll_cycle_if_needed(self, guild: discord.Guild, channel: Optional[discord.TextChannel] = None):
        st = _table(guild.id)
        h, m = _sanitize_reset_time(st.get("reset_hour", BJ_DAILY_RESET_HOUR), st.get("reset_minute", BJ_DAILY_RESET_MINUTE))
        current_key = _cycle_key(h, m)
        prev_key = str(st.get("last_cycle_key", ""))
        if prev_key == current_key:
            return
        st["last_cycle_key"] = current_key
        await save_data()
        if prev_key and channel is not None:
            try:
                await channel.send(f"Blackjack reset window hit ({_draw_time_label(h, m)}). Locked players are eligible again.")
            except Exception:
                pass

    def _natural_blackjack_payout(self, bet: int) -> int:
        wager = max(0, int(bet))
        return (wager * 5) // 2

    async def _set_round_result_state(self, guild: discord.Guild, uid: int, *, result: str) -> bool:
        d = self._daily_state(guild.id, uid, self._current_cycle_key(guild.id))
        normalized = str(result).strip().lower()
        lost = normalized == "loss"
        cooldown_on = self._cooldown_enabled(guild.id)
        d["locked"] = bool(lost and cooldown_on)
        d["locked_ts"] = now_ts() if lost and cooldown_on else 0
        d["last_result"] = normalized if normalized in {"win", "loss", "push"} else ""
        d["streak"] = 0
        await save_data()
        return bool(lost and cooldown_on)

    # -------- State helpers --------
    def _player(self, gid: int, uid: int) -> dict:
        st = _table(gid)
        p = st["players"].get(str(uid))
        if not p:
            p = {
                "bet": 0,
                "locked": 0,
                "hand": [],  # Hand 1
                "hand2": [], # Hand 2 (after split)
                "dealt_hand": [],
                "replay_hand": [],
                "split": False,
                "active_hand": 0,  # 0 or 1
                # per-hand flags
                "stood": False, "busted": False, "finished": False, "doubled": False, "surrendered": False,
                "stood2": False, "busted2": False, "finished2": False, "doubled2": False, "surrendered2": False,
                "status": "betting",
                "in_table": False,
                "joined_ts": now_ts(),
                "last_active_ts": now_ts(),
                "natural_bj": False,  # only for unsplit original
            }
            st["players"][str(uid)] = p
        else:
            p.setdefault("in_table", False)
            p.setdefault("locked", 0)
            p.setdefault("dealt_hand", [])
            p.setdefault("replay_hand", [])
            p.setdefault("last_active_ts", now_ts())
        return p

    def _hand_ref(self, p: dict) -> List[str]:
        return p["hand"] if p.get("active_hand", 0) == 0 else p["hand2"]

    def _get_flag(self, p: dict, name: str) -> bool:
        return p[name] if p.get("active_hand", 0) == 0 else p[f"{name}2"]

    def _set_flag(self, p: dict, name: str, val: bool):
        if p.get("active_hand", 0) == 0:
            p[name] = val
        else:
            p[f"{name}2"] = val

    def _both_hands_finished(self, p: dict) -> bool:
        if not p.get("split"):
            return p.get("finished", False)
        return p.get("finished", False) and p.get("finished2", False)

    async def _apply_member_xp(
        self,
        guild: discord.Guild,
        member: discord.Member,
        delta: int,
        *,
        sync_level: bool = True,
        source: str = "blackjack",
    ):
        if delta != 0:
            # Balance-only move (wallet lock/refund/payout); optional level sync later.
            u = _udict(guild.id, member.id)
            total_xp = float(u.get("xp_f", u.get("xp", 0)))
            new_total = max(0.0, total_xp + float(delta))
            applied_delta = new_total - total_xp
            u["xp_f"] = float(new_total)
            u["xp"] = int(new_total)
            if applied_delta != 0.0:
                record_xp_change(guild.id, member.id, applied_delta, source=source)
            await save_data()

        if not sync_level:
            return

        # Recompute level/roles once from the final post-hand balance.
        changed = await apply_xp_change(member, 0, source=source)
        if changed:
            old_lvl, new_lvl = changed
            if new_lvl > old_lvl:
                await announce_level_up(member, new_lvl)
            await sync_level_roles(member, new_lvl)
        else:
            u = _udict(guild.id, member.id)
            await sync_level_roles(member, int(u.get("level", 1)))
        await enforce_level6_exclusive(guild)

    def _seated_order(self, st: dict, guild: discord.Guild) -> List[int]:
        items = []
        for uid, p in st["players"].items():
            if p.get("in_table"):
                items.append((p.get("joined_ts", 0), int(uid)))
        items.sort(key=lambda t: (t[0], t[1]))
        return [uid for _, uid in items if guild.get_member(uid)]

    def _reset_hand(self, st: dict):
        st["dealer"] = []
        st["turn_idx"] = 0
        if len(st["shoe"]) < 52:
            st["shoe"] = new_shoe(4)
        for p in st["players"].values():
            p.update({
                "hand": [], "hand2": [], "dealt_hand": [],
                "split": False, "active_hand": 0,
                "status": "betting",
                "stood": False, "busted": False, "finished": False, "doubled": False, "surrendered": False,
                "stood2": False, "busted2": False, "finished2": False, "doubled2": False, "surrendered2": False,
                "natural_bj": False,
            })

    def _deal_card(self, st: dict) -> str:
        if not st["shoe"]:
            st["shoe"] = new_shoe(4)
        return st["shoe"].pop()

    async def _broadcast(self, ctx_or_channel, msg: str):
        try:
            if isinstance(ctx_or_channel, commands.Context):
                return await ctx_or_channel.send(msg)
            else:
                return await ctx_or_channel.send(msg)
        except Exception:
            return None

    def _touch_player(self, p: dict):
        p["last_active_ts"] = now_ts()

    def _touch(self, st: dict): st["last_action_ts"] = now_ts()
    def _start_turn(self, st: dict):
        st["turn_started_ts"] = now_ts()
        st["last_action_ts"] = now_ts()
    class _ShimCtx:
        def __init__(self, guild: discord.Guild, channel: discord.TextChannel):
            self.guild = guild
            self.channel = channel

    # =========================
    # Deal button helpers
    # =========================
    async def _resolve_member(self, guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        member = guild.get_member(user_id)
        if member:
            return member
        try:
            return await guild.fetch_member(user_id)
        except Exception:
            return None

    async def _cleanup_old_blackjack_messages(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        force: bool = False,
    ):
        st = _table(guild.id)
        now = now_ts()
        last_cleanup = int(st.get("last_cleanup_ts", 0))
        if not force and (now - last_cleanup) < MESSAGE_CLEANUP_INTERVAL_SECONDS:
            return

        cutoff_ts = discord.utils.utcnow().timestamp() - MESSAGE_RETENTION_SECONDS
        bot_user_id = int(self.bot.user.id) if self.bot and self.bot.user else 0
        preserve_ids: set[int] = set()
        for k in ("deal_msg_id", "action_msg_id"):
            try:
                mid = int(st.get(k, 0) or 0)
            except Exception:
                mid = 0
            if mid > 0:
                preserve_ids.add(mid)

        # Fallback: if deal_msg_id was lost, keep the newest table prompt-looking bot message.
        if int(st.get("deal_msg_id", 0) or 0) <= 0:
            try:
                async for recent in channel.history(limit=40, oldest_first=False):
                    if bot_user_id and recent.author.id != bot_user_id:
                        continue
                    content = str(recent.content or "")
                    if "DEAL" in content and "ALL IN" in content:
                        preserve_ids.add(recent.id)
                        break
            except (discord.Forbidden, discord.HTTPException):
                pass

        deleted_ids: set[int] = set()
        try:
            async for msg in channel.history(limit=None, oldest_first=True):
                if msg.created_at.timestamp() >= cutoff_ts:
                    break
                if msg.pinned:
                    continue
                # Only clean up bot-authored messages; never touch user messages.
                if bot_user_id and msg.author.id != bot_user_id:
                    continue
                # Never delete currently active blackjack prompts.
                if msg.id in preserve_ids:
                    continue
                try:
                    await msg.delete()
                    deleted_ids.add(msg.id)
                except (discord.NotFound, discord.Forbidden):
                    continue
                except discord.HTTPException:
                    await asyncio.sleep(0.2)
                    continue
        except (discord.Forbidden, discord.HTTPException):
            # Missing permissions or transient API errors; try again on next cycle.
            st["last_cleanup_ts"] = now
            await save_data()
            return

        if st.get("deal_msg_id", 0) in deleted_ids:
            st["deal_msg_id"] = 0
        if st.get("action_msg_id", 0) in deleted_ids:
            st["action_msg_id"] = 0

        st["last_cleanup_ts"] = now
        await save_data()

    async def _join_table(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        member: discord.Member,
        *,
        bet_amount: Optional[int] = None,
        all_in: bool = False,
    ) -> bool:
        st = _table(guild.id)
        if st["phase"] not in ("betting", "idle"):
            await channel.send("Betting is locked while a hand is active.")
            return False

        locked, daily = self._is_locked(guild.id, member.id)
        if locked:
            h, m = _sanitize_reset_time(st.get("reset_hour", BJ_DAILY_RESET_HOUR), st.get("reset_minute", BJ_DAILY_RESET_MINUTE))
            nxt = _next_reset_dt(h, m)
            await channel.send(f"**{member.display_name}** is locked out until **{nxt.strftime('%Y-%m-%d %I:%M %p ET')}**.")
            return False

        p = self._player(guild.id, member.id)
        current_locked = int(p.get("locked", 0))
        u = _udict(guild.id, member.id)
        current_xp = int(u.get("xp_f", u.get("xp", 0)))
        bankroll = max(0, current_xp + current_locked)
        desired_bet = bankroll if all_in else max(0, int(bet_amount or 0))
        if desired_bet <= 0:
            if all_in:
                await channel.send(f"**{member.display_name}** has no XP available to go all in.")
            else:
                await channel.send("Bet amount must be at least 1 XP.")
            return False
        if desired_bet > bankroll:
            await channel.send(
                f"**{member.display_name}** only has **{bankroll} XP** available but needs **{desired_bet} XP**."
            )
            return False

        seated_count = sum(1 for seat in st.get("players", {}).values() if seat.get("in_table"))
        if not p.get("in_table") and seated_count >= BJ_MAX_PLAYERS:
            await channel.send(f"Table is full (**{BJ_MAX_PLAYERS}** players).")
            return False

        if current_locked > 0:
            await self._apply_member_xp(
                guild,
                member,
                current_locked,
                sync_level=False,
                source="blackjack refund pending",
            )

        await self._apply_member_xp(
            guild,
            member,
            -desired_bet,
            sync_level=False,
            source="blackjack ante",
        )

        was_seated = bool(p.get("in_table"))
        p["bet"] = desired_bet
        p["locked"] = desired_bet
        p["status"] = "betting"
        p["in_table"] = True
        p["joined_ts"] = now_ts()
        self._touch_player(p)
        self._touch(st)
        st["phase"] = "betting"
        record_game_fields(
            guild.id,
            member.id,
            "blackjack",
            bets_set=1,
            stake_set_total=desired_bet,
            streak_snapshot=int(daily.get("streak", 0)),
        )
        await save_data()

        if all_in:
            verb = "updated to all in" if was_seated else "went all in"
        else:
            verb = "updated bet" if was_seated else "joined with a bet"
        await channel.send(f"{EMOJI_JOIN} **{member.display_name}** {verb} of **{desired_bet} XP**.")
        if not st.get("deal_msg_id"):
            await self._post_new_deal_button(guild, channel)
        return True

    async def _leave_table(self, guild: discord.Guild, channel: discord.TextChannel, uid: int):
        st = _table(guild.id)
        p = self._player(guild.id, uid)
        if not p.get("in_table"):
            member = guild.get_member(uid)
            if member:
                await channel.send(f"**{member.display_name}** is not seated.")
            return
        if st.get("phase") == "acting" and p.get("status") == "acting":
            await channel.send("You cannot leave while your hand is active.")
            return
        await self._remove_from_table(
            guild,
            channel,
            uid,
            reason="left the table",
            refund_locked=True,
            announce=True,
        )


    async def _remove_from_table(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        uid: int,
        *,
        reason: str,
        refund_locked: bool,
        preserve_replay_hand: bool = False,
        announce: bool = True,
    ):
        st = _table(guild.id)
        p = self._player(guild.id, uid)
        member = guild.get_member(uid)
        locked = int(p.get("locked", 0))
        replay_hand = _opening_hand_snapshot(p.get("dealt_hand")) or _opening_hand_snapshot(p.get("hand"))
        if refund_locked and locked > 0 and member:
            await self._apply_member_xp(guild, member, locked, sync_level=False, source="blackjack refund")
        if preserve_replay_hand and replay_hand:
            p["replay_hand"] = replay_hand

        p.update(
            {
                "bet": 0,
                "locked": 0,
                "hand": [],
                "hand2": [],
                "dealt_hand": [],
                "split": False,
                "active_hand": 0,
                "status": "done",
                "in_table": False,
                "stood": False,
                "busted": False,
                "finished": True,
                "doubled": False,
                "surrendered": False,
                "stood2": False,
                "busted2": False,
                "finished2": True,
                "doubled2": False,
                "surrendered2": False,
                "natural_bj": False,
            }
        )
        self._touch_player(p)
        await save_data()

        if announce:
            await channel.send(f"{member.display_name if member else uid} was removed from the table: {reason}.")

        if st.get("phase") == "acting":
            order = self._seated_order(st, guild)
            acting_ids = [sid for sid in order if st["players"][str(sid)].get("status") == "acting" and st["players"][str(sid)].get("in_table")]
            if not acting_ids:
                await self._dealer_then_payout(self._ShimCtx(guild, channel), st)
            else:
                st["turn_idx"] = st["turn_idx"] % len(acting_ids)
                self._start_turn(st)
                await save_data()
                await self._prompt_turn_with_reactions(guild, channel)

    async def _expire_old_deal_button(self, guild: discord.Guild, channel: discord.TextChannel):
        st = _table(guild.id); old_id = st.get("deal_msg_id")
        if not old_id: return
        try: msg = await channel.fetch_message(old_id)
        except Exception:
            st["deal_msg_id"] = 0; await save_data(); return
        try:
            await msg.edit(content=f"{msg.content}\n\n*Previous hand complete - use the **new** prompt below.*")
        except Exception: pass
        try: await msg.clear_reactions()
        except Exception: pass
        st["deal_msg_id"] = 0; await save_data()

    async def _post_new_deal_button(self, guild: discord.Guild, channel: discord.TextChannel):
        st = _table(guild.id)
        m = await channel.send(
            f"**{EMOJI_DEAL}** DEAL | **{EMOJI_JOIN}** ALL IN | **{EMOJI_LEAVE}** CANCEL BET"
        )
        st["deal_msg_id"] = m.id
        try: await m.add_reaction(EMOJI_DEAL)
        except Exception: pass
        try: await m.add_reaction(EMOJI_JOIN)
        except Exception: pass
        try: await m.add_reaction(EMOJI_LEAVE)
        except Exception: pass
        await save_data()

    # =========================
    # Commands
    # =========================
    @commands.command(name="bjreset")
    @owner_only()
    async def bjreset(self, ctx: commands.Context):
        if not in_right_channel(ctx):
            await ctx.reply("Run this in the Blackjack channel."); return

        st = _table(ctx.guild.id)
        await self._expire_old_deal_button(ctx.guild, ctx.channel)
        refunds = []
        for uid_str, p in list(st.get("players", {}).items()):
            uid = int(uid_str)
            locked = int(p.get("locked", 0))
            if locked > 0:
                member = ctx.guild.get_member(uid)
                if member:
                    await self._apply_member_xp(ctx.guild, member, locked, source="blackjack refund")
                refunds.append(((member.display_name if member else f"User {uid_str}"), locked))
            p.update({
                "bet": 0, "locked": 0, "hand": [], "hand2": [], "split": False, "active_hand": 0,
                "status": "betting",
                "stood": False, "busted": False, "finished": False, "doubled": False, "surrendered": False,
                "stood2": False, "busted2": False, "finished2": False, "doubled2": False, "surrendered2": False,
                "in_table": True, "natural_bj": False,
            })
        st.update({
            "active": True, "phase": "betting", "players": {}, "dealer": [],
            "shoe": [], "turn_idx": 0, "dealing_lock": False,
            "last_action_ts": 0, "turn_started_ts": 0,
            "deal_msg_id": 0, "action_msg_id": 0,
        })
        await save_data()
        if refunds:
            lines = [f"{EMOJI_RECYCLING_SYMBOL} **Blackjack reset by admin. Bets refunded:**"] + [f"- **{n}**: {b} XP" for n, b in refunds]
            await ctx.send("\n".join(lines))
        else:
            await ctx.send(f"{EMOJI_RECYCLING_SYMBOL} **Blackjack reset by admin.** No active bets to refund.")
        await ctx.send(f"{EMOJI_RECYCLING_SYMBOL} **Blackjack table reset. Betting is open.**")
        await self._post_new_deal_button(ctx.guild, ctx.channel)

    @commands.command(name="bjtime")
    @owner_only()
    async def bjtime(self, ctx: commands.Context, *, when: Optional[str] = None):
        st = _table(ctx.guild.id)
        cur_h, cur_m = _sanitize_reset_time(st.get("reset_hour", BJ_DAILY_RESET_HOUR), st.get("reset_minute", BJ_DAILY_RESET_MINUTE))
        if not when:
            nxt = _next_reset_dt(cur_h, cur_m)
            await ctx.reply(
                f"Blackjack reset time is **{_draw_time_label(cur_h, cur_m)}**.\n"
                f"Next reset: **{nxt.strftime('%Y-%m-%d %I:%M %p ET')}**."
            )
            return

        parsed = _parse_reset_time(when)
        if not parsed:
            p = ctx.clean_prefix
            await ctx.reply(
                f"Usage: `{p}bjtime <time>` where time is `HH:MM` (24h) or `h[:mm]am/pm`.\n"
                f"Examples: `{p}bjtime 00:00`, `{p}bjtime 6pm`, `{p}bjtime 6:30pm`."
            )
            return

        h, m = parsed
        st["reset_hour"] = int(h)
        st["reset_minute"] = int(m)
        st["last_cycle_key"] = _cycle_key(h, m)
        await save_data()
        nxt = _next_reset_dt(h, m)
        await ctx.reply(
            f"Blackjack reset time set to **{_draw_time_label(h, m)}** by {ctx.author.mention}.\n"
            f"Next reset: **{nxt.strftime('%Y-%m-%d %I:%M %p ET')}**."
        )

    @commands.command(name="bjcooldown")
    @owner_only()
    async def bjcooldown(self, ctx: commands.Context, mode: Optional[str] = None):
        st = _table(ctx.guild.id)
        current = bool(st.get("cooldown_enabled", BJ_COOLDOWN_ENABLED))
        if mode is None or not mode.strip():
            await ctx.reply(f"Blackjack daily lockout cooldown is currently **{'ENABLED' if current else 'DISABLED'}**.")
            return

        raw = mode.strip().lower()
        if raw in ("on", "enable", "enabled", "true", "1"):
            new_val = True
        elif raw in ("off", "disable", "disabled", "false", "0"):
            new_val = False
        elif raw in ("toggle", "flip"):
            new_val = not current
        else:
            await ctx.reply(f"Usage: `{ctx.clean_prefix}bjcooldown [on|off|toggle]`")
            return

        st["cooldown_enabled"] = bool(new_val)
        unlocked = 0
        if not new_val:
            users = _gdict(ctx.guild.id).get("users", {})
            for u in users.values():
                if not isinstance(u, dict):
                    continue
                d = u.get("blackjack_daily")
                if not isinstance(d, dict):
                    continue
                if d.get("locked"):
                    unlocked += 1
                d["locked"] = False
                d["locked_ts"] = 0
        await save_data()

        if new_val:
            await ctx.reply("Blackjack daily lockout cooldown is now **ENABLED**.")
        else:
            await ctx.reply(f"Blackjack daily lockout cooldown is now **DISABLED**. Cleared locks for **{unlocked}** user(s).")

    @commands.command(name="blackjack", aliases=["bj"])
    async def blackjack(self, ctx: commands.Context, arg: Optional[str] = None):
        if not in_right_channel(ctx):
            return

        st = _table(ctx.guild.id)
        configured_channel_id = int(get_blackjack_channel_id(ctx.guild.id) or 0)
        st["channel_id"] = configured_channel_id if configured_channel_id > 0 else ctx.channel.id

        await self._roll_cycle_if_needed(ctx.guild, ctx.channel)
        await self._cleanup_old_blackjack_messages(ctx.guild, ctx.channel)

        if arg is None:
            await self._show_table(ctx)
            if not st.get("deal_msg_id") and st.get("phase") in ("betting", "idle"):
                await self._post_new_deal_button(ctx.guild, ctx.channel)
            return

        a = arg.lower().strip()
        if a in ("table", "state", "status"):
            await self._show_table(ctx)
            return

        if a in ("join", "enter", "buy", "buyin"):
            await ctx.reply(
                f"Blackjack is XP-based now. Use `{ctx.clean_prefix}bj <amount>` or `{ctx.clean_prefix}bj all`."
            )
            return

        if a in ("leave", "stop", "exit"):
            await self._leave_table(ctx.guild, ctx.channel, ctx.author.id)
            return

        if a in ("deal", "start"):
            await self._begin_deal_core(ctx.guild, ctx.channel)
            return

        if a in ("all", "allin", "all-in", "ai", "max"):
            await self._join_table(ctx.guild, ctx.channel, ctx.author, all_in=True)
            return

        if a.replace(",", "").isdigit():
            await self._join_table(ctx.guild, ctx.channel, ctx.author, bet_amount=int(a.replace(",", "")))
            return

        if st["phase"] != "acting":
            await ctx.reply("No hand in progress or it is not action time.")
            return
        await self._handle_action_command(ctx, a)
    # ---- Debug commands ----
    @commands.command(name="bjdebug")
    @owner_only()
    async def bjdebug(self, ctx: commands.Context, mode: str = "toggle"):
        st = mode.lower()
        if st in ("on","true","1","enable","enabled"):
            self.debug_enabled[ctx.guild.id] = True
        elif st in ("off","false","0","disable","disabled"):
            self.debug_enabled[ctx.guild.id] = False
        else:
            self.debug_enabled[ctx.guild.id] = not self.debug_enabled.get(ctx.guild.id, False)
        await ctx.reply(f"Debug is now **{'ON' if self._dbg(ctx.guild.id) else 'OFF'}**.")

    @commands.command(name="bjstate")
    @owner_only()
    async def bjstate(self, ctx: commands.Context):
        st = _table(ctx.guild.id)
        await ctx.reply("\n".join([
            f"active={st.get('active')}",
            f"phase={st.get('phase')}",
            f"deal_msg_id={st.get('deal_msg_id')}",
            f"action_msg_id={st.get('action_msg_id')}",
            f"channel_id={st.get('channel_id')}",
            f"reset={st.get('reset_hour')}:{int(st.get('reset_minute', 0)):02d} ET",
            f"cooldown_enabled={bool(st.get('cooldown_enabled', BJ_COOLDOWN_ENABLED))}",
            f"players={list(st.get('players', {}).keys())}",
        ]))

    @commands.command(name="bjintents")
    @owner_only()
    async def bjintents(self, ctx: commands.Context):
        i = ctx.bot.intents
        await ctx.reply(f"reactions={i.reactions}, message_content={i.message_content}, members={i.members}, guilds={i.guilds}")

    # =========================
    # Display helpers
    # =========================
    async def _show_table(self, ctx: commands.Context):
        st = _table(ctx.guild.id)
        seated = self._seated_order(st, ctx.guild)
        dealer = st.get("dealer", [])
        h, m = _sanitize_reset_time(st.get("reset_hour", BJ_DAILY_RESET_HOUR), st.get("reset_minute", BJ_DAILY_RESET_MINUTE))
        nxt = _next_reset_dt(h, m)

        lines = [
            "**Blackjack Table**",
            f"Bet with `{ctx.clean_prefix}bj <amount>` or react **{EMOJI_JOIN}** on the deal prompt to go all in.",
            f"Daily reset: **{_draw_time_label(h, m)}** (next {nxt.strftime('%Y-%m-%d %I:%M %p ET')})",
            f"Daily lockout cooldown: **{'ENABLED' if self._cooldown_enabled(ctx.guild.id) else 'DISABLED'}**",
        ]

        if not dealer:
            lines.append("Dealer: (none)")
        elif st.get("phase") in ("dealing", "acting"):
            lines.append(f"Dealer shows: {dealer[0]}")
        else:
            lines.append(f"Dealer: {pretty(dealer)}")

        for uid in seated:
            mbr = ctx.guild.get_member(uid)
            p = st["players"][str(uid)]
            parts = [f"H1 {pretty(p.get('hand', [])) or '(no cards)'}"]
            if p.get("split"):
                parts.append(f"H2 {pretty(p.get('hand2', [])) or '(no cards)'}")
            lines.append(
                f"- **{mbr.display_name if mbr else uid}** bet **{p.get('bet', 0)} XP** | {' | '.join(parts)} | {p.get('status', 'betting')}"
            )

        if not seated:
            lines.append(f"No active bets. Use `{ctx.clean_prefix}bj <amount>` or react **{EMOJI_JOIN}** to go all in.")

        locked, d = self._is_locked(ctx.guild.id, ctx.author.id)
        if not self._cooldown_enabled(ctx.guild.id):
            lines.append("Cooldown is disabled. Losses do not lock you out.")
        elif locked:
            lines.append("You are currently locked out until the next blackjack reset.")
        else:
            lines.append("You are eligible to bet.")

        await ctx.reply("\n".join(lines))
    # =========================
    # Deal flow
    # =========================
    async def _round_cleanup_and_prompt(self, guild: discord.Guild, channel: discord.TextChannel):
        st = _table(guild.id)
        st["phase"] = "betting"
        st["dealer"] = []
        st["action_msg_id"] = 0
        st["turn_idx"] = 0

        for p in st["players"].values():
            p.update({
                "status": "idle",
                "bet": 0,
                "locked": 0,
                "hand": [],
                "hand2": [],
                "dealt_hand": [],
                "split": False,
                "active_hand": 0,
                "stood": False,
                "busted": False,
                "finished": False,
                "doubled": False,
                "surrendered": False,
                "stood2": False,
                "busted2": False,
                "finished2": False,
                "doubled2": False,
                "surrendered2": False,
                "natural_bj": False,
                "in_table": False,
            })
            p["last_active_ts"] = now_ts()

        self._touch(st)
        await save_data()
        await self._expire_old_deal_button(guild, channel)
        await self._post_new_deal_button(guild, channel)

    async def _begin_deal_core(self, guild: discord.Guild, channel: discord.TextChannel):
        st = _table(guild.id)
        if st.get("phase") not in ("betting", "idle"):
            await channel.send("A hand is already in progress."); return
        if st.get("dealing_lock"):
            await channel.send("Dealing is already starting. Please wait."); return

        await self._roll_cycle_if_needed(guild, channel)
        st["dealing_lock"] = True; await save_data()
        try:
            seated = self._seated_order(st, guild)
            ready = [
                uid for uid in seated
                if st["players"][str(uid)].get("in_table") and int(st["players"][str(uid)].get("bet", 0)) > 0
            ]
            if not ready:
                await channel.send(f"No active bets. Use `!bj <amount>` or react **{EMOJI_JOIN}** to go all in."); return

            for uid in ready:
                bet_amt = int(st["players"][str(uid)].get("bet", 0))
                record_game_fields(guild.id, uid, "blackjack", rounds_played=1, hands_played=1, xp_wagered_total=bet_amt)

            st["phase"] = "dealing"; self._reset_hand(st)

            for _ in range(2):
                for uid in ready:
                    st["players"][str(uid)]["hand"].append(self._deal_card(st))
                st["dealer"].append(self._deal_card(st))

            forced_natural_ids: set[int] = set()
            forced_natural_users: list[str] = []
            for uid in ready:
                if not consume_blackjack_natural_charge(guild.id, uid):
                    continue
                p = st["players"][str(uid)]
                p["hand"] = _natural_hand_from_shoe(st)
                forced_natural_ids.add(uid)
                mbr = guild.get_member(uid)
                forced_natural_users.append(mbr.display_name if mbr else str(uid))

            for uid in ready:
                if uid in forced_natural_ids:
                    continue
                p = st["players"][str(uid)]
                replay_hand = _opening_hand_snapshot(p.get("replay_hand"))
                if replay_hand:
                    p["hand"] = replay_hand
                    # Timeout replays are single-use; consume them once dealt.
                    p["replay_hand"] = []

            for uid in ready:
                p = st["players"][str(uid)]
                p["dealt_hand"] = list(p["hand"])
                p["status"] = "acting"
                self._touch_player(p)

            self._touch(st); await save_data()

            if forced_natural_users:
                await channel.send(
                    "Wheel buff triggered: guaranteed naturals for "
                    + ", ".join(f"**{name}**" for name in forced_natural_users)
                    + "."
                )

            up = st["dealer"][0]
            lines = [f"Dealing... Dealer shows: `{up}`"]
            for uid in ready:
                mbr = guild.get_member(uid)
                ph = st["players"][str(uid)]["hand"]
                tot, bj = value_of_hand(ph)
                lines.append(f"- **{mbr.display_name if mbr else uid}**: {pretty(ph)} -> **{tot}**{' (blackjack)' if bj else ''}")
            await channel.send("\n".join(lines))

            dealer_total, dealer_bj = value_of_hand(st["dealer"])
            if dealer_bj:
                out_lines = ["Dealer has a natural blackjack."]
                cooldown_on = self._cooldown_enabled(guild.id)
                for uid in ready:
                    p = st["players"][str(uid)]
                    member = guild.get_member(uid)
                    locked_amt = int(p.get("locked", 0))
                    _, player_bj = value_of_hand(p["hand"])

                    if player_bj:
                        if member and locked_amt > 0:
                            await self._apply_member_xp(
                                guild,
                                member,
                                locked_amt,
                                sync_level=True,
                                source="blackjack payout",
                            )
                        await self._set_round_result_state(guild, uid, result="push")
                        record_game_fields(guild.id, uid, "blackjack", draws=1, naturals=1, xp_profit_total=0)
                        out_lines.append(f"- **{member.display_name if member else uid}**: push")
                    else:
                        record_game_fields(guild.id, uid, "blackjack", losses=1, xp_profit_total=-locked_amt)
                        locked_out = await self._set_round_result_state(guild, uid, result="loss")
                        if member:
                            lock_text = "locked until reset" if locked_out else "can play again immediately"
                            out_lines.append(f"- **{member.display_name}**: loss ({lock_text})")

                    p["bet"] = 0
                    p["locked"] = 0
                    p["status"] = "done"
                    p["finished"] = True

                await save_data()
                await channel.send("\n".join(out_lines))
                await self._round_cleanup_and_prompt(guild, channel)
                return

            winners = []
            for uid in ready:
                p = st["players"][str(uid)]
                _, p_bj = value_of_hand(p["hand"])
                if not p_bj:
                    continue
                member = guild.get_member(uid)
                name = member.display_name if member else str(uid)
                winners.append((name, int(p.get("bet", 0))))
                p["natural_bj"] = True
                p["status"] = "done"
                p["finished"] = True

            if winners:
                msg_lines = ["Natural blackjack winners:"]
                for name, bet_amt in winners:
                    msg_lines.append(
                        f"- **{name}**: blackjack pays **{self._natural_blackjack_payout(bet_amt)} XP** on a **{bet_amt} XP** bet"
                    )
                await channel.send("\n".join(msg_lines))

            st["phase"] = "acting"; st["turn_idx"] = 0; self._start_turn(st); await save_data()
            remaining = [uid for uid in ready if st["players"][str(uid)].get("status") == "acting"]
            if not remaining:
                await self._dealer_then_payout(self._ShimCtx(guild, channel), st); return

            await channel.send("Turn order: " + " -> ".join(guild.get_member(uid).display_name for uid in remaining if guild.get_member(uid)))
            await self._prompt_turn_with_reactions(guild, channel); return
        finally:
            st["dealing_lock"] = False; await save_data()
    # =========================
    # Player actions (cmd compatibility)
    # =========================
    async def _handle_action_command(self, ctx: commands.Context, a: str):
        st = _table(ctx.guild.id); p = st["players"].get(str(ctx.author.id))
        if not p or not p.get("in_table"):
            await ctx.reply("You're not at the table."); return
        if self._both_hands_finished(p):
            await ctx.reply("Your action is already finished for this hand."); return

        order = self._seated_order(st, ctx.guild)
        acting_ids = [uid for uid in order if st["players"][str(uid)].get("status") == "acting" and st["players"][str(uid)].get("in_table")]
        if not acting_ids:
            await ctx.reply("No players available to act."); return
        cur_uid = acting_ids[st["turn_idx"] % len(acting_ids)]
        if ctx.author.id != cur_uid:
            await ctx.reply(f"It's **{ctx.guild.get_member(cur_uid).display_name}**'s turn."); return

        a = a.lower(); self._touch(st)
        if a == "hit":   await self._do_hit(ctx.guild, ctx.channel, cur_uid); return
        if a == "stand": await self._do_stand(ctx.guild, ctx.channel, cur_uid); return
        if a in ("doubledown","dd"): await self._do_dd(ctx.guild, ctx.channel, cur_uid); return
        if a == "surrender": await self._do_surrender(ctx.guild, ctx.channel, cur_uid); return
        if a == "split": await self._do_split(ctx.guild, ctx.channel, cur_uid); return
        await ctx.reply(
            f"Unknown action. React on the action prompt: "
            f"{EMOJI_HIT} Hit / {EMOJI_STAND} Stand / {EMOJI_DD} Double / {EMOJI_SURRENDER} Surrender / {EMOJI_SPLIT} Split"
        )

    async def _prompt_turn_with_reactions(self, guild: discord.Guild, channel: discord.TextChannel):
        st = _table(guild.id)
        order = self._seated_order(st, guild)
        acting = [uid for uid in order if st["players"][str(uid)].get("status") == "acting" and st["players"][str(uid)].get("in_table")]
        if not acting:
            await self._dealer_then_payout(self._ShimCtx(guild, channel), st); return
        cur_uid = acting[st["turn_idx"] % len(acting)]
        p = st["players"][str(cur_uid)]
        self._touch_player(p)
        self._start_turn(st); await save_data()
        # Determine if split is available on the current hand
        hand = self._hand_ref(p)
        can_split = (not p.get("split")) and len(hand) == 2 and rank_of(hand[0]) == rank_of(hand[1])
        hand_label = "Hand 1" if p.get("active_hand",0) == 0 else "Hand 2"
        msg = await channel.send(
            f"{EMOJI_WHITE_RIGHT_POINTING_BACKHAND_INDEX} **{guild.get_member(cur_uid).display_name}**, your move - **{hand_label}** - react: "
            f"{EMOJI_HIT} Hit | {EMOJI_STAND} Stand | {EMOJI_DD} Double | {EMOJI_SURRENDER} Surrender"
            + (f" | {EMOJI_SPLIT} Split" if can_split else "")
        )
        st["action_msg_id"] = msg.id; await save_data()
        # Add reactions
        choices = [EMOJI_HIT, EMOJI_STAND, EMOJI_DD, EMOJI_SURRENDER] + ([EMOJI_SPLIT] if can_split else [])
        for emo in choices:
            try: await msg.add_reaction(emo)
            except Exception: pass

    async def _advance_after_hand_if_split(self, guild: discord.Guild, channel: discord.TextChannel, st: dict, uid: int) -> bool:
        """If the player has a split and the other hand isn't finished, switch to it and prompt. Returns True if stayed on same player."""
        p = st["players"][str(uid)]
        if p.get("split"):
            # If we're on H1 and H2 not finished, go to H2; otherwise if on H2 and H1 not finished (shouldn't happen), go to H1
            if p.get("active_hand",0) == 0 and not p.get("finished2", False):
                p["active_hand"] = 1; self._start_turn(st); await save_data()
                await self._prompt_turn_with_reactions(guild, channel); return True
            if p.get("active_hand",0) == 1 and not p.get("finished", False):
                p["active_hand"] = 0; self._start_turn(st); await save_data()
                await self._prompt_turn_with_reactions(guild, channel); return True
        return False

    async def _advance_turn_or_dealer(self, ctx_or_channel, st: dict):
        if isinstance(ctx_or_channel, commands.Context):
            guild = ctx_or_channel.guild; channel = ctx_or_channel.channel
        else:
            guild = ctx_or_channel.guild; channel = ctx_or_channel.channel

        order = self._seated_order(st, guild)
        # remove acting status for finished/busted/surrendered (per-hand aware)
        for uid in order:
            p = st["players"][str(uid)]
            if p.get("status") == "acting" and self._both_hands_finished(p):
                p["status"] = "done"

        acting = [uid for uid in order if st["players"][str(uid)].get("status") == "acting" and st["players"][str(uid)].get("in_table")]
        st["action_msg_id"] = 0; await save_data()

        if not acting:
            await self._dealer_then_payout(self._ShimCtx(guild, channel), st); return

        # If current player's other split hand is pending, keep on same uid
        cur_uid = acting[st["turn_idx"] % len(acting)]
        stayed = await self._advance_after_hand_if_split(guild, channel, st, cur_uid)
        if stayed: return

        st["turn_idx"] = (st["turn_idx"] + 1) % len(acting)
        self._start_turn(st); await save_data()
        await self._prompt_turn_with_reactions(guild, channel)

    # ---- concrete actions ----
    async def _do_hit(self, guild: discord.Guild, channel: discord.TextChannel, uid: int):
        st = _table(guild.id); p = st["players"][str(uid)]
        hand = self._hand_ref(p)
        card = self._deal_card(st); hand.append(card)
        self._touch_player(p)
        tot, _ = value_of_hand(hand); await save_data()
        label = "Hand 1" if p.get("active_hand",0)==0 else "Hand 2"
        await channel.send(f"{EMOJI_RAISED_HAND} **{guild.get_member(uid).display_name} HIT** ({label}) -> drew `{card}` -> {pretty(hand)} (**{tot}**)")
        if tot > 21:
            self._set_flag(p, "busted", True); self._set_flag(p, "finished", True); await save_data()
            record_game_fields(guild.id, uid, "blackjack", busts=1)
            await channel.send(f"{EMOJI_COLLISION_SYMBOL} **{guild.get_member(uid).display_name} BUSTED** ({label}).")
            # If split, switch to the other hand if it remains
            if await self._advance_after_hand_if_split(guild, channel, st, uid): return
            await self._advance_turn_or_dealer(self._ShimCtx(guild, channel), st); return
        self._start_turn(st); await save_data()

    async def _do_stand(self, guild: discord.Guild, channel: discord.TextChannel, uid: int):
        st = _table(guild.id); p = st["players"][str(uid)]
        self._set_flag(p, "stood", True); self._set_flag(p, "finished", True); await save_data()
        self._touch_player(p)
        label = "Hand 1" if p.get("active_hand",0)==0 else "Hand 2"
        await channel.send(f"{EMOJI_STANDING_PERSON} **{guild.get_member(uid).display_name} STANDS** ({label}).")
        if await self._advance_after_hand_if_split(guild, channel, st, uid): return
        await self._advance_turn_or_dealer(self._ShimCtx(guild, channel), st)

    async def _do_dd(self, guild: discord.Guild, channel: discord.TextChannel, uid: int):
        st = _table(guild.id); p = st["players"][str(uid)]
        hand = self._hand_ref(p)
        # Only on 2 cards of that hand and not already doubled
        if len(hand) != 2 or self._get_flag(p, "doubled"):
            await channel.send("You can only double down on your first action (2-card hand)."); return
        u = _udict(guild.id, uid); xp_cur = int(u.get("xp_f", u.get("xp", 0)))
        # Need one additional base bet reserved for this hand.
        need = int(p.get("bet", 0))
        if xp_cur < need:
            await channel.send(f"You need at least **{need} XP** available to double down."); return
        member = guild.get_member(uid)
        if not member:
            await channel.send("Could not verify your member state for double down."); return
        await self._apply_member_xp(
            guild,
            member,
            -need,
            sync_level=False,
            source="blackjack ante double",
        )
        self._touch_player(p)
        p["locked"] = int(p.get("locked", 0)) + need
        self._set_flag(p, "doubled", True)
        record_game_fields(guild.id, uid, "blackjack", doubles=1, xp_wagered_total=need)
        card = self._deal_card(st); hand.append(card)
        tot, _ = value_of_hand(hand)
        self._set_flag(p, "finished", True); self._set_flag(p, "stood", True); await save_data()
        label = "Hand 1" if p.get("active_hand",0)==0 else "Hand 2"
        await channel.send(f"{EMOJI_VICTORY_HAND} **{guild.get_member(uid).display_name} DOUBLE DOWN** ({label}) -> drew `{card}` -> {pretty(hand)} (**{tot}**)")
        if tot > 21:
            self._set_flag(p, "busted", True); await save_data()
            record_game_fields(guild.id, uid, "blackjack", busts=1)
            await channel.send(f"{EMOJI_COLLISION_SYMBOL} **{guild.get_member(uid).display_name} BUSTED** ({label}).")
        if await self._advance_after_hand_if_split(guild, channel, st, uid): return
        await self._advance_turn_or_dealer(self._ShimCtx(guild, channel), st)

    async def _do_surrender(self, guild: discord.Guild, channel: discord.TextChannel, uid: int):
        st = _table(guild.id); p = st["players"][str(uid)]
        hand = self._hand_ref(p)
        if len(hand) != 2 or self._get_flag(p, "doubled"):
            await channel.send("You can only surrender on your first action (before hitting)."); return
        self._set_flag(p, "surrendered", True); self._set_flag(p, "finished", True); self._set_flag(p, "stood", True)
        self._touch_player(p)
        record_game_fields(guild.id, uid, "blackjack", surrenders=1)
        await save_data()
        label = "Hand 1" if p.get("active_hand",0)==0 else "Hand 2"
        await channel.send(f"{EMOJI_WAVING_WHITE_FLAG} **{guild.get_member(uid).display_name} SURRENDERS** ({label}) (loses half their bet).")
        if await self._advance_after_hand_if_split(guild, channel, st, uid): return
        await self._advance_turn_or_dealer(self._ShimCtx(guild, channel), st)

    async def _do_split(self, guild: discord.Guild, channel: discord.TextChannel, uid: int):
        st = _table(guild.id); p = st["players"][str(uid)]
        # Only once, only on Hand 1, only with two cards same rank
        if p.get("split"):
            await channel.send("You can only split once."); return
        if p.get("active_hand",0) != 0:
            await channel.send("You may only split while playing your first hand."); return
        h = p["hand"]
        if len(h) != 2 or rank_of(h[0]) != rank_of(h[1]):
            await channel.send("You can only split when your first two cards are the same rank."); return
        u = _udict(guild.id, uid); xp_cur = int(u.get("xp_f", u.get("xp", 0)))
        # Need one additional base bet reserved for the second hand.
        need = int(p.get("bet", 0))
        if xp_cur < need:
            await channel.send(f"You need at least **{need} XP** available to split."); return
        member = guild.get_member(uid)
        if not member:
            await channel.send("Could not verify your member state for split."); return
        await self._apply_member_xp(
            guild,
            member,
            -need,
            sync_level=False,
            source="blackjack ante split",
        )
        self._touch_player(p)
        p["locked"] = int(p.get("locked", 0)) + need
        record_game_fields(guild.id, uid, "blackjack", splits=1, hands_played=1, xp_wagered_total=need)
        # Perform split: move one card to hand2; start active_hand=0
        c = h.pop()                # move second to hand2
        p["hand2"] = [c]
        p["split"] = True
        # Reset flags for hand2
        p["stood2"] = p["busted2"] = p["finished2"] = p["doubled2"] = p["surrendered2"] = False
        await save_data()
        await channel.send(f"{EMOJI_BLACK_SCISSORS} **{guild.get_member(uid).display_name} SPLITS** -> Hand 1: {pretty(p['hand'])} | Hand 2: {pretty(p['hand2'])}")
        # Continue acting on Hand 1; player can hit/stand/etc.; when finished, engine will switch to Hand 2
        self._start_turn(st); await save_data()

    # =========================
    # Dealer & Payout
    # =========================
    async def _dealer_then_payout(self, ctx, st: dict):
        guild = ctx.guild
        channel = ctx.channel

        st["phase"] = "dealer"; self._touch(st); await save_data()
        dhand = st["dealer"]
        await channel.send(f"Dealer reveals: {pretty(dhand)}")

        while True:
            total, _ = value_of_hand(dhand)
            if total < 17:
                card = self._deal_card(st); dhand.append(card)
                await channel.send(f"Dealer draws `{card}` -> {pretty(dhand)} (**{value_of_hand(dhand)[0]}**)")
                self._touch(st)
            else:
                break

        dealer_total, _ = value_of_hand(dhand)
        dealer_bust = dealer_total > 21

        st["phase"] = "payout"; self._touch(st); await save_data()
        lines = ["Payouts"]

        for uid_str, p in list(st["players"].items()):
            if not p.get("in_table"):
                continue

            uid = int(uid_str)
            member = guild.get_member(uid)
            bet = int(p.get("bet", 0))

            locked_total = int(p.get("locked", 0))

            def settle_one(
                hand: List[str],
                doubled_flag: bool,
                surrendered_flag: bool,
                busted_flag: bool,
                label: str,
                *,
                natural_flag: bool = False,
            ):
                total, _ = value_of_hand(hand)
                base_bet = bet * (2 if doubled_flag else 1)
                if natural_flag:
                    return self._natural_blackjack_payout(base_bet), f"{label} natural blackjack", "win"
                if surrendered_flag:
                    return base_bet // 2, f"{label} surrender", "loss"
                if busted_flag:
                    return 0, f"{label} bust", "loss"
                if dealer_bust:
                    return base_bet * 2, f"{label} win (dealer bust)", "win"
                if total > dealer_total:
                    return base_bet * 2, f"{label} win", "win"
                if total < dealer_total:
                    return 0, f"{label} loss", "loss"
                return base_bet, f"{label} push", "push"

            r1, out1, kind1 = settle_one(
                p["hand"],
                p.get("doubled", False),
                p.get("surrendered", False),
                p.get("busted", False),
                "H1",
                natural_flag=bool(p.get("natural_bj", False)),
            )
            r2 = 0
            out2 = None
            kind2 = None
            if p.get("split"):
                r2, out2, kind2 = settle_one(
                    p.get("hand2", []),
                    p.get("doubled2", False),
                    p.get("surrendered2", False),
                    p.get("busted2", False),
                    "H2",
                )

            payout_total = int(r1 + r2)
            if member and payout_total > 0:
                await self._apply_member_xp(guild, member, payout_total, sync_level=True, source="blackjack payout")

            profit = payout_total - locked_total
            if profit < 0:
                record_game_fields(guild.id, uid, "blackjack", losses=1, xp_profit_total=profit)
            elif profit > 0:
                extra = {"naturals": 1} if p.get("natural_bj") else {}
                record_game_fields(guild.id, uid, "blackjack", wins=1, xp_profit_total=profit, **extra)
            else:
                record_game_fields(guild.id, uid, "blackjack", draws=1, xp_profit_total=profit)

            effect_line = ""
            result_key = "loss" if profit < 0 else ("win" if profit > 0 else "push")
            locked_out = await self._set_round_result_state(guild, uid, result=result_key)
            if profit < 0:
                effect_line = "locked until reset" if locked_out else "can play again immediately"

            name = member.display_name if member else f"User {uid}"
            parts = [f"- {name}: {out1}"]
            if out2 is not None:
                parts.append(out2)
            parts.append(f"payout={payout_total} XP")
            if effect_line:
                parts.append(effect_line)
            lines.append(" | ".join(parts))

            p["bet"] = 0
            p["locked"] = 0

        await channel.send("\n".join(lines))
        await self._round_cleanup_and_prompt(guild, channel)
    # =========================
    # Reaction listeners
    # =========================
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user and payload.user_id == self.bot.user.id: return
        guild = self.bot.get_guild(payload.guild_id or 0)
        if not guild: return
        st = _table(guild.id)
        fallback_channel_id = int(get_blackjack_channel_id(guild.id) or 0)
        ch = guild.get_channel(int(st.get("channel_id", 0) or fallback_channel_id))
        if not isinstance(ch, discord.TextChannel): return
        if payload.channel_id != ch.id:
            await self._dprint(guild, ch, f"skip: different channel (payload={payload.channel_id}, bj={ch.id})")
            return

        await self._roll_cycle_if_needed(guild, ch)

        em_name = _norm_emoji_name(payload.emoji)
        await self._dprint(guild, ch, f"reaction on msg_id={payload.message_id}, emoji='{em_name}', user={payload.user_id}")

        # Deal button
        deal_id = st.get("deal_msg_id")
        if deal_id and payload.message_id == deal_id:
            await self._dprint(guild, ch, f"match deal_msg_id={deal_id}")
            if em_name in DEAL_EMOJIS:
                await self._dprint(guild, ch, "deal emoji accepted -> begin deal")
                try: await self._begin_deal_core(guild, ch)
                except Exception as e: await self._dprint(guild, ch, f"_begin_deal_core error: {type(e).__name__}: {e}")
            elif em_name in JOIN_EMOJIS or payload.emoji.name in JOIN_EMOJIS:
                member = await self._resolve_member(guild, payload.user_id)
                if not member or member.bot:
                    return
                await self._join_table(guild, ch, member, all_in=True)
            elif em_name in LEAVE_EMOJIS or payload.emoji.name in LEAVE_EMOJIS:
                member = await self._resolve_member(guild, payload.user_id)
                if not member or member.bot:
                    return
                await self._leave_table(guild, ch, member.id)
            else:
                await self._dprint(
                    guild,
                    ch,
                    f"deal emoji not accepted: '{em_name}' not in {DEAL_EMOJIS | JOIN_EMOJIS | LEAVE_EMOJIS}",
                )
            return

        # Action prompt
        act_id = st.get("action_msg_id")
        if act_id and payload.message_id == act_id:
            await self._dprint(guild, ch, f"match action_msg_id={act_id}, phase={st.get('phase')}")
            if st.get("phase") != "acting":
                await self._dprint(guild, ch, "ignored: phase not 'acting'"); return

            # Map to action
            action = ACTION_EMOJI_MAP.get(em_name) or ACTION_EMOJI_MAP.get(payload.emoji.name)
            if not action:
                await self._dprint(guild, ch, f"no action mapping for '{em_name}'"); return

            order = self._seated_order(st, guild)
            acting_ids = [uid for uid in order if st["players"][str(uid)].get("status") == "acting" and st["players"][str(uid)].get("in_table")]
            if not acting_ids:
                await self._dprint(guild, ch, "no acting players"); return
            cur_uid = acting_ids[st["turn_idx"] % len(acting_ids)]
            if payload.user_id != cur_uid:
                await self._dprint(guild, ch, f"ignored: not actor (actor={cur_uid}, reacted_by={payload.user_id})"); return

            await self._dprint(guild, ch, f"action '{action}' accepted for {cur_uid}")
            try:
                if action == "hit": await self._do_hit(guild, ch, cur_uid)
                elif action == "stand": await self._do_stand(guild, ch, cur_uid)
                elif action == "dd": await self._do_dd(guild, ch, cur_uid)
                elif action == "surrender": await self._do_surrender(guild, ch, cur_uid)
                elif action == "split": await self._do_split(guild, ch, cur_uid)
            except Exception as e:
                await self._dprint(guild, ch, f"action handler error: {type(e).__name__}: {e}")
            return

        await self._dprint(guild, ch, f"ignored: msg_id {payload.message_id} != deal({deal_id})/action({act_id})")

    # Optional relay (still requires intents.reactions=True)
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User | discord.Member):
        if user.bot: return
        try:
            payload = discord.RawReactionActionEvent(
                message_id=reaction.message.id, user_id=user.id,
                channel_id=reaction.message.channel.id,
                guild_id=reaction.message.guild.id if reaction.message.guild else None,
                emoji=discord.PartialEmoji.from_str(str(reaction.emoji)),
                event_type="REACTION_ADD"
            )
            await self.on_raw_reaction_add(payload)
        except Exception:
            pass

    # =========================
    # Anti-stall loop
    # =========================
    @tasks.loop(seconds=5)
    async def guard_loop(self):
        for guild in self.bot.guilds:
            st = _table(guild.id)
            fallback_channel_id = int(get_blackjack_channel_id(guild.id) or 0)
            ch = guild.get_channel(int(st.get("channel_id", 0) or fallback_channel_id))
            if not isinstance(ch, discord.TextChannel):
                continue

            await self._roll_cycle_if_needed(guild, ch)
            await self._cleanup_old_blackjack_messages(guild, ch)
            if st.get("phase") in ("betting", "idle") and not st.get("deal_msg_id"):
                await self._post_new_deal_button(guild, ch)

            if st.get("phase") in ("betting", "idle"):
                idle_cutoff = int(BJ_SEAT_IDLE_TIMEOUT_SECONDS)
                for uid_str, p in list(st.get("players", {}).items()):
                    if not p.get("in_table"):
                        continue
                    last_active = int(p.get("last_active_ts", p.get("joined_ts", 0)) or 0)
                    if last_active and now_ts() - last_active > idle_cutoff:
                        await self._remove_from_table(
                            guild,
                            ch,
                            int(uid_str),
                            reason=f"idle for more than {idle_cutoff // 60} minutes",
                            refund_locked=True,
                            announce=True,
                        )

            if st.get("phase") != "acting":
                continue

            order = self._seated_order(st, guild)
            acting_ids = [uid for uid in order if st["players"][str(uid)].get("status") == "acting" and st["players"][str(uid)].get("in_table")]
            if not acting_ids:
                continue

            cur_uid = acting_ids[st.get("turn_idx", 0) % len(acting_ids)]
            started = int(st.get("turn_started_ts", 0))
            timeout_s = int(BJ_TURN_TIMEOUT_SECONDS)
            if started and now_ts() - started > timeout_s:
                await self._remove_from_table(
                    guild,
                    ch,
                    cur_uid,
                    reason=f"no action for more than {timeout_s // 60} minutes",
                    refund_locked=True,
                    preserve_replay_hand=True,
                    announce=True,
                )

                if not any(x.get("in_table") for x in st["players"].values()):
                    if st.get("phase") not in ("betting", "idle"):
                        st.update({
                            "active": True, "phase": "betting", "dealer": [],
                            "turn_idx": 0, "turn_started_ts": 0, "dealing_lock": False,
                            "action_msg_id": 0,
                        })
                        await save_data()
                    if not st.get("deal_msg_id"):
                        await ch.send("No active players remain. Table is open.")
                        await self._post_new_deal_button(guild, ch)
    @guard_loop.before_loop
    async def _before_guard(self):
        await self.bot.wait_until_ready()
