import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from mtsdump import ffmpeg
from mtsdump.download import AudioFile

Interval = tuple[float, float]

NOISE_FLOOR_DB = -50
MIX_MIN_SILENCE_SECONDS = 2
# Streams are scanned with a finer step, otherwise a short pause at a stream edge counts as sound.
STREAM_MIN_SILENCE_SECONDS = 0.5
# volumedetect floors at -91 dB on digital silence.
DIGITAL_SILENCE_DB = -90.0
TOLERANCE_SECONDS = 0.5

SILENCE_START = re.compile(r"silence_start: (-?\d+(?:\.\d+)?)")
SILENCE_END = re.compile(r"silence_end: (-?\d+(?:\.\d+)?)")
VOLUME = re.compile(r"(mean|max)_volume: (-?\d+(?:\.\d+)?|-inf) dB")


@dataclass(frozen=True)
class Levels:
    mean_db: float
    max_db: float
    silences: list[Interval]


@dataclass(frozen=True)
class Report:
    length: float
    expected_length: float
    levels: Levels
    unexplained_silences: list[Interval]
    silent_stream_ids: list[int]


def verify(mix_path: str, files: list[AudioFile], jobs: int) -> Report:
    length = ffmpeg.probe_duration(mix_path)
    with ThreadPoolExecutor(jobs) as pool:
        pending_mix_levels = pool.submit(_measure, mix_path, length, MIX_MIN_SILENCE_SECONDS)
        stream_levels = list(pool.map(lambda file: _measure(file.path, file.duration, STREAM_MIN_SILENCE_SECONDS), files))
        mix_levels = pending_mix_levels.result()
    streams = [(file.stream.start, file.duration, levels.silences) for file, levels in zip(files, stream_levels)]
    return Report(
        length=length,
        expected_length=max(file.stream.start + file.duration for file in files),
        levels=mix_levels,
        unexplained_silences=_unexplained_silences(mix_levels.silences, streams),
        silent_stream_ids=[
            file.stream.id for file, levels in zip(files, stream_levels) if levels.max_db <= DIGITAL_SILENCE_DB
        ],
    )


def is_digital_silence(path: str) -> bool:
    volumes = dict(VOLUME.findall(ffmpeg.analyze(path, "volumedetect")))
    return float(volumes.get("max", "-inf")) <= DIGITAL_SILENCE_DB


def _measure(path: str, duration: float, min_silence: float) -> Levels:
    # A hole in the timestamps turns into silence in the mix, so fill it the same way before looking for silence.
    filters = f"aresample=async=1:first_pts=0,silencedetect=noise={NOISE_FLOOR_DB}dB:d={min_silence},volumedetect"
    return _parse_levels(ffmpeg.analyze(path, filters), duration)


def _parse_levels(output: str, duration: float) -> Levels:
    starts = [max(0.0, float(value)) for value in SILENCE_START.findall(output)]
    ends = [float(value) for value in SILENCE_END.findall(output)]
    # A file that ends in silence gets a silence_start without a matching silence_end.
    ends += [duration] * (len(starts) - len(ends))
    volumes = {kind: float(value) for kind, value in VOLUME.findall(output)}
    return Levels(
        mean_db=volumes.get("mean", float("-inf")),
        max_db=volumes.get("max", float("-inf")),
        silences=list(zip(starts, ends)),
    )


def _unexplained_silences(
    mix_silences: list[Interval], streams: list[tuple[float, float, list[Interval]]]
) -> list[Interval]:
    """Mix silences during which some stream still has sound of its own.

    streams: (session start, duration, own silences) per stream.
    """
    sounds = [sound for start, duration, silences in streams for sound in _sounds(start, duration, silences)]
    return [
        silence
        for silence in mix_silences
        if any(_overlap(silence, sound) > TOLERANCE_SECONDS for sound in sounds)
    ]


def _overlap(first: Interval, second: Interval) -> float:
    return max(0.0, min(first[1], second[1]) - max(first[0], second[0]))


def _sounds(start: float, duration: float, silences: list[Interval]) -> list[Interval]:
    sounds, cursor = [], start
    for silence_start, silence_end in silences:
        if start + silence_start > cursor:
            sounds.append((cursor, start + silence_start))
        cursor = max(cursor, start + silence_end)
    if cursor < start + duration:
        sounds.append((cursor, start + duration))
    return sounds
