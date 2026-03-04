# blackjack.py
from __future__ import annotations
from typing import Dict, List, Optional, Tuple
import random
import time
import asyncio

import discord
from discord.ext import commands, tasks

# --- your project-specific imports (unchanged) ---
from .config import (
    BJ_MAX_PLAYERS,  # if unused, safe to remove
    BJ_MIN_BET,
    BJ_MAX_BET,
)
from .guild_setup import get_blackjack_channel_id
from .storage import _gdict, _udict, save_data
from .stats_store import record_game_fields, record_xp_change
from .xp import apply_xp_change
from .roles import announce_level_up, sync_level_roles, enforce_level6_exclusive
from .ownership import owner_only


# =========================
# Cards & Emoji UI
# =========================
SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

EMOJI_DEAL = "▶️"
EMOJI_HIT = "🟩"
EMOJI_STAND = "🟥"
EMOJI_DD = "2️⃣"
EMOJI_SURRENDER = "🏳️"
EMOJI_SPLIT = "✂️"

# Accept both unicode and the Discord alias for the play arrow
DEAL_EMOJIS = {"▶", "▶️", "arrow_forward"}

# Map normalized emoji -> action
ACTION_EMOJI_MAP = {
    EMOJI_HIT: "hit",
    EMOJI_STAND: "stand",
    EMOJI_DD: "dd",
    EMOJI_SURRENDER: "surrender",
    EMOJI_SPLIT: "split",
    # Optional alternates:
    "🇭": "hit",
    "🇸": "stand",
    "🇩": "dd",
    "🇷": "surrender",
}

def _norm_emoji_name(e: discord.PartialEmoji | str) -> str:
    """Normalize for comparison: strip VS16 so '▶️' == '▶'."""
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


