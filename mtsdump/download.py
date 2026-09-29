import functools
import logging
import os
import shutil
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from mtsdump import ffmpeg, hls, net
from mtsdump.record import Stream

PROGRESS_INTERVAL_SECONDS = 0.5

log = logging.getLogger(__name__)


class DownloadError(Exception):
    pass


@dataclass(frozen=True)
class Source:
    stream: Stream
    # None: audio comes from the stream's full mp4
    playlist: hls.MediaPlaylist | None


@dataclass(frozen=True)
class AudioFile:
    stream: Stream
    path: str
    duration: float


def resolve_sources(streams: tuple[Stream, ...], prefer_mp4: bool, jobs: int) -> list[Source | None]:
    """Resolves each stream to its audio source, None when the stream has no audio."""
    cancel = threading.Event()
    with ThreadPoolExecutor(jobs) as pool:
        try:
            futures = [pool.submit(_resolve, stream, prefer_mp4, cancel) for stream in streams]
            return [future.result() for future in futures]
        except BaseException:
            _stop(pool, cancel)
            raise


def download_all(
    sources: list[Source],
    work_dir: str,
    jobs: int,
    on_progress: Callable[[int, int, int], None],
) -> list[AudioFile]:
    """Downloads the audio of every source into work_dir, reusing what an earlier run left there.

    on_progress(done_parts, total_parts, bytes_saved).
    """
    cancel = threading.Event()
    sizes = _SizeCounter()
    missing = [source for source in sources if not os.path.exists(_target(source, work_dir))]
    with ThreadPoolExecutor(jobs) as pool:
        try:
            owners = {}
            for source in missing:
                for task in _tasks(source, work_dir, sizes, cancel):
                    owners[pool.submit(task)] = source
            parts_left = Counter(source.stream.id for source in owners.values())
            pending = set(owners)
            while pending:
                done, pending = wait(pending, timeout=PROGRESS_INTERVAL_SECONDS, return_when=FIRST_COMPLETED)
                for future in done:
                    source = owners[future]
                    try:
                        future.result()
                    except (net.HttpStatusError, net.NetworkError) as error:
                        raise DownloadError(f"stream {source.stream.id}: {error}") from error
                    parts_left[source.stream.id] -= 1
                    if parts_left[source.stream.id] == 0 and source.playlist is not None:
                        _join_segments(source, work_dir)
                on_progress(len(owners) - len(pending), len(owners), sizes.total())
        except BaseException:
            _stop(pool, cancel)
            raise
    with ThreadPoolExecutor(jobs) as pool:
        durations = list(pool.map(lambda source: ffmpeg.probe_duration(_target(source, work_dir)), sources))
    return [AudioFile(source.stream, _target(source, work_dir), duration) for source, duration in zip(sources, durations)]


def _stop(pool: ThreadPoolExecutor, cancel: threading.Event) -> None:
    # Running requests notice the event at their next retry or chunk, queued ones never start.
    cancel.set()
    pool.shutdown(cancel_futures=True)


def _resolve(stream: Stream, prefer_mp4: bool, cancel: threading.Event) -> Source | None:
    if prefer_mp4 or not stream.hls_url:
        return _mp4_source(stream)
    try:
        master = net.get(stream.hls_url, cancel=cancel).decode(errors="replace")
        audio_url = hls.audio_rendition_url(master, stream.hls_url)
        if audio_url is None:
            # Screen sharing has no audio at all; audio muxed into the video variant would only be in the mp4.
            return _mp4_source(stream) if "mp4a" in master else None
        playlist = net.get(audio_url, cancel=cancel).decode(errors="replace")
        return Source(stream, hls.parse_media_playlist(playlist, audio_url))
    except (net.HttpStatusError, net.NetworkError, hls.PlaylistError) as error:
        if cancel.is_set():
            raise net.Cancelled() from error
        log.warning("stream %d: no HLS audio (%s), taking the audio from the mp4", stream.id, error)
        return _mp4_source(stream)


def _mp4_source(stream: Stream) -> Source | None:
    return Source(stream, None) if ffmpeg.has_audio(stream.url) else None


def _tasks(
    source: Source, work_dir: str, sizes: _SizeCounter, cancel: threading.Event
) -> list[Callable[[], None]]:
    target = _target(source, work_dir)
    if source.playlist is None:
        return [functools.partial(net.download, source.stream.url, target, functools.partial(sizes.set, target), cancel)]
    segment_dir = _segment_dir(source, work_dir)
    os.makedirs(segment_dir, exist_ok=True)
    urls = [source.playlist.init_url, *source.playlist.segment_urls]
    return [
        functools.partial(_fetch_part, url, _part_path(segment_dir, index), sizes, cancel)
        for index, url in enumerate(urls)
    ]


def _fetch_part(url: str, path: str, sizes: _SizeCounter, cancel: threading.Event) -> None:
    if not os.path.exists(path):
        data = net.get(url, cancel=cancel)
        with open(path + ".tmp", "wb") as out:
            out.write(data)
        os.replace(path + ".tmp", path)
    sizes.set(path, os.path.getsize(path))


def _join_segments(source: Source, work_dir: str) -> None:
    segment_dir = _segment_dir(source, work_dir)
    target = _target(source, work_dir)
    # The init section followed by the media segments is a valid fragmented mp4.
    with open(target + ".tmp", "wb") as out:
        for index in range(len(source.playlist.segment_urls) + 1):
            with open(_part_path(segment_dir, index), "rb") as part:
                shutil.copyfileobj(part, out)
    os.replace(target + ".tmp", target)
    shutil.rmtree(segment_dir)


def _target(source: Source, work_dir: str) -> str:
    extension = "mp4" if source.playlist is None else "m4a"
    return os.path.join(work_dir, f"{source.stream.id}.{extension}")


def _segment_dir(source: Source, work_dir: str) -> str:
    return os.path.join(work_dir, str(source.stream.id))


def _part_path(segment_dir: str, index: int) -> str:
    return os.path.join(segment_dir, f"{index:05d}")


class _SizeCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sizes: dict[str, int] = {}

    def set(self, key: str, size: int) -> None:
        with self._lock:
            self._sizes[key] = size

    def total(self) -> int:
        with self._lock:
            return sum(self._sizes.values())
