from typing import Optional, Dict
import random
import time
import discord
from discord.ext import commands

from .config import CF_MAX_BET, CF_POT_BOOST_MULTIPLIER, CF_TTL_SECONDS
from .emojis import EMOJI_COIN
from .guild_state import effective_unix_ts, is_guild_enabled
from .ownership import is_control_user_id
from .spin_support import consume_coinflip_win_edge
from .storage import _udict
from .stats_store import record_game_fields
from .xp import apply_xp_change, grant_bonus_xp_equivalent_boost
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

    async def _accept_challenge(
        self,
        guild: discord.Guild,
        channel,
        acceptor: discord.Member,
        *,
        reply,
        expected_challenger_id: Optional[int] = None,
        expected_message_id: Optional[int] = None,
    ) -> bool:
        chan_id = channel.id
        async with coinflip_lock:
            pending = coinflip_challenges.get(chan_id)
            if expected_message_id is not None and (
                not pending or int(pending.get("message_id", 0)) != expected_message_id
            ):
                return False
            if not pending:
                await reply("No pending coin flip in this channel.")
                return False
            if float(effective_unix_ts(guild.id)) - float(pending["created_ts"]) > CF_TTL_SECONDS:
                coinflip_challenges.pop(chan_id, None)
                await reply("That coin flip expired. Ask the challenger to start a new one.")
                return False

            challenger_id = pending["challenger_id"]
            if expected_challenger_id is not None and expected_challenger_id != challenger_id:
                await reply("That mention doesn't match the pending challenger in this channel.")
                return False
            if acceptor.id == challenger_id:
                await reply("You can't accept your own challenge.")
                return False

            challenger = guild.get_member(challenger_id)
            if not challenger:
                coinflip_challenges.pop(chan_id, None)
                await reply("Challenger left the server. Coin flip canceled.")
                return False

            amount = int(pending["amount"])
            chal_xp = _get_user_xp_int(challenger)
            acc_xp = _get_user_xp_int(acceptor)

            if chal_xp < amount:
                coinflip_challenges.pop(chan_id, None)
                await reply(f"{challenger.display_name} no longer has {amount} XP. Coin flip canceled.")
                return False
            if acc_xp < amount:
                coinflip_challenges.pop(chan_id, None)
                await reply(f"You don't have {amount} XP to accept this coin flip.")
                return False

            coinflip_challenges.pop(chan_id, None)

        pot = amount * 2
        await apply_xp_change(challenger, -amount, source="coinflip ante")
        await apply_xp_change(acceptor, -amount, source="coinflip ante")

        chal_edge = float(consume_coinflip_win_edge(guild.id, challenger.id))
        acc_edge = float(consume_coinflip_win_edge(guild.id, acceptor.id))
        challenger_weight = max(0.01, 1.0 + chal_edge)
        acceptor_weight = max(0.01, 1.0 + acc_edge)
        winner = random.choices(
            [challenger, acceptor],
            weights=[challenger_weight, acceptor_weight],
            k=1,
        )[0]
        loser = acceptor if winner is challenger else challenger
        target_bonus_xp = float(pot) * float(CF_POT_BOOST_MULTIPLIER)
        boost = await grant_bonus_xp_equivalent_boost(
            winner,
            target_bonus_xp,
            source="coinflip win",
            reward_seed_xp=target_bonus_xp,
        )

        record_game_fields(
            guild.id,
            challenger.id,
            "coinflip",
            matches_played=1,
            xp_wagered_total=amount,
        )
        record_game_fields(
            guild.id,
            acceptor.id,
            "coinflip",
            matches_played=1,
            xp_wagered_total=amount,
        )
        record_game_fields(
            guild.id,
            winner.id,
            "coinflip",
            wins=1,
            boost_seed_xp_total=target_bonus_xp,
            boost_percent_total=boost["percent"],
            boost_minutes_total=boost["minutes"],
        )
        record_game_fields(guild.id, loser.id, "coinflip", losses=1)

        await enforce_level6_exclusive(guild)
        edge_note = ""
        if chal_edge > 0.0 or acc_edge > 0.0:
            edge_note = (
                f"\nWheel edges: {challenger.display_name} +{chal_edge * 100.0:.1f}% | "
                f"{acceptor.display_name} +{acc_edge * 100.0:.1f}%"
            )
        await reply(
            f"Coin Flip! {challenger.mention} vs {acceptor.mention} - Bet **{amount} XP** each (pot **{pot} XP**)\n"
            f"**Winner:** {winner.mention} earned **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m** "
            f"(worth about **{boost['equivalent_bonus_xp']:.0f} XP**, target **{target_bonus_xp:.0f} XP** = {CF_POT_BOOST_MULTIPLIER:g}x pot)\n"
            f"**Loser:** {loser.mention} lost **{amount} XP**"
            f"{edge_note}"
        )
        contracts_cog = self.bot.get_cog("ContractsCog")
        if contracts_cog is not None:
            await contracts_cog.maybe_auto_complete_contract_for_member(
                guild,
                challenger,
                channel=channel,
            )
            await contracts_cog.maybe_auto_complete_contract_for_member(
                guild,
                acceptor,
                channel=channel,
            )
        return True

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
            await self._accept_challenge(
                ctx.guild,
                ctx.channel,
                ctx.author,
                reply=ctx.reply,
                expected_challenger_id=maybe_user.id if maybe_user is not None else None,
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
            if pending and (float(effective_unix_ts(ctx.guild.id)) - float(pending["created_ts"]) <= CF_TTL_SECONDS):
                await ctx.reply(f"There's already a pending coin flip in this channel. Use `{p}cf accept` or `{p}cf cancel`.")
                return
            challenge = {
                "challenger_id": ctx.author.id,
                "amount": amount,
                "created_ts": float(effective_unix_ts(ctx.guild.id)),
                "message_id": 0,
            }
            coinflip_challenges[chan_id] = challenge
            record_game_fields(ctx.guild.id, ctx.author.id, "coinflip", challenges_created=1)

        challenge_message = await ctx.reply(
            f"Coin Flip created! {ctx.author.mention} is betting **{amount} XP**.\n"
            f"React with {EMOJI_COIN} or type `{p}cf accept` "
            f"(or `{p}cf accept @{ctx.author.display_name}`) to take the bet. "
            f"(Expires in {CF_TTL_SECONDS // 60} minute(s))"
        )

        async with coinflip_lock:
            if coinflip_challenges.get(chan_id) is not challenge:
                return
            challenge["message_id"] = challenge_message.id

        try:
            await challenge_message.add_reaction(EMOJI_COIN)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji).replace("\ufe0f", "") != EMOJI_COIN.replace("\ufe0f", ""):
            return

        guild = self.bot.get_guild(payload.guild_id or 0)
        if guild is None or not is_guild_enabled(guild):
            return
        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            return

        payload_member = getattr(payload, "member", None)
        member = payload_member if isinstance(payload_member, discord.Member) else guild.get_member(payload.user_id)
        if member is None or member.bot:
            return

        await self._accept_challenge(
            guild,
            channel,
            member,
            reply=channel.send,
            expected_message_id=payload.message_id,
        )