# =========================
# Table State
# =========================
def _table(gid: int) -> dict:
    g = _gdict(gid)
    st = g.setdefault("blackjack", {})
    st.setdefault("dealing_lock", False)
    st.setdefault("active", False)
    st.setdefault("players", {})  # uid -> per-player dict (see below)
    st.setdefault("shoe", [])
    st.setdefault("dealer", [])
    st.setdefault("phase", "idle")  # idle|betting|dealing|acting|dealer|payout
    st.setdefault("turn_idx", 0)
    st.setdefault("channel_id", 0)
    st.setdefault("last_action_ts", 0)
    st.setdefault("turn_started_ts", 0)
    st.setdefault("deal_msg_id", 0)     # current deal-button message id
    st.setdefault("action_msg_id", 0)   # current action prompt id
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
    Blackjack in one channel, always open:
      - `!bj <amount>` sets your bet.
      - React **▶️** on the *deal button* to start the hand.
      - On your turn, react: 🟩 Hit • 🟥 Stand • 2️⃣ Double • 🏳️ Surrender • ✂️ Split (when legal)

    Split rules:
      - Exactly one split per player per hand.
      - Only allowed with two cards of the same rank (e.g., 8-8, Q-Q).
      - Second hand places an additional bet equal to the original bet.
      - You play Hand 1 to completion, then Hand 2.

    Debug:
      !bjdebug on/off/toggle, !bjstate, !bjintents

    Anti-stall:
      - Acting player inactivity >120s: removed/refunded.
      - Table idle >5m: closed automatically.
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
            await channel.send(f"🛠️ **BJ DEBUG**: {msg}")

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
                "split": False,
                "active_hand": 0,  # 0 or 1
                # per-hand flags
                "stood": False, "busted": False, "finished": False, "doubled": False, "surrendered": False,
                "stood2": False, "busted2": False, "finished2": False, "doubled2": False, "surrendered2": False,
                "status": "betting",
                "in_table": True,
                "joined_ts": now_ts(),
                "natural_bj": False,  # only for unsplit original
            }
            st["players"][str(uid)] = p
        else:
            p.setdefault("in_table", True)
            p.setdefault("locked", 0)
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
                "hand": [], "hand2": [],
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
    async def _expire_old_deal_button(self, guild: discord.Guild, channel: discord.TextChannel):
        st = _table(guild.id); old_id = st.get("deal_msg_id")
        if not old_id: return
        try: msg = await channel.fetch_message(old_id)
        except Exception:
            st["deal_msg_id"] = 0; await save_data(); return
        try:
            await msg.edit(content=f"{msg.content}\n\n*⏭️ Previous hand complete — use the **new** prompt below.*")
        except Exception: pass
        try: await msg.clear_reactions()
        except Exception: pass
        st["deal_msg_id"] = 0; await save_data()

    async def _post_new_deal_button(self, guild: discord.Guild, channel: discord.TextChannel):
        st = _table(guild.id)
        m = await channel.send("🪙 **Place bets** with `!bj <amount>`; then react **▶️** here to deal the next hand.")
        st["deal_msg_id"] = m.id
        try: await m.add_reaction(EMOJI_DEAL)
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
            "active": False, "phase": "idle", "players": {}, "dealer": [],
            "shoe": [], "turn_idx": 0, "dealing_lock": False,
            "last_action_ts": 0, "turn_started_ts": 0,
            "deal_msg_id": 0, "action_msg_id": 0,
        })
        await save_data()
        if refunds:
            lines = ["♻️ **Blackjack reset by admin. Bets refunded:**"] + [f"- **{n}**: {b} XP" for n, b in refunds]
            await ctx.send("\n".join(lines))
        else:
            await ctx.send("♻️ **Blackjack reset by admin.** No active bets to refund.")
        await ctx.send("🛑 **Blackjack table closed.** Use `!bj` to open again.")

    @commands.command(name="blackjack", aliases=["bj"])
    async def blackjack(self, ctx: commands.Context, arg: Optional[str] = None):
        if not in_right_channel(ctx): return
        st = _table(ctx.guild.id)
        configured_channel_id = int(get_blackjack_channel_id(ctx.guild.id) or 0)
        st["channel_id"] = configured_channel_id if configured_channel_id > 0 else ctx.channel.id

        # Open table
        if not st["active"]:
            st["active"] = True; st["phase"] = "betting"; self._touch(st); await save_data()
            await self._broadcast(ctx, "🃏 **Blackjack is open!** Use `!bj <amount>` to set your bet.")
            await self._post_new_deal_button(ctx.guild, ctx.channel)
            return

        if arg is None:
            await self._show_table(ctx)
            if not st.get("deal_msg_id") and st.get("phase") in ("betting", "idle"):
                await self._post_new_deal_button(ctx.guild, ctx.channel)
            return

        a = arg.lower().strip()
        if a in ("table", "state", "status"):
            await self._show_table(ctx); return

        # set bet (supports numbers and 'all')
        if a.isdigit() or a == "all":
            if st["phase"] not in ("betting", "idle"):
                await ctx.reply("Bets are locked for the current hand.")
                return

            p = self._player(ctx.guild.id, ctx.author.id)
            u = _udict(ctx.guild.id, ctx.author.id)
            xp_cur = int(u.get("xp_f", u.get("xp", 0)))
            locked_cur = int(p.get("locked", 0))
            spendable = xp_cur + locked_cur

            if a == "all":
                # All-in includes currently reserved bet XP.
                amt = min(spendable, BJ_MAX_BET)
                if amt < BJ_MIN_BET:
                    await ctx.reply(f"All-in would be **{amt} XP**, which is below the minimum bet (**{BJ_MIN_BET} XP**).")
                    return
                note = " (all-in)"
            else:
                amt = int(a)
                note = ""
                if amt < BJ_MIN_BET or amt > BJ_MAX_BET:
                    await ctx.reply(f"Bet must be between **{BJ_MIN_BET}** and **{BJ_MAX_BET}** XP.")
                    return
                if spendable < amt:
                    await ctx.reply(f"You only have **{spendable} XP** available for betting, cannot bet **{amt} XP**.")
                    return

            # Reserve/deduct bet so it cannot be spent elsewhere.
            reserve_delta = locked_cur - amt
            if reserve_delta != 0:
                await self._apply_member_xp(
                    ctx.guild,
                    ctx.author,
                    reserve_delta,
                    sync_level=False,
                    source="blackjack ante",
                )

            p["bet"] = amt
            p["locked"] = amt
            p["status"] = "betting"
            p["in_table"] = True
            record_game_fields(
                ctx.guild.id,
                ctx.author.id,
                "blackjack",
                bets_set=1,
                stake_set_total=amt,
            )
            self._touch(st)
            st["phase"] = "betting"
            await save_data()

            await self._broadcast(ctx, f"💰 **{ctx.author.display_name}** set bet **{amt} XP**{note}.")
            if not _table(ctx.guild.id).get("deal_msg_id"):
                await self._post_new_deal_button(ctx.guild, ctx.channel)
            return

        if a in ("deal", "start"):
            await self._begin_deal_core(ctx.guild, ctx.channel); return

        if st["phase"] != "acting":
            await ctx.reply("No hand in progress or it’s not action time. React **▶️** to deal once bets are placed.")
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
        if not st["active"]:
            await ctx.reply("No active table."); return
        seated = self._seated_order(st, ctx.guild)
        dealer = st.get("dealer", [])
        lines = ["**Table**"]
        if not dealer:
            lines.append("Dealer: (none)")
        elif st.get("phase") in ("dealing", "acting"):
            # Hide the hole card until the dealer reveals
            lines.append(f"Dealer shows: {dealer[0]}")
        else:
            # After reveal (dealer/payout) or between hands, show full
            lines.append(f"Dealer: {pretty(dealer)}")
        for uid in seated:
            m = ctx.guild.get_member(uid); p = st["players"][str(uid)]
            # Build per-hand display
            parts = []
            parts.append(f"H1 {pretty(p.get('hand', [])) or '(no cards)'}")
            if p.get("split"):
                parts.append(f"H2 {pretty(p.get('hand2', [])) or '(no cards)'}")
            tags = []
            if p.get("busted"): tags.append("H1 bust")
            if p.get("stood"): tags.append("H1 stand")
            if p.get("surrendered"): tags.append("H1 surrender")
            if p.get("doubled"): tags.append("H1 DD")
            if p.get("split"):
                if p.get("busted2"): tags.append("H2 bust")
                if p.get("stood2"): tags.append("H2 stand")
                if p.get("surrendered2"): tags.append("H2 surrender")
                if p.get("doubled2"): tags.append("H2 DD")
            label = f" ({', '.join(tags)})" if tags else ""
            lines.append(f"- **{m.display_name}** bet **{p.get('bet',0)}**: {' | '.join(parts)}{label}")
        await ctx.reply("\n".join(lines))

    # =========================
    # Deal flow
    # =========================
    async def _begin_deal_core(self, guild: discord.Guild, channel: discord.TextChannel):
        st = _table(guild.id)
        if not st["active"]:
            st["active"] = True; st["phase"] = "betting"; await save_data()
        if st.get("phase") != "betting":
            await channel.send("A hand is already in progress or the table isn’t ready. Dealing only when bets are open."); return
        if st.get("dealing_lock"):
            await channel.send("Dealing is already starting… hang on."); return

        st["dealing_lock"] = True; await save_data()
        try:
            seated = self._seated_order(st, guild)
            ready = [uid for uid in seated if st["players"][str(uid)]["bet"] >= BJ_MIN_BET]
            if not ready:
                await channel.send("No bets placed. Each player must set a bet with `!bj <amount>`."); return

            # Safety check for legacy/unfunded bets: ensure each ready player has
            # their current stake actually reserved before cards are dealt.
            funded_ready = []
            for uid in ready:
                p = st["players"][str(uid)]
                bet = int(p.get("bet", 0))
                locked = int(p.get("locked", 0))
                missing = max(0, bet - locked)
                if missing > 0:
                    member = guild.get_member(uid)
                    if not member:
                        p["bet"] = 0; p["locked"] = 0; p["status"] = "betting"
                        continue
                    u = _udict(guild.id, uid)
                    xp_cur = int(u.get("xp_f", u.get("xp", 0)))
                    if xp_cur < missing:
                        p["bet"] = 0; p["locked"] = 0; p["status"] = "betting"
                        await channel.send(f"**{member.display_name}** no longer has enough XP for their saved bet and was removed from this deal.")
                        continue
                    await self._apply_member_xp(
                        guild,
                        member,
                        -missing,
                        sync_level=False,
                        source="blackjack ante",
                    )
                    p["locked"] = locked + missing
                funded_ready.append(uid)
            ready = funded_ready
            if not ready:
                await save_data()
                await channel.send("No funded bets available. Place bets again with `!bj <amount>`.")
                return

            for uid in ready:
                bet_amt = int(st["players"][str(uid)].get("bet", 0))
                record_game_fields(
                    guild.id,
                    uid,
                    "blackjack",
                    rounds_played=1,
                    hands_played=1,
                    xp_wagered_total=bet_amt,
                )

            st["phase"] = "dealing"; self._reset_hand(st)

            # initial deal: two to each, two to dealer
            for _ in range(2):
                for uid in ready: st["players"][str(uid)]["hand"].append(self._deal_card(st))
                st["dealer"].append(self._deal_card(st))

            for uid in ready: st["players"][str(uid)]["status"] = "acting"

            self._touch(st); await save_data()

            up = st["dealer"][0]
            lines = [f"🂡 **Dealing...** Dealer shows: `{up}`"]
            for uid in ready:
                m = guild.get_member(uid); ph = st["players"][str(uid)]["hand"]
                tot, bj = value_of_hand(ph); tag = " (blackjack!)" if bj else ""
                lines.append(f"- **{m.display_name}**: {pretty(ph)} → **{tot}**{tag}")
            await channel.send("\n".join(lines))

            # Dealer natural
            dealer_total, dealer_bj = value_of_hand(st["dealer"])
            if dealer_bj:
                st["phase"] = "payout"; lines = ["🏁 **Dealer has Blackjack!**"]
                for uid in ready:
                    p = st["players"][str(uid)]; bet = int(p.get("bet", 0))
                    ptot, p_bj = value_of_hand(p["hand"])
                    payout = bet if p_bj else 0
                    if p_bj:
                        record_game_fields(guild.id, uid, "blackjack", draws=1, naturals=1, xp_profit_total=0)
                    else:
                        record_game_fields(guild.id, uid, "blackjack", losses=1, xp_profit_total=-bet)
                    outcome = "push (both blackjack)" if p_bj else f"lose −{bet}"
                    member = guild.get_member(uid)
                    if member:
                        await self._apply_member_xp(
                            guild,
                            member,
                            payout,
                            sync_level=True,
                            source="blackjack payout",
                        )
                    name = member.display_name if member else f"User {uid}"
                    lines.append(f"- **{name}** ({pretty(p['hand'])}) → {outcome}")
                    p["bet"] = 0; p["locked"] = 0; p["status"] = "done"; p["finished"] = True
                await channel.send("\n".join(lines))

                st["phase"] = "betting"; st["dealer"] = []
                for p in st["players"].values():
                    if p.get("in_table"):
                        p.update({"status":"betting","bet":0,"locked":0,"hand":[],"hand2":[],"split":False,"active_hand":0,
                                  "stood":False,"busted":False,"finished":False,"doubled":False,"surrendered":False,
                                  "stood2":False,"busted2":False,"finished2":False,"doubled2":False,"surrendered2":False,
                                  "natural_bj":False})
                self._touch(st); await save_data()
                await self._expire_old_deal_button(guild, channel)
                await self._post_new_deal_button(guild, channel)
                return

            # Pay naturals (3:2) on unsplit starters
            winners = []
            for uid in ready:
                p = st["players"][str(uid)]
                tot, p_bj = value_of_hand(p["hand"])
                if p_bj:
                    bet = int(p.get("bet", 0))
                    profit = int(bet * 1.5)
                    payout = bet + profit
                    member = guild.get_member(uid)
                    if member and payout > 0:
                        await self._apply_member_xp(guild, member, payout, source="blackjack payout")
                    record_game_fields(guild.id, uid, "blackjack", wins=1, naturals=1, xp_profit_total=profit)
                    p["locked"] = 0
                    p["natural_bj"] = True; p["status"] = "done"; p["finished"] = True
                    winners.append((uid, bet, profit))

            if winners:
                lines = ["🎉 **Natural Blackjack!**"]
                for uid, bet, profit in winners:
                    member = guild.get_member(uid); name = member.display_name if member else f"User {uid}"
                    lines.append(f"- **{name}** won **{profit} XP** on a {bet} XP bet!")
                await channel.send("\n".join(lines))

            st["phase"] = "acting"; st["turn_idx"] = 0; self._start_turn(st); await save_data()
            remaining = [uid for uid in ready if st["players"][str(uid)].get("status") == "acting"]
            if not remaining:
                await self._dealer_then_payout(self._ShimCtx(guild, channel), st); return

            await channel.send("Turn order: " + " → ".join(guild.get_member(uid).display_name for uid in remaining))
            await self._prompt_turn_with_reactions(guild, channel); return
        finally:
            st["dealing_lock"] = False; await save_data()

    # =========================
    # Player actions (cmd compatibility)
    # =========================
    async def _handle_action_command(self, ctx: commands.Context, a: str):
        st = _table(ctx.guild.id); p = st["players"].get(str(ctx.author.id))
        if not p or not p.get("in_table"):
            await ctx.reply("You’re not at the table."); return
        if self._both_hands_finished(p):
            await ctx.reply("Your action is already finished for this hand."); return

        order = self._seated_order(st, ctx.guild)
        acting_ids = [uid for uid in order if st["players"][str(uid)].get("status") == "acting" and st["players"][str(uid)].get("in_table")]
        if not acting_ids:
            await ctx.reply("No players available to act."); return
        cur_uid = acting_ids[st["turn_idx"] % len(acting_ids)]
        if ctx.author.id != cur_uid:
            await ctx.reply(f"It’s **{ctx.guild.get_member(cur_uid).display_name}**’s turn."); return

        a = a.lower(); self._touch(st)
        if a == "hit":   await self._do_hit(ctx.guild, ctx.channel, cur_uid); return
        if a == "stand": await self._do_stand(ctx.guild, ctx.channel, cur_uid); return
        if a in ("doubledown","dd"): await self._do_dd(ctx.guild, ctx.channel, cur_uid); return
        if a == "surrender": await self._do_surrender(ctx.guild, ctx.channel, cur_uid); return
        if a == "split": await self._do_split(ctx.guild, ctx.channel, cur_uid); return
        await ctx.reply("Unknown action. React on the action prompt: 🟩 Hit / 🟥 Stand / 2️⃣ Double / 🏳️ Surrender / ✂️ Split")

    async def _prompt_turn_with_reactions(self, guild: discord.Guild, channel: discord.TextChannel):
        st = _table(guild.id)
        order = self._seated_order(st, guild)
        acting = [uid for uid in order if st["players"][str(uid)].get("status") == "acting" and st["players"][str(uid)].get("in_table")]
        if not acting:
            await self._dealer_then_payout(self._ShimCtx(guild, channel), st); return
        cur_uid = acting[st["turn_idx"] % len(acting)]
        p = st["players"][str(cur_uid)]
        self._start_turn(st); await save_data()
        # Determine if split is available on the current hand
        hand = self._hand_ref(p)
        can_split = (not p.get("split")) and len(hand) == 2 and rank_of(hand[0]) == rank_of(hand[1])
        hand_label = "Hand 1" if p.get("active_hand",0) == 0 else "Hand 2"
        msg = await channel.send(
            f"👉 **{guild.get_member(cur_uid).display_name}**, your move — **{hand_label}** — react: "
            f"{EMOJI_HIT} Hit • {EMOJI_STAND} Stand • {EMOJI_DD} Double • {EMOJI_SURRENDER} Surrender"
            + (f" • {EMOJI_SPLIT} Split" if can_split else "")
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
        tot, _ = value_of_hand(hand); await save_data()
        label = "Hand 1" if p.get("active_hand",0)==0 else "Hand 2"
        await channel.send(f"✋ **{guild.get_member(uid).display_name} HIT** ({label}) → drew `{card}` → {pretty(hand)} (**{tot}**)")
        if tot > 21:
            self._set_flag(p, "busted", True); self._set_flag(p, "finished", True); await save_data()
            record_game_fields(guild.id, uid, "blackjack", busts=1)
            await channel.send(f"💥 **{guild.get_member(uid).display_name} BUSTED** ({label}).")
            # If split, switch to the other hand if it remains
            if await self._advance_after_hand_if_split(guild, channel, st, uid): return
            await self._advance_turn_or_dealer(self._ShimCtx(guild, channel), st); return
        self._start_turn(st); await save_data()

    async def _do_stand(self, guild: discord.Guild, channel: discord.TextChannel, uid: int):
        st = _table(guild.id); p = st["players"][str(uid)]
        self._set_flag(p, "stood", True); self._set_flag(p, "finished", True); await save_data()
        label = "Hand 1" if p.get("active_hand",0)==0 else "Hand 2"
        await channel.send(f"🧍 **{guild.get_member(uid).display_name} STANDS** ({label}).")
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
        p["locked"] = int(p.get("locked", 0)) + need
        self._set_flag(p, "doubled", True)
        record_game_fields(guild.id, uid, "blackjack", doubles=1, xp_wagered_total=need)
        card = self._deal_card(st); hand.append(card)
        tot, _ = value_of_hand(hand)
        self._set_flag(p, "finished", True); self._set_flag(p, "stood", True); await save_data()
        label = "Hand 1" if p.get("active_hand",0)==0 else "Hand 2"
        await channel.send(f"✌️ **{guild.get_member(uid).display_name} DOUBLE DOWN** ({label}) → drew `{card}` → {pretty(hand)} (**{tot}**)")
        if tot > 21:
            self._set_flag(p, "busted", True); await save_data()
            record_game_fields(guild.id, uid, "blackjack", busts=1)
            await channel.send(f"💥 **{guild.get_member(uid).display_name} BUSTED** ({label}).")
        if await self._advance_after_hand_if_split(guild, channel, st, uid): return
        await self._advance_turn_or_dealer(self._ShimCtx(guild, channel), st)

    async def _do_surrender(self, guild: discord.Guild, channel: discord.TextChannel, uid: int):
        st = _table(guild.id); p = st["players"][str(uid)]
        hand = self._hand_ref(p)
        if len(hand) != 2 or self._get_flag(p, "doubled"):
            await channel.send("You can only surrender on your first action (before hitting)."); return
        self._set_flag(p, "surrendered", True); self._set_flag(p, "finished", True); self._set_flag(p, "stood", True)
        record_game_fields(guild.id, uid, "blackjack", surrenders=1)
        await save_data()
        label = "Hand 1" if p.get("active_hand",0)==0 else "Hand 2"
        await channel.send(f"🏳️ **{guild.get_member(uid).display_name} SURRENDERS** ({label}) (loses half their bet).")
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
        p["locked"] = int(p.get("locked", 0)) + need
        record_game_fields(guild.id, uid, "blackjack", splits=1, hands_played=1, xp_wagered_total=need)
        # Perform split: move one card to hand2; start active_hand=0
        c = h.pop()                # move second to hand2
        p["hand2"] = [c]
        p["split"] = True
        # Reset flags for hand2
        p["stood2"] = p["busted2"] = p["finished2"] = p["doubled2"] = p["surrendered2"] = False
        await save_data()
        await channel.send(f"✂️ **{guild.get_member(uid).display_name} SPLITS** → Hand 1: {pretty(p['hand'])} • Hand 2: {pretty(p['hand2'])}")
        # Continue acting on Hand 1; player can hit/stand/etc.; when finished, engine will switch to Hand 2
        self._start_turn(st); await save_data()

    # =========================
    # Dealer & Payout
    # =========================
    async def _dealer_then_payout(self, ctx, st: dict):
        guild = ctx.guild; channel = ctx.channel

        st["phase"] = "dealer"; self._touch(st); await save_data()

        dhand = st["dealer"]; await channel.send(f"🃏 **Dealer reveals**: {pretty(dhand)}")
        while True:
            total, _ = value_of_hand(dhand)
            if total < 17:
                card = self._deal_card(st); dhand.append(card)
                await channel.send(f"Dealer draws `{card}` → {pretty(dhand)} (**{value_of_hand(dhand)[0]}**)")
                self._touch(st)
            else: break

        dealer_total, _ = value_of_hand(dhand)
        dealer_bust = dealer_total > 21

        st["phase"] = "payout"; self._touch(st); await save_data()
        lines = ["🏁 **Payouts**"]

        for uid_str, p in list(st["players"].items()):
            if not p.get("in_table"): continue
            bet = int(p.get("bet", 0))
            if bet < BJ_MIN_BET: continue

            # Natural BJ was already settled up front.
            if p.get("natural_bj"):
                p["bet"] = 0
                p["locked"] = 0
                continue

            # Helper to settle a single hand
            def settle_one(hand: List[str], doubled_flag: bool, surrendered_flag: bool, busted_flag: bool, label: str):
                nonlocal lines
                total, _ = value_of_hand(hand)
                base_bet = bet * (2 if doubled_flag else 1)
                if surrendered_flag:
                    delta = -(bet // 2); outcome = f"{label} surrender −{bet // 2}"
                elif busted_flag:
                    delta = -base_bet; outcome = f"{label} bust −{base_bet}"
                else:
                    if dealer_bust:
                        delta = +base_bet; outcome = f"{label} dealer bust +{base_bet}"
                    else:
                        if total > dealer_total:
                            delta = +base_bet; outcome = f"{label} win +{base_bet}"
                        elif total < dealer_total:
                            delta = -base_bet; outcome = f"{label} lose −{base_bet}"
                        else:
                            delta = 0; outcome = f"{label} push ±0"
                payout = max(0, base_bet + delta)
                return payout, outcome

            # Resolve Hand 1
            p1, out1 = settle_one(
                p["hand"], p.get("doubled", False), p.get("surrendered", False), p.get("busted", False), "H1"
            )
            # Resolve Hand 2 if split
            p2 = 0; out2 = None
            if p.get("split"):
                p2, out2 = settle_one(
                    p.get("hand2", []), p.get("doubled2", False), p.get("surrendered2", False), p.get("busted2", False), "H2"
                )

            def _result_key(outcome_text: str) -> str:
                t = outcome_text.lower()
                if "surrender" in t:
                    return "surrender"
                if "lose" in t:
                    return "loss"
                if "push" in t:
                    return "draw"
                if "win" in t or "dealer bust" in t:
                    return "win"
                if "bust" in t:
                    return "loss"
                return "draw"

            uid = int(uid_str)
            base1 = bet * (2 if p.get("doubled", False) else 1)
            res1 = _result_key(out1)
            profit1 = p1 - base1
            if res1 == "win":
                record_game_fields(guild.id, uid, "blackjack", wins=1, xp_profit_total=profit1)
            elif res1 == "loss":
                record_game_fields(guild.id, uid, "blackjack", losses=1, xp_profit_total=profit1)
            elif res1 == "surrender":
                record_game_fields(guild.id, uid, "blackjack", losses=1, xp_profit_total=profit1)
            else:
                record_game_fields(guild.id, uid, "blackjack", draws=1, xp_profit_total=profit1)

            if out2 is not None:
                base2 = bet * (2 if p.get("doubled2", False) else 1)
                res2 = _result_key(out2)
                profit2 = p2 - base2
                if res2 == "win":
                    record_game_fields(guild.id, uid, "blackjack", wins=1, xp_profit_total=profit2)
                elif res2 == "loss":
                    record_game_fields(guild.id, uid, "blackjack", losses=1, xp_profit_total=profit2)
                elif res2 == "surrender":
                    record_game_fields(guild.id, uid, "blackjack", losses=1, xp_profit_total=profit2)
                else:
                    record_game_fields(guild.id, uid, "blackjack", draws=1, xp_profit_total=profit2)

            payout_total = p1 + p2
            member = guild.get_member(int(uid_str))
            if member:
                await self._apply_member_xp(
                    guild,
                    member,
                    payout_total,
                    sync_level=True,
                    source="blackjack payout",
                )

            name = guild.get_member(int(uid_str)).display_name if guild.get_member(int(uid_str)) else f"User {uid_str}"
            lines.append(f"- **{name}** ({pretty(p['hand'])}" + (f" | {pretty(p.get('hand2', []))}" if p.get("split") else "") + f")")
            lines.append(f"  • {out1}")
            if out2 is not None:
                lines.append(f"  • {out2}")

            p["bet"] = 0
            p["locked"] = 0

        await channel.send("\n".join(lines))

        # Reset for next betting round
        st["phase"] = "betting"; st["dealer"] = []; st["action_msg_id"] = 0
        for p in st["players"].values():
            if p.get("in_table"):
                p.update({
                    "status": "betting", "bet": 0, "locked": 0, "hand": [], "hand2": [], "split": False, "active_hand": 0,
                    "stood": False, "busted": False, "finished": False, "doubled": False, "surrendered": False,
                    "stood2": False, "busted2": False, "finished2": False, "doubled2": False, "surrendered2": False,
                    "natural_bj": False,
                })
        self._touch(st); await save_data()

        await self._expire_old_deal_button(guild, channel)
        await self._post_new_deal_button(guild, channel)

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

        em_name = _norm_emoji_name(payload.emoji)
        await self._dprint(guild, ch, f"reaction on msg_id={payload.message_id}, emoji='{em_name}', user={payload.user_id}")

        # Deal button
        deal_id = st.get("deal_msg_id")
        if deal_id and payload.message_id == deal_id:
            await self._dprint(guild, ch, f"match deal_msg_id={deal_id}")
            if em_name in DEAL_EMOJIS:
                await self._dprint(guild, ch, "deal emoji accepted → begin deal")
                try: await self._begin_deal_core(guild, ch)
                except Exception as e: await self._dprint(guild, ch, f"_begin_deal_core error: {type(e).__name__}: {e}")
            else:
                await self._dprint(guild, ch, f"deal emoji not accepted: '{em_name}' not in {DEAL_EMOJIS}")
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
            if not st.get("active"): continue
            fallback_channel_id = int(get_blackjack_channel_id(guild.id) or 0)
            ch = guild.get_channel(int(st.get("channel_id", 0) or fallback_channel_id))
            if not isinstance(ch, discord.TextChannel): continue

            last = int(st.get("last_action_ts", 0))
            if last and now_ts() - last > 5 * 60:
                for uid_str, p in list(st.get("players", {}).items()):
                    locked = int(p.get("locked", 0))
                    if locked <= 0:
                        continue
                    member = guild.get_member(int(uid_str))
                    if member:
                        await self._apply_member_xp(guild, member, locked, source="blackjack refund")
                st.update({
                    "active": False, "phase": "idle", "players": {}, "dealer": [],
                    "turn_idx": 0, "turn_started_ts": 0, "dealing_lock": False,
                    "deal_msg_id": 0, "action_msg_id": 0
                })
                await save_data(); await ch.send("⏱️ **Blackjack table closed due to inactivity (5 minutes).**")
                continue

            if st.get("phase") != "acting": continue

            order = self._seated_order(st, guild)
            acting_ids = [uid for uid in order if st["players"][str(uid)].get("status") == "acting" and st["players"][str(uid)].get("in_table")]
            if not acting_ids: continue
            cur_idx = st.get("turn_idx", 0) % len(acting_ids)
            cur_uid = acting_ids[cur_idx]
            p = st["players"][str(cur_uid)]
            started = int(st.get("turn_started_ts", 0))
            if started and now_ts() - started > 120:
                locked = int(p.get("locked", 0))
                if locked > 0:
                    member = guild.get_member(cur_uid)
                    if member:
                        await self._apply_member_xp(guild, member, locked, source="blackjack refund")
                name = guild.get_member(cur_uid).display_name if guild.get_member(cur_uid) else f"User {cur_uid}"
                p["bet"] = 0; p["locked"] = 0; p["in_table"] = False; p["status"] = "done"; p["finished"] = True; p["finished2"] = True
                await save_data(); await ch.send(f"⏲️ **{name}** was removed for inactivity (>120s). Bet refunded and seat released.")

                if not any(x.get("in_table") for x in st["players"].values()):
                    st.update({
                        "active": False, "phase": "idle", "players": {}, "dealer": [],
                        "turn_idx": 0, "turn_started_ts": 0, "dealing_lock": False,
                        "deal_msg_id": 0, "action_msg_id": 0
                    })
                    await save_data(); await ch.send("🛑 **Blackjack table closed (no seated players).**")
                    continue

                acting_ids = [uid for uid in self._seated_order(st, guild)
                              if st["players"][str(uid)].get("status") == "acting" and st["players"][str(uid)].get("in_table")]
                if not acting_ids:
                    await self._dealer_then_payout(self._ShimCtx(guild, ch), st)
                else:
                    st["turn_idx"] = st["turn_idx"] % len(acting_ids)
                    self._start_turn(st); await save_data()
                    await self._prompt_turn_with_reactions(guild, ch)

    @guard_loop.before_loop
    async def _before_guard(self):
        await self.bot.wait_until_ready()
