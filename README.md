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

- Default command prefix: `!` (set via `COMMAND_PREFIX` in `.env`)
- Data is stored per guild in `paragon_data/<guild_id>.db`
- Global per-user settings are stored in `paragon_data/_user_settings.db`
- If cloning, bot requires `DISCORD_TOKEN` in an `.env` that has proper permissions set in Discord's dev portal
- `!say` uses ElevenLabs TTS (`ELEVEN_API`) and requires FFmpeg available on the host
- `!play` uses `yt-dlp` when available, with a direct-download fallback for simple file links

## Command Reference (By Cog)

`Admin` below means elevated access for that command (owner/admin checks in code).  
`Non-Admin` means regular members can use it.

## CoreCog

### Non-Admin
- `!help`
- `!re`
- `!rank [@member]` (aliases: `!xp`, `!level`)
- `!leaderboard [limit]` (aliases: `!lb`, `!xps`)
- `!boosts [@member]` (aliases: `!rate`, `!mult`) to view a user's active boosts/debuffs  
  (This is the `boosts` line shown in `!help`.)

### Admin
- `!adminhelp`
- `!boosts add {+/-}{rate} {time} {@user|@role|@everyone}`
- `!boosts remove {+/-}{rate} {time} {@user|@role|@everyone}`
- `!boosts clear {@user|@role|@everyone}`  
  (These are the `boosts` variants shown in `!adminhelp`.)

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
- Max bet is **unlimited by default** (`CF_MAX_BET=-1`).  
  Set `CF_MAX_BET` to a non-negative value to enforce a cap.

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

## SpinCog

### Non-Admin
- `!spin` (alias: `!wheel`) once per reset cycle
- `!spinstatus` (alias: `!wheelstatus`) to view spin lock + active wheel buffs
- Wheel rewards now include game-specific buffs (blackjack, wordle, anagram, roulette, coinflip, lotto) plus XP/prestige rewards.

### Admin
- `!spintime [time]` (view/set daily wheel reset time, ET; e.g. `!spintime 5:00am`)
- `!spinrewards [reward_key] [on|off|toggle|default]` (list/toggle reward pool)
- `!spinreset @user` (reset that user's spin lock for the current cycle)

## BlackjackCog

### Non-Admin
- `!blackjack [arg]` (alias: `!bj`)
  - Common use:
  - `!bj` (open/show table state)
  - `!bj join` / `!bj enter` / `!bj buyin` (enter table)
  - `!bj leave` / `!bj stop` / `!bj exit` (leave table)
  - Or react on the table prompt:
    - `:dollar:` enter table
    - `:octagonal_sign:` leave table
    - `:arrow_forward:` deal
  - `!bj hit`
  - `!bj stand`
  - `!bj dd` / `!bj doubledown`
  - `!bj surrender`
  - `!bj split`
- Entry is **free** (no XP cost).
- Daily eligibility is reset on a configurable ET schedule (default midnight ET).
- Win: grants a scaled XP-rate buff and you can keep playing.
- Loss: applies a scaled XP-rate debuff (current blackjack win buff remains active).
- Daily lockout is enabled by default, but can be toggled by admin.
- Push: no buff/debuff.

### Admin
- `!bjreset`
- `!bjtime [time]` (view/set daily reset time, ET; e.g. `!bjtime 12:00am`)
- `!bjcooldown [on|off|toggle]`
- `!bjdebug`
- `!bjstate`
- `!bjintents`

## VoiceCog

### Non-Admin
- `!join`
- `!leave` (aliases: `!disconnect`, `!dc`)

### Admin
- `!voicehealth`

## PlaybackCog

### Non-Admin
- `!play <link or search terms> [speed 0.5-2.0]`
- Queues guild-local audio requests for the caller's current voice channel.
- Plain-text input performs a YouTube top-result search via `yt-dlp` and queues the first match.
- Optional trailing speed clamps to `0.5x`-`2.0x`.
- Tries `yt-dlp` first, then falls back to direct file download for simpler audio URLs.
- Rejects tracks over 20 minutes and cleans up downloaded temp files after playback.
- Leaves voice after 30 seconds of true idleness.
- Posts a skip-vote message with `⏩`; 50% of non-bot users in the current call must react to skip.

### Admin
- `!playskip`
- `!playclear`

## TTSCog

### Non-Admin
- `!tts` (aliases: `!ttstags`, `!ttshelp`) to view style/emotion/non-verbal tag examples
- `!say {message} {@user}`
- `!rerollvoice` (aliases: `!ttsreroll`, `!voicereroll`)
- `!setvoice <voice_id> [stability] [similarity_boost] [style] [use_speaker_boost] [speed] [seed]`
- TTS stays connected across queued `!say` requests instead of leaving/rejoining between each one.
- If queued audio from `!play` is active in the same voice channel, TTS interrupts it and playback resumes afterward.
- Voice auto-leaves after 30 seconds of true idleness.
- Voice/profile is persisted globally per caller (same user keeps the same voice selection/settings across servers).
- Voice options are pulled from available voices in your ElevenLabs account.
- For `!setvoice`, any omitted optional settings use default profile values.

### Admin
- `!rerollvoice @user` (force reroll for another member)
- `!ttsmodel [model_id]` (when empty, lists current available models for your account)

## WakeupCog

### Non-Admin
- `!wakeup @user` (alias: `!wakeywakey @user`)
- Caller must be in a voice channel.
- Target must currently be in AFK.
- Runs 10 random eligible voice-channel hops, then moves target to caller's channel.
- If target sends no message within 60 seconds, they are moved back to AFK.
- Wakeup lock is per-target and only clears once target returns to AFK.

### Admin
- None

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
- `SpinCog`
- `PrestigeCog`
- `BlackjackCog`
- `VoiceCog`
- `TTSCog`
- `WakeupCog`
- `StatsCog`
- `AdminCog`

