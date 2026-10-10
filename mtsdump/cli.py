import argparse
import contextlib
import dataclasses
import errno
import functools
import logging
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

from mtsdump import __version__, api, download, ffmpeg, mix, naming, net, verify, vk
from mtsdump.console import Console, ConsoleHandler
from mtsdump.cookies import CookieError, chrome_cookie_header
from mtsdump.links import RecordRef, VkVideoRef, parse_record_ref, parse_vk_video_ref
from mtsdump.record import Record, RecordFormatError, Speaker, parse_record

DEFAULT_JOBS = 6
LISTENING = mix.Encoding(sample_rate=48000, bitrate="64k", highpass=False)
ASR = mix.Encoding(sample_rate=16000, bitrate="32k", highpass=True)
SPARSE_COVERAGE = 0.5
MAX_REPORTED_SILENCES = 10
RECORD_ERRORS = (
    api.AccessDenied,
    api.ApiError,
    CookieError,
    RecordFormatError,
    download.DownloadError,
    ffmpeg.FfmpegError,
    net.HttpStatusError,
    net.NetworkError,
    OSError,
    vk.AccessDenied,
    vk.ApiError,
)

log = logging.getLogger("mtsdump")


@dataclasses.dataclass(frozen=True)
class SpeakerTrack:
    speaker: Speaker | None
    label: str
    path: str


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        refs = [_ref(value) for value in args.records]
        naming.check_template(args.name)
    except ValueError as error:
        parser.error(str(error))
    console = Console()
    logging.basicConfig(level=logging.INFO, handlers=[ConsoleHandler(console)], force=True)
    if sys.platform != "darwin":
        log.error("mtsdump runs on macOS only")
        return 1
    try:
        ffmpeg.require_tools()
    except ffmpeg.FfmpegError as error:
        log.error("%s", error)
        return 1
    cookie_for = _cookie_source(args)
    failures = 0
    try:
        for ref in refs:
            process = _process_vk if isinstance(ref, VkVideoRef) else _process
            try:
                process(ref, args, cookie_for, console)
            except RECORD_ERRORS as error:
                console.end_progress()
                log.error("record %s: %s", ref.session_id, _describe(error))
                failures += 1
    except KeyboardInterrupt:
        console.end_progress()
        log.error("interrupted, downloaded parts stay in <id>.parts in the output folder for the next run")
        return 130
    return 1 if failures else 0


def _ref(value: str) -> RecordRef | VkVideoRef:
    try:
        return parse_record_ref(value)
    except ValueError:
        pass
    try:
        return parse_vk_video_ref(value)
    except ValueError:
        raise ValueError(f"not a record link, a session id or a vkvideo.ru video link: {value!r}") from None


