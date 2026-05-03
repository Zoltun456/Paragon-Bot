from __future__ import annotations

from datetime import datetime
from typing import Optional

import discord
from discord.ext import commands

from .anagram import ANAGRAM_DAILY_LIMIT, _state as _anagram_state
from .blackjack import (
    _cycle_key as _bj_cycle_key,
    _next_reset_dt as _bj_next_reset_dt,
    _sanitize_reset_time as _bj_sanitize_reset_time,
    _table as _bj_table,
)
from .bounty import _bounty_state
from .config import LOCAL_TZ
from .contracts import (
    CONTRACT_FAST_CLEAR_BONUS_MINUTES,
    CONTRACT_FAST_CLEAR_BONUS_PCT,
    CONTRACT_FAST_CLEAR_WINDOW_SECONDS,
    _contract_state,
    _is_complete,
    _progress_rows,
)
from .fish import WATER_STATES, _session_state as _fish_session_state, _state_root as _fish_state_root
from .fish_support import get_bait
from .include import _as_dict, _as_int, _parse_iso, _utcnow
from .lotto import (
    LOTTO_MAX_PER_USER,
    LOTTO_TICKET_COST,
    _lotto_state,
    _next_draw_dt,
    _sanitize_draw_time,
    _ticket_totals,
)
from .ownership import is_control_user_id
from .shop import (
    SHOP_ITEMS,
    _shop_buy_counter_key,
    _shop_cycle,
    _shop_item_costs,
    _shop_state,
    _sync_shop_cycle_state,
)
from .spin import (
    _available_spins,
    _cycle_key as _spin_cycle_key,
    _next_reset_dt as _spin_next_reset_dt,
    _sanitize_reset_time as _spin_sanitize_reset_time,
    _spin_user_state,
    _sync_spin_cycle_state,
    _wheel_state,
)
from .storage import _udict, save_data
from .surprise import _state as _surprise_state
from .thanks import _thanks_state
from .time_windows import _date_key, _today_local
from .wordle import WORDLE_MAX_GUESSES, _reset_daily_wordle_state, _user_wordle_state


_BAIT_CRATE_ITEM = next(
    (item for item in SHOP_ITEMS if str(item.get("key", "")).strip().lower() == "bait_crate"),
    None,
)


def _can_view_others(ctx: commands.Context) -> bool:
    if is_control_user_id(ctx.guild, ctx.author.id):
        return True
    perms = getattr(ctx.author, "guild_permissions", None)
    if not perms:
        return False
    return bool(perms.manage_guild or perms.administrator)


def _fmt_when(dt: datetime) -> str:
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %I:%M %p ET")


def _fmt_duration_seconds(seconds: int | float) -> str:
    total = max(0, int(round(float(seconds))))
    minutes, secs = divmod(total, 60)
    if minutes <= 0:
        return f"{secs}s"
    if secs <= 0:
        return f"{minutes}m"
    return f"{minutes}m {secs:02d}s"


def _chunk_lines(lines: list[str], max_len: int = 1800) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        add_len = len(line) + (1 if current else 0)
        if current and current_len + add_len > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += add_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def _contract_progress_label(row: dict[str, object]) -> str:
    target = max(1, int(row.get("target", 1)))
    current = min(float(target), max(0.0, float(row.get("current", 0.0))))
    if abs(current - round(current)) < 1e-9:
        current_label = str(int(round(current)))
    else:
        current_label = f"{current:.1f}"
    return f"{current_label}/{target}"


class ChecklistCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _send_lines(self, ctx: commands.Context, lines: list[str]) -> None:
        chunks = _chunk_lines(lines)
        for idx, chunk in enumerate(chunks):
            if idx == 0:
                await ctx.reply(chunk)
            else:
                await ctx.send(chunk)

    async def _ensure_external_daily_state(self, guild: discord.Guild, member: discord.Member) -> None:
        contracts_cog = self.bot.get_cog("ContractsCog")
        if contracts_cog is not None and hasattr(contracts_cog, "_ensure_daily_contract"):
            try:
                await contracts_cog._ensure_daily_contract(guild, member)
            except Exception:
                pass

        bounty_cog = self.bot.get_cog("BountyCog")
        if bounty_cog is not None and hasattr(bounty_cog, "_ensure_today_state"):
            try:
                await bounty_cog._ensure_today_state(guild)
            except Exception:
                pass

    def _wordle_lines(self, guild: discord.Guild, member: discord.Member, today: str) -> tuple[list[str], bool]:
        dirty = False
        st = _user_wordle_state(guild.id, member.id)
        if str(st.get("date", "")) != today:
            _reset_daily_wordle_state(st, today)
            dirty = True

        guesses = [str(guess) for guess in st.get("guesses", []) if str(guess).strip()]
        if bool(st.get("done", False)):
            if bool(st.get("win", False)):
                return ["- Wordle: **done**. Solved today."], dirty
            return ["- Wordle: **done**. Finished without a solve today."], dirty

        remaining = max(0, int(WORDLE_MAX_GUESSES) - len(guesses))
        if guesses:
            return [
                f"- Wordle: **ready** with **{remaining}** guess(es) left ({len(guesses)}/{WORDLE_MAX_GUESSES} used)."
            ], dirty
        return [f"- Wordle: **ready** with **{WORDLE_MAX_GUESSES}** guess(es) left."], dirty

    def _anagram_lines(self, guild: discord.Guild, member: discord.Member, today: str) -> tuple[list[str], bool]:
        dirty = False
        st = _anagram_state(guild.id, member.id)
        if str(st.get("date", "")) != today:
            st["date"] = today
            st["used"] = 0
            st["solved"] = 0
            st["idx"] = 0
            st["awaiting"] = False
            st["scrambled"] = ""
            st["answer"] = ""
            dirty = True

        used = max(0, int(st.get("used", 0)))
        remaining = max(0, int(ANAGRAM_DAILY_LIMIT) - used)
        if remaining <= 0:
            return [f"- Anagram: **done**. Used **{ANAGRAM_DAILY_LIMIT}/{ANAGRAM_DAILY_LIMIT}** attempts today."], dirty

        active_text = " Active puzzle waiting." if bool(st.get("awaiting", False)) else ""
        return [
            f"- Anagram: **ready** with **{remaining}** attempt(s) left ({used}/{ANAGRAM_DAILY_LIMIT} used).{active_text}"
        ], dirty

    def _thanks_lines(self, guild: discord.Guild, member: discord.Member, today: str) -> tuple[list[str], bool]:
        dirty = False
        st = _thanks_state(guild.id, member.id)
        if str(st.get("date", "")) != today:
            st["date"] = today
            st["used"] = False
            st["target"] = 0
            dirty = True

        if not bool(st.get("used", False)):
            return ["- Thanks: **ready**."], dirty

        target_id = int(st.get("target", 0) or 0)
        target = guild.get_member(target_id) if target_id > 0 else None
        target_name = target.display_name if target is not None else f"User {target_id}" if target_id > 0 else "someone"
        return [f"- Thanks: **done**. Sent to **{target_name}**."], dirty

    def _spin_lines(self, guild: discord.Guild, member: discord.Member) -> tuple[list[str], bool]:
        dirty = False
        wheel_state = _wheel_state(guild.id)
        hour, minute = _spin_sanitize_reset_time(
            wheel_state.get("reset_hour", 0),
            wheel_state.get("reset_minute", 0),
        )
        cycle = _spin_cycle_key(hour, minute)
        next_reset = _spin_next_reset_dt(hour, minute)

        st = _spin_user_state(guild.id, member.id)
        before = (
            str(st.get("cycle_key", "")),
            int(st.get("daily_spins_remaining", 0)),
            int(st.get("bonus_spins", 0)),
            bool(st.get("spun", False)),
        )
        _sync_spin_cycle_state(st, cycle)
        after = (
            str(st.get("cycle_key", "")),
            int(st.get("daily_spins_remaining", 0)),
            int(st.get("bonus_spins", 0)),
            bool(st.get("spun", False)),
        )
        dirty = before != after

        free_spins = max(0, int(st.get("daily_spins_remaining", 0)))
        bonus_spins = max(0, int(st.get("bonus_spins", 0)))
        total_spins = _available_spins(st)
        if free_spins > 0:
            status = f"**ready** ({free_spins} free, {bonus_spins} bonus, {total_spins} total)"
        elif bonus_spins > 0:
            status = f"**bonus only** ({bonus_spins} bonus, {total_spins} total)"
        else:
            status = "**used** (0 spins left)"
        return [f"- Wheel Spin: {status}. Next reset: **{_fmt_when(next_reset)}**."], dirty

    def _fishing_lines(self, guild: discord.Guild, member: discord.Member) -> tuple[list[str], bool]:
        dirty = False
        shop_state = _shop_state(guild.id, member.id)
        cycle = _shop_cycle(guild.id)
        before = (str(shop_state.get("cycle_key", "")), int(shop_state.get(_shop_buy_counter_key("bait_crate"), 0)))
        _sync_shop_cycle_state(shop_state, cycle)
        after = (str(shop_state.get("cycle_key", "")), int(shop_state.get(_shop_buy_counter_key("bait_crate"), 0)))
        dirty = before != after

        bait_buys = max(0, int(shop_state.get(_shop_buy_counter_key("bait_crate"), 0)))
        bait = get_bait(guild.id, member.id)
        state_root = _fish_state_root(guild.id)
        water = WATER_STATES.get(
            str(state_root.get("state_key", "")).strip().lower(),
            WATER_STATES["empty_reach"],
        )
        water_name = str(water.get("name", "Unknown Water")).strip() or "Unknown Water"

        session = _fish_session_state(guild.id, member.id)
        phase = str(session.get("phase", "idle")).strip().lower()
        if bool(session.get("active", False)):
            if phase == "waiting":
                line_status = "line waiting for a bite"
            elif phase == "bite":
                line_status = "bite up now"
            elif phase == "fight":
                line_status = "actively reeling"
            else:
                line_status = "line out"
        else:
            line_status = "line packed up"

        if bait_buys <= 0:
            crate_text = "free Bait Crate x25 ready"
        else:
            next_cost = 0
            if _BAIT_CRATE_ITEM is not None:
                next_costs = _shop_item_costs(_BAIT_CRATE_ITEM, guild.id, member.id, 1)
                next_cost = next_costs[0] if next_costs else 0
            crate_text = (
                f"free Bait Crate used, next crate **{next_cost} XP**"
                if next_cost > 0
                else "free Bait Crate used"
            )

        return [
            f"- Fishing: **{bait}** bait | {crate_text} | {line_status} in **{water_name}**."
        ], dirty

    def _lotto_lines(self, guild: discord.Guild, member: discord.Member) -> tuple[list[str], bool]:
        st = _lotto_state(guild.id)
        if not bool(st.get("enabled", True)):
            return ["- Lotto: **disabled** on this server."], False

        tickets = max(0, int(_as_dict(st.get("tickets")).get(str(member.id), 0)))
        pot = max(0, int(st.get("pot", 0)))
        draw_hour, draw_minute = _sanitize_draw_time(
            st.get("draw_hour", 0),
            st.get("draw_minute", 0),
        )
        next_draw = _next_draw_dt(draw_hour, draw_minute)
        remaining_xp = max(0, int(LOTTO_MAX_PER_USER) - (tickets * int(LOTTO_TICKET_COST)))
        remaining_tickets = max(0, remaining_xp // max(1, int(LOTTO_TICKET_COST)))
        total_tickets, _ = _ticket_totals(st)

        if remaining_tickets > 0:
            status = f"**ready** ({tickets} ticket(s) in pot, {remaining_tickets} more before cap)"
        else:
            status = f"**cap reached** ({tickets} ticket(s) in pot)"
        return [
            f"- Lotto: {status}. Pot **{pot} XP** from **{total_tickets}** ticket(s). Next draw: **{_fmt_when(next_draw)}**."
        ], False

    def _surprise_lines(self, guild: discord.Guild) -> tuple[list[str], bool]:
        st = _surprise_state(guild.id)
        pending = st.get("pending_rewards")
        pending_rewards = list(pending) if isinstance(pending, list) else []
        if pending_rewards:
            return [f"- Surprise Drop: **claimable now** ({len(pending_rewards)} stacked drop(s))."], False

        next_at = _parse_iso(st.get("next_at"))
        if next_at is not None:
            return [f"- Surprise Drop: no active drop. Next roll: **{_fmt_when(next_at)}**."], False
        return ["- Surprise Drop: no active drop."], False

    def _blackjack_lines(self, guild: discord.Guild, member: discord.Member) -> tuple[list[str], bool]:
        dirty = False
        table = _bj_table(guild.id)
        hour, minute = _bj_sanitize_reset_time(
            table.get("reset_hour", 0),
            table.get("reset_minute", 0),
        )
        cycle = _bj_cycle_key(hour, minute)
        next_reset = _bj_next_reset_dt(hour, minute)

        user_state = _udict(guild.id, member.id).get("blackjack_daily")
        if not isinstance(user_state, dict):
            user_state = {"cycle_key": cycle, "locked": False, "locked_ts": 0, "last_result": "", "streak": 0}
            _udict(guild.id, member.id)["blackjack_daily"] = user_state
            dirty = True

        if str(user_state.get("cycle_key", "")) != cycle:
            user_state["cycle_key"] = cycle
            user_state["locked"] = False
            user_state["locked_ts"] = 0
            user_state["last_result"] = ""
            user_state["streak"] = 0
            dirty = True

        cooldown_enabled = bool(table.get("cooldown_enabled", False))
        if not cooldown_enabled:
            return ["- Blackjack: **open**. Daily lockout is disabled."], dirty
        if bool(user_state.get("locked", False)):
            return [f"- Blackjack: **locked until reset** (**{_fmt_when(next_reset)}**)."], dirty
        return [f"- Blackjack: **eligible**. Reset: **{_fmt_when(next_reset)}**."], dirty

    def _contract_lines(
        self,
        ctx: commands.Context,
        member: discord.Member,
    ) -> tuple[list[str], bool]:
        st = _contract_state(ctx.guild.id, member.id)
        quest = _as_dict(st.get("quest"))
        baselines = _as_dict(st.get("baselines"))
        progress_rows = _progress_rows(ctx.guild.id, member.id, quest, baselines)

        if not progress_rows:
            return ["- Contract: no daily contract assigned yet."], False

        claimed = bool(st.get("claimed", False))
        complete = _is_complete(progress_rows)
        done_count = sum(1 for row in progress_rows if bool(row.get("done", False)))
        total_count = len(progress_rows)
        title = str(quest.get("title", "Daily Contract")).strip() or "Daily Contract"

        lines: list[str] = []
        if claimed:
            lines.append(f"- Contract: **done**. **{title}** already claimed today.")
        elif complete:
            lines.append(f"- Contract: **complete**. **{title}** is waiting for auto-payout.")
        else:
            lines.append(f"- Contract: **{done_count}/{total_count}** done on **{title}**.")

        if member.id == ctx.author.id and not claimed:
            seen_at = _parse_iso(st.get("seen_at"))
            if seen_at is not None:
                remaining = int(
                    float(CONTRACT_FAST_CLEAR_WINDOW_SECONDS)
                    - max(0.0, (_utcnow() - seen_at).total_seconds())
                )
                if remaining > 0:
                    lines.append(
                        f"- Contract Bonus: fast clear active for **{_fmt_duration_seconds(remaining)}** "
                        f"(+{CONTRACT_FAST_CLEAR_BONUS_PCT * 100.0:.1f}% XP/min for {CONTRACT_FAST_CLEAR_BONUS_MINUTES}m)."
                    )

        if not claimed:
            for row in progress_rows:
                if bool(row.get("done", False)):
                    continue
                lines.append(
                    f"- Contract Left: {row.get('label', 'Objective')} (**{_contract_progress_label(row)}**)."
                )

        return lines, False

    def _bounty_lines(
        self,
        ctx: commands.Context,
        member: discord.Member,
    ) -> tuple[list[str], bool]:
        st = _bounty_state(ctx.guild.id)
        today = _date_key(_today_local())
        if str(st.get("date", "")) != today:
            return ["- Bounty: state is still syncing."], False

        target_id = _as_int(st.get("target_user_id", 0), 0)
        if target_id <= 0:
            return ["- Bounty: no target assigned today."], False

        target = ctx.guild.get_member(target_id)
        target_name = target.display_name if target is not None else f"User {target_id}"

        if bool(st.get("resolved", False)):
            if str(st.get("result", "")).strip().lower() == "claimed":
                winner_id = _as_int(st.get("winner_user_id", 0), 0)
                winner = ctx.guild.get_member(winner_id)
                winner_name = winner.display_name if winner is not None else f"User {winner_id}" if winner_id > 0 else "someone"
                return [f"- Bounty: **resolved**. **{winner_name}** collected the bounty on **{target_name}**."], False
            return [f"- Bounty: **resolved**. **{target_name}** survived today."], False

        claimant_id = _as_int(st.get("claimant_user_id", 0), 0)
        claimant = ctx.guild.get_member(claimant_id) if claimant_id > 0 else None

        expires_at = _parse_iso(st.get("claim_expires_at"))
        remaining_text = ""
        if expires_at is not None:
            seconds_left = max(0, int((expires_at - _utcnow()).total_seconds()))
            if seconds_left > 0:
                remaining_text = f" ({_fmt_duration_seconds(seconds_left)} left)"

        cooldown_expires = _as_int(_as_dict(st.get("cooldowns")).get(str(member.id), 0), 0)
        cooldown_left = max(0, cooldown_expires - int(_utcnow().timestamp()))

        if member.id == target_id:
            if claimant is not None:
                return [
                    f"- Bounty: **targeted**. {member.display_name} is today's target and **{claimant.display_name}** has the active claim{remaining_text}."
                ], False
            return [f"- Bounty: **targeted**. {member.display_name} is today's bounty target."], False

        if claimant_id == member.id:
            return [f"- Bounty: **claiming** **{target_name}**{remaining_text}."], False
        if cooldown_left > 0:
            return [
                f"- Bounty: **cooldown**. Target is **{target_name}**; claim cooldown **{_fmt_duration_seconds(cooldown_left)}** left."
            ], False
        if claimant is not None:
            return [
                f"- Bounty: **busy**. Target is **{target_name}**; **{claimant.display_name}** is already holding the claim{remaining_text}."
            ], False
        return [f"- Bounty: **available**. Target is **{target_name}**."], False

    async def _build_lines(
        self,
        ctx: commands.Context,
        member: discord.Member,
    ) -> tuple[list[str], bool]:
        today = _date_key(_today_local())
        dirty = False
        await self._ensure_external_daily_state(ctx.guild, member)

        lines = [
            f"**Checklist for {member.display_name}**",
            f"Date: **{today}**",
            "",
            "**Daily Cycle**",
        ]

        for builder in (
            self._wordle_lines,
            self._anagram_lines,
            self._thanks_lines,
        ):
            new_lines, changed = builder(ctx.guild, member, today)
            lines.extend(new_lines)
            dirty = dirty or changed

        for builder in (
            self._spin_lines,
            self._fishing_lines,
            self._lotto_lines,
            self._blackjack_lines,
        ):
            new_lines, changed = builder(ctx.guild, member)
            lines.extend(new_lines)
            dirty = dirty or changed

        surprise_lines, changed = self._surprise_lines(ctx.guild)
        lines.extend(surprise_lines)
        dirty = dirty or changed

        lines.extend(["", "**Objectives**"])
        contract_lines, changed = self._contract_lines(ctx, member)
        lines.extend(contract_lines)
        dirty = dirty or changed

        bounty_lines, changed = self._bounty_lines(ctx, member)
        lines.extend(bounty_lines)
        dirty = dirty or changed
        return lines, dirty

    @commands.command(name="checklist", aliases=["check"], usage="[@user]")
    async def checklist(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        target = target or ctx.author
        if target.bot:
            await ctx.reply("Bots do not have a daily checklist.")
            return
        if target.id != ctx.author.id and not _can_view_others(ctx):
            await ctx.reply("You can only inspect another user's checklist if you're an admin/owner.")
            return

        lines, dirty = await self._build_lines(ctx, target)
        if dirty:
            await save_data()
        await self._send_lines(ctx, lines)
