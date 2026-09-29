import unittest

from mtsdump.record import RecordFormatError, Speaker, parse_record

START = {
    "module": "eventsession.start",
    "relativeTime": 0,
    "time": 1_789_019_955,
    "data": {},
    "snapshot": {"data": {"conference": [{"id": 11, "user": {"id": 1, "nickname": "Lecturer"}}]}},
}


def meta(*logs, **fields):
    return {
        "id": 77,
        "eventSession": {"id": 5},
        "name": "Lecture",
        "duration": 100.0,
        "cuts": [],
        "convertedRecords": [],
        "eventLogs": list(logs),
        **fields,
    }


def add(stream_id, start, conference_id=None):
    stream = {"id": stream_id * 10}
    if conference_id is not None:
        stream["conference"] = {"id": conference_id}
    data = {
        "id": stream_id,
        "url": f"https://storage/{stream_id}.mp4",
        "hlsUrl": f"https://delivery/{stream_id}.mp4/playlist.m3u8",
        "stream": stream,
    }
    return {"module": "mediasession.add", "relativeTime": start, "data": data}


def update(stream_id, duration):
    return {"module": "mediasession.update", "relativeTime": 0, "data": {"id": stream_id, "duration": duration}}


def conference(module, conference_id, user_id, nickname):
    data = {"id": conference_id, "user": {"id": user_id, "nickname": nickname}}
    return {"module": module, "relativeTime": 0, "data": data}


class ParseRecordTest(unittest.TestCase):
    def test_joins_streams_with_their_durations(self):
        (stream,) = parse_record(meta(START, add(1, 10.0, 11), update(1, 40.0))).streams
        self.assertEqual((stream.id, stream.start, stream.duration), (1, 10.0, 40.0))
        self.assertEqual(stream.url, "https://storage/1.mp4")
        self.assertEqual(stream.hls_url, "https://delivery/1.mp4/playlist.m3u8")

    def test_stream_still_running_at_the_stop_has_no_duration(self):
        self.assertIsNone(parse_record(meta(START, add(1, 10.0, 11))).streams[0].duration)

    def test_last_update_wins(self):
        record = parse_record(meta(START, add(1, 0.0, 11), update(1, 5.0), update(1, 7.5)))
        self.assertEqual(record.streams[0].duration, 7.5)

    def test_streams_are_ordered_by_start(self):
        record = parse_record(meta(START, add(2, 30.0, 11), add(1, 10.0, 11)))
        self.assertEqual([stream.id for stream in record.streams], [1, 2])

    def test_speaker_comes_from_the_start_snapshot_or_conference_events(self):
        logs = (START, add(1, 10.0, 11), add(2, 20.0, 12), conference("conference.update", 12, 2, "Student"))
        record = parse_record(meta(*logs))
        self.assertEqual([stream.speaker for stream in record.streams], [Speaker(1, "Lecturer"), Speaker(2, "Student")])

    def test_one_speaker_per_user_even_when_the_nickname_changed(self):
        logs = (
            START,
            add(1, 10.0, 12),
            add(2, 20.0, 13),
            conference("conference.add", 12, 2, "Ivan"),
            conference("conference.add", 13, 2, "Ivan Petrov"),
        )
        record = parse_record(meta(*logs))
        self.assertEqual({stream.speaker for stream in record.streams}, {Speaker(2, "Ivan Petrov")})

    def test_screen_sharing_has_no_speaker(self):
        self.assertIsNone(parse_record(meta(START, add(1, 10.0))).streams[0].speaker)

    def test_session_fields(self):
        record = parse_record(meta(START, cuts=[{"start": 1}]))
        self.assertEqual((record.session_id, record.name, record.duration), (5, "Lecture", 100.0))
        self.assertEqual(record.started_at, 1_789_019_955)
        self.assertTrue(record.has_cuts)
        self.assertFalse(record.has_converted_records)

    def test_missing_fields_raise_a_format_error(self):
        with self.assertRaises(RecordFormatError):
            parse_record({"id": 5, "name": "Lecture", "duration": 1.0})

    def test_empty_event_log_raises_a_format_error(self):
        with self.assertRaises(RecordFormatError):
            parse_record(meta())
