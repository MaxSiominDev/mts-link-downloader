import json
import unittest
from unittest.mock import Mock, patch

from mtsdump import mix, vk
from mtsdump.cookies import CookieError
from mtsdump.links import VkVideoRef
from mtsdump.mix import Encoding

REF = VkVideoRef("vkvideo.ru", "111", "222", "ln-key")
TOKEN = [{"access_token": "tkn", "user_id": 1, "is_active": True}]
TOKEN_ERROR = {"error": True, "error_text": "access denied"}
ITEM = {
    "title": "Call",
    "date": 1791300000,
    "duration": 3336,
    "files": {"hls_ondemand": "https://cdn/hls.m3u8", "mp4_1080": "https://cdn/full.mp4"},
}
BY_IDS = {"response": {"items": [ITEM]}}


def body(data) -> bytes:
    return json.dumps(data).encode()


class FetchVideoTest(unittest.TestCase):
    @patch("mtsdump.vk.net.post", side_effect=[body(TOKEN), body(BY_IDS)])
    def test_fetches_the_token_and_then_the_video(self, post):
        video = vk.fetch_video(REF, lambda host: "a=1")
        self.assertEqual(video.session_id, "111_222")
        self.assertEqual(video.title, "Call")
        self.assertEqual(video.started_at, ITEM["date"])
        self.assertEqual(video.duration, ITEM["duration"])
        self.assertEqual(video.hls_url, "https://cdn/hls.m3u8")
        self.assertEqual(video.mp4_url, "https://cdn/full.mp4")
        token_url, videos_url = post.call_args_list[0].args[0], post.call_args_list[1].args[0]
        self.assertEqual(token_url, vk.WEB_TOKEN_URL)
        self.assertEqual(videos_url, vk.VIDEO_BY_IDS_URL)
        self.assertIn(b"videos=111_222_ln-key", post.call_args_list[1].args[1])
        self.assertIn(b"access_token=tkn", post.call_args_list[1].args[1])

    @patch("mtsdump.vk.net.post", side_effect=[body(TOKEN), body(BY_IDS)])
    def test_hosts_without_cookies_are_skipped(self, post):
        cookie_for = Mock(side_effect=[CookieError("no cookies"), "a=1", CookieError("no cookies")])
        self.assertEqual(vk.fetch_video(REF, cookie_for).title, "Call")
        post.assert_called()

    @patch("mtsdump.vk.net.post")
    def test_no_host_has_cookies(self, post):
        with self.assertRaisesRegex(vk.AccessDenied, "--cookies-from-browser"):
            vk.fetch_video(REF, Mock(side_effect=CookieError("no cookies")))
        post.assert_not_called()

    @patch("mtsdump.vk.net.post", side_effect=[body(TOKEN_ERROR), body(TOKEN), body(BY_IDS)])
    def test_the_first_token_post_only_walks_the_autologin_redirects(self, post):
        vk.fetch_video(REF, lambda host: "a=1")
        self.assertEqual(post.call_count, 3)

    @patch("mtsdump.vk.net.post", side_effect=[body(TOKEN_ERROR), body(TOKEN_ERROR)])
    def test_rejected_cookies(self, post):
        with self.assertRaisesRegex(vk.AccessDenied, "did not issue a token"):
            vk.fetch_video(REF, lambda host: "a=1")

    @patch("mtsdump.vk.net.post")
    def test_without_cookies(self, post):
        with self.assertRaisesRegex(vk.AccessDenied, "--cookies-from-browser"):
            vk.fetch_video(REF, lambda host: None)
        post.assert_not_called()

    @patch("mtsdump.vk.net.post", side_effect=[body(TOKEN), body({"error": {"error_msg": "bad video"}})])
    def test_the_api_error_message_reaches_the_user(self, post):
        with self.assertRaisesRegex(vk.ApiError, "bad video"):
            vk.fetch_video(REF, lambda host: "a=1")

    @patch("mtsdump.vk.net.post", side_effect=[body(TOKEN), body({"response": {"items": [{"files": {}}]}})])
    def test_a_video_without_hls(self, post):
        with self.assertRaisesRegex(vk.ApiError, "no HLS"):
            vk.fetch_video(REF, lambda host: "a=1")

    @patch(
        "mtsdump.vk.net.post",
        side_effect=[body(TOKEN), body({"response": {"items": [
            {"title": "half", "files": {"hls_ondemand": "https://cdn/x.m3u8"}},
        ]}})],
    )
    def test_an_item_missing_fields(self, post):
        with self.assertRaisesRegex(vk.ApiError, "unexpected video info"):
            vk.fetch_video(REF, lambda host: "a=1")


