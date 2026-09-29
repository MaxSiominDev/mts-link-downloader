import time
import unittest

from mtsdump import naming
from mtsdump.record import Record, Speaker

DATE_SUFFIX = " 2026-09-10 08:59:15"
DATE_PREFIX = "2026-09-10 08-59-15 "


def record(name: str, started_at: float = 0.0) -> Record:
    return Record(
        session_id=7,
        name=name,
        started_at=started_at,
        duration=100.0,
        streams=(),
        has_cuts=False,
        has_converted_records=False,
    )


# "résumé" is 6 characters but 8 UTF-8 bytes, so a byte limit would cut these names much earlier.
def words(count: int) -> str:
    return " ".join(["résumé"] * count)


class FilenameTest(unittest.TestCase):
    def test_moves_the_trailing_date_to_the_front(self):
        name = naming.filename(naming.DEFAULT_TEMPLATE, record("Graph theory Lecture/Practice" + DATE_SUFFIX))
        self.assertEqual(name, DATE_PREFIX + "Graph theory Lecture-Practice.mp3")

    def test_date_from_session_start_when_the_name_has_none(self):
        started_at = time.mktime((2026, 9, 10, 12, 19, 8, 0, 0, -1))
        name = naming.filename(naming.DEFAULT_TEMPLATE, record("Seminar", started_at))
        self.assertEqual(name, "2026-09-10 12-19-08 Seminar.mp3")

    def test_template_placeholders(self):
        self.assertEqual(naming.filename("{id} {name}", record("Seminar" + DATE_SUFFIX)), "7 Seminar.mp3")

    def test_speaker_label_follows_the_name(self):
        name = naming.filename(naming.DEFAULT_TEMPLATE, record("Seminar" + DATE_SUFFIX), "Ivan/Petrov")
        self.assertEqual(name, DATE_PREFIX + "Seminar - Ivan-Petrov.mp3")

    def test_empty_name_falls_back_to_the_session_id(self):
        self.assertEqual(naming.filename("{name}", record("")), "7.mp3")

    def test_cut_never_leaves_a_hidden_file(self):
        name = naming.filename("{name}", record("- " + "x" * 300))
        self.assertTrue(name.startswith("- x"))
        self.assertTrue(name.endswith("x.mp3"))


class LongNameTest(unittest.TestCase):
    def test_cuts_at_255_characters(self):
        name = naming.filename(naming.DEFAULT_TEMPLATE, record(words(60) + DATE_SUFFIX))
        self.assertEqual(name, DATE_PREFIX + words(33) + ".mp3")

    def test_speaker_label_survives_the_cut(self):
        name = naming.filename(naming.DEFAULT_TEMPLATE, record(words(60) + DATE_SUFFIX), "Ivan Petrov")
        self.assertEqual(name, DATE_PREFIX + words(31) + " - Ivan Petrov.mp3")


class CheckTemplateTest(unittest.TestCase):
    def test_accepts_every_placeholder(self):
        naming.check_template("{date} {name} {id}")

    def test_rejects_unknown_placeholders_and_stray_braces(self):
        for template in ("{title}", "{name", "{0}", "{date.year}"):
            with self.subTest(template=template), self.assertRaises(ValueError):
                naming.check_template(template)


class SpeakerLabelsTest(unittest.TestCase):
    def test_nicknames_as_they_are(self):
        labels = naming.speaker_labels([Speaker(1, "Ivan"), Speaker(3, "Anna")])
        self.assertEqual(labels, {Speaker(1, "Ivan"): "Ivan", Speaker(3, "Anna"): "Anna"})

    def test_namesakes_get_their_user_ids(self):
        labels = naming.speaker_labels([Speaker(1, "Ivan"), Speaker(2, "Ivan")])
        self.assertEqual(labels, {Speaker(1, "Ivan"): "Ivan 1", Speaker(2, "Ivan"): "Ivan 2"})

    def test_names_that_become_one_file_get_their_user_ids(self):
        speakers = [Speaker(1, "ivan"), Speaker(2, "Ivan"), Speaker(3, "A/B"), Speaker(4, "A:B")]
        expected = {
            Speaker(1, "ivan"): "ivan 1",
            Speaker(2, "Ivan"): "Ivan 2",
            Speaker(3, "A/B"): "A/B 3",
            Speaker(4, "A:B"): "A:B 4",
        }
        self.assertEqual(naming.speaker_labels(speakers), expected)

    def test_added_ids_do_not_collide_with_other_nicknames(self):
        labels = naming.speaker_labels([Speaker(1, "Ivan"), Speaker(2, "Ivan"), Speaker(3, "Ivan 1")])
        self.assertEqual(labels, {Speaker(1, "Ivan"): "Ivan 1", Speaker(2, "Ivan"): "Ivan 2", Speaker(3, "Ivan 1"): "Ivan 1 3"})

    def test_empty_nickname_gets_the_user_id(self):
        labels = naming.speaker_labels([Speaker(9, ""), Speaker(10, "...")])
        self.assertEqual(labels, {Speaker(9, ""): "user 9", Speaker(10, "..."): "user 10"})

    def test_streams_without_a_speaker_stay_unknown(self):
        labels = naming.speaker_labels([None, Speaker(5, "Unknown")])
        self.assertEqual(labels, {None: "unknown", Speaker(5, "Unknown"): "Unknown 5"})
