import errno
import http.cookiejar
import http.server
import os
import shutil
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from typing import ClassVar
from unittest.mock import Mock

from mtsdump import net
from tests.support import read, write

URL = "https://storage.example/file.mp4"
BODY = bytes(range(256)) * 4096


# Stands in for threading.Event, records the backoff delays instead of sleeping.
class FakeCancel:
    def __init__(self, cancel_on_wait: bool = False):
        self.delays = []
        self._cancel_on_wait = cancel_on_wait

    def is_set(self) -> bool:
        return False

    def wait(self, delay: float) -> bool:
        self.delays.append(delay)
        return self._cancel_on_wait


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, status, "error", {}, None)


class WithRetriesTest(unittest.TestCase):
    def test_retries_network_errors_until_success(self):
        action = Mock(side_effect=[ConnectionResetError("reset"), TimeoutError(), b"body"])
        cancel = FakeCancel()
        with self.assertLogs("mtsdump.net", "WARNING"):
            self.assertEqual(net._with_retries(URL, action, cancel), b"body")
        self.assertEqual(cancel.delays, [1, 2])

    def test_gives_up_after_10_attempts(self):
        action = Mock(side_effect=[http_error(503)] * net.ATTEMPTS)
        cancel = FakeCancel()
        with self.assertLogs("mtsdump.net", "WARNING"), self.assertRaises(net.HttpStatusError) as caught:
            net._with_retries(URL, action, cancel)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(action.call_count, net.ATTEMPTS)
        self.assertEqual(cancel.delays, [1, 2, 4, 8, 16, 30, 30, 30, 30])

    def test_does_not_retry_client_errors(self):
        action = Mock(side_effect=http_error(404))
        with self.assertRaises(net.HttpStatusError):
            net._with_retries(URL, action, FakeCancel())
        self.assertEqual(action.call_count, 1)

    def test_does_not_retry_a_full_disk(self):
        action = Mock(side_effect=OSError(errno.ENOSPC, "No space left on device"))
        with self.assertRaises(OSError):
            net._with_retries(URL, action, FakeCancel())
        self.assertEqual(action.call_count, 1)

    def test_does_not_retry_certificate_errors(self):
        action = Mock(side_effect=urllib.error.URLError(ssl.SSLCertVerificationError("self-signed certificate")))
        with self.assertRaises(net.NetworkError):
            net._with_retries(URL, action, FakeCancel())
        self.assertEqual(action.call_count, 1)

    def test_cancel_stops_the_backoff(self):
        action = Mock(side_effect=[ConnectionResetError("reset"), b"body"])
        with self.assertLogs("mtsdump.net", "WARNING"), self.assertRaises(net.Cancelled):
            net._with_retries(URL, action, FakeCancel(cancel_on_wait=True))
        self.assertEqual(action.call_count, 1)


class RangeHandler(http.server.BaseHTTPRequestHandler):
    range_headers: ClassVar[list] = []
    # Byte counts after which the next responses are cut off, like a reset connection.
    cut_after: ClassVar[list] = []
    honor_range = True

    def do_GET(self):
        header = self.headers.get("Range")
        RangeHandler.range_headers.append(header)
        start = int(header.removeprefix("bytes=").split("-")[0]) if header and RangeHandler.honor_range else 0
        if start >= len(BODY):
            self.send_response(416)
            self.end_headers()
            return
        body = BODY[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        limit = RangeHandler.cut_after.pop(0) if RangeHandler.cut_after else len(body)
        self.wfile.write(body[:limit])

    def log_message(self, *args):
        pass


class DownloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/stream.mp4"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        RangeHandler.range_headers, RangeHandler.cut_after, RangeHandler.honor_range = [], [], True
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        self.path = os.path.join(directory, "stream.mp4")

    def test_resumes_after_a_dropped_connection(self):
        RangeHandler.cut_after = [300_000]
        progress = []
        with self.assertLogs("mtsdump.net", "WARNING"):
            net.download(self.url, self.path, progress.append, FakeCancel())
        self.assertEqual(read(self.path), BODY)
        self.assertEqual(RangeHandler.range_headers, [None, "bytes=300000-"])
        self.assertEqual(progress[-1], len(BODY))

    def test_continues_a_part_left_by_an_earlier_run(self):
        write(self.path + ".part", BODY[:1000])
        net.download(self.url, self.path, lambda size: None, FakeCancel())
        self.assertEqual(read(self.path), BODY)
        self.assertEqual(RangeHandler.range_headers, ["bytes=1000-"])

    def test_starts_over_when_the_server_ignores_range(self):
        RangeHandler.honor_range = False
        write(self.path + ".part", BODY[:1000])
        net.download(self.url, self.path, lambda size: None, FakeCancel())
        self.assertEqual(read(self.path), BODY)

    def test_accepts_a_complete_part_on_range_not_satisfiable(self):
        write(self.path + ".part", BODY)
        net.download(self.url, self.path, lambda size: None, FakeCancel())
        self.assertEqual(read(self.path), BODY)
        self.assertFalse(os.path.exists(self.path + ".part"))


class PostHandler(http.server.BaseHTTPRequestHandler):
    received: ClassVar[list] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        PostHandler.received.append(self.rfile.read(length))
        body = b"posted"
        self.send_response(200)
        self.send_header("Set-Cookie", "t=1; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class PostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PostHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/token"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        PostHandler.received = []

    def test_posts_the_data_and_returns_the_body(self):
        self.assertEqual(net.post(self.url, b"a=1"), b"posted")
        self.assertEqual(PostHandler.received, [b"a=1"])

    def test_an_opener_collects_the_cookies_of_the_answer(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.assertEqual(net.post(self.url, b"a=1", opener=opener), b"posted")
        self.assertEqual([cookie.name for cookie in jar], ["t"])
