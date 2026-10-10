import unittest

from mtsdump.links import DEFAULT_HOST, RecordRef, VkVideoRef, parse_record_ref, parse_vk_video_ref


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


class ParseVkVideoRefTest(unittest.TestCase):
    def test_link_with_the_access_key(self):
        ref = parse_vk_video_ref("https://vkvideo.ru/video111_222?list=ln-abc")
        self.assertEqual(ref, VkVideoRef("vkvideo.ru", "111", "222", "ln-abc"))
        self.assertEqual(ref.session_id, "111_222")

    def test_link_without_the_access_key(self):
        self.assertEqual(parse_vk_video_ref("https://vkvideo.ru/video111_222"), VkVideoRef("vkvideo.ru", "111", "222", None))

    def test_link_without_scheme(self):
        self.assertEqual(parse_vk_video_ref("vkvideo.ru/video111_222?list=ln-abc&x=1").access_key, "ln-abc")

    def test_trailing_slash_and_query_noise(self):
        ref = parse_vk_video_ref("https://vkvideo.ru/video111_222/?showAutoLoginModal=1&list=ln-abc")
        self.assertEqual(ref, VkVideoRef("vkvideo.ru", "111", "222", "ln-abc"))

    def test_rejects_other_vk_hosts(self):
        for url in ("https://vk.com/video111_222", "https://m.vkvideo.ru/video111_222", "https://evil.ru/video111_222"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_vk_video_ref(url)

    def test_rejects_links_that_are_not_videos(self):
        for url in ("https://vkvideo.ru/video111", "https://vkvideo.ru/clip111_222", "https://vkvideo.ru/im"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_vk_video_ref(url)
