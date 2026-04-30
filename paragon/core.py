# paragon/core.py
# core v2
from datetime import datetime, timezone
from typing import Optional
import discord
from discord.ext import commands, tasks

from .config import resolve_afk_channel_id
from .emojis import EMOJI_BULLET, EMOJI_FIRST_PLACE_MEDAL, EMOJI_SECOND_PLACE_MEDAL, EMOJI_THIRD_PLACE_MEDAL
from .guild_setup import ensure_guild_setup
from .stats_store import record_game_fields
from .storage import load_data, _gdict, _udict, save_data
from .time_windows import _date_key, _today_local
from .xp import apply_delta, get_gain_state, grant_fixed_boost, grant_fixed_debuff
from .roles import enforce_level6_exclusive
from .ownership import owner_only, is_control_user_id

HELP_DESCRIPTIONS = {
    "help": "Show member-facing commands and usage.",
    "adminhelp": "Show admin-only commands and usage.",
    "re": "Quick bot responsiveness check.",
    "rank": "Show total XP, current gain rate, and active boosts.",
    "leaderboard": "Show top users by total XP.",
    "boosts": "Show boosts for a user, or manage boosts (admin): add/remove with signed rate+time; clear by target only.",
    "wordle": "Play Wordle (daily progression/guess command).",
    "resetwordle": "Admin: reset the current Wordle session.",
    "cf": "Start, accept, or cancel coinflip wagers.",
    "roulette": "Roulette duel with 20% base odds, 2.5% per prestige gap, and symmetric hit/backfire timeouts centered at 60s.",
    "claim": "Claim the active surprise drop.",
    "claimnow": "Admin: spawn a surprise drop immediately.",
    "quest": "Show your daily contract or inspect another user's contract progress.",
    "boss": "Show the current raid boss, affix, phase, timers, and where the fight is happening.",
    "attack": "Attack the current raid boss from inside its boss channel.",
    "resurrect": "Revive a downed raider in the boss channel so they can rejoin the fight.",
    "guard": "Brace for boss mechanics and protect yourself from the next big hit.",
    "interrupt": "Counter a telegraphed boss cast from inside the boss channel.",
    "purge": "Clear blight from a raider or answer a purge mechanic in the boss channel.",
    "focus": "Buff a raider's next attack with extra hit chance and damage.",
    "anagram": "Play the anagram phrase challenge.",
    "thanks": "Send a thanks reward to another user.",
    "lotto": "Buy lottery tickets or check pot and ticket counts.",
    "poplatto": "Admin: force an immediate lottery draw.",
    "lottotime": "Admin: view or set daily lottery draw time (ET).",
    "lottotoggle": "Admin: enable or disable the lottery.",
    "spin": "Spin your daily wheel, or use `all` to spend every available daily and bonus spin instantly.",
    "spinstatus": "Show your daily spin status and active wheel buffs.",
    "cleanse": "Use a stored Cleanse item to remove your active debuffs.",
    "drain": "Use a stored Drain item to debuff everyone else in your voice call and boost yourself.",
    "shop": "List the current XP shop items, including your prestige-scaled Wheel Spin price.",
    "buy": "Buy a shop item by index or name.",
    "fish": "Fish from the dock channel with bait, reel in bites, and keep casting until you stop.",
    "spawnboss": "Admin: force-spawn a raid boss immediately for testing or events.",
    "clearboss": "Admin: immediately remove the current raid boss without rewards, penalties, or boss stats.",
    "spintime": "Admin: view or set daily spin reset time (ET).",
    "spinrewards": "Admin: list and toggle spin rewards by key.",
    "spinreset": "Admin: reset current-cycle spin usage for a user.",
    "prestige": "Show the prestige board or spend XP to increase a prestige tier; add all to prestige as many times as possible.",
    "setp": "Admin: set a user's prestige tier.",
    "blackjack": "Join/leave the blackjack table, deal hands, and play until you lose.",
    "bjreset": "Admin: reset blackjack table state and refunds.",
    "bjtime": "Admin: view or set daily blackjack reset time (ET).",
    "bjcooldown": "Admin: enable/disable blackjack daily lockout cooldown.",
    "bjdebug": "Admin: toggle blackjack debug logging.",
    "bjstate": "Admin: print internal blackjack state.",
    "bjintents": "Admin: show Discord intent flags.",
    "join": "Join your current voice channel.",
    "leave": "Disconnect the bot from voice.",
    "voicehealth": "Admin: run voice system health checks.",
    "tts": "Show TTS style/emotion/non-verbal tag examples.",
    "ttscooldown": "Admin: toggle or view !say cooldown (per-user per-server).",
    "ttsmodel": "Admin: view or set the Eleven TTS model for this server.",
    "ttsqueue": "Admin: show, skip current, or clear the per-server TTS queue.",
    "wakeup": "Move an AFK user through random voice channels and into yours, then return them to AFK if silent.",
    "say": "Join a mentioned user's voice channel, speak a TTS message, then leave.",
    "rerollvoice": "Reroll your TTS voice profile. Admins can pass @user to reroll someone else.",
    "setvoice": "Set your TTS voice by Eleven voice ID with optional profile settings.",
    "shh": "Server mute a mentioned user for 30 seconds. 30 minute personal cooldown.",
    "gamestats": "Show per-user game stats and XP ledger.",
    "guildgamestats": "Admin: show aggregated server game stats.",
    "role": "Admin: toggle a Discord role on a member.",
    "xprate": "Admin: show passive XP/min rates.",
    "setxp": "Admin: set total XP for users or roles.",
    "adjust": "Admin: add or subtract XP from a user.",
    "softreset": "Admin: reset a user's current XP, prestige, and wheel rewards without deleting stats.",
    "fishreroll": "Owner: reroll the current fishing water state immediately.",
}

