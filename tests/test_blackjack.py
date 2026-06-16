import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from paragon.blackjack import BlackjackCog, SUITS, _table
from paragon.storage import _udict, data, load_data


class DummyMessage:
    def __init__(self, message_id: int, content: str = ""):
        self.id = message_id
        self.content = content
        self.add_reaction = AsyncMock()
        self.clear_reactions = AsyncMock()
        self.edit = AsyncMock()


class DummyChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self._next_id = 1000
        self.sent_text = []

    async def send(self, content: str):
        self.sent_text.append(content)
        self._next_id += 1
        return DummyMessage(self._next_id, content)


class DummyMember:
    def __init__(self, guild, uid: int, name: str):
        self.guild = guild
        self.id = uid
        self.display_name = name
        self.bot = False


class DummyGuild:
    def __init__(self, gid: int, *, members=None, channels=None):
        self.id = gid
        self._members = {member.id: member for member in (members or [])}
        self._channels = {channel.id: channel for channel in (channels or [])}

    def get_member(self, uid: int):
        return self._members.get(uid)

    async def fetch_member(self, uid: int):
        return self.get_member(uid)

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)


class BlackjackRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        load_data()

    def _clear_guild(self, guild_id: int) -> None:
        data.setdefault("guilds", {}).pop(str(guild_id), None)

    async def test_join_table_rolls_back_state_when_join_crashes(self):
        guild_id = 9101
        user_id = 9102
        self._clear_guild(guild_id)

        guild = DummyGuild(guild_id)
        member = DummyMember(guild, user_id, "Alice")
        guild._members[member.id] = member
        channel = DummyChannel(9103)
        bot = SimpleNamespace(user=SimpleNamespace(id=9999), get_guild=lambda _gid: guild)
        cog = BlackjackCog(bot)

        user = _udict(guild_id, user_id)
        user["xp"] = 500
        user["xp_f"] = 500.0

        def fail_touch(_player):
            raise RuntimeError("touch failed")

        cog._touch_player = fail_touch

        with patch("paragon.blackjack.save_data", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "touch failed"):
                await cog._join_table(guild, channel, member, bet_amount=125)

        refreshed = _udict(guild_id, user_id)
        self.assertEqual(refreshed["xp"], 500)
        self.assertEqual(refreshed["xp_f"], 500.0)
        self.assertNotIn(str(user_id), _table(guild_id).get("players", {}))

    async def test_begin_deal_core_reaches_acting_phase_after_bet(self):
        guild_id = 9201
        user_id = 9202
        self._clear_guild(guild_id)

        channel = DummyChannel(9203)
        guild = DummyGuild(guild_id, channels=[channel])
        member = DummyMember(guild, user_id, "Alice")
        guild._members[member.id] = member
        bot = SimpleNamespace(user=SimpleNamespace(id=9999), get_guild=lambda _gid: guild)
        cog = BlackjackCog(bot)

        user = _udict(guild_id, user_id)
        user["xp"] = 300
        user["xp_f"] = 300.0

        with (
            patch("paragon.blackjack.save_data", new=AsyncMock()),
            patch("paragon.blackjack.consume_blackjack_natural_charge", return_value=False),
        ):
            joined = await cog._join_table(guild, channel, member, bet_amount=100)
            self.assertTrue(joined)

            st = _table(guild_id)
            st["shoe"] = [f"2{SUITS[0]}"] * 60
            st["shoe"][-4:] = [
                f"8{SUITS[0]}",
                f"7{SUITS[1]}",
                f"9{SUITS[2]}",
                f"10{SUITS[3]}",
            ]

            await cog._begin_deal_core(guild, channel)

        st = _table(guild_id)
        player = st["players"][str(user_id)]
        self.assertEqual(st["phase"], "acting")
        self.assertEqual(player["status"], "acting")
        self.assertEqual(len(player["hand"]), 2)
        self.assertGreater(int(st.get("turn_started_ts", 0)), 0)
        self.assertGreater(int(st.get("action_msg_id", 0)), 0)
        self.assertTrue(any("Turn order:" in msg for msg in channel.sent_text))

    async def test_resume_guild_reprompts_current_actor_after_reenable(self):
        guild_id = 9301
        user_id = 9302
        self._clear_guild(guild_id)

        channel = DummyChannel(9303)
        guild = DummyGuild(guild_id, channels=[channel])
        member = DummyMember(guild, user_id, "Alice")
        guild._members[member.id] = member
        bot = SimpleNamespace(user=SimpleNamespace(id=9999), get_guild=lambda _gid: guild)
        cog = BlackjackCog(bot)

        st = _table(guild_id)
        st["channel_id"] = channel.id
        st["phase"] = "acting"
        st["action_msg_id"] = 4444
        st["turn_idx"] = 0
        st["turn_started_ts"] = 1
        st["dealing_lock"] = True
        player = cog._player(guild_id, user_id)
        player["in_table"] = True
        player["status"] = "acting"
        player["bet"] = 50
        player["locked"] = 50
        player["hand"] = [f"10{SUITS[0]}", f"7{SUITS[1]}"]

        cog._prompt_turn_with_reactions = AsyncMock()

        with (
            patch("paragon.blackjack.save_data", new=AsyncMock()),
            patch("paragon.blackjack.now_ts", return_value=500),
        ):
            await cog.resume_guild(guild_id)

        self.assertFalse(bool(st.get("dealing_lock")))
        self.assertEqual(int(st.get("turn_started_ts", 0)), 500)
        self.assertEqual(int(st.get("action_msg_id", 0)), 0)
        cog._prompt_turn_with_reactions.assert_awaited_once_with(guild, channel)
