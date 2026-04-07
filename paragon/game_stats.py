from __future__ import annotations

from typing import Optional

import discord
from discord.ext import commands

from .emojis import EMOJI_BAR_CHART, EMOJI_CHART_WITH_UPWARDS_TREND
from .ownership import is_control_user_id
from .stats_store import get_user_stats, iter_guild_user_stats
from .storage import _udict


def _as_dict(v):
    return v if isinstance(v, dict) else {}


def _as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _as_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _fmt_num(v) -> str:
    f = _as_float(v, 0.0)
    if abs(f - round(f)) < 1e-9:
        return f"{int(round(f)):,}"
    return f"{f:,.2f}"


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
                        "claims", "sent", "received", "xp_wagered_total", "xp_profit_total",
                        "xp_spent_total", "boost_seed_xp_total", "boost_percent_total", "boost_minutes_total",
                    }
                )
                lines.append(f"- {game}: {summary if summary else 'tracked'}")

        chunks = _chunk_lines(lines)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await ctx.reply(chunk)
            else:
                await ctx.send(chunk)
