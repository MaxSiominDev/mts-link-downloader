import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

DEFAULT_HOST = "my.mts-link.ru"
BARE_SESSION_ID = re.compile(r"\d+", re.ASCII)
SESSION_ID_IN_PATH = re.compile(r"/record-new/(\d+)/?$", re.ASCII)
VK_HOST = "vkvideo.ru"
VK_VIDEO_IN_PATH = re.compile(r"/video(\d+)_(\d+)/?$", re.ASCII)


@dataclass(frozen=True)
class RecordRef:
    host: str
    session_id: str


@dataclass(frozen=True)
class VkVideoRef:
    host: str
    owner_id: str
    video_id: str
    access_key: str | None

    @property
    def session_id(self) -> str:
        return f"{self.owner_id}_{self.video_id}"


def parse_record_ref(value: str) -> RecordRef:
    value = value.strip()
    if BARE_SESSION_ID.fullmatch(value):
        return RecordRef(DEFAULT_HOST, value)
    url = urlparse(value if "://" in value else f"https://{value}")
    match = SESSION_ID_IN_PATH.search(url.path)
    if not url.hostname or not match:
        raise ValueError(f"not a record-new link or a session id: {value!r}")
    return RecordRef(url.hostname, match.group(1))


def parse_vk_video_ref(value: str) -> VkVideoRef:
    # The host is pinned, session cookies must never go to a lookalike domain
    value = value.strip()
    url = urlparse(value if "://" in value else f"https://{value}")
    match = VK_VIDEO_IN_PATH.search(url.path)
    if url.hostname != VK_HOST or not match:
        raise ValueError(f"not a {VK_HOST} video link: {value!r}")
    access_key = parse_qs(url.query).get("list", [None])[0]
    return VkVideoRef(VK_HOST, match.group(1), match.group(2), access_key)