def _settings(gid: int) -> dict:
    g = _gdict(gid)
    st = g.get("settings")
    if st is None:
        st = {"inactive_loss_enabled": True}
        g["settings"] = st
    elif "inactive_loss_enabled" not in st:
        st["inactive_loss_enabled"] = True
    return st


def is_in_countable_vc(channel: Optional[discord.VoiceChannel]) -> bool:
    if channel is None: 
        return False
    afk_id = resolve_afk_channel_id(getattr(channel, "guild", None))
    if afk_id and channel.id == afk_id:
        return False
    return True

def should_apply_inactive_loss(member: discord.Member) -> bool:
    """
    Inactive loss conditions for XP v2:
    - Not in any VC                       -> loss
    - In AFK channel                      -> loss
    - In a VC but muted/deafened (self or server) -> loss
    """
    v = member.voice
    if not v or not v.channel:
        return True  # not in any call
    afk_id = resolve_afk_channel_id(member.guild)
    if afk_id and v.channel.id == afk_id:
        return True
    if v.mute or v.deaf or v.self_mute or v.self_deaf:
        return True
    return False  # fully active in a normal VC

def is_inactive_state(vstate: discord.VoiceState) -> bool:
    """
    'Inactive' means: user is in a countable VC but muted/deafened in any way.
    Outside VC (or in AFK VC) = not inactive for loss purposes.
    """
    if not is_in_countable_vc(vstate.channel): return False
    return bool(vstate.mute or vstate.deaf or vstate.self_mute or vstate.self_deaf)


PING_PONG_TARGET = 10
NEIGHBORHOOD_WATCH_TARGET = 5
REGULAR_CUSTOMER_TARGET = 5
WINGMAN_TARGET_MINUTES = 45
PARTY_BUS_MIN_HUMANS = 3
TOUCH_GRASS_MIN_SECONDS = 30 * 60


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _utcnow_ts() -> int:
    return int(discord.utils.utcnow().timestamp())


