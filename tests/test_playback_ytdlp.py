import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from paragon.playback import PlaybackCog, _PlayRequest


class PlaybackYtDlpTests(unittest.TestCase):
    def setUp(self):
        self.cog = PlaybackCog(SimpleNamespace())

    def test_youtube_tuning_is_applied_without_cookies(self):
        tuned = self.cog._apply_youtube_tuning(
            "https://www.youtube.com/watch?v=test",
            {"quiet": True},
        )

        self.assertEqual(tuned["js_runtimes"], {"node": {}})
        self.assertIn("ejs:github", tuned["remote_components"])
        self.assertNotIn("cookiefile", tuned)
        self.assertNotIn("cookiesfrombrowser", tuned)

    def test_metadata_retries_with_cookie_file_after_plain_failure(self):
        info = {
            "title": "Test Track",
            "duration": 120,
            "webpage_url": "https://www.youtube.com/watch?v=test",
            "original_url": "https://www.youtube.com/watch?v=test",
            "uploader": "Test Channel",
        }
        extract = Mock(side_effect=[RuntimeError("plain failed"), info])
        self.cog._extract_info_with_ytdlp = extract

        with (
            patch("paragon.playback.YoutubeDL", object()),
            patch("paragon.playback.YTDLP_COOKIE_FILE", "/tmp/cookies.txt"),
            patch("paragon.playback.YTDLP_COOKIES_FROM_BROWSER", ""),
            patch("paragon.playback.PLAY_DEBUG", False),
        ):
            result = self.cog._extract_track_request("https://www.youtube.com/watch?v=test")

        self.assertEqual(result[1], "Test Track")
        self.assertEqual(extract.call_count, 2)
        plain_opts = extract.call_args_list[0].kwargs["opts"]
        auth_opts = extract.call_args_list[1].kwargs["opts"]
        self.assertNotIn("cookiefile", plain_opts)
        self.assertEqual(auth_opts["cookiefile"], "/tmp/cookies.txt")
        self.assertEqual(plain_opts["js_runtimes"], {"node": {}})
        self.assertEqual(auth_opts["js_runtimes"], {"node": {}})
        self.assertTrue(plain_opts["noprogress"])
        self.assertTrue(auth_opts["noprogress"])
        self.assertNotIn("no_warnings", plain_opts)
        self.assertNotIn("no_warnings", auth_opts)

    def test_download_retries_with_cookie_file_after_plain_failure(self):
        attempts = []

        def fake_download(_source, *, opts, temp_dir):
            attempts.append(opts)
            if len(attempts) == 1:
                raise RuntimeError("plain failed")
            path = os.path.join(temp_dir, "track.webm")
            with open(path, "wb") as output:
                output.write(b"test audio")
            return {"duration": 120}, path

        request = _PlayRequest(
            ctx=SimpleNamespace(),
            source_url="https://www.youtube.com/watch?v=test",
            title="Test Track",
            duration_seconds=120,
            duration_known=True,
            playback_speed=1.0,
            webpage_url="https://www.youtube.com/watch?v=test",
            uploader="Test Channel",
            mode="ytdlp",
            requester_id=1,
            requester_name="Tester",
            target_channel_id=2,
            target_channel_name="Voice",
            text_channel_id=3,
            enqueued_at=0.0,
        )

        with tempfile.TemporaryDirectory(prefix="paragon_ytdlp_test_") as temp_dir:
            with (
                patch("paragon.playback.YoutubeDL", object()),
                patch("paragon.playback.YTDLP_COOKIE_FILE", "/tmp/cookies.txt"),
                patch("paragon.playback.YTDLP_COOKIES_FROM_BROWSER", ""),
                patch("paragon.playback.PLAY_DEBUG", False),
                patch("paragon.playback.tempfile.mkdtemp", return_value=temp_dir),
                patch.object(self.cog, "_download_with_ytdlp", side_effect=fake_download),
            ):
                prepared = self.cog._download_via_ytdlp(request)

            self.assertEqual(prepared.temp_path, os.path.join(temp_dir, "track.webm"))
            self.assertEqual(len(attempts), 2)
            self.assertNotIn("cookiefile", attempts[0])
            self.assertEqual(attempts[1]["cookiefile"], "/tmp/cookies.txt")
            self.assertEqual(attempts[0]["js_runtimes"], {"node": {}})
            self.assertEqual(attempts[1]["js_runtimes"], {"node": {}})
            self.assertTrue(attempts[0]["noprogress"])
            self.assertTrue(attempts[1]["noprogress"])
            self.assertNotIn("no_warnings", attempts[0])
            self.assertNotIn("no_warnings", attempts[1])


if __name__ == "__main__":
    unittest.main()
