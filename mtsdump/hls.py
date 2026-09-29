import re
from dataclasses import dataclass
from urllib.parse import urljoin

URI_ATTRIBUTE = re.compile(r'URI="([^"]+)"')


class PlaylistError(Exception):
    pass


@dataclass(frozen=True)
class MediaPlaylist:
    init_url: str
    segment_urls: tuple[str, ...]
    # Sum of EXTINF values, decoded audio runs up to a second longer
    duration: float


def audio_rendition_url(master: str, master_url: str) -> str | None:
    # An error page served with status 200 would otherwise look like a stream without audio.
    if not master.startswith("#EXTM3U"):
        raise PlaylistError(f"not a playlist: {master_url}")
    for line in master.splitlines():
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
            uri = URI_ATTRIBUTE.search(line)
            if uri:
                return urljoin(master_url, uri.group(1))
    return None


def parse_media_playlist(text: str, url: str) -> MediaPlaylist:
    lines = [line.strip() for line in text.splitlines()]
    inits = [URI_ATTRIBUTE.search(line) for line in lines if line.startswith("#EXT-X-MAP:")]
    segments = tuple(urljoin(url, line) for line in lines if line and not line.startswith("#"))
    complete = "#EXT-X-ENDLIST" in lines
    if len(inits) != 1 or inits[0] is None or not segments or not complete or any(map(_breaks_joining, lines)):
        raise PlaylistError(f"unsupported audio playlist {url}")
    try:
        duration = sum(float(line.removeprefix("#EXTINF:").split(",")[0]) for line in lines if line.startswith("#EXTINF:"))
    except ValueError:
        raise PlaylistError(f"unsupported audio playlist {url}") from None
    return MediaPlaylist(urljoin(url, inits[0].group(1)), segments, duration)


def _breaks_joining(line: str) -> bool:
    # Segments can be joined behind one init section only when they form one continuous, unencrypted file.
    if line.startswith("#EXT-X-KEY:"):
        return "METHOD=NONE" not in line
    return line == "#EXT-X-DISCONTINUITY" or line.startswith("#EXT-X-BYTERANGE:") or "BYTERANGE=" in line
