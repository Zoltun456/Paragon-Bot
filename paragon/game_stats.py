from __future__ import annotations

from typing import Optional

import discord
from discord.ext import commands

from .emojis import EMOJI_BAR_CHART, EMOJI_CHART_WITH_UPWARDS_TREND
from .include import _as_dict, _as_float, _as_int, _fmt_num
from .ownership import is_control_user_id
from .stats_store import get_user_stats, iter_guild_user_stats
from .storage import _udict


def _can_view_others(ctx: commands.Context) -> bool:
    if is_control_user_id(ctx.guild, ctx.author.id):
        return True
    perms = getattr(ctx.author, "guild_permissions", None)
    if not perms:
        return False
    return bool(perms.manage_guild or perms.administrator)


def _chunk_lines(lines: list[str], max_len: int = 1800) -> list[str]:
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for ln in lines:
        add = len(ln) + (1 if cur else 0)
        if cur and cur_len + add > max_len:
            chunks.append("\n".join(cur))
            cur = [ln]
            cur_len = len(ln)
        else:
            cur.append(ln)
            cur_len += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _game_stats_lines(games: dict) -> list[str]:
    out: list[str] = []

    bj = _as_dict(games.get("blackjack"))
    if bj:
        out.append(
            "Blackjack: "
            f"rounds {_fmt_num(bj.get('rounds_played', 0))} | "
            f"hands {_fmt_num(bj.get('hands_played', 0))} | "
            f"W-L-D {_fmt_num(bj.get('wins', 0))}-{_fmt_num(bj.get('losses', 0))}-{_fmt_num(bj.get('draws', 0))} | "
            f"naturals {_fmt_num(bj.get('naturals', 0))} | "
            f"busts {_fmt_num(bj.get('busts', 0))} | "
            f"doubles {_fmt_num(bj.get('doubles', 0))} | "
            f"splits {_fmt_num(bj.get('splits', 0))} | "
            f"surrenders {_fmt_num(bj.get('surrenders', 0))} | "
            f"wagered {_fmt_num(bj.get('xp_wagered_total', 0))} | "
            f"profit {_fmt_num(bj.get('xp_profit_total', 0))}"
        )

    cf = _as_dict(games.get("coinflip"))
    if cf:
        out.append(
            "Coinflip: "
            f"challenges {_fmt_num(cf.get('challenges_created', 0))} | "
            f"matches {_fmt_num(cf.get('matches_played', 0))} | "
            f"W-L {_fmt_num(cf.get('wins', 0))}-{_fmt_num(cf.get('losses', 0))} | "
            f"wagered {_fmt_num(cf.get('xp_wagered_total', 0))} | "
            f"boost seed {_fmt_num(cf.get('boost_seed_xp_total', 0))} | "
            f"boost minutes {_fmt_num(cf.get('boost_minutes_total', 0))}"
        )

    wd = _as_dict(games.get("wordle"))
    if wd:
        out.append(
            "Wordle: "
            f"puzzles {_fmt_num(wd.get('puzzles_played', 0))} | "
            f"guesses {_fmt_num(wd.get('guesses_submitted', 0))} | "
            f"W-L {_fmt_num(wd.get('wins', 0))}-{_fmt_num(wd.get('losses', 0))} | "
            f"boost seed {_fmt_num(wd.get('boost_seed_xp_total', 0))}"
        )

    an = _as_dict(games.get("anagram"))
    if an:
        out.append(
            "Anagram: "
            f"started {_fmt_num(an.get('puzzles_started', 0))} | "
            f"solves {_fmt_num(an.get('solves', 0))} | "
            f"fails {_fmt_num(an.get('fails', 0))} | "
            f"boost seed {_fmt_num(an.get('boost_seed_xp_total', 0))}"
        )

    lt = _as_dict(games.get("lotto"))
    if lt:
        out.append(
            "Lotto: "
            f"tickets {_fmt_num(lt.get('tickets_bought', 0))} | "
            f"spent {_fmt_num(lt.get('xp_spent_total', 0))} | "
            f"jackpots {_fmt_num(lt.get('jackpots_won', 0))} | "
            f"boost seed {_fmt_num(lt.get('boost_seed_xp_total', 0))} | "
            f"boost % total {_fmt_num(lt.get('boost_percent_total', 0))}% | "
            f"boost mins {_fmt_num(lt.get('boost_minutes_total', 0))}"
        )

    rt = _as_dict(games.get("roulette"))
    if rt:
        plays = _as_float(rt.get("plays", 0))
        avg_chance_pct = (_as_float(rt.get("chance_pct_total", 0.0)) / plays) if plays > 0 else 0.0
        out.append(
            "Roulette: "
            f"plays {_fmt_num(rt.get('plays', 0))} | "
            f"successes {_fmt_num(rt.get('successes', 0))} | "
            f"backfires {_fmt_num(rt.get('backfires', 0))} | "
            f"timeouts {_fmt_num(rt.get('got_timed_out', 0))} | "
            f"avg odds {avg_chance_pct:.2f}%"
        )

    th = _as_dict(games.get("thanks"))
    if th:
        out.append(
            "Thanks: "
            f"sent {_fmt_num(th.get('sent', 0))} | "
            f"received {_fmt_num(th.get('received', 0))} | "
            f"boost seed {_fmt_num(th.get('boost_seed_xp_total', 0))}"
        )

    sp = _as_dict(games.get("surprise"))
    if sp:
        out.append(
            "Surprise: "
            f"claims {_fmt_num(sp.get('claims', 0))} | "
            f"boost seed {_fmt_num(sp.get('boost_seed_xp_total', 0))}"
        )

    ct = _as_dict(games.get("contracts"))
    if ct:
        out.append(
            "Contracts: "
            f"assigned {_fmt_num(ct.get('assigned', 0))} | "
            f"completed {_fmt_num(ct.get('completed', 0))} | "
            f"chains {_fmt_num(ct.get('multi_step_completed', 0))} | "
            f"legendary {_fmt_num(ct.get('legendary_completed', 0))} | "
            f"objectives {_fmt_num(ct.get('objectives_completed_total', 0))} | "
            f"boost seed {_fmt_num(ct.get('boost_seed_xp_total', 0))}"
        )

    fs = _as_dict(games.get("fishing"))
    if fs:
        out.append(
            "Fishing: "
            f"sessions {_fmt_num(fs.get('sessions_started', 0))} | "
            f"casts {_fmt_num(fs.get('casts_started', 0))} | "
            f"bites {_fmt_num(fs.get('bites', 0))} | "
            f"catches {_fmt_num(fs.get('catches', 0))} | "
            f"escapes {_fmt_num(fs.get('escapes', 0))} | "
            f"perfect reels {_fmt_num(fs.get('perfect_reels', 0))} | "
            f"chests {_fmt_num(fs.get('chests_found', 0))} | "
            f"xp {_fmt_num(fs.get('xp_awarded_total', 0))}"
        )

    shop = _as_dict(games.get("shop"))
    if shop:
        out.append(
            "Shop: "
            f"purchases {_fmt_num(shop.get('purchases', 0))} | "
            f"commands {_fmt_num(shop.get('buy_commands', 0))} | "
            f"spent {_fmt_num(shop.get('spent_total', 0))}"
        )

    boss = _as_dict(games.get("boss"))
    if boss:
        out.append(
            "Boss: "
            f"attacks {_fmt_num(boss.get('attacks', 0))} | "
            f"hits {_fmt_num(boss.get('hits', 0))} | "
            f"misses {_fmt_num(boss.get('misses', 0))} | "
            f"damage {_fmt_num(boss.get('damage_total', 0))} | "
            f"resurrections {_fmt_num(boss.get('resurrections', 0))} | "
            f"support {_fmt_num(boss.get('support_actions', 0))} | "
            f"guard {_fmt_num(boss.get('guards', 0))} | "
            f"interrupt {_fmt_num(boss.get('interrupts', 0))} | "
            f"purge {_fmt_num(boss.get('cleanses', 0))} | "
            f"focus {_fmt_num(boss.get('focuses', 0))} | "
            f"mechanics {_fmt_num(boss.get('mechanics_countered', 0))} | "
            f"victory rewards {_fmt_num(boss.get('victory_rewards', 0))} | "
            f"failure penalties {_fmt_num(boss.get('failure_penalties', 0))}"
        )

    bounty = _as_dict(games.get("bounty"))
    if bounty:
        out.append(
            "Bounty: "
            f"assigned {_fmt_num(bounty.get('assigned', 0))} | "
            f"claims {_fmt_num(bounty.get('claims_started', 0))}/{_fmt_num(bounty.get('claims_completed', 0))} | "
            f"stops {_fmt_num(bounty.get('stops_used', 0))} | "
            f"canceled {_fmt_num(bounty.get('claims_canceled', 0))} | "
            f"W-L-S {_fmt_num(bounty.get('wins', 0))}-{_fmt_num(bounty.get('losses', 0))}-{_fmt_num(bounty.get('survives', 0))} | "
            f"exposure {_fmt_num(bounty.get('exposure_minutes_total', 0))}m | "
            f"reward eq {_fmt_num(bounty.get('reward_minutes_equivalent_total', 0))}m"
        )

    return out


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="gamestats", aliases=["stats", "mystats"])
    async def gamestats(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        if target is None:
            target = ctx.author
        elif target.id != ctx.author.id and not _can_view_others(ctx):
            await ctx.reply("You can only view other users' stats if you're an admin/owner.")
            return

        u = _udict(ctx.guild.id, target.id)
        stats = get_user_stats(ctx.guild.id, target.id)
        xp = _as_dict(stats.get("xp"))
        games = _as_dict(stats.get("games"))
        total_xp = int(u.get("xp_f", u.get("xp", 0)))

        lines = [
            f"{EMOJI_BAR_CHART} **Stats for {target.display_name}**",
            f"Total XP: **{total_xp:,}**",
            (
                "XP Ledger: "
                f"gained **{_fmt_num(xp.get('gained_total', 0))}**, "
                f"lost **{_fmt_num(xp.get('lost_total', 0))}**, "
                f"net **{_fmt_num(xp.get('net_total', 0))}**, "
                f"events **{_fmt_num(xp.get('event_count', 0))}**"
            ),
            (
                "Passive XP: "
                f"minutes **{_fmt_num(xp.get('passive_minutes_total', 0))}**, "
                f"ticks **{_fmt_num(xp.get('passive_ticks', 0))}**"
            ),
        ]

        boosts_by_source = _as_dict(xp.get("boosts_by_source"))
        if boosts_by_source:
            lines.append("Boost Sources:")
            rows = sorted(
                boosts_by_source.items(),
                key=lambda kv: (_as_float(_as_dict(kv[1]).get("reward_seed_xp_total", 0.0)), kv[0]),
                reverse=True,
            )
            for src, row_raw in rows[:8]:
                row = _as_dict(row_raw)
                lines.append(
                    f"- `{src}`: boosts {_fmt_num(row.get('count', 0))}, "
                    f"seed {_fmt_num(row.get('reward_seed_xp_total', 0))}, "
                    f"percent total {_fmt_num(row.get('percent_total', 0))}%, "
                    f"minutes total {_fmt_num(row.get('minutes_total', 0))}"
                )

        by_source = _as_dict(xp.get("by_source"))
        if by_source:
            lines.append("XP Sources:")
            rows = sorted(
                by_source.items(),
                key=lambda kv: (_as_float(_as_dict(kv[1]).get("event_count", 0.0)), kv[0]),
                reverse=True,
            )
            for src, row_raw in rows[:10]:
                row = _as_dict(row_raw)
                lines.append(
                    f"- `{src}`: events {_fmt_num(row.get('event_count', 0))}, "
                    f"gain {_fmt_num(row.get('gained_total', 0))}, "
                    f"loss {_fmt_num(row.get('lost_total', 0))}, "
                    f"net {_fmt_num(row.get('net_total', 0))}"
                )

        game_lines = _game_stats_lines(games)
        if game_lines:
            lines.append("Game Stats:")
            lines.extend(f"- {ln}" for ln in game_lines)
        else:
            lines.append("Game Stats: no game data recorded yet.")

        chunks = _chunk_lines(lines)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await ctx.reply(chunk)
            else:
                await ctx.send(chunk)

    @commands.command(name="guildgamestats", aliases=["serverstats"])
    async def guildgamestats(self, ctx: commands.Context):
        if not _can_view_others(ctx):
            await ctx.reply("This command is admin/owner only.")
            return

        entries = iter_guild_user_stats(ctx.guild.id)
        if not entries:
            await ctx.reply("No stats data found yet for this server.")
            return

        xp_gain = 0.0
        xp_loss = 0.0
        xp_net = 0.0
        xp_events = 0
        game_totals: dict[str, dict] = {}
        source_totals: dict[str, dict] = {}

        for _, stats in entries:
            xp = _as_dict(_as_dict(stats).get("xp"))
            xp_gain += _as_float(xp.get("gained_total", 0.0))
            xp_loss += _as_float(xp.get("lost_total", 0.0))
            xp_net += _as_float(xp.get("net_total", 0.0))
            xp_events += _as_int(xp.get("event_count", 0))

            for src, row_raw in _as_dict(xp.get("by_source")).items():
                row = _as_dict(row_raw)
                tgt = source_totals.setdefault(src, {"event_count": 0, "gained_total": 0.0, "lost_total": 0.0, "net_total": 0.0})
                tgt["event_count"] = _as_int(tgt.get("event_count", 0)) + _as_int(row.get("event_count", 0))
                tgt["gained_total"] = _as_float(tgt.get("gained_total", 0.0)) + _as_float(row.get("gained_total", 0.0))
                tgt["lost_total"] = _as_float(tgt.get("lost_total", 0.0)) + _as_float(row.get("lost_total", 0.0))
                tgt["net_total"] = _as_float(tgt.get("net_total", 0.0)) + _as_float(row.get("net_total", 0.0))

            games = _as_dict(_as_dict(stats).get("games"))
            for game, row_raw in games.items():
                row = _as_dict(row_raw)
                tgt = game_totals.setdefault(game, {})
                for k, v in row.items():
                    if isinstance(v, (int, float)):
                        prev = _as_float(tgt.get(k, 0.0))
                        tgt[k] = prev + float(v)

        lines = [
            f"{EMOJI_CHART_WITH_UPWARDS_TREND} **Guild Game Stats** ({len(entries)} users with stats)",
            (
                "XP Ledger: "
                f"gained **{_fmt_num(xp_gain)}**, "
                f"lost **{_fmt_num(xp_loss)}**, "
                f"net **{_fmt_num(xp_net)}**, "
                f"events **{_fmt_num(xp_events)}**"
            ),
        ]

        if source_totals:
            lines.append("Top XP Sources:")
            rows = sorted(source_totals.items(), key=lambda kv: (_as_float(_as_dict(kv[1]).get("event_count", 0.0)), kv[0]), reverse=True)
            for src, row in rows[:10]:
                lines.append(
                    f"- `{src}`: events {_fmt_num(row.get('event_count', 0))}, "
                    f"gain {_fmt_num(row.get('gained_total', 0))}, "
                    f"loss {_fmt_num(row.get('lost_total', 0))}, "
                    f"net {_fmt_num(row.get('net_total', 0))}"
                )

        if game_totals:
            lines.append("Game Totals:")
            for game in sorted(game_totals.keys()):
                row = _as_dict(game_totals.get(game))
                summary = ", ".join(
                    f"{k}={_fmt_num(v)}"
                    for k, v in sorted(row.items())
                    if k in {
                        "rounds_played", "hands_played", "wins", "losses", "draws",
                        "matches_played", "puzzles_played", "solves", "fails",
                        "tickets_bought", "jackpots_won", "plays", "successes", "backfires",
                        "claims", "sent", "received", "assigned", "completed", "multi_step_completed",
                        "legendary_completed", "objectives_completed_total",
                        "sessions_started", "casts_started", "bites", "catches", "escapes",
                        "perfect_reels", "chests_found", "purchases", "buy_commands",
                        "xp_wagered_total", "xp_profit_total",
                        "xp_spent_total", "spent_total", "xp_awarded_total",
                        "boost_seed_xp_total", "boost_percent_total", "boost_minutes_total",
                        "assigned", "claims_started", "claims_completed", "wins", "losses",
                        "survives", "stops_used", "claims_canceled", "reward_minutes_equivalent_total",
                        "exposure_minutes_total", "exposure_companion_total",
                    }
                )
                lines.append(f"- {game}: {summary if summary else 'tracked'}")

        chunks = _chunk_lines(lines)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await ctx.reply(chunk)
            else:
                await ctx.send(chunk)
