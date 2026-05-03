# Paragon Bot

Discord XP, voice, and mini-game bot with persistent per-guild storage.

## Quick Start

- Required: `DISCORD_TOKEN`
- Optional: `COMMAND_PREFIX` (defaults to `!`)
- Run: `python Paragon.py`
- Guild data: `paragon_data/<guild_id>.db`
- Global user settings: `paragon_data/_user_settings.db`
- Voice features need FFmpeg
- `!say` uses ElevenLabs and needs `ELEVEN_API`
- `!play` uses `yt-dlp`; `YTDLP_COOKIES_FROM_BROWSER` or `YTDLP_COOKIE_FILE` can help with restricted YouTube playback
- Restricted YouTube playback works best with a current exported cookie file plus a JavaScript runtime such as `node`
- Keep YouTube cookies local only. Do not commit browser cookies or exported `cookies.txt` files to the repo.
- Preferred local setup for Chrome: add `YTDLP_COOKIES_FROM_BROWSER=chrome` to your local `.env`
- If your YouTube login lives in a non-default Chrome profile, use `YTDLP_COOKIES_FROM_BROWSER=chrome:Profile 1` or `chrome:Profile 2`
- Use `YTDLP_COOKIE_FILE` only for a local exported cookie file that stays gitignored

## Active Cogs

Loaded by default from `Paragon.py`:

- `CoreCog`, `WordleCog`, `CoinFlipCog`, `RouletteCog`, `SurpriseCog`, `AnagramCog`
- `ContractsCog`, `ChecklistCog`, `BountyCog`, `FishCog`, `ThanksCog`, `LottoCog`, `SpinCog`
- `ShopCog`, `PrestigeCog`, `QuietCog`, `BlackjackCog`
- `PlaybackCog`, `VoiceCog`, `WakeupCog`, `TTSCog`
- `StatsCog`, `AdminCog`

Not loaded by default:

- `BossCog` exists in `paragon/boss.py` but is commented out in `Paragon.py`

## Command Index

These commands reflect the cogs currently loaded by `Paragon.py`.

Access legend:

- `User`: available to regular members
- `Elevated`: owner/admin-style permission checks in code
- `Owner`: owner-only

### General And Progression

| Command | Aliases | Access | Purpose |
|---|---|---:|---|
| `!help` | none | User | Show member-facing help |
| `!adminhelp` | none | Owner | Show elevated help |
| `!re` | none | User | Quick bot response check |
| `!rank [@member]` | `!xp`, `!level` | User | Show XP, gain rate, and active boosts |
| `!leaderboard [limit]` | `!lb`, `!xps` | User | Show top XP users |
| `!boosts [@member]` | `!rate`, `!mult` | User / Elevated | View boosts; elevated use can add, remove, or clear them |
| `!prestige [@user] [all]` | `!p` | User / Elevated | View prestige board or prestige yourself; elevated users can target others |
| `!gamestats [@user]` | `!stats`, `!mystats` | User / Elevated | Show your stats; elevated users can inspect others |
| `!guildgamestats` | `!serverstats` | Elevated | Show guild-wide game totals |

### Daily, Social, And Economy

| Command | Aliases | Access | Purpose |
|---|---|---:|---|
| `!checklist [@user]` | `!check` | User / Elevated | Show what you still have left in the current daily cycle; elevated users can inspect others |
| `!quest [@user]` | `!q` | User | Show daily contract progress |
| `!bounty [@user / stop]` | `!b` | User | Show, start, or stop the daily bounty flow |
| `!claim` | none | User | Claim the active surprise drop |
| `!thanks @user` | `!thx` | User | Give another user a thanks reward |
| `!lotto [count / @user]` | `!l` | User | Buy tickets or inspect ticket counts |
| `!spin [all]` | `!wheel` | User | Use your daily wheel spin(s) |
| `!spinstatus` | `!wheelstatus` | User | Show spin lock and wheel buffs |
| `!cleanse` | none | User | Spend a cleanse charge to remove debuffs |
| `!drain` | none | User | Debuff others in your voice call and buff yourself |
| `!shop` | none | User | View shop items |
| `!buy <item> [amount]` | none | User | Buy a shop item |

### Games

