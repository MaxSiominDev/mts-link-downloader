import json
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Callable

TOOLS = ("ffmpeg", "ffprobe")
ERROR_TAIL_CHARS = 2000
SHOWN_PROBLEM_LINES = 5
# ffprobe over HTTP: the delivery hosts stall and drop connections, so time out and reconnect.
REMOTE_OPTIONS = (
    "-rw_timeout", "30000000",
    "-reconnect", "1",
    "-reconnect_on_network_error", "1",
    "-reconnect_max_retries", "10",
)


log = logging.getLogger(__name__)


class FfmpegError(Exception):
    pass


def require_tools() -> None:
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    if missing:
        raise FfmpegError(f"{' and '.join(missing)} not found in PATH, install ffmpeg (brew install ffmpeg on macOS)")


def run(args: list[str], total_seconds: float, on_progress: Callable[[float], None]) -> None:
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y", *args]
    with (
        tempfile.TemporaryFile() as errors,
        subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=errors, encoding="utf-8", errors="replace"
        ) as process,
    ):
        try:
            for line in process.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.isdigit() and total_seconds > 0:
                    on_progress(min(int(value) / 1_000_000 / total_seconds, 1.0))
        except BaseException:
            process.kill()
            raise
        errors.seek(0)
        message = errors.read().decode(errors="replace").strip()
        if process.wait() != 0:
            raise FfmpegError(message[-ERROR_TAIL_CHARS:] or f"ffmpeg exited with code {process.returncode}")
    # ffmpeg exits with 0 after skipping broken packets, the output then has a glitch where they were.
    if message:
        lines = message.splitlines()
        shown = "\n".join(f"  {line}" for line in lines[:SHOWN_PROBLEM_LINES])
        log.warning("ffmpeg reported %d problems in the input, the audio may glitch there:\n%s", len(lines), shown)


def probe_duration(path: str) -> float:
    # A video track can outlast the audio, so prefer the audio stream duration.
    info = json.loads(_probe("-select_streams", "a:0", "-show_entries", "stream=duration:format=duration", "-of", "json", path))
    streams = info.get("streams") or [{}]
    for duration in (streams[0].get("duration"), info.get("format", {}).get("duration")):
        try:
            return float(duration)
        except (TypeError, ValueError):
            continue
    raise FfmpegError(f"ffprobe found no duration in {path}")


def has_audio(target: str) -> bool:
    remote = REMOTE_OPTIONS if target.startswith(("http://", "https://")) else ()
    return "audio" in _probe(*remote, "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", target)


def analyze(path: str, filters: str) -> str:
    """Runs report-only filters such as volumedetect and returns ffmpeg's stderr."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", path, "-map", "0:a:0", "-af", filters, "-f", "null", "-"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise FfmpegError(result.stderr.strip()[-ERROR_TAIL_CHARS:])
    return result.stderr


def _probe(*args: str) -> str:
    result = subprocess.run(["ffprobe", "-v", "error", *args], capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise FfmpegError(result.stderr.strip()[-ERROR_TAIL_CHARS:] or f"ffprobe failed on {args[-1]}")
    return result.stdout.strip()
