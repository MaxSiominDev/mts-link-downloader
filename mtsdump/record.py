from dataclasses import dataclass

CONFERENCE_EVENTS = frozenset({"conference.add", "conference.update", "conference.delete"})


class RecordFormatError(Exception):
    pass


@dataclass(frozen=True)
class Speaker:
    user_id: int
    nickname: str


@dataclass(frozen=True)
class Stream:
    id: int
    start: float
    # None if the session stopped while the stream was still running
    duration: float | None
    url: str
    hls_url: str | None
    # None when no conference owns the stream, e.g. screen sharing
    speaker: Speaker | None


@dataclass(frozen=True)
class Record:
    session_id: int
    name: str
    # Unix time of the session start, the point every stream start is counted from.
    started_at: float
    duration: float
    streams: tuple[Stream, ...]
    has_cuts: bool
    has_converted_records: bool


def parse_record(meta: dict) -> Record:
    try:
        logs = meta["eventLogs"]
        speakers = _speakers_by_conference(logs)
        durations = {
            event["data"]["id"]: event["data"]["duration"]
            for event in logs
            if event["module"] == "mediasession.update" and "duration" in event["data"]
        }
        streams = [_stream(event, durations, speakers) for event in logs if event["module"] == "mediasession.add"]
        return Record(
            session_id=meta["eventSession"]["id"],
            name=meta["name"],
            started_at=logs[0]["time"],
            duration=meta["duration"],
            streams=tuple(sorted(streams, key=lambda stream: stream.start)),
            has_cuts=bool(meta.get("cuts")),
            has_converted_records=bool(meta.get("convertedRecords")),
        )
    except (KeyError, IndexError, TypeError) as error:
        raise RecordFormatError(f"unexpected record metadata: {error!r}") from error


def _stream(event: dict, durations: dict[int, float], speakers: dict[int, Speaker]) -> Stream:
    data = event["data"]
    conference_id = ((data.get("stream") or {}).get("conference") or {}).get("id")
    return Stream(
        id=data["id"],
        start=event["relativeTime"],
        duration=durations.get(data["id"]),
        url=data["url"],
        hls_url=data.get("hlsUrl"),
        speaker=speakers.get(conference_id),
    )


def _speakers_by_conference(logs: list[dict]) -> dict[int, Speaker]:
    # The start snapshot lists only conferences live at that moment, later ones appear as events.
    conferences = []
    for event in logs:
        if event["module"] == "eventsession.start":
            conferences += ((event.get("snapshot") or {}).get("data") or {}).get("conference") or []
        elif event["module"] in CONFERENCE_EVENTS:
            conferences.append(event["data"])
    conferences = [conference for conference in conferences if conference.get("user")]
    # One person per user id, even if the nickname changed between reconnects; the last one wins.
    nicknames = {conference["user"]["id"]: conference["user"]["nickname"] for conference in conferences}
    return {
        conference["id"]: Speaker(conference["user"]["id"], nicknames[conference["user"]["id"]])
        for conference in conferences
    }
