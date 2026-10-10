import http.client
import logging
import os
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable

ATTEMPTS = 10
TIMEOUT_SECONDS = 30
MAX_BACKOFF_SECONDS = 30
CHUNK_SIZE = 1 << 20
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, ssl.SSLError)
PARTIAL_CONTENT = 206
RANGE_NOT_SATISFIABLE = 416

log = logging.getLogger(__name__)


class HttpStatusError(Exception):
    def __init__(self, url: str, status: int):
        super().__init__(f"HTTP {status} from {url}")
        self.url = url
        self.status = status


class NetworkError(Exception):
    pass


class Cancelled(Exception):
    pass


def get(url: str, headers: dict[str, str] | None = None, cancel: threading.Event | None = None) -> bytes:
    def read() -> bytes:
        request = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()

    return _with_retries(url, read, cancel)


def post(
    url: str,
    data: bytes,
    headers: dict[str, str] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    cancel: threading.Event | None = None,
) -> bytes:
    """Posts data, optionally through an opener that carries a cookie jar across redirects"""

    def read() -> bytes:
        request = urllib.request.Request(url, data=data, headers=headers or {})
        urlopen = opener.open if opener else urllib.request.urlopen
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()

    return _with_retries(url, read, cancel)


def download(
    url: str,
    path: str,
    on_progress: Callable[[int], None],
    cancel: threading.Event | None = None,
) -> None:
    """Downloads url into path, resuming from the .part file an earlier attempt or run left there.

    on_progress(bytes_written_so_far).
    """
    part_path = path + ".part"

    def attempt() -> None:
        offset = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        request = urllib.request.Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
        try:
            response = urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            if error.code == RANGE_NOT_SATISFIABLE and offset:
                error.close()
                return
            raise
        resumed = response.status == PARTIAL_CONTENT
        written = offset if resumed else 0
        with response, open(part_path, "ab" if resumed else "wb") as out:
            while chunk := response.read(CHUNK_SIZE):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                out.write(chunk)
                written += len(chunk)
                on_progress(written)
            # read(amt) returns b"" on a dropped connection instead of raising, so check what is left.
            if response.length:
                raise http.client.IncompleteRead(b"", response.length)

    _with_retries(url, attempt, cancel)
    os.replace(part_path, path)


def _with_retries[T](url: str, action: Callable[[], T], cancel: threading.Event | None = None) -> T:
    cancel = cancel or threading.Event()
    attempt = 1
    while True:
        if cancel.is_set():
            raise Cancelled()
        try:
            return action()
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in RETRYABLE_STATUSES or attempt == ATTEMPTS:
                raise HttpStatusError(url, error.code) from error
            reason = f"HTTP {error.code}"
        except NETWORK_ERRORS as error:
            reason = str(getattr(error, "reason", None) or error) or type(error).__name__
            if attempt == ATTEMPTS or _is_certificate_error(error):
                raise NetworkError(f"{url}: {reason}") from error
        delay = min(2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
        attempt += 1
        log.warning("%s: %s, attempt %d of %d in %d s", url, reason, attempt, ATTEMPTS, delay)
        if cancel.wait(delay):
            raise Cancelled()


def _is_certificate_error(error: Exception) -> bool:
    return isinstance(getattr(error, "reason", error), ssl.SSLCertVerificationError)
