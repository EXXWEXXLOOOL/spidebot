"""Discord voice playback helpers for Spide streams.

The mobile client exposes provider stream routes. This module deliberately
accepts only HTTPS stream URLs returned by Spide; it never scrapes or extracts
audio from Spotify/YouTube web pages.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import struct
import urllib.parse
from importlib.resources import files
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

import discord
import imageio_ffmpeg


def ensure_opus_loaded():
    """Load the bundled libopus when the host has no system Opus library."""
    if discord.opus.is_loaded():
        return

    native_dir = Path(str(files("opuslib_next").joinpath("_native")))
    candidates = sorted(native_dir.glob("libopus*.so*"))
    candidates += sorted(native_dir.glob("libopus*.dylib"))
    candidates += sorted(native_dir.glob("opus*.dll"))
    if not candidates:
        raise RuntimeError(
            f"В пакете opuslib-next-bundled не найдена библиотека Opus: {native_dir}"
        )

    discord.opus.load_opus(str(candidates[0]))


class AudioDiagnosticsSource(discord.AudioSource):
    """Pass PCM through unchanged while measuring whether FFmpeg outputs sound."""

    def __init__(self, source):
        self.source = source
        self.frames = 0
        self.nonzero_frames = 0
        self.peak = 0

    def read(self):
        data = self.source.read()
        if data:
            self.frames += 1
            samples = struct.iter_unpack("<h", data)
            frame_peak = max((abs(sample[0]) for sample in samples), default=0)
            self.peak = max(self.peak, frame_peak)
            if frame_peak:
                self.nonzero_frames += 1
        return data

    def is_opus(self):
        return False

    def cleanup(self):
        cleanup = getattr(self.source, "cleanup", None)
        if callable(cleanup):
            cleanup()


def install_voice_transport_diagnostics(voice, guild_id):
    """Observe actual UDP packet writes, including errors discord.py normally hides."""
    connection = getattr(voice, "_connection", None)
    if connection is None:
        logging.warning("Voice transport diagnostics unavailable guild=%s: no connection state", guild_id)
        return None

    state = getattr(voice, "_spide_transport_stats", None)
    if state is not None:
        state["track_packets"] = 0
        state["track_bytes"] = 0
        state["track_errors"] = 0
        state["guild_id"] = guild_id
        return state

    original_send_packet = connection.send_packet
    state = {
        "packets": 0,
        "bytes": 0,
        "errors": 0,
        "track_packets": 0,
        "track_bytes": 0,
        "track_errors": 0,
        "dave_packets": 0,
        "plain_packets": 0,
        "speak_success": 0,
        "speak_errors": 0,
        "guild_id": guild_id,
    }

    # AudioPlayer schedules this coroutine from its worker thread and does not
    # surface failures through its after= callback. Observe its actual outcome.
    original_speak = getattr(connection, "speak", None)
    if callable(original_speak):
        async def observed_speak(*args, **kwargs):
            try:
                result = await original_speak(*args, **kwargs)
            except Exception:
                state["speak_errors"] += 1
                logging.exception(
                    "VOICE SPEAKING SIGNAL FAILED guild=%s args=%r",
                    state["guild_id"], args,
                )
                raise
            state["speak_success"] += 1
            logging.info(
                "VOICE SPEAKING SIGNAL SENT guild=%s args=%r count=%s",
                state["guild_id"], args, state["speak_success"],
            )
            return result

        connection.speak = observed_speak

    # Record whether discord.py actually applies the DAVE layer before RTP
    # transport encryption. This is observational only; packet construction
    # remains entirely owned by discord.py.
    original_get_voice_packet = getattr(voice, "_get_voice_packet", None)
    if callable(original_get_voice_packet):
        def observed_get_voice_packet(data):
            has_dave_session = getattr(connection, "dave_session", None) is not None
            can_encrypt = bool(getattr(connection, "can_encrypt", False))
            try:
                packet = original_get_voice_packet(data)
            except Exception:
                logging.exception(
                    "VOICE PACKET ENCRYPTION FAILED guild=%s dave_session=%s can_encrypt=%s",
                    state["guild_id"], has_dave_session, can_encrypt,
                )
                raise
            key = "dave_packets" if has_dave_session and can_encrypt else "plain_packets"
            state[key] += 1
            total = state["dave_packets"] + state["plain_packets"]
            if total == 1 or total % 1000 == 0:
                logging.info(
                    "VOICE DAVE PACKET STATE guild=%s dave_session=%s can_encrypt=%s "
                    "dave_packets=%s non_dave_packets=%s",
                    state["guild_id"], has_dave_session, can_encrypt,
                    state["dave_packets"], state["plain_packets"],
                )
            return packet

        voice._get_voice_packet = observed_get_voice_packet

    def observed_send_packet(packet):
        try:
            result = original_send_packet(packet)
        except OSError:
            state["errors"] += 1
            state["track_errors"] += 1
            logging.exception(
                "VOICE UDP SEND FAILED guild=%s packets_ok=%s packet_bytes=%s",
                state["guild_id"], state["track_packets"], len(packet),
            )
            raise
        state["packets"] += 1
        state["bytes"] += len(packet)
        state["track_packets"] += 1
        state["track_bytes"] += len(packet)
        if state["track_packets"] == 1:
            logging.info(
                "VOICE UDP FIRST PACKET guild=%s packet_bytes=%s",
                state["guild_id"], len(packet),
            )
        elif state["track_packets"] % 1000 == 0:
            logging.info(
                "VOICE UDP PROGRESS guild=%s packets=%s bytes=%s",
                state["guild_id"], state["track_packets"], state["track_bytes"],
            )
        return result

    connection.send_packet = observed_send_packet
    voice._spide_transport_stats = state
    return state


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
    source_value = (
        track.get("source") or track.get("sourceType") or
        track.get("provider") or track.get("platform") or "spotify"
    )
    if isinstance(source_value, dict):
        source_value = (
            source_value.get("name") or source_value.get("type") or
            source_value.get("kind") or "spotify"
        )
    source = str(source_value).strip().lower()
    source = source.replace("music", "").replace("_", "").replace("-", "")
    aliases = {
        "spotify": "spotify", "sp": "spotify",
        "youtube": "youtube", "yt": "youtube",
        "soundcloud": "soundcloud", "sc": "soundcloud",
    }
    provider = aliases.get(source)
    if not provider:
        raise ValueError("Не удалось определить источник трека Spide.")

    source_ids = track.get("sourceIds") or track.get("source_ids") or {}
    if not isinstance(source_ids, dict):
        source_ids = {}
    provider_id = source_ids.get(provider) or source_ids.get(provider.lower())
    track_id = str(
        track.get("id") or track.get("trackId") or track.get("track_id") or
        track.get("sourceId") or track.get("source_id") or provider_id or
        track.get(f"{provider}Id") or track.get(f"{provider}_id") or ""
    ).strip()
    if not track_id:
        raise ValueError("В ответе поиска Spide отсутствует ID трека.")

    if provider in ("spotify", "youtube"):
        path = f"/{provider}/tracks/{urllib.parse.quote(track_id, safe='')}/stream"
        url = api_base.rstrip("/") + path
    else:
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
        self.last_errors = {}

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
                await self._start_next(guild.id, voice, raise_on_error=True)
            return (1 if guild.id in self.current else 0) + len(queue)

    async def _start_next(self, guild_id, voice=None, *, raise_on_error=False):
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
            ffmpeg_executable = imageio_ffmpeg.get_ffmpeg_exe()
            audio = discord.FFmpegPCMAudio(
                track.stream_url,
                executable=ffmpeg_executable,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn",
            )
            measured = AudioDiagnosticsSource(audio)
            source = discord.PCMVolumeTransformer(
                measured, volume=self.volumes[guild_id]
            )
            loop = self.bot.loop
            transport = install_voice_transport_diagnostics(voice, guild_id)
            connection = getattr(voice, "_connection", None)
            logging.info(
                "VOICE TRANSPORT STATE guild=%s discord_py=%s opus_loaded=%s "
                "mode=%s dave_session=%s can_encrypt=%s",
                guild_id, getattr(discord, "__version__", "unknown"),
                discord.opus.is_loaded(), getattr(voice, "mode", None),
                bool(getattr(connection, "dave_session", None)),
                getattr(connection, "can_encrypt", None),
            )

            def after_play(error):
                logging.info(
                    "Voice audio stats guild=%s title=%r pcm_frames=%s "
                    "non_silent_frames=%s peak_pcm=%s",
                    guild_id, track.title, measured.frames,
                    measured.nonzero_frames, measured.peak,
                )
                if transport is not None:
                    logging.info(
                        "VOICE UDP SUMMARY guild=%s title=%r packets_ok=%s "
                        "bytes_ok=%s socket_errors=%s",
                        guild_id, track.title, transport["track_packets"],
                        transport["track_bytes"], transport["track_errors"],
                    )
                    logging.info(
                        "VOICE DAVE SUMMARY guild=%s dave_packets=%s non_dave_packets=%s "
                        "speak_success=%s speak_errors=%s can_encrypt_final=%s",
                        guild_id, transport["dave_packets"], transport["plain_packets"],
                        transport["speak_success"], transport["speak_errors"],
                        getattr(getattr(voice, "_connection", None), "can_encrypt", None),
                    )
                if error:
                    self.last_errors[guild_id] = str(error)[:500]
                    logging.error(
                        "Voice playback failed in guild %s: %s",
                        guild_id, error,
                        exc_info=(type(error), error, error.__traceback__),
                    )
                asyncio.run_coroutine_threadsafe(self._start_next(guild_id), loop)

            ensure_opus_loaded()
            voice.play(source, after=after_play)
            logging.info(
                "Voice playback started guild=%s title=%r channel=%s is_playing=%s",
                guild_id, track.title, getattr(voice.channel, "id", None),
                voice.is_playing(),
            )
            self.last_errors.pop(guild_id, None)
        except Exception as exc:
            self.current.pop(guild_id, None)
            self.last_errors[guild_id] = str(exc)[:500]
            logging.exception("Could not start Spide stream in guild %s", guild_id)
            if raise_on_error:
                raise RuntimeError(
                    "Не удалось запустить аудиопоток. "
                    f"Подробность: {str(exc)[:300]}"
                ) from exc
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
        self.last_errors.pop(guild.id, None)
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
        last_error = self.last_errors.get(guild_id)
        if last_error:
            lines.append(f"⚠️ Последняя ошибка запуска: {last_error[:250]}")
        return "\n".join(lines) or "Очередь пуста."

    def set_volume(self, guild, volume):
        if not 0 <= volume <= 100:
            raise ValueError("Громкость должна быть от 0 до 100.")
        self.volumes[guild.id] = volume / 100
        voice = guild.voice_client
        if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = volume / 100
