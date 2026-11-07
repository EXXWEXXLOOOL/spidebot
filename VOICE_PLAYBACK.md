# Discord voice playback

The bot provides per-server voice queues for Spide tracks.

## Slash commands

- `/play channel:<voice channel> track:<Spide URL or search text>` — resolve a track through Spide and enqueue its stream.
- `/pause`, `/resume` — pause/resume.
- `/skip` — skip the current track.
- `/stop` — clear the queue and disconnect.
- `/queue` — show the current track and upcoming tracks.
- `/volume level:<0-100>` — set the guild playback volume.

## Runtime requirements

1. Install Python dependencies with `pip install -r requirements.txt`. The `discord.py[voice]` extra installs PyNaCl.
2. Install FFmpeg on the host and make sure the `ffmpeg` executable is on `PATH`.
   - Debian/Ubuntu: `sudo apt-get update && sudo apt-get install -y ffmpeg`
   - Alpine: `apk add --no-cache ffmpeg`
3. Configure the existing `DISCORD_TOKEN` and `SPIDE_COOKIE` / Spide access token.
4. In the Discord Developer Portal, enable the bot's required server installation scopes and grant it `Connect` and `Speak` in the target voice channels.

## Stream API note

The Android APK contains provider stream routes for Spotify and YouTube tracks, plus a SoundCloud media route. The bot requests the stream URL from Spide's API and passes only that returned HTTPS URL to FFmpeg. Stream response fields and provider-specific parameters can vary by API release; if Spide changes its response contract, the resolver in `spide_voice.py` is the place to update it.

The bot does not scrape Spotify/YouTube pages or bypass DRM.