def _process_vk(ref: VkVideoRef, args: argparse.Namespace, cookie_for: api.CookieSource, console: Console) -> None:
    if args.list:
        raise vk.ApiError("--list is not supported for VK links")
    video = vk.fetch_video(ref, cookie_for)
    log.info("%s: %s", video.session_id, video.title)
    output_dir = os.path.expanduser(args.output)
    os.makedirs(output_dir, exist_ok=True)
    main_path = os.path.join(
        output_dir, naming.filename_for(args.name, video.session_id, video.title, video.started_at)
    )
    if os.path.exists(main_path):
        log.info("already saved: %s", main_path)
        return
    if args.mp4 and not video.mp4_url:
        raise vk.ApiError(f"video {ref.session_id} has no mp4 file")
    source = video.mp4_url
    if not args.mp4:
        master = net.get(video.hls_url, headers={"User-Agent": vk.USER_AGENT})
        source = vk.cheapest_rendition(master, video.hls_url)
    # Written next to the result first, so an interrupted run never leaves a broken mp3 that looks finished.
    temporary = main_path + ".part"
    try:
        vk.encode_audio(
            source,
            temporary,
            video.duration,
            _encoding(args),
            lambda fraction: console.progress("extracting audio", fraction),
        )
        os.replace(temporary, main_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    log.info("saved %s", main_path)


def _process(ref: RecordRef, args: argparse.Namespace, cookie_for: api.CookieSource, console: Console) -> None:
    record = parse_record(api.fetch_record(ref, cookie_for))
    log.info("%s: %s", record.session_id, record.name)
    output_dir = os.path.expanduser(args.output)
    main_path = os.path.join(output_dir, naming.filename(args.name, record))
    if os.path.exists(main_path) and not (args.list or args.split_speakers):
        log.info("already saved: %s", main_path)
        return
    if record.has_cuts:
        log.warning("the record was cut in the MTS Link editor, the mix keeps the cut parts, "
                    "so timing drifts from the player after the first cut")
    if record.has_converted_records:
        log.warning("the record has a converted file, it is not used and the mix is built from the streams")
    sources = download.resolve_sources(record.streams, prefer_mp4=args.mp4, jobs=args.jobs)
    audio = [source for source in sources if source is not None]
    log.info("%d streams, %d with audio, %d without", len(sources), len(audio), len(sources) - len(audio))
    _warn_if_sparse(record, audio)
    if args.list:
        _print_streams(record, sources)
    elif not audio:
        raise download.DownloadError("no stream has audio")
    else:
        _build(record, audio, main_path, output_dir, args, console)


def _build(
    record: Record,
    audio: list[download.Source],
    main_path: str,
    output_dir: str,
    args: argparse.Namespace,
    console: Console,
) -> None:
    speaker_tracks = _speaker_tracks(args.name, record, audio, output_dir) if args.split_speakers else []
    missing_tracks = [track for track in speaker_tracks if not os.path.exists(track.path)]
    main_missing = not os.path.exists(main_path)
    if not main_missing and not missing_tracks:
        log.info("the mix and every speaker track are already saved")
        return
    work_dir = os.path.join(output_dir, f"{record.session_id}.parts")
    os.makedirs(work_dir, exist_ok=True)
    files = download.download_all(audio, work_dir, args.jobs, functools.partial(_show_download, console))
    console.end_progress()
    log.info("downloaded %d streams, %.0f MB", len(files), sum(os.path.getsize(file.path) for file in files) / 1e6)

    encoding = _encoding(args)
    length = max(file.stream.start + file.duration for file in files)
    log.info("mix length %s, session length %s", _clock(length), _clock(record.duration))
    saved = []
    if main_missing:
        _mix(files, main_path, encoding, work_dir, console)
        saved.append(main_path)
    for track in missing_tracks:
        own_files = [file for file in files if file.stream.speaker == track.speaker]
        if _all_silent(own_files, args.jobs):
            log.info("no sound from %s in the whole record, no track saved", track.label)
            continue
        _mix(
            own_files,
            track.path,
            encoding,
            work_dir,
            console,
            label=track.label,
            pad_to=length,
            normalize_loudness=False,
        )
        saved.append(track.path)
    if args.verify:
        _log_report(verify.verify(main_path, files, args.jobs))
    if args.keep:
        log.info("sources kept in %s", work_dir)
    else:
        shutil.rmtree(work_dir)
    for path in saved:
        log.info("saved %s", path)


def _speaker_tracks(template: str, record: Record, audio: list[download.Source], output_dir: str) -> list[SpeakerTrack]:
    labels = naming.speaker_labels({source.stream.speaker for source in audio})
    return [
        SpeakerTrack(speaker, label, os.path.join(output_dir, naming.filename(template, record, label)))
        for speaker, label in sorted(labels.items(), key=lambda item: item[1])
    ]


def _mix(
    files: list[download.AudioFile],
    path: str,
    encoding: mix.Encoding,
    work_dir: str,
    console: Console,
    label: str = "",
    pad_to: float | None = None,
    normalize_loudness: bool = True,
) -> None:
    # Written next to the parts first, so an interrupted run never leaves a broken mp3 that looks finished.
    temporary = os.path.join(work_dir, "mix.mp3")
    tracks = [mix.Track(file.path, file.stream.start, file.duration) for file in files]
    mix.mix(
        tracks,
        temporary,
        encoding,
        work_dir,
        lambda stage, fraction: console.progress(f"{stage} {label}".rstrip(), fraction),
        pad_to=pad_to,
        normalize_loudness=normalize_loudness,
    )
    console.end_progress()
    os.replace(temporary, path)


def _all_silent(files: list[download.AudioFile], jobs: int) -> bool:
    with ThreadPoolExecutor(jobs) as pool:
        return all(pool.map(lambda file: verify.is_digital_silence(file.path), files))


def _show_download(console: Console, done: int, total: int, size: int) -> None:
    console.progress("downloading", done / total, f"{done}/{total} parts, {size / 1e6:.0f} MB")


def _warn_if_sparse(record: Record, audio: list[download.Source]) -> None:
    covered = _covered_seconds(
        [(source.stream.start, source.stream.start + _length(source, record)) for source in audio]
    )
    if covered < SPARSE_COVERAGE * record.duration:
        log.warning("streams with audio cover only %s of the %s session", _clock(covered), _clock(record.duration))


def _covered_seconds(intervals: list[tuple[float, float]]) -> float:
    covered, reached = 0.0, float("-inf")
    for start, end in sorted(intervals):
        if end > reached:
            covered += end - max(start, reached)
            reached = end
    return covered


def _length(source: download.Source, record: Record) -> float:
    if source.playlist is not None:
        return source.playlist.duration
    if source.stream.duration is not None:
        return source.stream.duration
    return record.duration - source.stream.start


def _print_streams(record: Record, sources: list[download.Source | None]) -> None:
    lines = [f"{'stream':>10}  {'start':>8}  {'length':>8}  audio  speaker"]
    for stream, source in zip(record.streams, sources):
        length = _length(source, record) if source else stream.duration
        shown_length = _clock(length) if length is not None else "?"
        speaker = stream.speaker.nickname if stream.speaker else "-"
        lines.append(f"{stream.id:>10}  {_clock(stream.start):>8}  {shown_length:>8}  {'yes' if source else 'no':<5}  {speaker}")
    # Flushed right away so the table stays between the log lines of its record when stdout is a pipe.
    print("\n".join(lines), flush=True)


def _log_report(report: verify.Report) -> None:
    drift = abs(report.length - report.expected_length)
    log.log(
        logging.WARNING if drift > verify.TOLERANCE_SECONDS else logging.INFO,
        "verify: length %s, the streams end at %s",
        _clock(report.length, decimals=1),
        _clock(report.expected_length, decimals=1),
    )
    log.info("verify: mean %.1f dB, max %.1f dB", report.levels.mean_db, report.levels.max_db)
    unexplained = report.unexplained_silences
    log.info(
        "verify: %d silences of %d s or longer in the mix, %d of them while some stream has sound",
        len(report.levels.silences),
        verify.MIX_MIN_SILENCE_SECONDS,
        len(unexplained),
    )
    for start, end in unexplained[:MAX_REPORTED_SILENCES]:
        log.warning("verify: the mix is silent at %s-%s while a stream has sound there", _clock(start), _clock(end))
    if len(unexplained) > MAX_REPORTED_SILENCES:
        log.warning("verify: %d more such silences", len(unexplained) - MAX_REPORTED_SILENCES)
    if report.silent_stream_ids:
        log.info(
            "verify: %d streams are digital silence: %s",
            len(report.silent_stream_ids),
            ", ".join(map(str, report.silent_stream_ids)),
        )


def _describe(error: Exception) -> str:
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return "disk full, downloaded parts are kept, run again after freeing space"
    return str(error)


def _cookie_source(args: argparse.Namespace) -> api.CookieSource:
    if args.cookie:
        cookie = args.cookie.strip()
        if cookie.lower().startswith("cookie:"):
            cookie = cookie[len("cookie:"):].strip()
        return lambda host: cookie
    if args.cookies_from_browser:
        profile = args.cookies_from_browser.partition(":")[2] or None
        return functools.cache(lambda host: chrome_cookie_header(host, profile))
    return lambda host: None


def _encoding(args: argparse.Namespace) -> mix.Encoding:
    encoding = ASR if args.asr else LISTENING
    return dataclasses.replace(encoding, bitrate=args.bitrate) if args.bitrate else encoding


def _clock(seconds: float, decimals: int = 0) -> str:
    scale = 10**decimals
    hours, rest = divmod(round(seconds * scale), 3600 * scale)
    minutes, rest = divmod(rest, 60 * scale)
    whole, fraction = divmod(rest, scale)
    text = f"{hours}:{minutes:02d}:{whole:02d}"
    return f"{text}.{fraction:0{decimals}d}" if decimals else text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mtsdump", description="Builds one mp3 from an MTS Link meeting recording.")
    parser.add_argument("--version", action="version", version=f"mtsdump {__version__}")
    parser.add_argument(
        "records", nargs="+", metavar="url|id",
        help="record-new link, event session id or vkvideo.ru video link",
    )
    parser.add_argument("-o", "--output", default=".", metavar="DIR", help="where to save the files (default: current folder)")
    parser.add_argument(
        "--name",
        default=naming.DEFAULT_TEMPLATE,
        metavar="TEMPLATE",
        help="file name built from {date}, {name} and {id} (default: %(default)s)",
    )
    parser.add_argument("-b", "--bitrate", metavar="RATE", help="mp3 bitrate (default: 64k, or 32k with --asr)")
    parser.add_argument("--asr", action="store_true", help="16 kHz mono with a 70 Hz high-pass, for speech recognition")
    parser.add_argument("--keep", action="store_true", help="keep the downloaded streams in DIR/<id>.parts")
    parser.add_argument("--mp4", action="store_true", help="take the audio from the full mp4 files instead of HLS")
    cookies = parser.add_mutually_exclusive_group()
    cookies.add_argument("--cookie", metavar="STR", help="Cookie header copied from the browser devtools")
    cookies.add_argument(
        "--cookies-from-browser",
        metavar="chrome[:PROFILE]",
        type=_browser,
        help="read the cookies of the local Chrome profile",
    )
    parser.add_argument(
        "-j", "--jobs", type=_positive_int, default=DEFAULT_JOBS, metavar="N",
        help="parallel downloads (default: %(default)s)",
    )
    parser.add_argument("--list", action="store_true", help="show the streams and exit without downloading")
    parser.add_argument("--verify", action="store_true", help="check length, levels and silences of the saved mix")
    parser.add_argument("--split-speakers", action="store_true", help="also save one track per participant")
    return parser


def _browser(value: str) -> str:
    if value.partition(":")[0] != "chrome":
        raise argparse.ArgumentTypeError("only chrome is supported")
    return value


def _positive_int(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {value!r}")
    return int(value)
