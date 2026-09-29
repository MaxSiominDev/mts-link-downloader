import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from mtsdump import download, ffmpeg, hls, net
from mtsdump.record import Stream
from tests.support import AUDIO, CAMERA_MASTER, MASTER_URL, MUXED_MASTER, SCREEN_MASTER, read, write

STREAM = Stream(1, 0.0, None, "https://storage/1.mp4", MASTER_URL, None)


def hls_source(stream_id: int, segments: int) -> download.Source:
    playlist = hls.MediaPlaylist("init", tuple(f"seg{index}" for index in range(1, segments + 1)), float(segments))
    return download.Source(Stream(stream_id, 0.0, None, "mp4", "hls", None), playlist)


def fake_get(url, headers=None, cancel=None):
    return url.encode()


def get_failing_on(bad_url):
    def get(url, headers=None, cancel=None):
        if url == bad_url:
            raise net.HttpStatusError(url, 404)
        return url.encode()

    return get


class ResolveSourcesTest(unittest.TestCase):
    @patch("mtsdump.download.net.get", side_effect=[CAMERA_MASTER.encode(), AUDIO.encode()])
    def test_prefers_the_hls_audio_rendition(self, get):
        (source,) = download.resolve_sources((STREAM,), prefer_mp4=False, jobs=1)
        self.assertEqual(source.playlist.init_url, "https://delivery.example/record/2026/09/10/abc.mp4/a1/init/1.m4s")

    @patch("mtsdump.download.ffmpeg.has_audio")
    @patch("mtsdump.download.net.get", return_value=SCREEN_MASTER.encode())
    def test_screen_sharing_has_no_audio(self, get, has_audio):
        self.assertEqual(download.resolve_sources((STREAM,), prefer_mp4=False, jobs=1), [None])
        has_audio.assert_not_called()

    @patch("mtsdump.download.ffmpeg.has_audio", return_value=True)
    @patch("mtsdump.download.net.get", return_value=MUXED_MASTER.encode())
    def test_audio_muxed_into_the_video_comes_from_the_mp4(self, get, has_audio):
        (source,) = download.resolve_sources((STREAM,), prefer_mp4=False, jobs=1)
        self.assertIsNone(source.playlist)
        has_audio.assert_called_once_with("https://storage/1.mp4")

    @patch("mtsdump.download.ffmpeg.has_audio", return_value=True)
    @patch("mtsdump.download.net.get", side_effect=net.NetworkError("connection refused"))
    def test_falls_back_to_the_mp4_when_hls_fails(self, get, has_audio):
        with self.assertLogs("mtsdump.download", "WARNING"):
            (source,) = download.resolve_sources((STREAM,), prefer_mp4=False, jobs=1)
        self.assertIsNone(source.playlist)
        has_audio.assert_called_once_with("https://storage/1.mp4")

    @patch("mtsdump.download.ffmpeg.has_audio", return_value=True)
    @patch("mtsdump.download.net.get", side_effect=[CAMERA_MASTER.encode(), b"#EXTM3U\n#EXT-X-ENDLIST\n"])
    def test_falls_back_to_the_mp4_when_the_audio_playlist_is_unusable(self, get, has_audio):
        with self.assertLogs("mtsdump.download", "WARNING"):
            (source,) = download.resolve_sources((STREAM,), prefer_mp4=False, jobs=1)
        self.assertIsNone(source.playlist)
        has_audio.assert_called_once_with("https://storage/1.mp4")

    @patch("mtsdump.download.ffmpeg.has_audio", return_value=True)
    @patch("mtsdump.download.net.get", return_value=b"\xff\xfe<html>busy</html>")
    def test_a_page_that_is_not_a_playlist_falls_back_to_the_mp4(self, get, has_audio):
        with self.assertLogs("mtsdump.download", "WARNING"):
            (source,) = download.resolve_sources((STREAM,), prefer_mp4=False, jobs=1)
        self.assertIsNone(source.playlist)
        has_audio.assert_called_once_with("https://storage/1.mp4")

    def test_a_failure_stops_the_other_streams(self):
        first = Stream(1, 0.0, None, "https://storage/1.mp4", "https://delivery/1.mp4/playlist.m3u8", None)
        second = Stream(2, 0.0, None, "https://storage/2.mp4", "https://delivery/2.mp4/playlist.m3u8", None)

        def get(url, headers=None, cancel=None):
            if url == first.hls_url:
                raise net.HttpStatusError(url, 404)
            cancel.wait(5)
            raise net.NetworkError("connection dropped")

        with (
            patch("mtsdump.download.net.get", side_effect=get),
            patch("mtsdump.download.ffmpeg.has_audio", side_effect=ffmpeg.FfmpegError("mp4 unreachable")) as has_audio,
            self.assertLogs("mtsdump.download", "WARNING"),
            self.assertRaises(ffmpeg.FfmpegError),
        ):
            download.resolve_sources((first, second), prefer_mp4=False, jobs=2)
        has_audio.assert_called_once_with("https://storage/1.mp4")

    @patch("mtsdump.download.ffmpeg.has_audio", return_value=True)
    @patch("mtsdump.download.net.get")
    def test_mp4_mode_does_not_touch_hls(self, get, has_audio):
        (source,) = download.resolve_sources((STREAM,), prefer_mp4=True, jobs=1)
        self.assertIsNone(source.playlist)
        get.assert_not_called()


