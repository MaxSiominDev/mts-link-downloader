import unittest

from mtsdump.links import DEFAULT_HOST, RecordRef, parse_record_ref


class ParseRecordRefTest(unittest.TestCase):
    def test_organization_link(self):
        ref = parse_record_ref("https://my.mts-link.ru/j/Org/11111111111/record-new/22222222222")
        self.assertEqual(ref, RecordRef("my.mts-link.ru", "22222222222"))

    def test_user_link(self):
        ref = parse_record_ref("https://my.mts-link.ru/999999999/11111111111/record-new/22222222222")
        self.assertEqual(ref, RecordRef("my.mts-link.ru", "22222222222"))

    def test_keeps_the_host_of_the_link(self):
        ref = parse_record_ref("https://events.example.mts-link.ru/j/org/1/record-new/2")
        self.assertEqual(ref.host, "events.example.mts-link.ru")

    def test_ignores_trailing_slash_query_and_fragment(self):
        self.assertEqual(parse_record_ref("https://my.mts-link.ru/j/org/1/record-new/2/?t=15#player").session_id, "2")

    def test_link_without_scheme(self):
        self.assertEqual(parse_record_ref("my.mts-link.ru/j/org/1/record-new/2"), RecordRef("my.mts-link.ru", "2"))

    def test_bare_session_id_uses_the_default_host(self):
        self.assertEqual(parse_record_ref(" 22222222222 "), RecordRef(DEFAULT_HOST, "22222222222"))

    def test_rejects_an_event_link_without_a_record(self):
        with self.assertRaises(ValueError):
            parse_record_ref("https://my.mts-link.ru/j/Org/11111111111")

    def test_rejects_non_ascii_digits(self):
        with self.assertRaises(ValueError):
            parse_record_ref("２３")  # noqa: RUF001
