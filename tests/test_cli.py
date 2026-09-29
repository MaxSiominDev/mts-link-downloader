import argparse
import contextlib
import io
import logging
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import mtsdump
from mtsdump import api, cli, download, hls
from tests.support import read, write

SESSION = "22222222222"
OTHER_SESSION = "33333333333"
MAIN_FILE = "2026-09-10 08-59-15 Seminar.mp3"
SPEAKER_FILE = "2026-09-10 08-59-15 Seminar - Lecturer.mp3"


def namespace(**fields) -> argparse.Namespace:
    return argparse.Namespace(**{"cookie": None, "cookies_from_browser": None, **fields})


def meta(session_id: str) -> dict:
    lecturer = {"id": 1, "nickname": "Lecturer"}
    stream = {
        "id": 101,
        "url": "https://storage/101.mp4",
        "hlsUrl": "https://delivery/101.mp4/playlist.m3u8",
        "stream": {"conference": {"id": 11}},
    }
    start = {
        "module": "eventsession.start",
        "relativeTime": 0,
        "time": 0,
        "data": {},
        "snapshot": {"data": {"conference": [{"id": 11, "user": lecturer}]}},
    }
    return {
        "id": 1,
        "eventSession": {"id": int(session_id)},
        "name": "Seminar 2026-09-10 08:59:15",
        "duration": 100.0,
        "cuts": [],
        "convertedRecords": [],
        "eventLogs": [
            start,
            {"module": "mediasession.add", "relativeTime": 10.0, "data": stream},
            {"module": "mediasession.update", "relativeTime": 50.0, "data": {"id": 101, "duration": 40.0}},
        ],
    }


def resolve(streams, prefer_mp4, jobs):
    return [download.Source(stream, hls.MediaPlaylist("init", ("seg",), 40.0)) for stream in streams]


def fake_download(sources, work_dir, jobs, on_progress):
    files = []
    for source in sources:
        path = os.path.join(work_dir, f"{source.stream.id}.m4a")
        write(path, b"audio")
        files.append(download.AudioFile(source.stream, path, 40.0))
    return files


def fake_mix(tracks, output_path, encoding, work_dir, on_progress, pad_to=None, normalize_loudness=True):
    write(output_path, b"mp3")


class ProcessTest(unittest.TestCase):
    def setUp(self):
        self.output = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.output)
        self.addCleanup(logging.getLogger().handlers.clear)
        self.fetch_record = self.start(
            patch("mtsdump.api.fetch_record", side_effect=lambda ref, cookie_for: meta(ref.session_id))
        )
        self.resolve_sources = self.start(patch("mtsdump.download.resolve_sources", side_effect=resolve))
        self.mix = self.start(patch("mtsdump.mix.mix", side_effect=fake_mix))
        self.is_digital_silence = self.start(patch("mtsdump.verify.is_digital_silence", return_value=False))
        self.start(patch("mtsdump.download.download_all", side_effect=fake_download))
        self.start(patch("mtsdump.ffmpeg.require_tools"))

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def run_cli(self, *argv: str) -> int:
        with contextlib.redirect_stderr(io.StringIO()):
            return cli.main([*argv, "-o", self.output])

    def output_path(self, name: str) -> str:
        return os.path.join(self.output, name)

    def test_saves_the_mix_and_removes_the_work_folder(self):
        self.assertEqual(self.run_cli(SESSION), 0)
        self.assertEqual(read(self.output_path(MAIN_FILE)), b"mp3")
        self.assertFalse(os.path.exists(self.output_path(f"{SESSION}.parts")))

    def test_keep_leaves_the_downloaded_streams(self):
        self.run_cli(SESSION, "--keep")
        self.assertTrue(os.path.exists(self.output_path(f"{SESSION}.parts/101.m4a")))

    def test_existing_file_is_not_rebuilt(self):
        write(self.output_path(MAIN_FILE), b"old")
        self.assertEqual(self.run_cli(SESSION), 0)
        self.resolve_sources.assert_not_called()
        self.assertEqual(read(self.output_path(MAIN_FILE)), b"old")

    def test_a_failed_record_does_not_stop_the_next_one(self):
        self.fetch_record.side_effect = [api.AccessDenied("no cookies"), meta(OTHER_SESSION)]
        self.assertEqual(self.run_cli(SESSION, OTHER_SESSION), 1)
        self.assertTrue(os.path.exists(self.output_path(MAIN_FILE)))

    def test_ctrl_c_exits_with_130(self):
        self.resolve_sources.side_effect = KeyboardInterrupt
        self.assertEqual(self.run_cli(SESSION), 130)

    def test_split_speakers_adds_only_the_missing_tracks(self):
        write(self.output_path(MAIN_FILE), b"old")
        self.assertEqual(self.run_cli(SESSION, "--split-speakers"), 0)
        self.mix.assert_called_once()
        self.assertEqual(self.mix.call_args.kwargs["pad_to"], 50.0)
        self.assertFalse(self.mix.call_args.kwargs["normalize_loudness"])
        self.assertEqual(read(self.output_path(MAIN_FILE)), b"old")
        self.assertEqual(read(self.output_path(SPEAKER_FILE)), b"mp3")

    def test_split_speakers_builds_the_mix_and_the_tracks_in_one_run(self):
        self.assertEqual(self.run_cli(SESSION, "--split-speakers"), 0)
        main_call, speaker_call = self.mix.call_args_list
        self.assertEqual((main_call.kwargs["pad_to"], main_call.kwargs["normalize_loudness"]), (None, True))
        self.assertEqual((speaker_call.kwargs["pad_to"], speaker_call.kwargs["normalize_loudness"]), (50.0, False))
        self.assertTrue(os.path.exists(self.output_path(MAIN_FILE)))
        self.assertTrue(os.path.exists(self.output_path(SPEAKER_FILE)))

    def test_split_speakers_skips_participants_without_sound(self):
        write(self.output_path(MAIN_FILE), b"old")
        self.is_digital_silence.return_value = True
        with self.assertLogs("mtsdump", "INFO") as logs:
            self.assertEqual(self.run_cli(SESSION, "--split-speakers"), 0)
        self.mix.assert_not_called()
        self.assertFalse(os.path.exists(self.output_path(SPEAKER_FILE)))
        self.assertIn("no sound from Lecturer in the whole record, no track saved", "\n".join(logs.output))