def _parse_iso_ts(value) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _activity_state(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    st = u.get("activity_quests")
    if not isinstance(st, dict):
        st = {}
        u["activity_quests"] = st

    today = _date_key(_today_local())
    if str(st.get("date", "")) != today:
        last_activity_ts = _as_int(st.get("last_activity_ts", 0))
        last_text_channel_id = _as_int(st.get("last_text_channel_id", 0))
        touch_grass_completed_seen_at = str(st.get("touch_grass_completed_seen_at", ""))
        st.clear()
        st["date"] = today
        st["last_activity_ts"] = last_activity_ts
        st["last_text_channel_id"] = last_text_channel_id
        st["touch_grass_completed_seen_at"] = touch_grass_completed_seen_at

    st.setdefault("date", today)
    st.setdefault("last_activity_ts", 0)
    st.setdefault("last_text_channel_id", 0)
    st.setdefault("touch_grass_completed_seen_at", "")
    st["mentions_by_target"] = _as_dict(st.get("mentions_by_target"))
    st["ping_pong_awarded_targets"] = [str(v) for v in _as_list(st.get("ping_pong_awarded_targets"))]
    st["neighborhood_watch_awarded"] = bool(st.get("neighborhood_watch_awarded", False))
    st["commands_used"] = [str(v).strip().lower() for v in _as_list(st.get("commands_used")) if str(v).strip()]
    st["regular_customer_awarded"] = bool(st.get("regular_customer_awarded", False))
    st["wingman_by_target"] = _as_dict(st.get("wingman_by_target"))
    st["wingman_awarded_targets"] = [str(v) for v in _as_list(st.get("wingman_awarded_targets"))]
    return st

class CoreCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _last_contract_channel(self, guild: discord.Guild, uid: int):
        state = _activity_state(guild.id, uid)
        channel_id = _as_int(state.get("last_text_channel_id", 0))
        if channel_id <= 0:
            return None
        getter = getattr(guild, "get_channel_or_thread", None)
        if callable(getter):
            return getter(channel_id)
        return guild.get_channel(channel_id)

    async def _maybe_auto_complete_contract(self, member: discord.Member, *, channel=None) -> bool:
        if member.bot or member.guild is None:
            return False
        contracts_cog = self.bot.get_cog("ContractsCog")
        if contracts_cog is None or not hasattr(contracts_cog, "maybe_auto_complete_contract_for_member"):
            return False
        try:
            return await contracts_cog.maybe_auto_complete_contract_for_member(
                member.guild,
                member,
                channel=channel,
            )
        except Exception:
            return False

    def _record_touch_grass_progress(
        self,
        member: discord.Member,
        *,
        state: dict,
        previous_activity_ts: int,
        now_ts: int,
    ) -> bool:
        u = _udict(member.guild.id, member.id)
        contract_state = _as_dict(u.get("contracts"))
        if bool(contract_state.get("claimed", False)):
            return False

        quest = _as_dict(contract_state.get("quest"))
        if str(quest.get("key", "")).strip().lower() != "touch_grass":
            return False

        seen_at = str(contract_state.get("seen_at", "")).strip()
        seen_ts = _parse_iso_ts(seen_at)
        if seen_ts <= 0:
            return False
        if str(state.get("touch_grass_completed_seen_at", "")) == seen_at:
            return False

        quiet_start_ts = max(seen_ts, max(0, previous_activity_ts))
        if (now_ts - quiet_start_ts) < TOUCH_GRASS_MIN_SECONDS:
            return False

        state["touch_grass_completed_seen_at"] = seen_at
        record_game_fields(member.guild.id, member.id, "social", touch_grass_returns=1)
        return True

    async def _register_activity(self, member: discord.Member, *, channel=None) -> bool:
        state = _activity_state(member.guild.id, member.id)
        previous_activity_ts = _as_int(state.get("last_activity_ts", 0))
        now_ts = _utcnow_ts()
        changed = False

        if channel is not None:
            channel_id = _as_int(getattr(channel, "id", 0))
            if channel_id > 0 and channel_id != _as_int(state.get("last_text_channel_id", 0)):
                state["last_text_channel_id"] = channel_id
                changed = True

        if self._record_touch_grass_progress(
            member,
            state=state,
            previous_activity_ts=previous_activity_ts,
            now_ts=now_ts,
        ):
            changed = True

        if now_ts != previous_activity_ts:
            state["last_activity_ts"] = now_ts
            changed = True

        return changed

    async def _looks_like_command(self, message: discord.Message) -> bool:
        content = str(message.content or "")
        if not content:
            return False
        try:
            prefixes = await self.bot.get_prefix(message)
        except Exception:
            return False
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        return any(content.startswith(str(prefix)) for prefix in prefixes if str(prefix))

    def _record_social_message(self, message: discord.Message) -> bool:
        if message.guild is None:
            return False

        member = message.author
        if not isinstance(member, discord.Member) or member.bot:
            return False

        record_game_fields(message.guild.id, member.id, "social", messages_sent=1)
        state = _activity_state(message.guild.id, member.id)
        changed = True

        mentions = {
            int(target.id)
            for target in message.mentions
            if isinstance(target, discord.Member) and not target.bot and target.id != member.id
        }
        if not mentions:
            return changed

        record_game_fields(
            message.guild.id,
            member.id,
            "social",
            mention_messages=1,
            mentions_total=len(mentions),
        )

        counts = _as_dict(state.get("mentions_by_target"))
        state["mentions_by_target"] = counts
        awarded = {str(v) for v in _as_list(state.get("ping_pong_awarded_targets"))}
        newly_awarded = 0

        for target_id in mentions:
            key = str(target_id)
            counts[key] = _as_int(counts.get(key, 0)) + 1
            if _as_int(counts.get(key, 0)) >= PING_PONG_TARGET and key not in awarded:
                awarded.add(key)
                newly_awarded += 1

        state["ping_pong_awarded_targets"] = sorted(awarded)
        if newly_awarded > 0:
            record_game_fields(message.guild.id, member.id, "social", ping_pong_targets=newly_awarded)

        if (
            not bool(state.get("neighborhood_watch_awarded", False))
            and len(counts) >= NEIGHBORHOOD_WATCH_TARGET
        ):
            state["neighborhood_watch_awarded"] = True
            record_game_fields(message.guild.id, member.id, "social", neighborhood_watch_days=1)

        return changed

    def _record_command_usage(self, ctx: commands.Context) -> bool:
        if ctx.guild is None or ctx.author.bot or ctx.command is None:
            return False

        cmd_name = str(getattr(ctx.command, "qualified_name", "") or getattr(ctx.command, "name", "")).strip().lower()
        if not cmd_name:
            return False

        state = _activity_state(ctx.guild.id, ctx.author.id)
        used = {name for name in _as_list(state.get("commands_used")) if str(name).strip()}
        changed = False

        if cmd_name not in used:
            used.add(cmd_name)
            state["commands_used"] = sorted(used)
            changed = True

            if (
                not bool(state.get("regular_customer_awarded", False))
                and len(used) >= REGULAR_CUSTOMER_TARGET
            ):
                state["regular_customer_awarded"] = True
                record_game_fields(ctx.guild.id, ctx.author.id, "social", regular_customer_days=1)

        if cmd_name != "quest":
            record_game_fields(ctx.guild.id, ctx.author.id, "social", non_quest_commands_used=1)
            changed = True

        return changed

    def _record_voice_presence_minute(self, member: discord.Member) -> bool:
        voice_state = getattr(member, "voice", None)
        channel = getattr(voice_state, "channel", None)
        if not is_in_countable_vc(channel):
            return False

        record_game_fields(member.guild.id, member.id, "voice", minutes_in_call=1)
        state = _activity_state(member.guild.id, member.id)
        state["last_activity_ts"] = _utcnow_ts()
        human_members = [m for m in channel.members if isinstance(m, discord.Member) and not m.bot]
        if len(human_members) >= PARTY_BUS_MIN_HUMANS:
            record_game_fields(member.guild.id, member.id, "voice", party_bus_minutes=1)

        counts = _as_dict(state.get("wingman_by_target"))
        state["wingman_by_target"] = counts
        awarded = {str(v) for v in _as_list(state.get("wingman_awarded_targets"))}
        newly_awarded = 0

        for other in human_members:
            if other.id == member.id:
                continue
            key = str(other.id)
            counts[key] = _as_int(counts.get(key, 0)) + 1
            if _as_int(counts.get(key, 0)) >= WINGMAN_TARGET_MINUTES and key not in awarded:
                awarded.add(key)
                newly_awarded += 1

        state["wingman_awarded_targets"] = sorted(awarded)
        if newly_awarded > 0:
            record_game_fields(member.guild.id, member.id, "voice", wingman_targets=newly_awarded)

        return True

    def _can_manage_boosts(self, ctx: commands.Context) -> bool:
        if is_control_user_id(ctx.guild, ctx.author.id):
            return True
        perms = getattr(ctx.author, "guild_permissions", None)
        return bool(perms and perms.administrator)

    def _resolve_boost_targets(self, ctx: commands.Context) -> list[discord.Member]:
        members: set[discord.Member] = set(ctx.message.mentions)
        for role in ctx.message.role_mentions:
            members.update(role.members)
        if "@everyone" in (ctx.message.content or ""):
            members.update(ctx.guild.members)
        return sorted([m for m in members if not m.bot], key=lambda m: m.id)

    def _parse_signed_rate(self, token: str) -> tuple[str, float]:
        t = (token or "").strip()
        if len(t) < 2 or t[0] not in "+-":
            raise ValueError("Rate must be signed, e.g. +25 or -20.")
        sign = t[0]
        amount = float(t[1:])
        if amount <= 0:
            raise ValueError("Rate amount must be greater than 0.")
        if sign == "+" and amount > 500:
            raise ValueError("Positive rate is too high. Use up to 500.")
        if sign == "-" and amount > 95:
            raise ValueError("Negative rate is too high. Use up to 95.")
        return sign, amount

    def _is_admin_command(self, cmd: commands.Command) -> bool:
        if cmd.name in {"ttscooldown", "ttsmodel", "ttsqueue"}:
            return True
        if cmd.cog_name == "AdminCog":
            return True
        for check in getattr(cmd, "checks", []):
            module = getattr(check, "__module__", "")
            if module.endswith(".ownership"):
                return True
        return False

    def _command_description(self, cmd: commands.Command) -> str:
        mapped = HELP_DESCRIPTIONS.get(cmd.name)
        if mapped:
            return mapped
        short = (cmd.short_doc or "").strip()
        if short:
            return short
        return "No description set."

    def _format_help_entry(self, ctx: commands.Context, cmd: commands.Command, *, admin_view: bool = False) -> str:
        if cmd.name == "boosts":
            if admin_view:
                usage = (
                    f"{ctx.clean_prefix}boosts add {{+/-}}{{rate}} {{time}} {{@user|@role|@everyone}} | "
                    f"{ctx.clean_prefix}boosts remove {{+/-}}{{rate}} {{time}} {{@user|@role|@everyone}} | "
                    f"{ctx.clean_prefix}boosts clear {{@user|@role|@everyone}}"
                )
                summary = "Admin: manage boosts/debuffs for users, roles, or everyone."
            else:
                usage = f"{ctx.clean_prefix}boosts [@user]"
                summary = "Show active boosts/debuffs for you or a mentioned user."
        else:
            usage = f"{ctx.clean_prefix}{cmd.name}"
            if cmd.signature:
                usage = f"{usage} {cmd.signature}"
            summary = self._command_description(cmd)

        if cmd.aliases:
            aliases = ", ".join(f"{ctx.clean_prefix}{a}" for a in cmd.aliases)
            return f"`{usage}` - {summary} (aliases: {aliases})"
        return f"`{usage}` - {summary}"

    def _sorted_visible_commands(self) -> list[commands.Command]:
        cmds = [c for c in self.bot.commands if not c.hidden]
        cmds.sort(key=lambda c: c.name.lower())
        return cmds

    async def _send_help_chunks(self, ctx: commands.Context, lines: list[str]):
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for line in lines:
            add_len = len(line) + 1
            if current and (current_len + add_len) > 1900:
                chunks.append("\n".join(current))
                current = [line]
                current_len = add_len
            else:
                current.append(line)
                current_len += add_len

        if current:
            chunks.append("\n".join(current))

        for i, chunk in enumerate(chunks):
            if i == 0:
                await ctx.reply(chunk)
            else:
                await ctx.send(chunk)

    @commands.command(name="help")
    async def help_command(self, ctx: commands.Context):
        cmds = self._sorted_visible_commands()
        normal_cmds = [c for c in cmds if (not self._is_admin_command(c)) or c.name == "adminhelp"]

        lines = [
            "**Paragon Command Help**",
            f"Admin tools are listed separately with `{ctx.clean_prefix}adminhelp`.",
            "",
            "**Commands**",
        ]
        if normal_cmds:
            for cmd in normal_cmds:
                lines.append(f"- {self._format_help_entry(ctx, cmd, admin_view=False)}")
        else:
            lines.append("- No member commands found.")

        await self._send_help_chunks(ctx, lines)

    @commands.command(name="adminhelp")
    @owner_only()
    async def admin_help_command(self, ctx: commands.Context):
        cmds = self._sorted_visible_commands()
        admin_cmds = [c for c in cmds if self._is_admin_command(c) or c.name == "boosts"]

        lines = ["**Paragon Admin Command Help**", ""]
        if admin_cmds:
            for cmd in admin_cmds:
                lines.append(f"- {self._format_help_entry(ctx, cmd, admin_view=True)}")
        else:
            lines.append("- No admin commands found.")

        await self._send_help_chunks(ctx, lines)

    @commands.Cog.listener()
    async def on_ready(self):
        load_data()
        changed = 0
        for guild in self.bot.guilds:
            if await ensure_guild_setup(guild):
                changed += 1
        print(f"Logged in as {self.bot.user} (ID: {self.bot.user.id})")
        if changed:
            print(f"Synced guild setup for {changed} guild(s).")
        if not self.award_loop.is_running():
            self.award_loop.start()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await ensure_guild_setup(guild)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        if before.owner_id != after.owner_id:
            await ensure_guild_setup(after)

    @tasks.loop(minutes=1)
    async def award_loop(self):
        # One passive gain tick per minute for every non-bot member.
        for guild in self.bot.guilds:
            _gdict(guild.id)
            for member in guild.members:
                if member.bot:
                    continue
                voice_metrics_changed = self._record_voice_presence_minute(member)
                await apply_delta(member, minutes=1, inactive_minutes=0, source="voice minute")
                if voice_metrics_changed:
                    await self._maybe_auto_complete_contract(
                        member,
                        channel=self._last_contract_channel(guild, member.id),
                    )
            # Repurposed function now syncs Gold/Silver/Bronze podium roles.
            await enforce_level6_exclusive(guild)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        member = message.author if isinstance(message.author, discord.Member) else message.guild.get_member(message.author.id)
        if member is None or member.bot:
            return

        changed = await self._register_activity(member, channel=message.channel)
        if not await self._looks_like_command(message):
            changed = self._record_social_message(message) or changed

        if not changed:
            return

        await save_data()
        await self._maybe_auto_complete_contract(member, channel=message.channel)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        if ctx.guild is None or ctx.author.bot:
            return

        changed = await self._register_activity(ctx.author, channel=ctx.channel)
        changed = self._record_command_usage(ctx) or changed
        if changed:
            await save_data()
        await self._maybe_auto_complete_contract(ctx.author, channel=ctx.channel)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot or member.guild is None:
            return
        if before.channel == after.channel and before.self_mute == after.self_mute and before.self_deaf == after.self_deaf and before.mute == after.mute and before.deaf == after.deaf:
            return

        changed = await self._register_activity(member)
        if not changed:
            return

        await save_data()
        await self._maybe_auto_complete_contract(
            member,
            channel=self._last_contract_channel(member.guild, member.id),
        )


    # Simple ping & public commands
    @commands.command(name="re")
    async def ping(self, ctx):
        await ctx.reply("tard!")

    @commands.command(name="rank", aliases=["xp", "level"])
    async def rank(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        u = _udict(ctx.guild.id, member.id)
        total = int(u.get("xp_f", u.get("xp", 0)))
        st = await get_gain_state(member)
        boosts = st.get("boosts", [])
        debuffs = st.get("debuffs", [])
        if boosts:
            first = boosts[0]
            boost_extra = f"Active boosts: **{len(boosts)}** (next expires in {first['minutes_left']}m)"
        else:
            boost_extra = "Active boosts: **0**"
        if debuffs:
            first_d = debuffs[0]
            debuff_extra = f"Active debuffs: **{len(debuffs)}** (next expires in {first_d['minutes_left']}m)"
        else:
            debuff_extra = "Active debuffs: **0**"
        extra = f" | {boost_extra} | {debuff_extra}"
        await ctx.reply(
            f"**{member.display_name}** Total XP: **{total}**"
            f" | Rate: **{st['rate_per_min']:.2f} XP/min** (x{st['multiplier']:.2f})"
            f"{extra}"
        )

    @commands.command(name="leaderboard", aliases=["lb", "xps"])
    async def leaderboard(self, ctx, limit: Optional[int] = 10):
        limit = max(1, min(25, int(limit or 10)))
        g = _gdict(ctx.guild.id); users = g.get("users", {})
        rows = []
        for uid_str, u in users.items():
            uid = int(uid_str)
            total = int(u.get("xp_f", u.get("xp", 0)))
            rows.append((uid, total))
        rows.sort(key=lambda t: (-t[1], t[0]))
        rows = rows[:limit]
        if not rows:
            await ctx.reply("No data yet."); return

        lines = []
        for i, (uid, total) in enumerate(rows, start=1):
            m = ctx.guild.get_member(uid)
            name = m.display_name if m else f"User {uid}"
            medal = (
                EMOJI_FIRST_PLACE_MEDAL
                if i == 1
                else (
                    EMOJI_SECOND_PLACE_MEDAL
                    if i == 2
                    else (EMOJI_THIRD_PLACE_MEDAL if i == 3 else EMOJI_BULLET)
                )
            )
            lines.append(f"`{i:>2}.` {medal} **{name}** - {total} XP")
        await ctx.reply("\n".join(lines))

    async def _send_boost_view(self, ctx: commands.Context, member: discord.Member):
        st = await get_gain_state(member)
        lines = [
            f"**{member.display_name}** gain rate: **{st['rate_per_min']:.2f} XP/min** (base {st['base_per_min']:.2f}, x{st['multiplier']:.2f})"
        ]
        boosts = st.get("boosts", [])
        debuffs = st.get("debuffs", [])
        if not boosts:
            lines.append("No active boosts.")
        else:
            lines.append("Active boosts:")
            for b in boosts[:8]:
                lines.append(f"- **+{b['percent']:.1f}%** for **{b['minutes_left']}m** ({b['source']})")
            if len(boosts) > 8:
                lines.append(f"- ...and {len(boosts) - 8} more")
        if not debuffs:
            lines.append("No active debuffs.")
        else:
            lines.append("Active debuffs:")
            for d in debuffs[:8]:
                lines.append(f"- **-{d['percent']:.1f}%** for **{d['minutes_left']}m** ({d['source']})")
            if len(debuffs) > 8:
                lines.append(f"- ...and {len(debuffs) - 8} more")
        await ctx.reply("\n".join(lines))

    @commands.command(name="boosts", aliases=["rate", "mult"])
    async def boosts(self, ctx: commands.Context, *args: str):
        if not args:
            await self._send_boost_view(ctx, ctx.author)
            return

        action = str(args[0]).strip().lower()
        if action not in {"add", "remove", "clear"}:
            try:
                member = await commands.MemberConverter().convert(ctx, args[0])
            except commands.BadArgument:
                p = ctx.clean_prefix
                await ctx.reply(
                    f"Usage: `{p}boosts @user` | `{p}boosts add {{+/-}}{{rate}} {{time}} {{target}}` | "
                    f"`{p}boosts remove {{+/-}}{{rate}} {{time}} {{target}}` | `{p}boosts clear {{target}}`"
                )
                return
            await self._send_boost_view(ctx, member)
            return

        if not self._can_manage_boosts(ctx):
            await ctx.reply("You don't have permission to manage boosts.")
            return

        if action == "clear":
            targets = self._resolve_boost_targets(ctx)
            if not targets:
                await ctx.reply(f"Usage: `{ctx.clean_prefix}boosts clear {{@user|@role|@everyone}}`")
                return

            touched = 0
            removed_pos_total = 0
            removed_neg_total = 0
            for m in targets:
                u = _udict(ctx.guild.id, m.id)
                pos = u.get("xp_boosts")
                neg = u.get("xp_debuffs")
                removed_pos = len(pos) if isinstance(pos, list) else 0
                removed_neg = len(neg) if isinstance(neg, list) else 0
                if removed_pos or removed_neg:
                    touched += 1
                removed_pos_total += removed_pos
                removed_neg_total += removed_neg
                u["xp_boosts"] = []
                u["xp_debuffs"] = []
            await save_data()
            await ctx.reply(
                f"Cleared all boosts for **{touched}** member(s). Removed **{removed_pos_total}** positive and **{removed_neg_total}** negative entries."
            )
            return

        if len(args) < 3:
            p = ctx.clean_prefix
            await ctx.reply(
                f"Usage: `{p}boosts add {{+/-}}{{rate}} {{time}} {{target}}` or "
                f"`{p}boosts remove {{+/-}}{{rate}} {{time}} {{target}}`"
            )
            return

        try:
            sign, amount = self._parse_signed_rate(args[1])
        except ValueError as e:
            await ctx.reply(str(e))
            return

        try:
            minutes = int(args[2])
        except ValueError:
            await ctx.reply("Time must be an integer number of minutes.")
            return

        if minutes < 1 or minutes > 1440:
            await ctx.reply("Time must be between 1 and 1440 minutes for add/remove.")
            return

        targets = self._resolve_boost_targets(ctx)
        if not targets:
            await ctx.reply("Mention target(s): `@user`, `@role`, or `@everyone`.")
            return

        if action == "add":
            pct = amount / 100.0
            source = f"admin boosts {sign} add by {ctx.author.id}"
            applied = 0
            failed = 0
            for m in targets:
                try:
                    if sign == "+":
                        await grant_fixed_boost(m, pct=pct, minutes=minutes, source=source, persist=False)
                    else:
                        await grant_fixed_debuff(m, pct=pct, minutes=minutes, source=source, persist=False)
                    applied += 1
                except Exception:
                    failed += 1
            await save_data()
            label = "+" if sign == "+" else "-"
            msg = f"Applied **{label}{amount:g}%** for **{minutes}m** to **{applied}** member(s)."
            if failed:
                msg += f" Failed: **{failed}**."
            await ctx.reply(msg)
            return

        now = int(discord.utils.utcnow().timestamp())
        key = "xp_boosts" if sign == "+" else "xp_debuffs"
        target_pct = amount / 100.0
        tolerance_pct = 0.0005
        tolerance_minutes = 2
        touched = 0
        removed_entries = 0
        for m in targets:
            u = _udict(ctx.guild.id, m.id)
            raw = u.get(key)
            if not isinstance(raw, list):
                raw = []
            kept = []
            removed_for_member = 0
            for b in raw:
                if not isinstance(b, dict):
                    continue
                try:
                    pct = float(b.get("pct", 0.0))
                    until = int(b.get("until", 0))
                except Exception:
                    continue
                if until <= now:
                    continue
                mins_left = max(0, int((until - now + 59) // 60))
                pct_match = abs(pct - target_pct) <= tolerance_pct
                mins_match = abs(mins_left - minutes) <= tolerance_minutes
                if action == "remove" and pct_match and mins_match:
                    removed_for_member += 1
                    continue
                kept.append(b)

            if removed_for_member > 0:
                touched += 1
                removed_entries += removed_for_member
            u[key] = kept

        await save_data()
        op_label = "Removed"
        type_label = "positive" if sign == "+" else "negative"
        await ctx.reply(f"{op_label} **{removed_entries}** {type_label} boost entries across **{touched}** member(s).")

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        from discord.ext.commands import (
            CommandNotFound, MissingPermissions, CheckFailure,
            MissingRequiredArgument, BadArgument, CommandOnCooldown, DisabledCommand
        )
        orig = getattr(error, "original", error)
        try:
            if isinstance(orig, CommandNotFound):
                await ctx.reply(f"Unknown command. Try `{ctx.clean_prefix}help`."); return
            if isinstance(orig, CheckFailure):
                await ctx.reply("You don't have permission to use that command."); return
            if isinstance(orig, MissingPermissions):
                await ctx.reply("You're missing required Discord permissions."); return
            if isinstance(orig, MissingRequiredArgument):
                await ctx.reply(f"Missing argument(s). Try `{ctx.clean_prefix}help` or check the usage."); return
            if isinstance(orig, BadArgument):
                await ctx.reply("Bad argument. Please check your input."); return
            if isinstance(orig, DisabledCommand):
                await ctx.reply("That command is currently disabled."); return
            if isinstance(orig, CommandOnCooldown):
                await ctx.reply(f"Slow down-try again in {orig.retry_after:.1f}s."); return
            await ctx.reply("Something went wrong running that command.")
        except Exception:
            pass
        print(f"[Command Error] {type(orig).__name__}: {orig}")
