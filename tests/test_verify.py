import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mtsdump import verify
from mtsdump.download import AudioFile
from mtsdump.record import Stream
from tests.support import read, write

FFMPEG_LOG = """[silencedetect @ 0x6000] silence_start: -0.021
[silencedetect @ 0x6000] silence_end: 3.5 | silence_duration: 3.521
[silencedetect @ 0x6000] silence_start: 10.25
[Parsed_volumedetect_1 @ 0x6001] mean_volume: -24.3 dB
[Parsed_volumedetect_1 @ 0x6001] max_volume: -1.5 dB
"""


class ParseLevelsTest(unittest.TestCase):
    def test_reads_silences_and_volume(self):
        levels = verify._parse_levels(FFMPEG_LOG, duration=12.0)
        self.assertEqual(levels.silences, [(0.0, 3.5), (10.25, 12.0)])
        self.assertEqual((levels.mean_db, levels.max_db), (-24.3, -1.5))

    def test_reads_inf_volumes(self):
        levels = verify._parse_levels("mean_volume: -inf dB\nmax_volume: -inf dB\n", duration=5.0)
        self.assertEqual((levels.mean_db, levels.max_db), (float("-inf"), float("-inf")))


class UnexplainedSilencesTest(unittest.TestCase):
    def test_gap_between_streams_is_expected(self):
        self.assertEqual(verify._unexplained_silences([(10, 20)], [(0, 10, []), (20, 10, [])]), [])

    def test_muted_stream_is_expected(self):
        self.assertEqual(verify._unexplained_silences([(10, 20)], [(0, 30, [(9, 21)])]), [])

    def test_sound_under_a_silent_mix_is_reported(self):
        self.assertEqual(verify._unexplained_silences([(10, 20)], [(5, 30, [])]), [(10, 20)])

    def test_stream_offset_applies_to_its_own_silences(self):
        self.assertEqual(verify._unexplained_silences([(120, 140)], [(100, 100, [(0, 50)])]), [])

    def test_short_overlap_at_the_edge_is_tolerated(self):
        self.assertEqual(verify._unexplained_silences([(10, 20)], [(0, 10.3, [])]), [])


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class MeasureTest(unittest.TestCase):
    def test_a_hole_in_the_timestamps_counts_as_silence(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        parts = []
        for name, offset in (("first.ts", 0), ("second.ts", 3)):
            path = os.path.join(directory, name)
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac",
                    "-muxdelay", "0", "-muxpreload", "0", "-output_ts_offset", str(offset), path,
                ],
                check=True,
            )
            parts.append(read(path))
        joined = os.path.join(directory, "joined.ts")
        write(joined, b"".join(parts))
        (hole,) = verify._measure(joined, duration=4.0, min_silence=0.5).silences
        self.assertAlmostEqual(hole[0], 1.0, delta=0.15)
        self.assertAlmostEqual(hole[1], 3.0, delta=0.15)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class IsDigitalSilenceTest(unittest.TestCase):
    def test_tells_digital_silence_from_a_tone(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        silent, tone = os.path.join(directory, "silent.m4a"), os.path.join(directory, "tone.m4a")
        for source, path in (("anullsrc", silent), ("sine", tone)):
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", source, "-t", "1", "-c:a", "aac", path], check=True)
        self.assertTrue(verify.is_digital_silence(silent))
        self.assertFalse(verify.is_digital_silence(tone))


class VerifyTest(unittest.TestCase):
    @patch("mtsdump.verify.ffmpeg.probe_duration", return_value=15.0)
    @patch("mtsdump.verify._measure")
    def test_names_the_silent_streams(self, measure, probe):
        levels = {
            "mix.mp3": verify.Levels(-19.0, -1.5, [(0.0, 5.0)]),
            "muted.m4a": verify.Levels(-91.0, -91.0, [(0.0, 10.0)]),
            "talk.m4a": verify.Levels(-30.0, -3.0, []),
        }
        measure.side_effect = lambda path, duration, min_silence: levels[path]
        files = [
            AudioFile(Stream(1, 0.0, 10.0, "https://storage/1.mp4", None, None), "muted.m4a", 10.0),
            AudioFile(Stream(2, 5.0, 10.0, "https://storage/2.mp4", None, None), "talk.m4a", 10.0),
        ]
        report = verify.verify("mix.mp3", files, jobs=2)
        self.assertEqual(report.silent_stream_ids, [1])
        self.assertEqual(report.expected_length, 15.0)
        self.assertEqual(report.unexplained_silences, [])
        expected_calls = [
            ("mix.mp3", 15.0, verify.MIX_MIN_SILENCE_SECONDS),
            ("muted.m4a", 10.0, verify.STREAM_MIN_SILENCE_SECONDS),
            ("talk.m4a", 10.0, verify.STREAM_MIN_SILENCE_SECONDS),
        ]
        self.assertEqual(sorted(call.args for call in measure.call_args_list), expected_calls)
