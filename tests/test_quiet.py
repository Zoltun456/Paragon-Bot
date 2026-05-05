import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from paragon.quiet import COMMAND_PREFIX, QuietCog, _now_ts


def _voice_state(*, channel, mute):
    return SimpleNamespace(channel=channel, mute=mute)


def _member(*, guild_id: int, user_id: int, channel, mute: bool):
    guild = SimpleNamespace(id=guild_id)
    member = SimpleNamespace(
        id=user_id,
        bot=False,
        guild=guild,
        voice=_voice_state(channel=channel, mute=mute),
        edit=AsyncMock(),
    )
    guild.get_member = lambda candidate_id: member if candidate_id == user_id else None
    return member, guild


class QuietCogTests(unittest.IsolatedAsyncioTestCase):
    async def test_finish_mute_keeps_expired_tracking_while_member_is_out_of_voice(self):
        member, guild = _member(guild_id=1, user_id=2, channel=None, mute=True)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == guild.id else None)
        cog = QuietCog(bot)
        key = cog._key(guild.id, member.id)
        cog._active_mutes[key] = _now_ts() - 1

        await cog._finish_mute(guild.id, member.id)

        self.assertIn(key, cog._active_mutes)
        member.edit.assert_not_awaited()

    async def test_voice_state_update_unmutes_pending_expired_member_on_rejoin(self):
        channel = object()
        member, guild = _member(guild_id=10, user_id=20, channel=channel, mute=True)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == guild.id else None)
        cog = QuietCog(bot)
        key = cog._key(guild.id, member.id)
        pending_task = Mock()
        cog._active_mutes[key] = _now_ts() - 1
        cog._unmute_tasks[key] = pending_task

        before = _voice_state(channel=None, mute=True)
        after = _voice_state(channel=channel, mute=True)
        await cog.on_voice_state_update(member, before, after)

        member.edit.assert_awaited_once_with(mute=False, reason=f"{COMMAND_PREFIX}shh expired")
        pending_task.cancel.assert_called_once_with()
        self.assertNotIn(key, cog._active_mutes)
        self.assertNotIn(key, cog._unmute_tasks)

    async def test_voice_state_update_clears_stale_expired_marker_when_member_rejoins_unmuted(self):
        channel = object()
        member, guild = _member(guild_id=100, user_id=200, channel=channel, mute=False)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == guild.id else None)
        cog = QuietCog(bot)
        key = cog._key(guild.id, member.id)
        cog._active_mutes[key] = _now_ts() - 1

        before = _voice_state(channel=None, mute=False)
        after = _voice_state(channel=channel, mute=False)
        await cog.on_voice_state_update(member, before, after)

        member.edit.assert_not_awaited()
        self.assertNotIn(key, cog._active_mutes)
