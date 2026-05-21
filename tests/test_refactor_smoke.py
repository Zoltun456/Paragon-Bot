import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from discord.ext import commands

from Paragon import _global_check
from paragon.anagram import AnagramCog
from paragon.bounty import BountyCog, _bounty_state
from paragon.checklist import ChecklistCog
from paragon.lotto import LottoCog
from paragon.playback import PlaybackCog
from paragon.roulette import RouletteCog, _store_active_timeout
from paragon.spin import SpinCog, WHEEL_REWARDS
from paragon.storage import load_data
from paragon.surprise import SurpriseCog
from paragon.thanks import ThanksCog
from paragon.tts import TTSCog, _ActiveSayAudio
from paragon.voice import VoiceCog
from paragon.wordle import WordleCog


class DummyGuild:
    def __init__(self, gid: int, *, name: str = "Test Guild", voice_client=None, members=None):
        self.id = gid
        self.name = name
        self.voice_client = voice_client
        self.roles = []
        self.members = list(members or [])
        self.me = SimpleNamespace()

    def get_member(self, uid: int):
        for member in self.members:
            if member.id == uid:
                return member
        return None

    def get_channel(self, _channel_id: int):
        return None


class DummyMember:
    def __init__(self, guild: DummyGuild, uid: int, name: str, *, bot: bool = False):
        self.guild = guild
        self.id = uid
        self.display_name = name
        self.mention = f"<@{uid}>"
        self.bot = bot
        self.roles = []
        self.voice = None
        self.guild_permissions = SimpleNamespace(administrator=True, manage_guild=True)
        self.communication_disabled_until = None


class DummyVoiceClient:
    def __init__(self):
        self.stopped = False

    def is_playing(self) -> bool:
        return True

    def is_paused(self) -> bool:
        return False

    def stop(self) -> None:
        self.stopped = True

    def is_connected(self) -> bool:
        return True


class GlobalCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        load_data()

    async def test_global_check_blocks_non_bottoggle_when_disabled(self):
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=801),
            clean_prefix="?",
            command=SimpleNamespace(qualified_name="spin", name="spin"),
            reply=AsyncMock(),
        )

        with patch("Paragon.is_guild_enabled", return_value=False):
            with self.assertRaises(commands.CheckFailure):
                await _global_check(ctx)

        ctx.reply.assert_awaited_once()
        self.assertTrue(getattr(ctx, "_paragon_disabled_response_sent", False))

    async def test_global_check_allows_bottoggle_when_disabled(self):
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=802),
            clean_prefix="?",
            command=SimpleNamespace(qualified_name="bottoggle", name="bottoggle"),
            reply=AsyncMock(),
        )

        with patch("Paragon.is_guild_enabled", return_value=False):
            allowed = await _global_check(ctx)

        self.assertTrue(allowed)
        ctx.reply.assert_not_awaited()

    async def test_global_check_allows_playback_commands_when_disabled(self):
        for cmd_name in ("play", "playskip", "playclear"):
            with self.subTest(command=cmd_name):
                ctx = SimpleNamespace(
                    guild=SimpleNamespace(id=803),
                    clean_prefix="?",
                    command=SimpleNamespace(qualified_name=cmd_name, name=cmd_name),
                    reply=AsyncMock(),
                )

                with patch("Paragon.is_guild_enabled", return_value=False):
                    allowed = await _global_check(ctx)

                self.assertTrue(allowed)
                ctx.reply.assert_not_awaited()


class CommandSmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        load_data()

    async def test_spin_all_replies_after_bottoggle_refactor(self):
        guild = DummyGuild(811, name="Spin Test")
        member = DummyMember(guild, 812, "Alice")
        guild.members.append(member)
        ctx = SimpleNamespace(
            guild=guild,
            author=member,
            clean_prefix="?",
            reply=AsyncMock(),
            send=AsyncMock(),
            channel=SimpleNamespace(send=AsyncMock()),
        )
        cog = SpinCog(SimpleNamespace())
        reward_key = next(iter(WHEEL_REWARDS))

        async def fake_apply_reward(_ctx, _reward_key):
            return {
                "message": "reward applied",
                "flat_xp": 0,
                "prestige_gain": 0,
                "bonus_spins_gained": 0,
            }

        cog._apply_reward = fake_apply_reward
        cog._pick_reward = lambda _st: reward_key

        with patch("paragon.spin.save_data", new=AsyncMock()):
            await SpinCog.spin.callback(cog, ctx, "all")

        self.assertEqual(ctx.reply.await_count, 1)
        reply_text = ctx.reply.await_args.args[0]
        self.assertIn("Instant wheel sweep complete", reply_text)
        self.assertIn("Spent **1** spin(s)", reply_text)

    async def test_checklist_renders_after_time_refactor(self):
        guild = DummyGuild(821, name="Checklist Test")
        member = DummyMember(guild, 822, "Alice")
        guild.members.append(member)
        bot = SimpleNamespace(get_cog=lambda _name: None)
        ctx = SimpleNamespace(
            guild=guild,
            author=member,
            clean_prefix="?",
            reply=AsyncMock(),
            send=AsyncMock(),
            message=SimpleNamespace(mentions=[]),
        )
        cog = ChecklistCog(bot)

        with patch("paragon.checklist.save_data", new=AsyncMock()):
            await ChecklistCog.checklist.callback(cog, ctx)

        self.assertEqual(ctx.reply.await_count, 1)
        reply_text = ctx.reply.await_args.args[0]
        self.assertIn("Checklist for Alice", reply_text)
        self.assertIn("Wheel Spin:", reply_text)
        self.assertIn("Date:", reply_text)

    async def test_simple_daily_commands_still_reply(self):
        guild = DummyGuild(831, name="Daily Smoke")
        author = DummyMember(guild, 832, "Alice")
        other = DummyMember(guild, 833, "Bob")
        guild.members.extend([author, other])

        anagram_ctx = SimpleNamespace(guild=guild, author=author, clean_prefix="?", reply=AsyncMock())
        wordle_ctx = SimpleNamespace(guild=guild, author=author, clean_prefix="?", reply=AsyncMock())
        lotto_ctx = SimpleNamespace(
            guild=guild,
            author=author,
            clean_prefix="?",
            reply=AsyncMock(),
            message=SimpleNamespace(mentions=[]),
        )
        thanks_ctx = SimpleNamespace(guild=guild, author=author, clean_prefix="?", reply=AsyncMock())
        surprise_ctx = SimpleNamespace(guild=guild, author=author, clean_prefix="?", reply=AsyncMock())

        lotto_cog = LottoCog(SimpleNamespace(guilds=[]))
        surprise_bot = SimpleNamespace(guilds=[], get_cog=lambda _name: None)
        surprise_cog = SurpriseCog(surprise_bot)
        try:
            with (
                patch("paragon.anagram.save_data", new=AsyncMock()),
                patch("paragon.surprise.save_data", new=AsyncMock()),
                patch("paragon.surprise.get_log_channel", return_value=None),
            ):
                await AnagramCog.anagram.callback(AnagramCog(SimpleNamespace()), anagram_ctx)
                await WordleCog.wordle.callback(WordleCog(SimpleNamespace()), wordle_ctx)
                await LottoCog.lotto.callback(lotto_cog, lotto_ctx)
                await ThanksCog.thanks.callback(ThanksCog(SimpleNamespace()), thanks_ctx)
                await SurpriseCog.claimnow.callback(surprise_cog, surprise_ctx)
        finally:
            lotto_cog.lotto_draw_loop.cancel()
            surprise_cog.drop_loop.cancel()

        self.assertEqual(anagram_ctx.reply.await_count, 1)
        self.assertEqual(wordle_ctx.reply.await_count, 1)
        self.assertEqual(lotto_ctx.reply.await_count, 1)
        self.assertEqual(thanks_ctx.reply.await_count, 1)
        self.assertEqual(surprise_ctx.reply.await_count, 1)
        self.assertIn("Anagram:", anagram_ctx.reply.await_args.args[0])
        self.assertIn("Next draw:", lotto_ctx.reply.await_args.args[0])
        self.assertIn("Usage:", thanks_ctx.reply.await_args.args[0])
        self.assertIn("Triggered a surprise drop", surprise_ctx.reply.await_args.args[0])


class RuntimeHookSmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        load_data()

    async def test_voice_watchdog_keeps_playback_connected_while_disabled(self):
        vc = DummyVoiceClient()
        vc.is_playing = lambda: False
        vc.is_paused = lambda: False
        guild = DummyGuild(840, voice_client=vc)
        playback_stub = SimpleNamespace(
            should_keep_voice_connected=lambda guild_id: guild_id == 840,
            note_voice_disconnected=lambda _guild_id: None,
        )
        bot = SimpleNamespace(
            guilds=[guild],
            get_cog=lambda name: playback_stub if name == "PlaybackCog" else None,
        )
        voice = VoiceCog(bot)

        with (
            patch("paragon.voice.is_guild_enabled", return_value=False),
            patch("paragon.voice.cleanup_voice_client", new=AsyncMock()) as cleanup_voice,
        ):
            await VoiceCog.idle_watchdog.coro(voice)

        self.assertEqual(cleanup_voice.await_count, 0)

    async def test_pause_resume_hooks_smoke(self):
        playback_vc = DummyVoiceClient()
        playback_guild = DummyGuild(841, voice_client=playback_vc)
        playback_bot = SimpleNamespace(
            get_guild=lambda gid: playback_guild if gid == 841 else None,
            get_cog=lambda _name: None,
        )
        playback = PlaybackCog(playback_bot)
        playback._guild_current[841] = SimpleNamespace(
            started_at=0.0,
            started_offset=0.0,
            request=SimpleNamespace(playback_speed=1.0),
            duration_seconds=10.0,
        )
        playback._guild_active_vc[841] = playback_vc

        tts_vc = DummyVoiceClient()
        tts_guild = DummyGuild(842, voice_client=tts_vc)
        tts_bot = SimpleNamespace(get_guild=lambda gid: tts_guild if gid == 842 else None)
        tts = TTSCog(tts_bot)
        tts._guild_active_vc[842] = tts_vc
        tts._guild_active_audio[842] = _ActiveSayAudio(temp_path="voice.mp3", audio_seconds=12.0)

        voice_vc = DummyVoiceClient()
        voice_vc.is_playing = lambda: False
        voice_vc.is_paused = lambda: False
        voice_guild = DummyGuild(843, voice_client=voice_vc)
        voice_bot = SimpleNamespace(
            get_guild=lambda gid: voice_guild if gid == 843 else None,
            get_cog=lambda _name: None,
        )
        voice = VoiceCog(voice_bot)

        bounty_guild = DummyGuild(844)
        bounty_bot = SimpleNamespace(get_guild=lambda gid: bounty_guild if gid == 844 else None)
        bounty = BountyCog(bounty_bot)
        bounty_state = _bounty_state(844)
        bounty_state["claimant_user_id"] = 999
        bounty_state["resolved"] = False
        bounty_state["claim_expires_at"] = "2999-01-01T00:00:00+00:00"

        roulette_guild = DummyGuild(845)
        roulette_member = DummyMember(roulette_guild, 846, "Roulette User")
        roulette_member.communication_disabled_until = datetime.now(timezone.utc) + timedelta(seconds=120)
        roulette_guild.members = [roulette_member]
        roulette_bot = SimpleNamespace(get_guild=lambda gid: roulette_guild if gid == 845 else None)
        roulette = RouletteCog(roulette_bot)
        _store_active_timeout(845, 846, 120)

        with (
            patch("paragon.playback.cleanup_voice_client", new=AsyncMock()) as cleanup_playback,
            patch("paragon.playback.time.monotonic", return_value=1.5),
            patch("paragon.tts.cleanup_voice_client", new=AsyncMock()) as cleanup_tts,
            patch("paragon.tts.time.monotonic", return_value=2.0),
            patch("paragon.voice.cleanup_voice_client", new=AsyncMock()) as cleanup_voice,
            patch("paragon.bounty.is_guild_enabled", return_value=True),
            patch("paragon.roulette._clear_timeout_member", new=AsyncMock(return_value=True)) as clear_timeout,
            patch("paragon.roulette._timeout_member", new=AsyncMock(return_value=True)) as set_timeout,
            patch("paragon.roulette.save_data", new=AsyncMock()),
        ):
            await playback.pause_guild(841)
            await playback.resume_guild(841)
            await tts.pause_guild(842)
            await tts.resume_guild(842)
            await voice.pause_guild(843)
            await bounty.resume_guild(844)
            await roulette.pause_guild(845)
            await roulette.resume_guild(845)

        try:
            self.assertFalse(playback_vc.stopped)
            self.assertTrue(tts_vc.stopped)
            self.assertTrue(playback._play_allowed_for(841).is_set())
            self.assertTrue(tts._play_allowed_for(842).is_set())
            self.assertEqual(cleanup_playback.await_count, 0)
            self.assertEqual(cleanup_tts.await_count, 1)
            self.assertEqual(cleanup_voice.await_count, 1)
            self.assertIn(844, bounty._claim_tasks)
            self.assertEqual(clear_timeout.await_count, 1)
            self.assertEqual(set_timeout.await_count, 1)
        finally:
            bounty._cancel_claim_task(844)
            tts.cog_unload()
