import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from paragon.coinflip import CoinFlipCog, coinflip_challenges
from paragon.emojis import EMOJI_COIN
from paragon.storage import _udict, data, load_data


class DummyMessage:
    def __init__(self, message_id: int):
        self.id = message_id
        self.add_reaction = AsyncMock()


class DummyChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self.send = AsyncMock()


class DummyMember:
    def __init__(self, guild, user_id: int, name: str):
        self.guild = guild
        self.id = user_id
        self.display_name = name
        self.mention = f"<@{user_id}>"
        self.bot = False


class DummyGuild:
    def __init__(self, guild_id: int, channel: DummyChannel):
        self.id = guild_id
        self.channel = channel
        self.members = {}

    def get_channel(self, channel_id: int):
        return self.channel if channel_id == self.channel.id else None

    def get_member(self, user_id: int):
        return self.members.get(user_id)


class CoinFlipReactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        load_data()
        coinflip_challenges.clear()

    def tearDown(self):
        coinflip_challenges.clear()

    async def test_create_adds_coin_reaction_and_tracks_message(self):
        guild = DummyGuild(9601, DummyChannel(9602))
        challenger = DummyMember(guild, 9603, "Alice")
        guild.members[challenger.id] = challenger
        data.setdefault("guilds", {}).pop(str(guild.id), None)
        user = _udict(guild.id, challenger.id)
        user["xp"] = 100
        user["xp_f"] = 100.0

        challenge_message = DummyMessage(9604)
        ctx = SimpleNamespace(
            guild=guild,
            channel=guild.channel,
            author=challenger,
            clean_prefix="!",
            reply=AsyncMock(return_value=challenge_message),
        )
        bot = SimpleNamespace(user=SimpleNamespace(id=9999), get_cog=lambda _name: None)
        cog = CoinFlipCog(bot)

        await CoinFlipCog.cf.callback(cog, ctx, "25")

        challenge_message.add_reaction.assert_awaited_once_with(EMOJI_COIN)
        self.assertIn(f"React with {EMOJI_COIN}", ctx.reply.await_args.args[0])
        self.assertEqual(coinflip_challenges[guild.channel.id]["message_id"], challenge_message.id)

    async def test_coin_reaction_accepts_matching_challenge(self):
        channel = DummyChannel(9702)
        guild = DummyGuild(9701, channel)
        challenger = DummyMember(guild, 9703, "Alice")
        acceptor = DummyMember(guild, 9704, "Bob")
        guild.members = {challenger.id: challenger, acceptor.id: acceptor}
        data.setdefault("guilds", {}).pop(str(guild.id), None)
        for member in (challenger, acceptor):
            user = _udict(guild.id, member.id)
            user["xp"] = 100
            user["xp_f"] = 100.0

        message_id = 9705
        coinflip_challenges[channel.id] = {
            "challenger_id": challenger.id,
            "amount": 25,
            "created_ts": 1000.0,
            "message_id": message_id,
        }
        bot = SimpleNamespace(
            user=SimpleNamespace(id=9999),
            get_guild=lambda guild_id: guild if guild_id == guild.id else None,
            get_cog=lambda _name: None,
        )
        cog = CoinFlipCog(bot)
        payload = SimpleNamespace(
            user_id=acceptor.id,
            guild_id=guild.id,
            channel_id=channel.id,
            message_id=message_id,
            emoji=EMOJI_COIN,
            member=acceptor,
        )
        boost = {
            "percent": 10.0,
            "minutes": 5,
            "equivalent_bonus_xp": 50.0,
        }

        with (
            patch("paragon.coinflip.is_guild_enabled", return_value=True),
            patch("paragon.coinflip.effective_unix_ts", return_value=1001.0),
            patch("paragon.coinflip.apply_xp_change", new=AsyncMock()) as apply_xp,
            patch("paragon.coinflip.consume_coinflip_win_edge", return_value=0.0),
            patch("paragon.coinflip.random.choices", return_value=[challenger]),
            patch("paragon.coinflip.grant_bonus_xp_equivalent_boost", new=AsyncMock(return_value=boost)),
            patch("paragon.coinflip.record_game_fields"),
            patch("paragon.coinflip.enforce_level6_exclusive", new=AsyncMock()),
        ):
            await cog.on_raw_reaction_add(payload)

        self.assertNotIn(channel.id, coinflip_challenges)
        self.assertEqual(apply_xp.await_count, 2)
        channel.send.assert_awaited_once()
        self.assertIn("Coin Flip!", channel.send.await_args.args[0])

    async def test_coin_reaction_ignores_untracked_message(self):
        channel = DummyChannel(9802)
        guild = DummyGuild(9801, channel)
        member = DummyMember(guild, 9803, "Alice")
        guild.members[member.id] = member
        bot = SimpleNamespace(
            user=SimpleNamespace(id=9999),
            get_guild=lambda guild_id: guild if guild_id == guild.id else None,
        )
        cog = CoinFlipCog(bot)
        payload = SimpleNamespace(
            user_id=member.id,
            guild_id=guild.id,
            channel_id=channel.id,
            message_id=9804,
            emoji=EMOJI_COIN,
            member=member,
        )

        with patch("paragon.coinflip.is_guild_enabled", return_value=True):
            await cog.on_raw_reaction_add(payload)

        channel.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
