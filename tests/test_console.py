import io
import unittest

from mtsdump.console import Console


class ConsoleTest(unittest.TestCase):
    def test_non_tty_prints_every_10_percent(self):
        stream = io.StringIO()
        console = Console(stream)
        for fraction in (0.0, 0.04, 0.12, 0.19, 0.2, 0.55, 1.0):
            console.progress("mixing", fraction)
        self.assertEqual(stream.getvalue().splitlines(), ["mixing 0%", "mixing 12%", "mixing 20%", "mixing 55%", "mixing 100%"])

    def test_new_stage_starts_counting_again(self):
        stream = io.StringIO()
        console = Console(stream)
        console.progress("downloading", 1.0)
        console.end_progress()
        console.progress("mixing", 0.0)
        self.assertEqual(stream.getvalue().splitlines(), ["downloading 100%", "mixing 0%"])