@patch("mtsdump.download.ffmpeg.probe_duration", return_value=12.5)
class DownloadAllTest(unittest.TestCase):
    def setUp(self):
        self.work_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work_dir)

    def run_download(self, sources, jobs=3):
        return download.download_all(sources, self.work_dir, jobs=jobs, on_progress=lambda done, total, size: None)

    @patch("mtsdump.download.net.get", side_effect=fake_get)
    def test_joins_init_and_segments_in_playlist_order(self, get, probe):
        (file,) = self.run_download([hls_source(7, 3)])
        self.assertEqual(file.path, os.path.join(self.work_dir, "7.m4a"))
        self.assertEqual(file.duration, 12.5)
        self.assertEqual(read(file.path), b"initseg1seg2seg3")
        self.assertFalse(os.path.exists(os.path.join(self.work_dir, "7")))

    @patch("mtsdump.download.net.get", side_effect=fake_get)
    def test_reuses_parts_saved_by_an_earlier_run(self, get, probe):
        os.makedirs(os.path.join(self.work_dir, "7"))
        write(os.path.join(self.work_dir, "7", "00001"), b"kept1")
        (file,) = self.run_download([hls_source(7, 3)])
        self.assertEqual(read(file.path), b"initkept1seg2seg3")
        self.assertNotIn("seg1", [call.args[0] for call in get.call_args_list])

    @patch("mtsdump.download.net.get", side_effect=fake_get)
    def test_skips_streams_that_are_already_joined(self, get, probe):
        write(os.path.join(self.work_dir, "7.m4a"), b"joined")
        self.run_download([hls_source(7, 3)])
        get.assert_not_called()

    @patch("mtsdump.download.net.get", side_effect=get_failing_on("seg2"))
    def test_names_the_stream_that_failed(self, get, probe):
        with self.assertRaisesRegex(download.DownloadError, "stream 7"):
            self.run_download([hls_source(7, 3)])

    def test_a_failure_cancels_the_remaining_parts(self, probe):
        delivered = []

        def get(url, headers=None, cancel=None):
            if url == "seg1":
                raise net.HttpStatusError(url, 404)
            if url != "init" and cancel.wait(5):
                raise net.Cancelled()
            delivered.append(url)
            return url.encode()

        with patch("mtsdump.download.net.get", side_effect=get), self.assertRaises(download.DownloadError):
            self.run_download([hls_source(7, 3)], jobs=1)
        self.assertEqual(delivered, ["init"])

    @patch("mtsdump.download.net.download")
    def test_mp4_source_is_downloaded_whole(self, download_file, probe):
        download_file.side_effect = lambda url, path, on_progress, cancel: write(path, b"mp4")
        source = download.Source(Stream(8, 0.0, 5.0, "https://storage/8.mp4", None, None), None)
        (file,) = self.run_download([source])
        self.assertEqual(file.path, os.path.join(self.work_dir, "8.mp4"))
        self.assertEqual(download_file.call_args.args[:2], ("https://storage/8.mp4", file.path))
