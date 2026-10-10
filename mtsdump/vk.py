import http.cookiejar
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from mtsdump import ffmpeg, mix, net
from mtsdump.api import CookieSource
from mtsdump.cookies import CookieError
from mtsdump.links import VkVideoRef
from mtsdump.mix import Encoding

API_ID = 52461373
WEB_TOKEN_URL = "https://vkvideo.ru/al_video.php?act=web_token"
VIDEO_BY_IDS_URL = f"https://api.vk.ru/method/video.getByIds?v=5.289&client_id={API_ID}"
COOKIE_HOSTS = ("vk.ru", "vk.com", "login.vk.ru")
# vkvideo bounces requests without a browser User-Agent into an endless redirect,
# and the delivery hosts sign their links for the agent the metadata was fetched with.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
BANDWIDTH = re.compile(r"[,:]BANDWIDTH=(\d+)")


class AccessDenied(Exception):
    pass


class ApiError(Exception):
    pass


@dataclass(frozen=True)
class VkVideo:
    session_id: str
    title: str
    started_at: float
    duration: float
    hls_url: str
    mp4_url: str | None


def fetch_video(ref: VkVideoRef, cookie_for: CookieSource) -> VkVideo:
    cookies = _host_cookies(cookie_for)
    if not any(cookies.values()):
        raise AccessDenied("the video is open to VK users only, pass --cookies-from-browser chrome or --cookie")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar(cookies)))
    item = _video_item(opener, ref)
    if not (item.get("files") or {}).get("hls_ondemand"):
        raise ApiError(f"video {ref.session_id} has no HLS stream")
    try:
        return VkVideo(
            session_id=ref.session_id,
            title=item["title"],
            started_at=item["date"],
            duration=item["duration"],
            hls_url=item["files"]["hls_ondemand"],
            mp4_url=item["files"].get("mp4_1080"),
        )
    except (KeyError, TypeError) as error:
        raise ApiError(f"unexpected video info for {ref.session_id}: {error!r}") from error


def cheapest_rendition(master: bytes, base_url: str) -> str:
    """Returns the lowest-bandwidth variant playlist; the audio track is the same in every variant."""
    lines = master.decode(errors="replace").splitlines()
    best_url, best_bandwidth = None, None
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        match = BANDWIDTH.search(line)
        uri = lines[index + 1]
        if not match or uri.startswith("#"):
            raise ApiError(f"unexpected master playlist at {base_url}")
        if best_bandwidth is None or int(match.group(1)) < best_bandwidth:
            best_url, best_bandwidth = urllib.parse.urljoin(base_url, uri), int(match.group(1))
    if best_url is None:
        raise ApiError(f"master playlist at {base_url} lists no renditions")
    return best_url


def encode_audio(
    source_url: str,
    output_path: str,
    duration: float,
    encoding: Encoding,
    on_progress: Callable[[float], None],
) -> None:
    """Downloads the audio of one VK video rendition and writes it as mp3, without loudness normalization.

    The fold to mono runs inside the filter chain and before the limiter: as an output option it would
    come after the limiting and lift the peaks it just capped.
    """
    tail = [mix.ASR_HIGHPASS] if encoding.highpass else []
    filters = ["aformat=channel_layouts=mono", *tail, mix.LIMITER]
    ffmpeg.run(
        [
            "-user_agent", USER_AGENT,
            *ffmpeg.REMOTE_OPTIONS,
            "-i", source_url,
            "-vn", "-map", "0:a:0",
            "-c:a", "libmp3lame", "-b:a", encoding.bitrate,
            "-ar", str(encoding.sample_rate),
            "-af", ",".join(filters),
            # the .part suffix of the temporary path hides the .mp3 one, so name the muxer explicitly
            "-f", "mp3",
            output_path,
        ],
        duration,
        on_progress,
    )


def _host_cookies(cookie_for: CookieSource) -> dict[str, str | None]:
    # A profile may hold a session for only some of the VK hosts, whatever is there is worth a try.
    cookies = {}
    for host in COOKIE_HOSTS:
        try:
            cookies[host] = cookie_for(host)
        except CookieError:
            cookies[host] = None
    return cookies


def _video_item(opener: urllib.request.OpenerDirector, ref: VkVideoRef) -> dict:
    videos = ref.session_id + (f"_{ref.access_key}" if ref.access_key else "")
    answer = _json(
        net.post(
            VIDEO_BY_IDS_URL,
            urllib.parse.urlencode({
                "videos": videos,
                "video_fields": "files",
                "access_token": _token(opener),
            }).encode(),
            headers={"User-Agent": USER_AGENT},
            opener=opener,
        )
    )
    if not isinstance(answer, dict):
        raise ApiError(f"unexpected answer for video {ref.session_id}")
    if "error" in answer:
        raise ApiError(answer["error"].get("error_msg", f"video {ref.session_id} is unavailable"))
    items = answer.get("response", {}).get("items") or []
    if not items:
        raise ApiError(f"video {ref.session_id} not found")
    return items[0]


def _token(opener: urllib.request.OpenerDirector) -> str:
    body = urllib.parse.urlencode({"version": "1", "app_id": API_ID}).encode()
    headers = {"User-Agent": USER_AGENT}
    answer = _json(net.post(WEB_TOKEN_URL, body, headers=headers, opener=opener))
    if isinstance(answer, dict) and answer.get("error"):
        # The first post walks the autologin redirects and only settles the session in the jar along the way.
        answer = _json(net.post(WEB_TOKEN_URL, body, headers=headers, opener=opener))
    entry = answer[0] if isinstance(answer, list) and answer else None
    if not isinstance(entry, dict) or not entry.get("access_token"):
        raise AccessDenied("vkvideo did not issue a token, log in to vk.ru in Chrome again or pass --cookie")
    return entry["access_token"]


def _json(body: bytes) -> dict | list:
    try:
        return json.loads(body)
    except ValueError as error:
        raise ApiError("vkvideo did not return JSON") from error


def _jar(headers: dict[str, str]) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    for host, header in headers.items():
        for pair in (header or "").split("; "):
            name, separator, value = pair.partition("=")
            if not separator or not name:
                continue
            jar.set_cookie(http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=host,
                domain_specified=False,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
            ))
    return jar