| Command | Aliases | Access | Purpose |
|---|---|---:|---|
| `!wordle [guess]` | `!w`, `!wd` | User | Play daily Wordle |
| `!anagram [guess]` | `!a` | User | Play the anagram game |
| `!cf <amount>` | `!coinflip` | User | Start, accept, or cancel a coinflip wager |
| `!roulette @user` | `!r` | User | Fire a roulette shot at another user |
| `!fish [action]` | none | User | Run fishing actions like `cast`, `reel`, `lift`, `give`, `set`, `stop`, or `status` |
| `!blackjack [arg]` | `!bj` | User | Join and play blackjack |

### Voice And Audio

| Command | Aliases | Access | Purpose |
|---|---|---:|---|
| `!join` | none | User | Join your voice channel |
| `!leave` | `!disconnect`, `!dc` | User | Disconnect from voice |
| `!play <query>` | none | User | Queue audio in voice |
| `!say {message} {@user}` | none | User | Speak TTS in a user's voice channel |
| `!tts` | `!ttstags`, `!ttshelp` | User | Show TTS tags/help |
| `!rerollvoice [@user]` | `!ttsreroll`, `!voicereroll` | User / Elevated | Reroll your TTS voice; elevated users can target others |
| `!setvoice <voice_id> ...` | `!ttsvoice`, `!voiceid` | User | Save your TTS voice profile |
| `!wakeup @user` | `!wakeywakey` | User | Pull an AFK user through random channels into yours |
| `!shh @user` | none | User | Server mute a user for 30 seconds with a personal cooldown |

### Elevated Commands

| Command | Access | Purpose |
|---|---:|---|
| `!boosts add/remove/clear ...` | Elevated | Manage user boosts and debuffs |
| `!resetwordle` | Elevated | Reset the current Wordle session |
| `!claimnow` | Elevated | Spawn a surprise drop immediately |
| `!poplatto` | Elevated | Force a lottery draw |
| `!lottotime [time]` | Elevated | View or set lottery draw time |
| `!lottotoggle` | Elevated | Enable or disable the lottery |
| `!spintime [time]` | Elevated | View or set spin reset time |
| `!spinrewards ...` | Elevated | List or toggle spin rewards |
| `!spinreset @user` | Elevated | Reset a user's current spin cycle |
| `!setp <amount> @user` | Elevated | Set a user's prestige |
| `!bjreset` | Elevated | Reset blackjack table state |
| `!bjtime [time]` | Elevated | View or set blackjack reset time |
| `!bjcooldown [mode]` | Elevated | Toggle blackjack daily cooldown behavior |
| `!bjdebug` | Elevated | Toggle blackjack debug mode |
| `!bjstate` | Elevated | Print blackjack internal state |
| `!bjintents` | Elevated | Show Discord intent flags |
| `!playskip` | Elevated | Skip playback |
| `!playclear` | Elevated | Clear playback queue |
| `!ttscooldown [mode]` | Elevated | Manage `!say` cooldown |
| `!ttsmodel [model_id]` | Elevated | View or set ElevenLabs TTS model |
| `!ttsqueue ...` | Elevated | Show, skip, or clear the TTS queue |
| `!role @user @role` | Elevated | Toggle a role on a member |
| `!xprate [@user]` | Elevated | Show passive XP/min rates |
| `!setxp <targets...> <xp>` | Elevated | Set XP totals |
| `!adjust @user <+amount / -amount>` | Elevated | Add or subtract XP |
| `!softreset @user` | Elevated | Reset XP, prestige, and wheel rewards without deleting stats |
| `!voicehealth` | Owner | Run voice diagnostics |
| `!fishreroll` | Owner | Reroll the active fishing water state |

## Project Conventions

- Use `paragon/emojis.py` for shared emoji and UI symbol constants.
- Use `paragon/include.py` for small generic helpers shared across modules.
- `paragon/include.py` currently contains: `_as_dict`, `_as_list`, `_as_int`, `_as_float`, `_fmt_num`, `_utcnow`, `_iso`, `_parse_iso`, and `_inc_num`.
- Keep feature-specific helpers local to the module that owns the behavior.
- Update this README whenever commands, loaded cogs, setup requirements, or repository conventions change.
