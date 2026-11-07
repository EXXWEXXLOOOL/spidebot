import asyncio
import collections
import unittest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

from spide_voice import AudioDiagnosticsSource, GuildVoicePlayer, QueuedTrack, resolve_spide_stream


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class SpideStreamResolverTests(unittest.TestCase):
    def test_spotify_track_route_and_stream_url(self):
        seen = {}

        def fake_get(url, credential, **kwargs):
            seen["url"] = url
            seen["credential"] = credential
            return FakeResponse({"url": "https://cdn.example.test/audio.mp3"})

        result = resolve_spide_stream(
            {"id": "spotify-id", "source": "spotify"},
            "access-token",
            fake_get,
            "https://api.spidemusic.com/v1",
        )
        self.assertEqual(result, "https://cdn.example.test/audio.mp3")
        self.assertEqual(
            seen["url"],
            "https://api.spidemusic.com/v1/spotify/tracks/spotify-id/stream",
        )
        self.assertEqual(seen["credential"], "access-token")

    def test_youtube_route(self):
        def fake_get(url, credential, **kwargs):
            self.assertEqual(
                url,
                "https://api.spidemusic.com/v1/youtube/tracks/youtube-id/stream",
            )
            return FakeResponse({"data": {"streamUrl": "https://cdn.example.test/youtube.m4a"}})

        result = resolve_spide_stream(
            {"id": "youtube-id", "source": "youtube"},
            "access-token",
            fake_get,
            "https://api.spidemusic.com/v1",
        )
        self.assertEqual(result, "https://cdn.example.test/youtube.m4a")

    def test_soundcloud_route(self):
        def fake_get(url, credential, **kwargs):
            parsed = urlparse(url)
            self.assertEqual(parsed.path, "/v1/soundcloud/stream")
            self.assertEqual(parse_qs(parsed.query), {"trackId": ["sc-id"]})
            return FakeResponse({"stream": {"url": "https://cdn.example.test/sc.mp3"}})

        result = resolve_spide_stream(
            {"id": "sc-id", "source": "soundcloud"},
            "access-token",
            fake_get,
            "https://api.spidemusic.com/v1",
        )
        self.assertEqual(result, "https://cdn.example.test/sc.mp3")

    def test_rejects_payload_without_https_stream_url(self):
        def fake_get(url, credential, **kwargs):
            return FakeResponse({"url": "http://insecure.example.test/audio.mp3"})

        with self.assertRaisesRegex(RuntimeError, "нет HTTPS-ссылки"):
            resolve_spide_stream(
                {"id": "spotify-id", "source": "spotify"},
                "access-token",
                fake_get,
                "https://api.spidemusic.com/v1",
            )



    def test_audio_diagnostics_detects_non_silent_pcm(self):
        class FakeSource:
            def __init__(self):
                self.chunks = [b"\\x00\\x00\\x10\\x00", b"",]

            def read(self):
                return self.chunks.pop(0)

            def cleanup(self):
                pass

        measured = AudioDiagnosticsSource(FakeSource())
        self.assertEqual(measured.read(), b"\\x00\\x00\\x10\\x00")
        self.assertEqual(measured.read(), b"")
        self.assertEqual(measured.frames, 1)
        self.assertEqual(measured.nonzero_frames, 1)
        self.assertEqual(measured.peak, 16)

    def test_player_reports_ffmpeg_startup_failure(self):
        class FakeVoice:
            def is_connected(self):
                return True

            def play(self, source, after=None):
                raise RuntimeError("ffmpeg executable not found")

        class FakeGuild:
            id = 123
            voice_client = FakeVoice()

        class FakeBot:
            loop = asyncio.new_event_loop()

            def get_guild(self, guild_id):
                return FakeGuild()

        async def run_test():
            player = GuildVoicePlayer(FakeBot())
            player.queues[123] = collections.deque([
                QueuedTrack("Test", "https://cdn.example.test/audio.mp3")
            ])
            with patch("spide_voice.imageio_ffmpeg.get_ffmpeg_exe", return_value="/bundled/ffmpeg") as get_ffmpeg, patch(
                "spide_voice.discord.FFmpegPCMAudio", return_value=object()
            ) as ffmpeg_audio, patch(
                "spide_voice.discord.PCMVolumeTransformer", return_value=object()
            ):
                with self.assertRaisesRegex(RuntimeError, "FFmpeg не смог запустить"):
                    await player._start_next(123, raise_on_error=True)
                get_ffmpeg.assert_called_once_with()
                self.assertEqual(ffmpeg_audio.call_args.kwargs["executable"], "/bundled/ffmpeg")
            self.assertIn("ffmpeg executable not found", player.last_errors[123])
            FakeBot.loop.close()

        asyncio.run(run_test())

if __name__ == "__main__":
    unittest.main()
