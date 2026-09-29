import glob
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing

CHROME_DIR = os.path.expanduser("~/Library/Application Support/Google/Chrome")
KEYCHAIN_SERVICE = "Chrome Safe Storage"

# Chrome on macOS: AES-128-CBC, key is PBKDF2-SHA1 over the Keychain secret, IV is 16 spaces.
PBKDF2_SALT = b"saltysalt"
PBKDF2_ITERATIONS = 1003
AES_IV = b" " * 16
ENCRYPTED_PREFIX = b"v10"

# Since cookie DB version 24 the plaintext starts with SHA-256 of the cookie's host key.
HOST_HASH_DB_VERSION = 24
HOST_HASH_LENGTH = 32

SECONDS_FROM_1601_TO_1970 = 11644473600


class CookieError(Exception):
    pass


def chrome_cookie_header(host: str, profile: str | None = None) -> str:
    db_path = _find_cookie_db(profile)
    version, rows = _read_cookies(db_path, host)
    if not rows:
        raise CookieError(f"Chrome has no cookies for {host}, log in to MTS Link in Chrome first")
    key = None
    cookies = {}
    for name, value, encrypted in rows:
        if not value and encrypted:
            key = key or _derive_key(_keychain_secret())
            value = _decrypt(encrypted, key, has_host_hash=version >= HOST_HASH_DB_VERSION)
        cookies[name] = value
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _find_cookie_db(profile: str | None) -> str:
    profile_pattern = profile or "*"
    candidates = glob.glob(os.path.join(CHROME_DIR, profile_pattern, "Cookies"))
    candidates += glob.glob(os.path.join(CHROME_DIR, profile_pattern, "Network", "Cookies"))
    if not candidates:
        raise CookieError(f"no Chrome cookie database for profile {profile_pattern!r} in {CHROME_DIR}")
    return max(candidates, key=os.path.getmtime)


def _read_cookies(db_path: str, host: str) -> tuple[int, list[tuple[str, str, bytes]]]:
    # Chrome keeps the database open, so read a copy together with its journal.
    with tempfile.TemporaryDirectory() as tmp:
        copy_path = os.path.join(tmp, "Cookies")
        for suffix in ("", "-journal", "-wal"):
            if os.path.exists(db_path + suffix):
                shutil.copyfile(db_path + suffix, copy_path + suffix)
        try:
            with closing(sqlite3.connect(copy_path)) as db:
                version = db.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
                rows = db.execute(
                    "SELECT host_key, name, value, encrypted_value, expires_utc FROM cookies ORDER BY length(host_key)"
                ).fetchall()
        except sqlite3.Error as error:
            raise CookieError(f"cannot read the Chrome cookie database {db_path}: {error}") from error
    if version is None or not str(version[0]).isdigit():
        raise CookieError(f"the Chrome cookie database {db_path} has no version")
    now = (time.time() + SECONDS_FROM_1601_TO_1970) * 1_000_000
    return int(version[0]), [
        (name, value, encrypted)
        for host_key, name, value, encrypted, expires in rows
        if _host_matches(host_key, host) and (expires == 0 or expires > now)
    ]


def _host_matches(host_key: str, host: str) -> bool:
    if host_key.startswith("."):
        return host == host_key[1:] or host.endswith(host_key)
    return host == host_key


def _keychain_secret() -> bytes:
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE],
        capture_output=True,
    )
    if result.returncode != 0:
        reason = result.stderr.decode(errors="replace").strip()
        raise CookieError(f"cannot read {KEYCHAIN_SERVICE!r} from Keychain: {reason}")
    return result.stdout.rstrip(b"\n")


def _derive_key(secret: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", secret, PBKDF2_SALT, PBKDF2_ITERATIONS, dklen=16)


def _decrypt(encrypted: bytes, key: bytes, has_host_hash: bool) -> str:
    if not encrypted.startswith(ENCRYPTED_PREFIX):
        raise CookieError(f"unsupported Chrome cookie encryption {encrypted[:3]!r}")
    result = subprocess.run(
        ["/usr/bin/openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", AES_IV.hex()],
        input=encrypted[len(ENCRYPTED_PREFIX):],
        capture_output=True,
    )
    if result.returncode != 0:
        raise CookieError("cannot decrypt Chrome cookies with the key from Keychain")
    plaintext = result.stdout[HOST_HASH_LENGTH:] if has_host_hash else result.stdout
    try:
        return plaintext.decode()
    except UnicodeDecodeError:
        raise CookieError("cannot decrypt Chrome cookies with the key from Keychain") from None
