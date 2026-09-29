import unittest
from unittest.mock import Mock, patch

from mtsdump import api, net
from mtsdump.links import RecordRef

REF = RecordRef("my.mts-link.ru", "5")
PUBLIC = "https://my.mts-link.ru/api/eventsessions/5/record"
GATEWAY = "https://gw.mts-link.ru/api/eventsessions/5/record"


class FetchRecordTest(unittest.TestCase):
    @patch("mtsdump.api.net.get", return_value=b'{"id": 5}')
    def test_public_record_needs_no_cookies(self, get):
        cookie_for = Mock()
        self.assertEqual(api.fetch_record(REF, cookie_for), {"id": 5})
        get.assert_called_once_with(PUBLIC, headers=None)
        cookie_for.assert_not_called()

    @patch("mtsdump.api.net.get", side_effect=[net.HttpStatusError(PUBLIC, 403), b'{"id": 5}'])
    def test_private_record_goes_through_the_gateway_with_cookies(self, get):
        self.assertEqual(api.fetch_record(REF, lambda host: f"access=for-{host}"), {"id": 5})
        get.assert_called_with(GATEWAY, headers={"Cookie": "access=for-gw.mts-link.ru"})

    @patch("mtsdump.api.net.get", side_effect=[net.HttpStatusError(PUBLIC, 403)])
    def test_private_record_without_cookies(self, get):
        with self.assertRaisesRegex(api.AccessDenied, "--cookies-from-browser"):
            api.fetch_record(REF, lambda host: None)

    @patch("mtsdump.api.net.get", side_effect=[net.HttpStatusError(PUBLIC, 403), net.HttpStatusError(GATEWAY, 403)])
    def test_rejected_cookies(self, get):
        with self.assertRaisesRegex(api.AccessDenied, "rejected"):
            api.fetch_record(REF, lambda host: "access=expired")

    @patch("mtsdump.api.net.get", side_effect=[net.HttpStatusError(PUBLIC, 404)])
    def test_missing_record(self, get):
        with self.assertRaisesRegex(api.ApiError, "not found"):
            api.fetch_record(REF, lambda host: None)

    @patch("mtsdump.api.net.get", return_value=b"<html>")
    def test_answer_that_is_not_json(self, get):
        with self.assertRaises(api.ApiError):
            api.fetch_record(REF, lambda host: None)


class CookieHostTest(unittest.TestCase):
    @patch("mtsdump.api.net.get", side_effect=net.HttpStatusError(PUBLIC, 403))
    def test_a_lookalike_link_gets_no_cookies(self, get):
        cookie_for = Mock(return_value="access=1")
        for host in ("my.evil.com", "my.mts-link.ru.evil.com", "my.evilmts-link.ru"):
            with self.subTest(host=host), self.assertRaises(api.AccessDenied):
                api.fetch_record(RecordRef(host, "5"), cookie_for)
        cookie_for.assert_not_called()


class GatewayHostTest(unittest.TestCase):
    def test_gateway_sits_on_the_parent_domain(self):
        self.assertEqual(api._gateway_host("my.mts-link.ru"), "gw.mts-link.ru")
        self.assertEqual(api._gateway_host("events.example.mts-link.ru"), "gw.mts-link.ru")
