from typing import Union

import discord
from discord.ext import commands

from .config import BASE_XP_PER_MINUTE
from .ownership import owner_only
from .roles import announce_level_up, enforce_level6_exclusive, sync_level_roles
from .stats_store import record_xp_change
from .storage import _udict, save_data
from .xp import (
    _compute_level_from_total_xp,
    apply_xp_change,
    get_gain_state,
)


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


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
