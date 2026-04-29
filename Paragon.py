import discord
from discord.ext import commands
import inspect

#import logging

from paragon.admin import AdminCog
from paragon.anagram import AnagramCog
from paragon.blackjack import BlackjackCog
from paragon.boss import BossCog
from paragon.coinflip import CoinFlipCog
from paragon.config import AUTHOR_USER_ID, COMMAND_PREFIX, TOKEN
from paragon.contracts import ContractsCog
from paragon.core import CoreCog
from paragon.game_stats import StatsCog
from paragon.lotto import LottoCog
from paragon.playback import PlaybackCog
from paragon.prestige import PrestigeCog
from paragon.quiet import QuietCog
from paragon.roulette import RouletteCog
from paragon.shop import ShopCog
from paragon.storage import load_data
from paragon.surprise import SurpriseCog
from paragon.thanks import ThanksCog
from paragon.tts import TTSCog
from paragon.spin import SpinCog
from paragon.voice import VoiceCog
from paragon.wakeup import WakeupCog
from paragon.wordle import WordleCog


def create_bot() -> commands.Bot:
    intents = discord.Intents(
        guilds=True,
        members=True,
        messages=True,
        message_content=True,
        voice_states=True,
        reactions=True,
    )
    return commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)


bot = create_bot()


@bot.check_once
async def _global_check(_ctx):
    return True


def _build_cogs() -> list[commands.Cog]:
    return [
        CoreCog(bot),
        WordleCog(bot),
        CoinFlipCog(bot),
        RouletteCog(bot),
        SurpriseCog(bot),
        AnagramCog(bot),
        ContractsCog(bot),
        #BossCog(bot),
        ThanksCog(bot),
        LottoCog(bot),
        SpinCog(bot),
        ShopCog(bot),
        PrestigeCog(bot),
        QuietCog(bot),
        BlackjackCog(bot),
        PlaybackCog(bot),
        VoiceCog(bot),
        WakeupCog(bot),
        TTSCog(bot),
        StatsCog(bot),
        AdminCog(bot),
    ]


async def _add_cog(cog: commands.Cog) -> None:
    name = cog.__class__.__name__
    if bot.get_cog(name) is not None:
        return

    maybe = bot.add_cog(cog)
    if inspect.isawaitable(maybe):
        await maybe


async def setup_cogs() -> None:
    for cog in _build_cogs():
        await _add_cog(cog)


def preload_cogs_if_sync_add_cog() -> None:
    """
    Compatibility path: some discord forks expose sync add_cog and may not call setup_hook.
    """
    if inspect.iscoroutinefunction(bot.add_cog):
        return
    for cog in _build_cogs():
        name = cog.__class__.__name__
        if bot.get_cog(name) is not None:
            continue
        bot.add_cog(cog)


@bot.event
async def on_ready():
    await setup_cogs()
    author_info = f"{AUTHOR_USER_ID}" if AUTHOR_USER_ID else "unset"
    print(f"Bot online as {bot.user} | author={author_info}")
    print(f"Cogs loaded: {len(bot.cogs)} | commands: {len(bot.commands)}")


@bot.event
async def setup_hook():
    await setup_cogs()


def _bootstrap_storage() -> None:
    load_data()


if __name__ == "__main__":
    _bootstrap_storage()
    preload_cogs_if_sync_add_cog()
    bot.run(TOKEN)
