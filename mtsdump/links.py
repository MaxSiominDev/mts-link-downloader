import re
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_HOST = "my.mts-link.ru"
BARE_SESSION_ID = re.compile(r"\d+", re.ASCII)
SESSION_ID_IN_PATH = re.compile(r"/record-new/(\d+)/?$", re.ASCII)


@dataclass(frozen=True)
class RecordRef:
    host: str
    session_id: str


def parse_record_ref(value: str) -> RecordRef:
    value = value.strip()
    if BARE_SESSION_ID.fullmatch(value):
        return RecordRef(DEFAULT_HOST, value)
    url = urlparse(value if "://" in value else f"https://{value}")
    match = SESSION_ID_IN_PATH.search(url.path)
    if not url.hostname or not match:
        raise ValueError(f"not a record-new link or a session id: {value!r}")
    return RecordRef(url.hostname, match.group(1))
