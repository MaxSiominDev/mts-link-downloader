import os
from collections.abc import Callable
from dataclasses import dataclass

from mtsdump import ffmpeg

MIX_SAMPLE_RATE = 48000
SINGLE_PASS_LIMIT = 50
GROUP_SIZE = 20
# The ceiling is -3 dBFS because mp3 encoding lifts peaks by up to 2 dB (measured at 64k).
LOUDNESS = "loudnorm=I=-16:TP=-3:LRA=11"
# level=0 turns off the automatic gain of alimiter, which would push the limited peaks back to full scale.
LIMITER = "alimiter=limit=0.708:level=0"
ASR_HIGHPASS = "highpass=f=70"


@dataclass(frozen=True)
class Track:
    path: str
    # seconds from session start
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass(frozen=True)
class Encoding:
    sample_rate: int
    bitrate: str
    highpass: bool


def mix(
    tracks: list[Track],
    output_path: str,
    encoding: Encoding,
    work_dir: str,
    on_progress: Callable[[str, float], None],
    pad_to: float | None = None,
    normalize_loudness: bool = True,
) -> None:
    """Sums the tracks at their session offsets and writes mp3.

    pad_to pads with silence, so a per-speaker track comes out as long as the full mix.
    normalize_loudness=False keeps the recorded mic levels and runs the limiter only.
    """
    length = max(pad_to or 0.0, *(track.end for track in tracks))
    if len(tracks) > SINGLE_PASS_LIMIT:
        tracks = _premix_in_groups(tracks, work_dir, on_progress)
    tail = [ASR_HIGHPASS] if encoding.highpass else []
    if pad_to is not None:
        tail.append(f"apad=whole_dur={pad_to}")
    if normalize_loudness:
        tail.append(LOUDNESS)
    tail += [LIMITER, f"aresample={encoding.sample_rate}"]
    ffmpeg.run(
        [
            *_inputs(tracks),
            "-filter_complex", _mix_graph(tracks, ",".join(tail)),
            "-map", "[out]",
            "-c:a", "libmp3lame",
            "-b:a", encoding.bitrate,
            output_path,
        ],
        length,
        lambda fraction: on_progress("mixing", fraction),
    )


def _mix_graph(tracks: list[Track], tail: str) -> str:
    chains = [
        f"[{index}:a]aresample=async=1:first_pts=0,"
        f"aformat=sample_fmts=fltp:sample_rates={MIX_SAMPLE_RATE}:channel_layouts=mono,"
        f"adelay={round(track.start * 1000)}:all=1[a{index}]"
        for index, track in enumerate(tracks)
    ]
    labels = "".join(f"[a{index}]" for index in range(len(tracks)))
    # normalize=0 makes amix a plain sum; by default it divides every input by the number of inputs.
    mixed = f"{labels}amix=inputs={len(tracks)}:duration=longest:normalize=0"
    return ";".join([*chains, f"{mixed},{tail}[out]" if tail else f"{mixed}[out]"])


def _premix_in_groups(tracks: list[Track], work_dir: str, on_progress: Callable[[str, float], None]) -> list[Track]:
    groups = [tracks[index:index + GROUP_SIZE] for index in range(0, len(tracks), GROUP_SIZE)]
    premixed = []
    for number, group in enumerate(groups, start=1):
        path = os.path.join(work_dir, f"group{number}.wv")
        stage = f"premixing group {number}/{len(groups)}"
        length = max(track.end for track in group)
        # WavPack keeps the float sum lossless, so summing the groups gives the same result as one big mix.
        ffmpeg.run(
            [
                *_inputs(group),
                "-filter_complex", _mix_graph(group, ""),
                "-map", "[out]",
                "-c:a", "wavpack",
                "-sample_fmt", "fltp",
                path,
            ],
            length,
            lambda fraction, stage=stage: on_progress(stage, fraction),
        )
        premixed.append(Track(path, start=0.0, duration=length))
    return premixed


def _inputs(tracks: list[Track]) -> list[str]:
    return [argument for track in tracks for argument in ("-i", track.path)]
