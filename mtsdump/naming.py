import re
import time
from collections import Counter
from collections.abc import Iterable

from mtsdump.record import Record, Speaker

DEFAULT_TEMPLATE = "{date} {name}"
EXTENSION = ".mp3"
NAME_MAX = 255
TRAILING_DATE = re.compile(r"\s*(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})\s*$")
UNSAFE_CHARACTERS = str.maketrans({"/": "-", ":": "-", "\0": None})
UNKNOWN_SPEAKER = "unknown"


def check_template(template: str) -> None:
    try:
        template.format(date="", name="", id="")
    except (KeyError, IndexError, ValueError, AttributeError) as error:
        raise ValueError(f"--name {template!r}: only {{date}}, {{name}} and {{id}} are supported") from error


def filename(template: str, record: Record, speaker_label: str | None = None) -> str:
    return filename_for(template, str(record.session_id), record.name, record.started_at, speaker_label)


def filename_for(
    template: str,
    session_id: str,
    name: str,
    started_at: float,
    speaker_label: str | None = None,
) -> str:
    title, date = _title_and_date(name, started_at)
    stem = _clean(template.format(date=date, name=title, id=session_id)) or str(session_id)
    suffix = f" - {_clean(speaker_label)}" if speaker_label else ""
    return _fit(stem, suffix + EXTENSION)


def speaker_labels(speakers: Iterable[Speaker | None]) -> dict[Speaker | None, str]:
    """Labels speakers by nickname and adds user ids until no two labels end up as one file name."""
    labels = {speaker: _plain_label(speaker) for speaker in speakers}
    with_id = set()
    while True:
        clashes = Counter(map(_on_disk, labels.values()))
        clashing = [
            speaker
            for speaker, label in labels.items()
            if speaker is not None and speaker not in with_id and clashes[_on_disk(label)] > 1
        ]
        if not clashing:
            return labels
        for speaker in clashing:
            labels[speaker] = f"{labels[speaker]} {speaker.user_id}"
            with_id.add(speaker)


def _plain_label(speaker: Speaker | None) -> str:
    if speaker is None:
        return UNKNOWN_SPEAKER
    return speaker.nickname if _clean(speaker.nickname) else f"user {speaker.user_id}"


def _on_disk(label: str) -> str:
    # APFS is case-insensitive by default and _clean collapses several characters into one.
    return _clean(label).casefold()


def _title_and_date(name: str, started_at: float) -> tuple[str, str]:
    match = TRAILING_DATE.search(name)
    if match:
        day, hours, minutes, seconds = match.groups()
        return name[: match.start()], f"{day} {hours}-{minutes}-{seconds}"
    return name, time.strftime("%Y-%m-%d %H-%M-%S", time.localtime(started_at))


def _clean(text: str) -> str:
    return " ".join(text.translate(UNSAFE_CHARACTERS).split()).lstrip(". ")


def _fit(stem: str, suffix: str) -> str:
    while stem and not _fits(stem + suffix):
        cut = stem.rfind(" ")
        shorter = stem[:cut].rstrip(" ,.-") if cut > 0 else ""
        stem = shorter or stem[:-1]
    name = stem + suffix
    while not _fits(name):
        name = name[: -len(EXTENSION) - 1] + EXTENSION
    return name


def _fits(name: str) -> bool:
    # APFS caps a name at 255 characters, counted as UTF-16 units to stay safe with emoji.
    return len(name.encode("utf-16-le")) // 2 <= NAME_MAX
