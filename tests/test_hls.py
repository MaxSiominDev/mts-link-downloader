import unittest

from mtsdump.hls import PlaylistError, audio_rendition_url, parse_media_playlist
from tests.support import AUDIO, AUDIO_URL, CAMERA_MASTER, MASTER_URL, SCREEN_MASTER

AUDIO_BASE = "https://delivery.example/record/2026/09/10/abc.mp4/a1/"


class AudioRenditionTest(unittest.TestCase):
    def test_resolves_the_audio_rendition(self):
        self.assertEqual(audio_rendition_url(CAMERA_MASTER, MASTER_URL), AUDIO_URL)

    def test_attribute_order_does_not_matter(self):
        master = '#EXTM3U\n#EXT-X-MEDIA:URI="a1/index.m3u8",GROUP-ID="audio",TYPE=AUDIO\n'
        self.assertEqual(audio_rendition_url(master, MASTER_URL), AUDIO_URL)

    def test_screen_sharing_has_no_audio(self):
        self.assertIsNone(audio_rendition_url(SCREEN_MASTER, MASTER_URL))

    def test_a_page_that_is_not_a_playlist(self):
        with self.assertRaises(PlaylistError):
            audio_rendition_url("<html>Service unavailable</html>", MASTER_URL)


class MediaPlaylistTest(unittest.TestCase):
    def test_parses_init_segments_and_duration(self):
        playlist = parse_media_playlist(AUDIO, AUDIO_URL)
        self.assertEqual(playlist.init_url, AUDIO_BASE + "init/1.m4s")
        self.assertEqual(playlist.segment_urls, (AUDIO_BASE + "media/1.m4s", AUDIO_BASE + "media/2.m4s"))
        self.assertAlmostEqual(playlist.duration, 15.208)

    def test_rejects_an_unfinished_playlist(self):
        with self.assertRaises(PlaylistError):
            parse_media_playlist(AUDIO.replace("#EXT-X-ENDLIST\n", ""), AUDIO_URL)

    def test_rejects_byte_ranges_and_encryption(self):
        for extra in ("#EXT-X-BYTERANGE:1000@0\n", '#EXT-X-KEY:METHOD=AES-128,URI="key"\n'):
            with self.subTest(extra=extra), self.assertRaises(PlaylistError):
                parse_media_playlist(AUDIO.replace("#EXTINF:11.626,\n", "#EXTINF:11.626,\n" + extra), AUDIO_URL)
        with self.assertRaises(PlaylistError):
            parse_media_playlist(AUDIO.replace('URI="init/1.m4s"', 'URI="init/1.m4s",BYTERANGE="650@0"'), AUDIO_URL)

    def test_accepts_tags_that_only_look_like_trouble(self):
        header = "#EXT-X-MEDIA-SEQUENCE:1\n"
        text = AUDIO.replace(header, header + "#EXT-X-DISCONTINUITY-SEQUENCE:0\n#EXT-X-KEY:METHOD=NONE\n")
        self.assertEqual(len(parse_media_playlist(text, AUDIO_URL).segment_urls), 2)

    def test_rejects_a_malformed_duration(self):
        with self.assertRaises(PlaylistError):
            parse_media_playlist(AUDIO.replace("#EXTINF:3.582,", "#EXTINF:abc,"), AUDIO_URL)

    def test_rejects_discontinuities_and_a_second_init_section(self):
        for extra in ("#EXT-X-DISCONTINUITY\n", '#EXT-X-MAP:URI="init/2.m4s"\n'):
            with self.subTest(extra=extra), self.assertRaises(PlaylistError):
                parse_media_playlist(AUDIO.replace("#EXTINF:3.582,\n", extra + "#EXTINF:3.582,\n"), AUDIO_URL)

    def test_rejects_a_playlist_without_an_init_section(self):
        with self.assertRaises(PlaylistError):
            parse_media_playlist(AUDIO.replace('#EXT-X-MAP:URI="init/1.m4s"\n', ""), AUDIO_URL)