class CookieSourceTest(unittest.TestCase):
    def test_strips_the_header_name_copied_from_devtools(self):
        cookie_for = cli._cookie_source(namespace(cookie="Cookie: access=1; refresh=2"))
        self.assertEqual(cookie_for("gw.mts-link.ru"), "access=1; refresh=2")

    def test_no_cookies_by_default(self):
        self.assertIsNone(cli._cookie_source(namespace())("gw.mts-link.ru"))


class PlatformTest(unittest.TestCase):
    def test_refuses_to_run_outside_macos(self):
        self.addCleanup(logging.getLogger().handlers.clear)
        with patch.object(cli.sys, "platform", "linux"), self.assertLogs("mtsdump", "ERROR"):
            self.assertEqual(cli.main([SESSION]), 1)


class ArgumentsTest(unittest.TestCase):
    def assert_usage_error(self, *argv: str) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.main(list(argv))
        self.assertEqual(caught.exception.code, 2)

    def test_rejects_links_that_are_not_records(self):
        self.assert_usage_error("https://my.mts-link.ru/j/Org/11111111111")

    def test_rejects_unknown_name_placeholders(self):
        self.assert_usage_error("123", "--name", "{title}")
        self.assert_usage_error("123", "--name", "{date.year}")

    def test_rejects_browsers_other_than_chrome(self):
        self.assert_usage_error("123", "--cookies-from-browser", "firefox")

    def test_rejects_zero_jobs(self):
        self.assert_usage_error("123", "-j", "0")

    def test_version(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            cli.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"mtsdump {mtsdump.__version__}")


class CoveredSecondsTest(unittest.TestCase):
    def test_overlapping_intervals_count_once(self):
        self.assertEqual(cli._covered_seconds([(0, 10), (5, 15), (20, 25)]), 20)

    def test_nested_interval_adds_nothing(self):
        self.assertEqual(cli._covered_seconds([(0, 10), (2, 3)]), 10)

    def test_no_intervals(self):
        self.assertEqual(cli._covered_seconds([]), 0)


class ClockTest(unittest.TestCase):
    def test_rounding_never_shows_60_seconds(self):
        self.assertEqual(cli._clock(3599.6), "1:00:00")

    def test_decimals(self):
        self.assertEqual(cli._clock(3723.26, decimals=1), "1:02:03.3")
