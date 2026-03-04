# Paragon Bot

Paragon is a Discord XP/game bot with persistent per-guild storage.

It provides:
- Passive XP gain
- XP boosts and prestige progression
- Voice utilities
- Multiple games (Wordle, Anagram, Blackjack, Coinflip, Lotto, Roulette, Surprise drops)
- User and guild game statistics
- Owner/admin management commands

## Runtime Notes

- Default command prefix: `!` (configurable will come later)
- Data is stored per guild in `paragon_data/<guild_id>.db`
- If cloning, bot requires `DISCORD_TOKEN` in an `.env` that has proper permissions set in Discord's dev portal

## Command Reference (By Cog)

`Admin` below means elevated access for that command (owner/admin checks in code).  
`Non-Admin` means regular members can use it.

## CoreCog

### Non-Admin
- `!help`
- `!re`
- `!rank [@member]` (aliases: `!xp`, `!level`)
- `!leaderboard [limit]` (aliases: `!lb`, `!xps`)
- `!boosts [@member]` (aliases: `!rate`, `!mult`)

### Admin
- `!adminhelp`

## WordleCog

### Non-Admin
- `!wordle [guess]` (aliases: `!w`, `!wd`)

### Admin
- `!resetwordle`

## CoinFlipCog

### Non-Admin
- `!cf <amount>` (alias: `!coinflip`)
- `!cf accept [@challenger]`
- `!cf cancel`

### Admin
- None

## RouletteCog

### Non-Admin
- `!roulette @user` (alias: `!r`)
- No XP cost
- 30 minute personal cooldown per user
- Success chance scales by prestige and approaches a 50% cap
- Timeout ranges from 10s to 5m based on prestige gap of loser vs winner

### Admin
- None

## SurpriseCog

### Non-Admin
- `!claim`

### Admin
- `!claimnow`

## AnagramCog

### Non-Admin
- `!anagram [guess]` (alias: `!a`)

### Admin
- None

## ThanksCog

### Non-Admin
- `!thanks @user` (alias: `!thx`)

### Admin
- None

## LottoCog

### Non-Admin
- `!lotto [ticket_count]` (alias: `!l`)
- `!lotto @user` to inspect ticket count
- Jackpot reward is a temporary XP-rate boost (not direct XP)
- Daily auto-draw defaults to **6:00 PM ET**

### Admin
- `!poplatto` (force draw)
- `!lottotime [time]` (set draw time, e.g. `!lottotime 6pm`)
- `!lottotoggle`

## PrestigeCog

### Non-Admin
- `!prestige [@self]` (alias: `!p`)

### Admin
- `!setp <amount> @user`

## BlackjackCog

### Non-Admin
- `!blackjack [arg]` (alias: `!bj`)
  - Common use:
  - `!bj` (open/show table state)
  - `!bj <amount|all>` (set bet)
  - `!bj hit`
  - `!bj stand`
  - `!bj dd` / `!bj doubledown`
  - `!bj surrender`
  - `!bj split`

### Admin
- `!bjreset`
- `!bjdebug`
- `!bjstate`
- `!bjintents`

## VoiceCog

### Non-Admin
- `!join`
- `!leave` (aliases: `!disconnect`, `!dc`)

### Admin
- `!voicehealth`

## StatsCog

### Non-Admin
- `!gamestats` (aliases: `!stats`, `!mystats`) for your own stats

### Admin
- `!gamestats @user` (view someone else)
- `!guildgamestats` (alias: `!serverstats`)

## AdminCog

### Non-Admin
- None

### Admin
- `!role @user @role` (toggle role)
- `!xprate [@user ...]`
- `!setxp <targets...> <xp>`
- `!adjust @user <+amount|-amount>`

## Loaded Cogs

Current entrypoint (`Paragon.py`) loads:
- `CoreCog`
- `WordleCog`
- `CoinFlipCog`
- `RouletteCog`
- `SurpriseCog`
- `AnagramCog`
- `ThanksCog`
- `LottoCog`
- `PrestigeCog`
- `BlackjackCog`
- `VoiceCog`
- `StatsCog`
- `AdminCog`
