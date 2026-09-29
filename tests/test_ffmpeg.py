import os
import shutil
import subprocess
import tempfile
import unittest

from mtsdump import ffmpeg


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class FfmpegTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)

    def camera_recording(self) -> str:
        # 1 s of audio next to 3 s of video
        path = os.path.join(self.directory, "camera.mp4")
        subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-f", "lavfi", "-i", "sine=duration=1",
                "-f", "lavfi", "-i", "color=size=64x64:duration=3",
                "-c:a", "aac", "-c:v", "mpeg4", path,
            ],
            check=True,
        )
        return path

    def test_audio_length_wins_over_a_longer_video(self):
        self.assertAlmostEqual(ffmpeg.probe_duration(self.camera_recording()), 1.0, delta=0.05)

    def test_analyze_leaves_the_video_alone(self):
        mapping = [line for line in ffmpeg.analyze(self.camera_recording(), "volumedetect").splitlines() if "->" in line]
        self.assertTrue(any("aac" in line for line in mapping))
        self.assertFalse(any("mpeg4" in line for line in mapping))

    def test_broken_packets_that_ffmpeg_skips_are_reported(self):
        path = os.path.join(self.directory, "broken.aac")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=5", "-c:a", "aac", "-f", "adts", path],
            check=True,
        )
        with open(path, "r+b") as file:
            file.seek(os.path.getsize(path) // 2)
            file.write(b"\x00\xff\x13\x37" * 512)
        with self.assertLogs("mtsdump.ffmpeg", "WARNING"):
            ffmpeg.run(["-i", path, "-f", "null", "-"], 5.0, lambda fraction: None)

    def test_failed_run_carries_the_ffmpeg_message(self):
        missing = os.path.join(self.directory, "missing.m4a")
        with self.assertRaisesRegex(ffmpeg.FfmpegError, "No such file"):
            ffmpeg.run(["-i", missing, "-f", "null", "-"], 1.0, lambda fraction: None)
