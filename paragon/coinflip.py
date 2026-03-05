from typing import Optional, Dict
import random
import time
import discord
from discord.ext import commands

from .config import CF_MAX_BET, CF_TTL_SECONDS
from .ownership import is_control_user_id
from .storage import _udict
from .stats_store import record_game_fields
from .xp import apply_xp_change, grant_reward_boost
from .roles import enforce_level6_exclusive

coinflip_challenges: Dict[int, dict] = {}
coinflip_lock = None  # set in setup


def _get_user_xp_int(member: discord.Member) -> int:
    u = _udict(member.guild.id, member.id)
    return int(u.get("xp_f", u.get("xp", 0)))


def _coinflip_cap_enabled() -> bool:
    return int(CF_MAX_BET) >= 0


def _coinflip_cap_label() -> str:
    return str(CF_MAX_BET) if _coinflip_cap_enabled() else "unlimited"


class CoinFlipCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        global coinflip_lock
        import asyncio

        coinflip_lock = asyncio.Lock()

    @commands.command(name="cf", aliases=["coinflip"])
    async def cf(self, ctx, action_or_amount: Optional[str] = None, maybe_user: Optional[discord.Member] = None):
        chan_id = ctx.channel.id
        p = ctx.clean_prefix

        if action_or_amount is None:
            await ctx.reply(
                f"Usage:\n`{p}cf <amount>` to challenge (max {_coinflip_cap_label()})\n"
                f"`{p}cf accept [@challenger]`\n`{p}cf cancel`"
            )
            return

        action = action_or_amount.strip().lower()

        # Cancel
        if action == "cancel":
            async with coinflip_lock:
                pending = coinflip_challenges.get(chan_id)
                if not pending:
                    await ctx.reply("No pending coin flip in this channel.")
                    return
                if ctx.author.id != pending["challenger_id"] and not is_control_user_id(ctx.guild, ctx.author.id):
                    await ctx.reply("Only the challenger (or the bot owner) can cancel this coin flip.")
                    return
                coinflip_challenges.pop(chan_id, None)
            await ctx.reply("Coin flip canceled.")
            return

        # Accept
        if action == "accept":
            async with coinflip_lock:
                pending = coinflip_challenges.get(chan_id)
                if not pending:
                    await ctx.reply("No pending coin flip in this channel.")
                    return
                if time.monotonic() - pending["created"] > CF_TTL_SECONDS:
                    coinflip_challenges.pop(chan_id, None)
                    await ctx.reply("That coin flip expired. Ask the challenger to start a new one.")
                    return

                challenger_id = pending["challenger_id"]
                if maybe_user is not None and maybe_user.id != challenger_id:
                    await ctx.reply("That mention doesn't match the pending challenger in this channel.")
                    return
                if ctx.author.id == challenger_id:
                    await ctx.reply("You can't accept your own challenge.")
                    return

                challenger = ctx.guild.get_member(challenger_id)
                acceptor = ctx.author
                if not challenger:
                    coinflip_challenges.pop(chan_id, None)
                    await ctx.reply("Challenger left the server. Coin flip canceled.")
                    return

                amount = int(pending["amount"])
                chal_xp = _get_user_xp_int(challenger)
                acc_xp = _get_user_xp_int(acceptor)

                if chal_xp < amount:
                    coinflip_challenges.pop(chan_id, None)
                    await ctx.reply(f"{challenger.display_name} no longer has {amount} XP. Coin flip canceled.")
                    return
                if acc_xp < amount:
                    coinflip_challenges.pop(chan_id, None)
                    await ctx.reply(f"You don't have {amount} XP to accept this coin flip.")
                    return

                coinflip_challenges.pop(chan_id, None)

            pot = amount * 2
            await apply_xp_change(challenger, -amount, source="coinflip ante")
            await apply_xp_change(acceptor, -amount, source="coinflip ante")

            winner = random.choice([challenger, acceptor])
            loser = acceptor if winner is challenger else challenger
            boost = await grant_reward_boost(winner, pot, source="coinflip win")

            record_game_fields(
                ctx.guild.id,
                challenger.id,
                "coinflip",
                matches_played=1,
                xp_wagered_total=amount,
            )
            record_game_fields(
                ctx.guild.id,
                acceptor.id,
                "coinflip",
                matches_played=1,
                xp_wagered_total=amount,
            )
            record_game_fields(
                ctx.guild.id,
                winner.id,
                "coinflip",
                wins=1,
                boost_seed_xp_total=pot,
                boost_percent_total=boost["percent"],
                boost_minutes_total=boost["minutes"],
            )
            record_game_fields(ctx.guild.id, loser.id, "coinflip", losses=1)

            await enforce_level6_exclusive(ctx.guild)
            await ctx.reply(
                f"Coin Flip! {challenger.mention} vs {acceptor.mention} - Bet **{amount} XP** each (pot seed **{pot}**)\n"
                f"**Winner:** {winner.mention} earned **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**\n"
                f"**Loser:** {loser.mention} lost **{amount} XP**"
            )
            return

        # Create
        try:
            amount = int(action)
        except ValueError:
            await ctx.reply(f"Usage: `{p}cf <amount>` (number) or `{p}cf accept` / `{p}cf cancel`.")
            return

        if amount <= 0:
            await ctx.reply("Bet must be a positive number.")
            return
        if _coinflip_cap_enabled() and amount > CF_MAX_BET:
            await ctx.reply(f"Max bet is **{CF_MAX_BET} XP**.")
            return

        cur_xp = _get_user_xp_int(ctx.author)
        if cur_xp < amount:
            await ctx.reply(f"You don't have {amount} XP to bet.")
            return

        async with coinflip_lock:
            pending = coinflip_challenges.get(chan_id)
            if pending and time.monotonic() - pending["created"] <= CF_TTL_SECONDS:
                await ctx.reply(f"There's already a pending coin flip in this channel. Use `{p}cf accept` or `{p}cf cancel`.")
                return
            coinflip_challenges[chan_id] = {
                "challenger_id": ctx.author.id,
                "amount": amount,
                "created": time.monotonic(),
            }
            record_game_fields(ctx.guild.id, ctx.author.id, "coinflip", challenges_created=1)

        await ctx.reply(
            f"Coin Flip created! {ctx.author.mention} is betting **{amount} XP**.\n"
            f"Type `{p}cf accept` (or `{p}cf accept @{ctx.author.display_name}`) to take the bet. "
            f"(Expires in {CF_TTL_SECONDS // 60} minute(s))"
        )
