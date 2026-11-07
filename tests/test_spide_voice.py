import unittest
from urllib.parse import urlparse, parse_qs

from spide_voice import resolve_spide_stream


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


if __name__ == "__main__":
    unittest.main()
