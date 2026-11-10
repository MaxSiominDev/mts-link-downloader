import itertools
import math
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from mtsdump import ffmpeg
from mtsdump.record import Speaker, Stream

# Reconnects leave gaps of a second or two between the streams of one camera; such a gap is shown as a frozen frame
GAP_TOLERANCE_SECONDS = 5.0
CANVAS_WIDTH = 1280
CANVAS_HEIGHT = 720
STRIP_HEIGHT = 120
STRIP_TILES = CANVAS_WIDTH // STRIP_HEIGHT
MAX_TILES = 25
VIDEO_FPS = 25
VIDEO_BITRATE = "2500k"
AUDIO_BITRATE = "128k"
# A stream that ended inside the interval is held by its last second, which tpad clones over the rest of the gap.
# One frame is not enough: the frame nearest the metadata end can sit further back at any fps
FROZEN_WINDOW_SECONDS = 1.0


@dataclass(frozen=True)
class TilePiece:
    stream: Stream
    trim_start: float
    length: float
    # Seconds of cloned last frame that follow the shown part of the stream
    pad_seconds: float


@dataclass(frozen=True)
class Interval:
    start: float
    end: float
    share: TilePiece | None
    tiles: tuple[TilePiece, ...]

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Plan:
    intervals: tuple[Interval, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _Box:
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class _Chain:
    # Streams of one camera, ordered by start; a gap of up to the tolerance between them counts as one camera
    streams: tuple[Stream, ...]
    session_end: float

    @property
    def start(self) -> float:
        return self.streams[0].start

    @property
    def end(self) -> float:
        return max(_end(stream, self.session_end) for stream in self.streams)


def plan(
    duration: float,
    cameras: dict[Speaker, list[Stream]],
    shares: list[Stream] | None = None,
    black: dict[int, list[tuple[float, float]]] | None = None,
) -> Plan:
    """Lays the meeting video out as the web player does: screen share large, cameras in a grid below or instead of it.

    The session is split into intervals where the set of visible tiles is constant, and every tile shows
    one stream per interval. A tile whose stream is black over its whole interval (a camera left on with
    nobody in front of it) does not occupy the layout when its black windows are given
    """
    camera_chains = {speaker: chains for speaker, chains in
                     ((speaker, _chains(streams, duration)) for speaker, streams in cameras.items()) if chains}
    share_chains = _chains(shares or [], duration)
    order = sorted(camera_chains, key=lambda speaker: min(chain.start for chain in camera_chains[speaker]))
    if not camera_chains and not share_chains:
        return Plan((), ())
    boundaries = {0.0, duration}
    for chains in [*camera_chains.values(), share_chains]:
        for chain in chains:
            for stream in chain.streams:
                boundaries.update((_end(stream, duration), stream.start))
    warnings: set[str] = set()
    points = sorted(boundaries)
    intervals = []
    for start, end in itertools.pairwise(points):
        if end <= start:
            continue
        share = None
        if covering := [chain for chain in share_chains if chain.start <= start < chain.end]:
            share = _piece(_current_stream(max(covering, key=lambda chain: chain.start), start), start, end, duration)
        visible = [speaker for speaker in order if _is_covered(camera_chains[speaker], start)]
        tiles = tuple(
            _piece(_current_stream(_covering_chain(camera_chains[speaker], start), start), start, end, duration)
            for speaker in visible
        )
        tiles = tuple(piece for piece in tiles if _has_picture(piece, black))
        limit = STRIP_TILES if share else MAX_TILES
        if len(tiles) > limit:
            warnings.add(f"only {limit} of {len(tiles)} camera tiles fit, the rest are hidden")
            tiles = tiles[:limit]
        intervals.append(Interval(start, end, share, tiles))
    return Plan(tuple(intervals), tuple(sorted(warnings)))


def scan_black(paths: dict[int, str], jobs: int, on_progress: Callable[[int, int], None]) -> dict[int, list[tuple[float, float]]]:
    """Finds the fully black spans of every stream in parallel, so the planner can drop camera-off tiles."""
    done = 0
    result = {}
    with ThreadPoolExecutor(jobs) as pool:
        for stream_id, windows in zip(paths, pool.map(ffmpeg.black_windows, paths.values())):
            result[stream_id] = windows
            done += 1
            on_progress(done, len(paths))
    return result


def render(
    plan: Plan,
    paths: dict[int, str],
    audio_path: str | None,
    output_path: str,
    work_dir: str,
    on_progress: Callable[[float], None],
) -> None:
    """Renders every interval into a segment and joins the segments with the audio mix into output_path.

    Finished segments stay in work_dir/video, so an interrupted run encodes only what is left
    """
    segment_dir = os.path.join(work_dir, "video")
    os.makedirs(segment_dir, exist_ok=True)
    total = sum(interval.length for interval in plan.intervals)
    done = 0.0
    for number, interval in enumerate(plan.intervals):
        segment = os.path.join(segment_dir, f"{number:05d}.mp4")
        if not os.path.exists(segment):
            ffmpeg.run(_segment_args(interval, paths, segment + ".tmp.mp4"), interval.length,
                       lambda fraction, done=done, length=interval.length: on_progress((done + fraction * length) / total))
            os.replace(segment + ".tmp.mp4", segment)
        done += interval.length
    list_path = os.path.join(segment_dir, "concat.txt")
    with open(list_path, "w") as out:
        for number in range(len(plan.intervals)):
            out.write(f"file '{os.path.join(segment_dir, f'{number:05d}.mp4')}'\n")
    args = ["-f", "concat", "-safe", "0", "-i", list_path]
    if audio_path:
        args += ["-i", audio_path, "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", AUDIO_BITRATE]
    else:
        args += ["-map", "0:v:0"]
    args += ["-c:v", "copy", "-movflags", "+faststart",
             # the .part suffix of the output path hides the .mp4 one, so name the muxer explicitly
             "-f", "mp4", output_path + ".part"]
    ffmpeg.run(args, total, on_progress)
    os.replace(output_path + ".part", output_path)


def _segment_args(interval: Interval, paths: dict[int, str], output: str) -> list[str]:
    boxes = _boxes(interval)
    inputs = []
    for piece, _ in boxes:
        # The input frame rate keeps a sparse stream (a static camera is recorded at ~0.1 fps) from
        # starving the overlay chain, which deadlocks ffmpeg when another input is seeked at the same time
        inputs += [
            "-r", str(VIDEO_FPS),
            "-ss", f"{piece.trim_start:.3f}",
            "-t", f"{piece.length:.3f}",
            "-i", paths[piece.stream.id],
        ]
    chains = []
    for index, (piece, box) in enumerate(boxes):
        filters = []
        if piece.pad_seconds > 0:
            filters.append(f"tpad=stop_mode=clone:stop_duration={piece.pad_seconds:.3f}")
        filters += [
            f"scale={box.w}:{box.h}:force_original_aspect_ratio=decrease",
            f"pad={box.w}:{box.h}:(ow-iw)/2:(oh-ih)/2",
        ]
        chains.append(f"[{index}:v]" + ",".join(filters) + f"[t{index}]")
    graph = f"color=black:s={CANVAS_WIDTH}x{CANVAS_HEIGHT}:d={interval.length:.3f}[base]"
    if boxes:
        graph += ";" + ";".join(chains)
        previous = "base"
        for index, (_, box) in enumerate(boxes):
            label = "v" if index == len(boxes) - 1 else f"o{index}"
            graph += f";[{previous}][t{index}]overlay={box.x}:{box.y}[{label}]"
            previous = label
        mapped = "[v]"
    else:
        mapped = "[base]"
    return [
        *inputs,
        "-filter_complex", graph,
        "-map", mapped,
        "-r", str(VIDEO_FPS),
        # Frame rate resampling of a sparse input can overrun the interval, so cap the segment at its length
        "-t", f"{interval.length:.3f}",
        "-c:v", "h264_videotoolbox", "-b:v", VIDEO_BITRATE,
        "-pix_fmt", "yuv420p",
        output,
    ]


def _boxes(interval: Interval) -> list[tuple[TilePiece, _Box]]:
    if interval.share:
        tiles = interval.tiles[:STRIP_TILES]
        boxes = [_Box(index * (CANVAS_WIDTH // STRIP_TILES), CANVAS_HEIGHT - STRIP_HEIGHT,
                      CANVAS_WIDTH // STRIP_TILES, STRIP_HEIGHT) for index in range(len(tiles))]
        return [(interval.share, _Box(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT - STRIP_HEIGHT)), *zip(tiles, boxes)]
    tiles = interval.tiles
    if not tiles:
        return []
    columns = math.ceil(math.sqrt(len(tiles)))
    rows = math.ceil(len(tiles) / columns)
    width = _even(CANVAS_WIDTH // columns)
    height = _even(CANVAS_HEIGHT // rows)
    return [
        (piece, _Box((index % columns) * width, (index // columns) * height, width, height))
        for index, piece in enumerate(tiles)
    ]


def _piece(stream: Stream, start: float, end: float, session_end: float) -> TilePiece:
    stream_end = _end(stream, session_end)
    if stream_end >= end:
        return TilePiece(stream, start - stream.start, end - start, 0.0)
    if stream_end > start:
        return TilePiece(stream, start - stream.start, stream_end - start, end - stream_end)
    frozen = min(FROZEN_WINDOW_SECONDS, stream_end - stream.start)
    return TilePiece(stream, stream_end - stream.start - frozen, frozen, (end - start) - frozen)


def _has_picture(piece: TilePiece, black: dict[int, list[tuple[float, float]]] | None) -> bool:
    if black is None:
        return True
    covered = 0.0
    for start, end in black.get(piece.stream.id, ()):
        covered += max(0.0, min(end, piece.trim_start + piece.length) - max(start, piece.trim_start))
    remaining = piece.length - covered
    # A window shorter than a second is dropped only when it is fully black; a longer one must keep a
    # second of picture, so a stray keyframe does not glue a black camera into the grid
    return remaining > 0.01 if piece.length <= 1.0 else remaining > 1.0


def _is_covered(chains: list[_Chain], moment: float) -> bool:
    return any(chain.start <= moment < chain.end for chain in chains)


def _covering_chain(chains: list[_Chain], moment: float) -> _Chain:
    return next(chain for chain in chains if chain.start <= moment < chain.end)


def _current_stream(chain: _Chain, moment: float) -> Stream:
    live = [stream for stream in chain.streams if stream.start <= moment < _end(stream, chain.session_end)]
    if live:
        return live[-1]
    # The moment sits in a reconnect gap: the camera holds the frames of the stream that ended last
    ended = [stream for stream in chain.streams if _end(stream, chain.session_end) <= moment]
    return max(ended, key=lambda stream: _end(stream, chain.session_end))


def _chains(streams: list[Stream], session_end: float) -> list[_Chain]:
    chains: list[list[Stream]] = []
    current: list[Stream] = []
    for stream in sorted((s for s in streams if _end(s, session_end) > s.start), key=lambda stream: stream.start):
        if current and stream.start > _end(current[-1], session_end) + GAP_TOLERANCE_SECONDS:
            chains.append(current)
            current = []
        current.append(stream)
    if current:
        chains.append(current)
    return [_Chain(tuple(chain), session_end) for chain in chains]


def _end(stream: Stream, session_end: float) -> float:
    return stream.start + stream.duration if stream.duration is not None else session_end


def _even(value: int) -> int:
    return value - value % 2
