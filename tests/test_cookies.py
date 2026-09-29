import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing

from mtsdump import cookies
from tests.support import write

OPENSSL = "/usr/bin/openssl"
HOST = "gw.mts-link.ru"


def chrome_time(offset_seconds: float) -> int:
    return int((time.time() + cookies.SECONDS_FROM_1601_TO_1970 + offset_seconds) * 1_000_000)


def make_database(path: str, version: str | None = "24", rows: tuple = ()) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        if version is not None:
            db.execute("INSERT INTO meta VALUES ('version', ?)", (version,))
        db.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, expires_utc INTEGER)")
        db.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?, ?)", rows)
        db.commit()


@unittest.skipUnless(os.path.exists(OPENSSL), "needs /usr/bin/openssl")
class DecryptTest(unittest.TestCase):
    key = cookies._derive_key(b"keychain secret")

    def encrypt(self, plaintext: bytes) -> bytes:
        result = subprocess.run(
            [OPENSSL, "enc", "-aes-128-cbc", "-K", self.key.hex(), "-iv", cookies.AES_IV.hex()],
            input=plaintext,
            capture_output=True,
            check=True,
        )
        return cookies.ENCRYPTED_PREFIX + result.stdout

    def test_strips_the_host_hash_of_new_databases(self):
        encrypted = self.encrypt(hashlib.sha256(b".mts-link.ru").digest() + b"jwt-token")
        self.assertEqual(cookies._decrypt(encrypted, self.key, has_host_hash=True), "jwt-token")

    def test_old_databases_have_no_host_hash(self):
        self.assertEqual(cookies._decrypt(self.encrypt(b"jwt-token"), self.key, has_host_hash=False), "jwt-token")

    def test_wrong_key(self):
        with self.assertRaises(cookies.CookieError):
            cookies._decrypt(self.encrypt(b"jwt-token"), cookies._derive_key(b"another secret"), has_host_hash=False)

    def test_plaintext_that_is_not_text(self):
        with self.assertRaises(cookies.CookieError):
            cookies._decrypt(self.encrypt(b"\xff\xfe\xfd"), self.key, has_host_hash=False)

    def test_unknown_encryption_version(self):
        with self.assertRaises(cookies.CookieError):
            cookies._decrypt(b"v20" + bytes(16), self.key, has_host_hash=False)


class ReadCookiesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        self.path = os.path.join(directory, "Cookies")

    def test_keeps_live_cookies_that_apply_to_the_host(self):
        rows = (
            (".mts-link.ru", "access", "", b"v10-encrypted", chrome_time(3600)),
            (".mts-link.ru", "session", "plain", b"", 0),
            (".mts-link.ru", "stale", "", b"v10-encrypted", chrome_time(-3600)),
            ("my.mts-link.ru", "sticky", "plain", b"", 0),
            (".example.com", "other", "plain", b"", 0),
        )
        make_database(self.path, rows=rows)
        version, cookies_for_host = cookies._read_cookies(self.path, HOST)
        self.assertEqual(version, 24)
        self.assertEqual([(name, value) for name, value, _ in cookies_for_host], [("access", ""), ("session", "plain")])

    def test_database_without_a_version(self):
        make_database(self.path, version=None)
        with self.assertRaises(cookies.CookieError):
            cookies._read_cookies(self.path, HOST)

    def test_file_that_is_not_a_database(self):
        write(self.path, b"not a database" * 100)
        with self.assertRaises(cookies.CookieError):
            cookies._read_cookies(self.path, HOST)


class HostMatchTest(unittest.TestCase):
    def test_domain_cookie_covers_the_domain_and_its_subdomains(self):
        self.assertTrue(cookies._host_matches(".mts-link.ru", "gw.mts-link.ru"))
        self.assertTrue(cookies._host_matches(".mts-link.ru", "mts-link.ru"))

    def test_domain_cookie_does_not_cover_lookalike_hosts(self):
        self.assertFalse(cookies._host_matches(".mts-link.ru", "evil-mts-link.ru"))

    def test_host_cookie_covers_only_its_host(self):
        self.assertTrue(cookies._host_matches("my.mts-link.ru", "my.mts-link.ru"))
        self.assertFalse(cookies._host_matches("my.mts-link.ru", "gw.mts-link.ru"))
