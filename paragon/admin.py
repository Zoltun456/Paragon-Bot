import inspect
import time
from typing import Optional, Union

import discord
from discord.ext import commands

from .config import BASE_XP_PER_MINUTE
from .guild_setup import ensure_guild_setup
from .guild_state import (
    current_disabled_elapsed_seconds,
    hide_managed_channels,
    is_guild_enabled,
    mark_guild_disabled,
    mark_guild_enabled,
    restore_managed_channels,
)
from .ownership import is_control_user_id, owner_only
from .roles import announce_level_up, enforce_level6_exclusive, sync_level_roles
from .stats_store import record_xp_change
from .storage import (
    _gdict,
    _udict,
    active_database_version,
    available_database_versions,
    create_database_version,
    database_version_user_count,
    has_database_version,
    next_database_version,
    save_data,
    set_active_database_version,
)
from .xp import (
    _compute_level_from_total_xp,
    apply_xp_change,
    get_gain_state,
    prestige_cost,
    prestige_state_from_spent_xp,
)

SOFTRESET_CONFIRM_TTL_SECONDS = 30
BOT_TOGGLE_CONFIRM_TTL_SECONDS = 30
DATABASE_CONFIRM_TTL_SECONDS = 30


# ---- Helpers ----
def _set_member_xp_fields(member: discord.Member, xp_amount: int, *, source: str = "admin setxp"):
        u = _udict(member.guild.id, member.id)
        old_level = int(u.get("level", 1))
        old_xp = float(u.get("xp_f", u.get("xp", 0)))

        u["xp_f"] = float(xp_amount)
        u["xp"] = int(xp_amount)
        u["level"] = _compute_level_from_total_xp(int(xp_amount))
        delta = float(xp_amount) - old_xp
        if delta != 0.0:
            record_xp_change(member.guild.id, member.id, delta, source=source)
        return old_level, u["level"]


def _clear_spin_effect_rows(u: dict, key: str) -> int:
        rows = u.get(key)
        if not isinstance(rows, list):
            u[key] = []
            return 0

        kept = []
        removed = 0
        for row in rows:
            src = ""
            if isinstance(row, dict):
                src = str(row.get("source", "")).strip().lower()
            if src == "wheel" or src.startswith("wheel "):
                removed += 1
                continue
            kept.append(row)

        u[key] = kept
        return removed


def _soft_reset_member_state(member: discord.Member) -> dict[str, int]:
        u = _udict(member.guild.id, member.id)

        removed_boosts = _clear_spin_effect_rows(u, "xp_boosts")
        removed_debuffs = _clear_spin_effect_rows(u, "xp_debuffs")

        bonus_spins_removed = 0
        spin_daily = u.get("spin_daily")
        if isinstance(spin_daily, dict):
            bonus_spins_removed = max(0, int(spin_daily.get("bonus_spins", 0)))
            spin_daily["bonus_spins"] = 0
            spin_daily["last_reward"] = ""

        had_wheel_buffs = 1 if isinstance(u.get("wheel_buffs"), dict) and u.get("wheel_buffs") else 0
        u.pop("wheel_buffs", None)

        old_xp = int(u.get("xp_f", u.get("xp", 0)))
        old_prestige = max(0, int(u.get("prestige", 0)))
        u["xp_f"] = 0.0
        u["xp"] = 0
        u["level"] = _compute_level_from_total_xp(0)
        u["prestige"] = 0

        return {
            "old_xp": old_xp,
            "old_prestige": old_prestige,
            "bonus_spins_removed": bonus_spins_removed,
            "wheel_buffs_cleared": had_wheel_buffs,
            "boosts_removed": removed_boosts,
            "debuffs_removed": removed_debuffs,
        }


def _historical_prestige_spent_xp(u: dict) -> int:
        stats = u.get("stats")
        if not isinstance(stats, dict):
            return 0
        xp = stats.get("xp")
        if not isinstance(xp, dict):
            return 0
        by_source = xp.get("by_source")
        if not isinstance(by_source, dict):
            return 0
        row = by_source.get("prestige cost")
        if not isinstance(row, dict):
            return 0
        try:
            spent = float(row.get("lost_total", 0.0))
        except Exception:
            spent = 0.0
        return max(0, int(round(spent)))