class CheapestRenditionTest(unittest.TestCase):
    MASTER = b"""#EXTM3U
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-STREAM-INF:BANDWIDTH=538112,RESOLUTION=424x240,CODECS="avc1.42001e,mp4a.40.2"
low/track.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=212992,RESOLUTION=256x144,CODECS="avc1.42001e,mp4a.40.2"
mobile/track.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3883008,RESOLUTION=1280x720,CODECS="avc1.640020,mp4a.40.2"
high/track.m3u8
"""

    def test_picks_the_lowest_bandwidth_and_joins_the_url(self):
        url = vk.cheapest_rendition(self.MASTER, "https://cdn/pr/43/ondemand/")
        self.assertEqual(url, "https://cdn/pr/43/ondemand/mobile/track.m3u8")

    def test_master_without_renditions(self):
        with self.assertRaisesRegex(vk.ApiError, "lists no renditions"):
            vk.cheapest_rendition(b"#EXTM3U\n", "https://cdn/")

    def test_a_variant_without_the_bandwidth_attribute(self):
        master = b'#EXTM3U\n#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=538112,RESOLUTION=424x240\nlow/track.m3u8\n'
        with self.assertRaisesRegex(vk.ApiError, "unexpected master playlist"):
            vk.cheapest_rendition(master, "https://cdn/")

    def test_average_bandwidth_does_not_shadow_the_declared_one(self):
        master = (
            b"#EXTM3U\n"
            b'#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=900000,BANDWIDTH=538112\nlow/track.m3u8\n'
        )
        self.assertEqual(vk.cheapest_rendition(master, "https://cdn/"), "https://cdn/low/track.m3u8")


class JarTest(unittest.TestCase):
    def test_cookies_are_scoped_to_their_host(self):
        jar = vk._jar({"vk.ru": "a=1; b=2", "vk.com": "", "login.vk.ru": "c=3"})
        by_host = {}
        for cookie in jar:
            by_host.setdefault(cookie.domain, []).append(cookie.name)
        self.assertEqual(by_host, {"vk.ru": ["a", "b"], "login.vk.ru": ["c"]})


class EncodeAudioTest(unittest.TestCase):
    def test_drops_video_and_encodes_mono_mp3(self):
        run = Mock()
        encoding = Encoding(sample_rate=16000, bitrate="32k", highpass=True)
        with patch("mtsdump.vk.ffmpeg.run", run):
            vk.encode_audio("https://cdn/track.m3u8", "out.mp3", 3336.0, encoding, Mock())
        args = run.call_args.args[0]
        self.assertEqual(run.call_args.args[1], 3336.0)
        self.assertEqual(args[args.index("-i") + 1], "https://cdn/track.m3u8")
        self.assertLess(args.index("-user_agent"), args.index("-i"))
        for flag, value in (("-vn", None), ("-map", "0:a:0"), ("-b:a", "32k"), ("-ar", "16000"), ("-f", "mp3")):
            position = args.index(flag)
            if value is not None:
                self.assertEqual(args[position + 1], value)
        self.assertEqual(args[args.index("-af") + 1], f"aformat=channel_layouts=mono,{mix.ASR_HIGHPASS},{mix.LIMITER}")

    def test_listening_keeps_the_limiter_without_the_highpass(self):
        encoding = Encoding(sample_rate=48000, bitrate="64k", highpass=False)
        with patch("mtsdump.vk.ffmpeg.run", Mock()) as run:
            vk.encode_audio("https://cdn/track.m3u8", "out.mp3", 10.0, encoding, Mock())
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("-af") + 1], f"aformat=channel_layouts=mono,{mix.LIMITER}")
