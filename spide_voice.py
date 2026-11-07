"""Discord voice playback helpers for Spide streams.

The mobile client exposes provider stream routes. This module deliberately
accepts only HTTPS stream URLs returned by Spide; it never scrapes or extracts
audio from Spotify/YouTube web pages.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Optional

import discord


@dataclass
class QueuedTrack:
    title: str
    stream_url: str
    webpage_url: str = ""


def _find_stream_url(payload):
    if isinstance(payload, str):
        return payload if payload.startswith("https://") else ""
    if isinstance(payload, dict):
        for key in ("url", "streamUrl", "stream_url", "mediaUrl", "media_url"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("https://"):
                return value
        for key in ("data", "stream", "result", "playback"):
            value = _find_stream_url(payload.get(key))
            if value:
                return value
    return ""


def resolve_spide_stream(track: dict, credential: str, spide_get: Callable,
                         api_base: str) -> str:
    """Resolve a provider track using Spide's app API and return its HTTPS URL."""
    source = str(
        track.get("source") or track.get("sourceType") or
        track.get("provider") or track.get("platform") or "spotify"
    ).strip().lower()
    source = source.replace("music", "").replace("_", "").replace("-", "")
    aliases = {
        "spotify": "spotify", "sp": "spotify",
        "youtube": "youtube", "yt": "youtube",
        "soundcloud": "soundcloud", "sc": "soundcloud",
    }
    provider = aliases.get(source)
    if not provider:
        raise ValueError("Не удалось определить источник трека Spide.")

    track_id = str(
        track.get("id") or track.get("trackId") or track.get("track_id") or
        track.get("sourceId") or track.get("source_id") or ""
    ).strip()
    if not track_id:
        raise ValueError("В ответе поиска Spide отсутствует ID трека.")

    if provider in ("spotify", "youtube"):
        path = f"/{provider}/tracks/{urllib.parse.quote(track_id, safe='')}/stream"
        url = api_base.rstrip("/") + path
    else:
        # The Android client exposes /soundcloud/stream (unlike the
        # provider-specific /tracks/{id}/stream routes).
        url = api_base.rstrip("/") + "/soundcloud/stream?" + urllib.parse.urlencode(
            {"trackId": track_id}
        )

    response = spide_get(
        url, credential, referer="https://web.spidemusic.com/",
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Spide не выдал аудиопоток (HTTP {response.status_code}).")
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError("Spide вернул неожиданный ответ вместо ссылки на аудио.") from exc
    stream_url = _find_stream_url(payload)
    if not stream_url:
        raise RuntimeError("В ответе Spide нет HTTPS-ссылки на аудиопоток.")
    return stream_url


class GuildVoicePlayer:
    """One independent queue/player per Discord guild."""

    def __init__(self, bot):
        self.bot = bot
        self.queues = {}
        self.current = {}
        self.volumes = collections.defaultdict(lambda: 0.5)
        self._locks = collections.defaultdict(asyncio.Lock)

    async def enqueue(self, guild, channel, track: QueuedTrack):
        if guild is None:
            raise ValueError("Команда доступна только на сервере.")
        async with self._locks[guild.id]:
            voice = guild.voice_client
            if voice and voice.is_connected() and voice.channel != channel:
                await voice.move_to(channel)
            elif not voice or not voice.is_connected():
                permissions = channel.permissions_for(guild.me)
                if not permissions.connect or not permissions.speak:
                    raise PermissionError("Боту нужны права «Подключаться» и «Говорить» в голосовом канале.")
                voice = await channel.connect()
            queue = self.queues.setdefault(guild.id, collections.deque())
            queue.append(track)
            if not voice.is_playing() and not voice.is_paused():
                await self._start_next(guild.id, voice)
            return (1 if guild.id in self.current else 0) + len(queue)

    async def _start_next(self, guild_id, voice=None):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        voice = voice or guild.voice_client
        if voice is None or not voice.is_connected():
            return
        queue = self.queues.setdefault(guild_id, collections.deque())
        if not queue:
            self.current.pop(guild_id, None)
            return
        track = queue.popleft()
        self.current[guild_id] = track
        try:
            audio = discord.FFmpegPCMAudio(
                track.stream_url,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn",
            )
            source = discord.PCMVolumeTransformer(audio, volume=self.volumes[guild_id])
            loop = self.bot.loop

            def after_play(error):
                if error:
                    logging.error("Voice playback failed in guild %s: %s", guild_id, error)
                asyncio.run_coroutine_threadsafe(self._start_next(guild_id), loop)

            voice.play(source, after=after_play)
        except Exception:
            logging.exception("Could not start Spide stream in guild %s", guild_id)
            self.current.pop(guild_id, None)
            await self._start_next(guild_id, voice)

    async def skip(self, guild):
        voice = guild.voice_client
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
            return True
        return False

    async def stop(self, guild):
        voice = guild.voice_client
        self.queues[guild.id] = collections.deque()
        self.current.pop(guild.id, None)
        if voice:
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            await voice.disconnect()
        return True

    def queue_text(self, guild_id):
        queue = list(self.queues.get(guild_id, []))
        current = self.current.get(guild_id)
        lines = []
        if current:
            lines.append(f"▶ Сейчас: **{current.title[:150]}**")
        if queue:
            lines.extend(f"{i}. {item.title[:140]}" for i, item in enumerate(queue[:10], 1))
        if len(queue) > 10:
            lines.append(f"…и ещё {len(queue) - 10}")
        return "\n".join(lines) or "Очередь пуста."

    def set_volume(self, guild, volume):
        if not 0 <= volume <= 100:
            raise ValueError("Громкость должна быть от 0 до 100.")
        self.volumes[guild.id] = volume / 100
        voice = guild.voice_client
        if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = volume / 100