def _retro_prestige_state(u: dict) -> dict[str, int]:
        old_prestige = max(0, int(u.get("prestige", 0)))
        spent_total = _historical_prestige_spent_xp(u)
        new_prestige, spent_used, spent_remainder = prestige_state_from_spent_xp(spent_total)
        return {
            "old_prestige": old_prestige,
            "new_prestige": int(new_prestige),
            "spent_total": int(spent_total),
            "spent_used": int(spent_used),
            "spent_remainder": int(spent_remainder),
        }


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending_softresets: dict[tuple[int, int, int], float] = {}
        self._pending_bot_toggles: dict[tuple[int, int], tuple[bool, float]] = {}
        self._pending_database_actions: dict[tuple[int, int], tuple[str, int, float]] = {}

    async def _call_cog_hook(self, hook_name: str, guild_id: int) -> None:
        cogs = list(self.bot.cogs.values())
        if hook_name == "pause_guild":
            cogs.sort(key=lambda cog: 1 if cog.__class__.__name__ == "VoiceCog" else 0)
        for cog in cogs:
            hook = getattr(cog, hook_name, None)
            if hook is None or not callable(hook):
                continue
            try:
                maybe = hook(guild_id)
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception:
                continue

    async def _pause_guild_runtime(self, guild_id: int) -> None:
        await self._call_cog_hook("pause_guild", guild_id)

    async def _resume_guild_runtime(self, guild_id: int) -> None:
        await self._call_cog_hook("resume_guild", guild_id)

    def _can_toggle_bot(self, ctx: commands.Context) -> bool:
        perms = getattr(ctx.author, "guild_permissions", None)
        is_admin = bool(perms and (perms.administrator or perms.manage_guild))
        return bool(is_admin or is_control_user_id(ctx.guild, ctx.author.id))

    def _can_manage_database(self, ctx: commands.Context) -> bool:
        return self._can_toggle_bot(ctx)

    def _database_status_lines(self, guild_id: int) -> list[str]:
        active_id = active_database_version(guild_id)
        next_id = next_database_version(guild_id)
        ids = available_database_versions(guild_id)
        summary = ", ".join(
            f"`{version_id}` ({database_version_user_count(guild_id, version_id)} user(s))"
            for version_id in ids
        ) or "(none)"
        current_users = len(_gdict(guild_id).get("users", {}))
        return [
            f"Active database: **{active_id}** with **{current_users}** stored user(s).",
            f"Available databases: {summary}.",
            f"Next new database id: **{next_id}**.",
        ]

    def _confirm_database_action(
        self,
        ctx: commands.Context,
        *,
        action: str,
        target_id: int,
        now: float,
    ) -> bool:
        self._pending_database_actions = {
            key: value
            for key, value in self._pending_database_actions.items()
            if value[2] > now
        }
        pending_key = (int(ctx.guild.id), int(ctx.author.id))
        pending = self._pending_database_actions.get(pending_key)
        if pending is not None and pending[0] == action and int(pending[1]) == int(target_id) and pending[2] > now:
            self._pending_database_actions.pop(pending_key, None)
            return True

        self._pending_database_actions[pending_key] = (str(action), int(target_id), now + DATABASE_CONFIRM_TTL_SECONDS)
        return False

    async def _refresh_post_database_swap(self, guild: discord.Guild) -> None:
        try:
            await enforce_level6_exclusive(guild)
        except Exception:
            pass

    @commands.command(name="role")
    @owner_only()
    async def role(self, ctx: commands.Context, member: discord.Member, role: discord.Role):
        """
        Adds or removes a role from a member.
        Usage: !role @user @role
        - Toggles the role: adds if missing, removes if present.
        """

        try:
            # Prevent the bot from editing roles above its own
            if role >= ctx.guild.me.top_role:
                await ctx.reply("I can't manage that role because it is higher than my top role.")
                return

            # Toggle role
            if role in member.roles:
                await member.remove_roles(role, reason=f"Removed by {ctx.author}")
                await ctx.reply(f"Removed **{role.name}** from **{member.display_name}**.")
            else:
                await member.add_roles(role, reason=f"Added by {ctx.author}")
                await ctx.reply(f"Added **{role.name}** to **{member.display_name}**.")

        except discord.Forbidden:
            await ctx.reply("Missing permission to manage roles or edit that user.")
        except discord.HTTPException as e:
            await ctx.reply(f"Discord API error: {e}")
        except Exception as e:
            await ctx.reply(f"Unexpected error: {type(e).__name__}: {e}")

    @commands.command(name="xprate")
    @owner_only()
    ## @commands.has_permissions(administrator=True)
    async def xprate(self, ctx: commands.Context, member: discord.Member = None):
        """
        Show passive XP/min gain rate(s).
        - !xprate           -> list all current guild members' rates
        - !xprate @user     -> show that user's rate only
        Uses live XP engine math (prestige + active boosts + compression mode, if enabled).
        """

        async def rate_for(m: discord.Member) -> tuple[float, float, int, float, int]:
            u = _udict(ctx.guild.id, m.id)
            prestige = int(u.get("prestige", 0))
            st = await get_gain_state(m)
            base_rate = float(st.get("base_per_min", BASE_XP_PER_MINUTE))
            rate = float(st.get("rate_per_min", BASE_XP_PER_MINUTE))
            p_mult = float(st.get("prestige_multiplier", 1.0))
            boost_count = len(st.get("boosts", []))
            return base_rate, rate, prestige, p_mult, boost_count

        # Single user
        if member is not None:
            if member.bot:
                await ctx.reply("Bots do not earn XP.")
                return
            base_rate, rate, p, p_mult, n_boosts = await rate_for(member)
            await ctx.reply(
                f"**{member.display_name}** - Prestige **{p}** | Base **{base_rate:.3f} XP/min** | "
                f"Prestige x{p_mult:.3f} | Active boosts: **{n_boosts}** | **{rate:.3f} XP/min**"
            )
            return

        # All users (current guild members only, no bots)
        rows = []
        for m in ctx.guild.members:
            if m.bot:
                continue
            base_rate, rate, p, p_mult, n_boosts = await rate_for(m)
            rows.append((rate, base_rate, p, p_mult, n_boosts, m.display_name))

        if not rows:
            await ctx.reply("No human members found.")
            return

        rows.sort(key=lambda t: (-t[0], t[5].lower()))
        lines = ["Passive XP Rates (stepped base + prestige + boosts)"]
        for rate, base_rate, p, p_mult, n_boosts, name in rows:
            lines.append(
                f"- **{name}** - P{p} | base {base_rate:.3f} | prestige x{p_mult:.3f} | "
                f"boosts:{n_boosts} -> {rate:.3f} XP/min"
            )

        # Avoid overly long messages by chunking if needed
        msg = "\n".join(lines)
        if len(msg) > 1900:
            chunk = []
            for line in lines:
                if sum(len(x) + 1 for x in chunk) + len(line) > 1900:
                    await ctx.send("\n".join(chunk))
                    chunk = [line]
                else:
                    chunk.append(line)
            if chunk:
                await ctx.send("\n".join(chunk))
        else:
            await ctx.reply(msg)

    @commands.command(name="setxp")
    @owner_only()
    async def setxp(
        self,
        ctx: commands.Context,
        targets: commands.Greedy[Union[discord.Member, discord.Role]],
        xp_amount: int,
    ):
        """
        Set XP for one or more members. Supports user mentions and role mentions.
        Examples:
          !setxp @user 3000
          !setxp @Role 3000
          !setxp @everyone 3000
          !setxp @user1 @user2 1500
        """
        xp_amount = max(0, int(xp_amount))

        # Resolve targets -> concrete members
        members: set[discord.Member] = set()
        for t in targets:
            if isinstance(t, discord.Member):
                members.add(t)
            elif isinstance(t, discord.Role):
                # includes @everyone (guild.default_role)
                members.update(t.members)

        # If nothing parsed (e.g., user forgot a mention), default to self
        if not members:
            if isinstance(ctx.author, discord.Member):
                members.add(ctx.author)
            else:
                await ctx.reply(
                    f"No valid targets. Mention users or roles, e.g. `{ctx.clean_prefix}setxp @everyone 3000`."
                )
                return

        updated = 0
        leveled_up: list[tuple[discord.Member, int]] = []

        for m in members:
            old_level, new_level = _set_member_xp_fields(m, xp_amount)
            if new_level > old_level:
                leveled_up.append((m, new_level))
            updated += 1

        await save_data()

        for m, new_level in leveled_up:
            try:
                await announce_level_up(m, new_level)
            except Exception:
                pass
        for m in members:
            try:
                u = _udict(ctx.guild.id, m.id)
                await sync_level_roles(m, int(u.get("level", 1)))
            except Exception:
                pass

        await enforce_level6_exclusive(ctx.guild)
        await ctx.reply(f"Set XP for **{updated}** member(s) -> **{xp_amount}**.")

    @commands.command(name="adjust")
    @owner_only()
    async def adjust(self, ctx, member: discord.Member, delta: str):
        s = delta.strip().replace(",", "")
        if not (s.startswith("+") or s.startswith("-")):
            await ctx.reply("Please include a sign on the amount, e.g. `+100` or `-15`.")
            return
        try:
            delta_xp = int(s)
        except ValueError:
            await ctx.reply("That amount is not a valid number. Use `+100` or `-15`.")
            return
        if delta_xp == 0:
            await ctx.reply("No change (amount is 0).")
            return
        changed = await apply_xp_change(member, delta_xp, source="admin adjust")
        if changed:
            old, new = changed
            if new > old:
                await announce_level_up(member, new)
            await sync_level_roles(member, new)
        else:
            u = _udict(ctx.guild.id, member.id)
            await sync_level_roles(member, int(u.get("level", 1)))
        await enforce_level6_exclusive(ctx.guild)
        u = _udict(ctx.guild.id, member.id)
        total = int(u.get("xp_f", u.get("xp", 0)))
        sign = "+" if delta_xp > 0 else ""
        await ctx.reply(f"Adjusted {member.display_name} by **{sign}{delta_xp} XP**. New total: **{total} XP**.")

    @commands.command(name="retroprestige")
    @owner_only()
    async def retroprestige(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if member is not None and member.bot:
            await ctx.reply("Bots do not have prestige progression to remap.")
            return

        if member is not None:
            targets = [(int(member.id), member.display_name)]
        else:
            users = _gdict(ctx.guild.id).get("users", {})
            targets = []
            for uid_s in users:
                try:
                    uid = int(uid_s)
                except Exception:
                    continue
                live_member = ctx.guild.get_member(uid)
                display_name = live_member.display_name if live_member is not None else f"User {uid}"
                targets.append((uid, display_name))
            targets.sort(key=lambda row: row[1].lower())

        if not targets:
            await ctx.reply("No stored user progression was found for this server.")
            return

        changed: list[tuple[str, dict[str, int]]] = []
        unchanged = 0
        total_levels_down = 0
        total_levels_up = 0
        selected_result: dict[str, int] | None = None

        for uid, display_name in targets:
            u = _udict(ctx.guild.id, uid)
            result = _retro_prestige_state(u)
            if member is not None and uid == int(member.id):
                selected_result = dict(result)
            old_prestige = int(result["old_prestige"])
            new_prestige = int(result["new_prestige"])
            if new_prestige == old_prestige:
                unchanged += 1
                continue

            u["prestige"] = new_prestige
            if new_prestige < old_prestige:
                total_levels_down += old_prestige - new_prestige
            else:
                total_levels_up += new_prestige - old_prestige
            changed.append((display_name, result))

        if changed:
            await save_data()
            await enforce_level6_exclusive(ctx.guild)

        if member is not None:
            current_user = _udict(ctx.guild.id, member.id)
            result = selected_result or {
                "old_prestige": max(0, int(current_user.get("prestige", 0))),
                "new_prestige": max(0, int(current_user.get("prestige", 0))),
                "spent_total": _historical_prestige_spent_xp(current_user),
                "spent_used": 0,
                "spent_remainder": 0,
            }
            next_cost = prestige_cost(int(current_user.get("prestige", 0)))
            await ctx.reply(
                f"Retro prestige remap for **{member.display_name}** complete. "
                f"Prestige **{result['old_prestige']} -> {result['new_prestige']}** using "
                f"**{result['spent_total']}** logged prestige-spend XP. "
                f"Remainder under the new curve: **{result['spent_remainder']} XP** "
                f"(current XP left unchanged). Next prestige cost: **{next_cost} XP**."
            )
            return

        summary_lines = [
            f"Retro prestige remap complete for **{len(targets)}** stored user(s).",
            f"Changed: **{len(changed)}** | Unchanged: **{unchanged}** | "
            f"Levels down: **{total_levels_down}** | Levels up: **{total_levels_up}**.",
            "Uses only logged `prestige cost` spend. Manual `setp` changes and free prestige rewards are not counted.",
        ]
        if changed:
            preview = changed[:10]
            for display_name, result in preview:
                summary_lines.append(
                    f"- **{display_name}**: P{result['old_prestige']} -> P{result['new_prestige']} "
                    f"(spent {result['spent_total']}, remainder {result['spent_remainder']})"
                )
            if len(changed) > len(preview):
                summary_lines.append(f"- ...and **{len(changed) - len(preview)}** more changed user(s).")

        await ctx.reply("\n".join(summary_lines))

    @commands.command(name="softreset")
    @owner_only()
    async def softreset(self, ctx: commands.Context, member: discord.Member):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if member.bot:
            await ctx.reply("Bots do not have player progression to soft reset.")
            return

        now = time.monotonic()
        self._pending_softresets = {
            key: expiry for key, expiry in self._pending_softresets.items() if expiry > now
        }
        pending_key = (int(ctx.guild.id), int(ctx.author.id), int(member.id))
        expires_at = self._pending_softresets.get(pending_key, 0.0)

        if expires_at <= now:
            self._pending_softresets[pending_key] = now + SOFTRESET_CONFIRM_TTL_SECONDS
            await ctx.reply(
                f"Soft reset is armed for **{member.display_name}**. "
                f"Send `{ctx.clean_prefix}softreset {member.mention}` again within "
                f"**{SOFTRESET_CONFIRM_TTL_SECONDS} seconds** to confirm."
            )
            return

        self._pending_softresets.pop(pending_key, None)
        result = _soft_reset_member_state(member)
        await save_data()

        try:
            await sync_level_roles(member, int(_udict(ctx.guild.id, member.id).get("level", 1)))
        except Exception:
            pass
        await enforce_level6_exclusive(ctx.guild)

        spin_lines = []
        if result["bonus_spins_removed"] > 0:
            spin_lines.append(f"bonus spins removed **{result['bonus_spins_removed']}**")
        if result["wheel_buffs_cleared"] > 0:
            spin_lines.append("wheel item/buff charges cleared")
        if result["boosts_removed"] > 0:
            spin_lines.append(f"wheel boosts removed **{result['boosts_removed']}**")
        if result["debuffs_removed"] > 0:
            spin_lines.append(f"wheel debuffs removed **{result['debuffs_removed']}**")
        spin_summary = ", ".join(spin_lines) if spin_lines else "no active wheel rewards were present"

        await ctx.reply(
            f"Soft reset applied to **{member.display_name}**. "
            f"XP **{result['old_xp']} -> 0**, prestige **{result['old_prestige']} -> 0**, "
            f"and {spin_summary}. Stats were left intact."
        )

    @commands.command(name="bottoggle", aliases=["togglebot"])
    async def bottoggle(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if not self._can_toggle_bot(ctx):
            await ctx.reply("You need administrator or Manage Server permission to toggle Paragon for this server.")
            return

        now = time.monotonic()
        self._pending_bot_toggles = {
            key: value
            for key, value in self._pending_bot_toggles.items()
            if value[1] > now
        }

        guild_id = int(ctx.guild.id)
        author_id = int(ctx.author.id)
        currently_enabled = is_guild_enabled(ctx.guild)
        target_enabled = not currently_enabled
        pending_key = (guild_id, author_id)
        pending = self._pending_bot_toggles.get(pending_key)

        if pending is None or bool(pending[0]) != bool(target_enabled) or pending[1] <= now:
            self._pending_bot_toggles[pending_key] = (bool(target_enabled), now + BOT_TOGGLE_CONFIRM_TTL_SECONDS)
            action = "disable" if currently_enabled else "re-enable"
            await ctx.reply(
                f"Paragon {action} is armed for **{ctx.guild.name}**. "
                f"Send `{ctx.clean_prefix}bottoggle` again within "
                f"**{BOT_TOGGLE_CONFIRM_TTL_SECONDS} seconds** to confirm."
            )
            return

        self._pending_bot_toggles.pop(pending_key, None)

        if currently_enabled:
            await ensure_guild_setup(ctx.guild)
            await self._pause_guild_runtime(guild_id)
            await mark_guild_disabled(ctx.guild)
            hidden_channels = await hide_managed_channels(ctx.guild)
            await ctx.reply(
                f"Paragon is now **DISABLED** for **{ctx.guild.name}**. "
                f"Managed channels hidden: **{hidden_channels}**. "
                f"All guild timers and daily cycles are now frozen until you re-enable with `{ctx.clean_prefix}bottoggle`."
            )
            return

        frozen_seconds = current_disabled_elapsed_seconds(ctx.guild)
        await mark_guild_enabled(ctx.guild)
        restored_channels = await restore_managed_channels(ctx.guild)
        await self._resume_guild_runtime(guild_id)
        await ctx.reply(
            f"Paragon is now **ENABLED** for **{ctx.guild.name}**. "
            f"Managed channels restored: **{restored_channels}**. "
            f"Frozen time resumed after **{frozen_seconds} seconds** offline."
        )

    @commands.command(name="database", aliases=["db"])
    async def database(self, ctx: commands.Context, action: Optional[str] = None, target: Optional[str] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if not self._can_manage_database(ctx):
            await ctx.reply("You need administrator or Manage Server permission to manage guild databases.")
            return

        token = str(action or "status").strip().lower()
        if token in {"", "status", "show", "list"}:
            lines = [
                f"**Database status for {ctx.guild.name}**",
                *self._database_status_lines(ctx.guild.id),
                (
                    f"Use `{ctx.clean_prefix}database new` for a fresh user/game database, or "
                    f"`{ctx.clean_prefix}database set <id>` to switch to an existing one."
                ),
            ]
            await ctx.reply("\n".join(lines))
            return

        now = time.monotonic()
        guild_id = int(ctx.guild.id)
        was_enabled = is_guild_enabled(ctx.guild)

        if token == "new":
            pending_id = next_database_version(guild_id)
            if not self._confirm_database_action(ctx, action="new", target_id=pending_id, now=now):
                await ctx.reply(
                    f"Database create is armed for **ID {pending_id}**. "
                    f"Send `{ctx.clean_prefix}database new` again within "
                    f"**{DATABASE_CONFIRM_TTL_SECONDS} seconds** to confirm.\n"
                    f"Shared server settings stay in place, and gameplay/user data starts fresh."
                )
                return

            if was_enabled:
                await self._pause_guild_runtime(guild_id)
            try:
                new_id = await create_database_version(guild_id)
            finally:
                if was_enabled:
                    await self._resume_guild_runtime(guild_id)

            await self._refresh_post_database_swap(ctx.guild)
            await ctx.reply(
                f"Database **{new_id}** is now active for **{ctx.guild.name}**. "
                f"Shared server settings were kept, and gameplay/user data started fresh.\n"
                f"Available databases: "
                + ", ".join(
                    f"`{version_id}` ({database_version_user_count(guild_id, version_id)} user(s))"
                    for version_id in available_database_versions(guild_id)
                )
                + "."
            )
            return

        if token == "set":
            if target is None:
                await ctx.reply(f"Usage: `{ctx.clean_prefix}database set <id>`")
                return
            try:
                target_id = int(str(target).strip())
            except ValueError:
                await ctx.reply(f"Database id must be an integer. Usage: `{ctx.clean_prefix}database set <id>`")
                return
            if target_id <= 0:
                await ctx.reply("Database id must be a positive integer.")
                return
            if not has_database_version(guild_id, target_id):
                await ctx.reply(
                    f"Database **{target_id}** does not exist. Available: "
                    + ", ".join(f"`{version_id}`" for version_id in available_database_versions(guild_id))
                    + "."
                )
                return
            if active_database_version(guild_id) == target_id:
                await ctx.reply(f"Database **{target_id}** is already active.")
                return

            if not self._confirm_database_action(ctx, action="set", target_id=target_id, now=now):
                await ctx.reply(
                    f"Database switch to **ID {target_id}** is armed. "
                    f"Send `{ctx.clean_prefix}database set {target_id}` again within "
                    f"**{DATABASE_CONFIRM_TTL_SECONDS} seconds** to confirm."
                )
                return

            if was_enabled:
                await self._pause_guild_runtime(guild_id)
            try:
                changed = await set_active_database_version(guild_id, target_id)
            finally:
                if was_enabled:
                    await self._resume_guild_runtime(guild_id)

            if not changed:
                await ctx.reply(f"Database **{target_id}** no longer exists.")
                return

            await self._refresh_post_database_swap(ctx.guild)
            await ctx.reply(
                f"Database **{target_id}** is now active for **{ctx.guild.name}**. "
                f"It currently has **{database_version_user_count(guild_id, target_id)}** stored user(s)."
            )
            return

        await ctx.reply(
            f"Usage: `{ctx.clean_prefix}database` | `{ctx.clean_prefix}database new` | "
            f"`{ctx.clean_prefix}database set <id>`"
        )
