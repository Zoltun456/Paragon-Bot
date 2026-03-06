# paragon/core.py
# core v2
from typing import Optional
import discord
from discord.ext import commands, tasks

from .config import resolve_afk_channel_id
from .guild_setup import ensure_guild_setup
from .storage import load_data, _gdict, _udict, save_data
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
    "roulette": "Prestige-based roulette duel (30m cooldown, timeout scales by prestige gap).",
    "claim": "Claim the active surprise drop.",
    "claimnow": "Admin: spawn a surprise drop immediately.",
    "anagram": "Play the anagram phrase challenge.",
    "thanks": "Send a thanks reward to another user.",
    "lotto": "Buy lottery tickets or check pot and ticket counts.",
    "poplatto": "Admin: force an immediate lottery draw.",
    "lottotime": "Admin: view or set daily lottery draw time (ET).",
    "lottotoggle": "Admin: enable or disable the lottery.",
    "spin": "Spin a once-per-reset daily wheel for random buffs and rewards.",
    "spinstatus": "Show your daily spin status and active wheel buffs.",
    "spintime": "Admin: view or set daily spin reset time (ET).",
    "spinrewards": "Admin: list and toggle spin rewards by key.",
    "spinreset": "Admin: reset current-cycle spin usage for a user.",
    "prestige": "Spend XP to increase your prestige tier.",
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
    "ttscooldown": "Admin: toggle or view !say cooldown (per-user per-server).",
    "ttsqueue": "Admin: show, skip current, or clear the per-server TTS queue.",
    "wakeup": "Move an AFK user through random voice channels and into yours, then return them to AFK if silent.",
    "say": "Join a mentioned user's voice channel, speak a TTS message, then leave.",
    "rerollvoice": "Reroll your TTS voice profile. Admins can pass @user to reroll someone else.",
    "setvoice": "Set your TTS voice by Eleven voice ID with optional profile settings.",
    "gamestats": "Show per-user game stats and XP ledger.",
    "guildgamestats": "Admin: show aggregated server game stats.",
    "role": "Admin: toggle a Discord role on a member.",
    "xprate": "Admin: show passive XP/min rates.",
    "setxp": "Admin: set total XP for users or roles.",
    "adjust": "Admin: add or subtract XP from a user.",
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

class CoreCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
        if cmd.name in {"ttscooldown", "ttsqueue"}:
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
                await apply_delta(member, minutes=1, inactive_minutes=0, source="voice minute")
            # Repurposed function now syncs Gold/Silver/Bronze podium roles.
            await enforce_level6_exclusive(guild)


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
            medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else "•"))
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
