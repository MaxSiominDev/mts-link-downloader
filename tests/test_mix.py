import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mtsdump import ffmpeg, mix, verify

LISTENING = mix.Encoding(sample_rate=48000, bitrate="64k", highpass=False)
ASR = mix.Encoding(sample_rate=16000, bitrate="32k", highpass=True)
MASTERING = f"{mix.LOUDNESS},{mix.LIMITER}"


def graph_of(args: list[str]) -> str:
    return args[args.index("-filter_complex") + 1]


def inputs_of(args: list[str]) -> list[str]:
    return [args[index + 1] for index, argument in enumerate(args) if argument == "-i"]


def tracks(count: int) -> list[mix.Track]:
    return [mix.Track(f"{index}.m4a", float(index), 10.0) for index in range(count)]


class MixGraphTest(unittest.TestCase):
    def test_delays_each_track_and_sums_without_normalizing(self):
        graph = mix._mix_graph([mix.Track("a.m4a", 0.0, 5.0), mix.Track("b.m4a", 12.3456, 5.0)], "loudnorm")
        first, second, mixed = graph.split(";")
        self.assertEqual(
            first,
            "[0:a]aresample=async=1:first_pts=0,"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=mono,adelay=0:all=1[a0]",
        )
        self.assertTrue(second.endswith("adelay=12346:all=1[a1]"))
        self.assertEqual(mixed, "[a0][a1]amix=inputs=2:duration=longest:normalize=0,loudnorm[out]")

    def test_without_a_tail_the_sum_is_the_output(self):
        graph = mix._mix_graph([mix.Track("a.m4a", 1.0, 5.0)], "")
        self.assertTrue(graph.endswith("[a0]amix=inputs=1:duration=longest:normalize=0[out]"))


@patch("mtsdump.mix.ffmpeg.run")
class MixTest(unittest.TestCase):
    def test_single_pass_up_to_50_tracks(self, run):
        mix.mix(tracks(50), "out.mp3", LISTENING, "work", lambda stage, fraction: None)
        run.assert_called_once()
        args, length, _ = run.call_args.args
        self.assertEqual(len(inputs_of(args)), 50)
        self.assertEqual(length, 59.0)
        self.assertTrue(graph_of(args).endswith(f"normalize=0,{MASTERING},aresample=48000[out]"))
        self.assertEqual(args[-5:], ["-c:a", "libmp3lame", "-b:a", "64k", "out.mp3"])

    def test_premixes_groups_above_50_tracks(self, run):
        mix.mix(tracks(51), "out.mp3", LISTENING, "work", lambda stage, fraction: None)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual([len(inputs_of(args)) for args in calls], [20, 20, 11, 3])
        self.assertEqual(calls[0][-5:], ["-c:a", "wavpack", "-sample_fmt", "fltp", "work/group1.wv"])
        self.assertNotIn("loudnorm", graph_of(calls[0]))
        self.assertIn("adelay=20000:all=1", graph_of(calls[1]))
        self.assertEqual(inputs_of(calls[-1]), ["work/group1.wv", "work/group2.wv", "work/group3.wv"])
        self.assertEqual(graph_of(calls[-1]).count("adelay=0:all=1"), 3)

    def test_asr_adds_a_highpass_and_downsamples(self, run):
        mix.mix(tracks(2), "out.mp3", ASR, "work", lambda stage, fraction: None)
        args = run.call_args.args[0]
        self.assertTrue(graph_of(args).endswith(f"normalize=0,highpass=f=70,{MASTERING},aresample=16000[out]"))
        self.assertEqual(args[args.index("-b:a") + 1], "32k")

    def test_without_loudness_normalization_only_the_limiter_runs(self, run):
        mix.mix(tracks(2), "out.mp3", LISTENING, "work", lambda stage, fraction: None, normalize_loudness=False)
        graph = graph_of(run.call_args.args[0])
        self.assertTrue(graph.endswith(f"normalize=0,{mix.LIMITER},aresample=48000[out]"))
        self.assertNotIn("loudnorm", graph)

    def test_pads_a_speaker_track_to_the_full_length(self, run):
        mix.mix(tracks(2), "out.mp3", LISTENING, "work", lambda stage, fraction: None, pad_to=3600.0)
        args, length, _ = run.call_args.args
        self.assertIn(f"normalize=0,apad=whole_dur=3600.0,{MASTERING}", graph_of(args))
        self.assertEqual(length, 3600.0)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
# Two 1 s tones at 0 s and 3 s, so the mix has a 2 s gap in the middle.
class RealMixTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.tracks = [self.tone("low.m4a", 440, start=0.0), self.tone("high.m4a", 880, start=3.0)]
        self.output = os.path.join(self.directory, "out.mp3")

    def tone(self, name: str, frequency: int, start: float) -> mix.Track:
        path = os.path.join(self.directory, name)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=1", "-c:a", "aac", path],
            check=True,
        )
        return mix.Track(path, start, ffmpeg.probe_duration(path))

    def assert_tones_at_their_offsets(self):
        duration = ffmpeg.probe_duration(self.output)
        self.assertAlmostEqual(duration, 4.0, delta=0.1)
        levels = verify._measure(self.output, duration, min_silence=0.5)
        (gap,) = levels.silences
        self.assertAlmostEqual(gap[0], 1.0, delta=0.15)
        self.assertAlmostEqual(gap[1], 3.0, delta=0.15)

    def test_single_pass(self):
        with self.assertNoLogs("mtsdump.ffmpeg", "WARNING"):
            mix.mix(self.tracks, self.output, LISTENING, self.directory, lambda stage, fraction: None)
        self.assert_tones_at_their_offsets()

    def test_premixed_groups(self):
        with (
            patch.object(mix, "SINGLE_PASS_LIMIT", 1),
            patch.object(mix, "GROUP_SIZE", 1),
            self.assertNoLogs("mtsdump.ffmpeg", "WARNING"),
        ):
            mix.mix(self.tracks, self.output, LISTENING, self.directory, lambda stage, fraction: None)
        self.assert_tones_at_their_offsets()
