import unittest

from mtsdump import video
from mtsdump.record import Speaker, Stream

IVAN = Speaker(1, "Ivan")
OLGA = Speaker(2, "Olga")


def stream(stream_id, start, duration, speaker=None):
    return Stream(stream_id, start, duration, f"https://storage/{stream_id}.mp4", None, speaker)


def camera(stream_id, start, duration, speaker=IVAN):
    return stream(stream_id, start, duration, speaker)


def piece(stream_id=1, trim_start=0.0, length=10.0, pad_seconds=0.0):
    return video.TilePiece(stream(stream_id, trim_start, length + pad_seconds), trim_start, length, pad_seconds)


def interval(start=0.0, end=10.0, share=None, tiles=()):
    return video.Interval(start, end, share, tuple(tiles))


class PlanTest(unittest.TestCase):
    def test_one_camera_the_whole_session_is_one_interval(self):
        plan = video.plan(100.0, {IVAN: [camera(1, 0.0, 100.0)]})
        self.assertEqual(len(plan.intervals), 1)
        interval = plan.intervals[0]
        self.assertEqual((interval.start, interval.end), (0.0, 100.0))
        (piece,) = interval.tiles
        self.assertEqual((piece.stream.id, piece.trim_start, piece.length, piece.pad_seconds), (1, 0.0, 100.0, 0.0))

    def test_the_camera_exists_only_while_its_stream_is_live(self):
        plan = video.plan(100.0, {IVAN: [camera(1, 10.0, 50.0)]})
        shown = [(i.start, i.end, len(i.tiles)) for i in plan.intervals]
        self.assertEqual(shown, [(0.0, 10.0, 0), (10.0, 60.0, 1), (60.0, 100.0, 0)])

    def test_a_reconnect_gap_shows_the_last_second_frozen(self):
        plan = video.plan(20.0, {IVAN: [camera(1, 0.0, 10.0), camera(2, 13.0, 10.0)]})
        gap = plan.intervals[1]
        self.assertEqual((gap.start, gap.end), (10.0, 13.0))
        (piece,) = gap.tiles
        self.assertEqual(piece.stream.id, 1)
        self.assertEqual((piece.trim_start, piece.length), (9.0, 1.0))
        self.assertAlmostEqual(piece.pad_seconds, 2.0)

    def test_a_gap_beyond_the_tolerance_hides_the_camera(self):
        plan = video.plan(100.0, {IVAN: [camera(1, 0.0, 10.0), camera(2, 50.0, 10.0)]})
        gap = next(i for i in plan.intervals if i.start == 10.0)
        self.assertEqual(gap.tiles, ())

    def test_an_overlapping_reconnect_takes_the_later_stream(self):
        plan = video.plan(18.0, {IVAN: [camera(1, 0.0, 10.0), camera(2, 8.0, 10.0)]})
        (piece,) = next(i for i in plan.intervals if i.start == 8.0).tiles
        self.assertEqual(piece.stream.id, 2)
        self.assertEqual((piece.trim_start, piece.length), (0.0, 2.0))

    def test_a_short_later_stream_falls_back_to_the_live_earlier_one(self):
        plan = video.plan(30.0, {IVAN: [camera(1, 0.0, 30.0), camera(2, 8.0, 4.0)]})
        (piece,) = next(i for i in plan.intervals if i.start == 12.0).tiles
        self.assertEqual(piece.stream.id, 1)
        self.assertEqual((piece.trim_start, piece.length), (12.0, 18.0))

    def test_a_stream_running_past_the_stop_lasts_until_the_session_end(self):
        plan = video.plan(80.0, {IVAN: [camera(1, 10.0, None)]})
        self.assertEqual([(i.start, i.end) for i in plan.intervals], [(0.0, 10.0), (10.0, 80.0)])
        (piece,) = plan.intervals[1].tiles
        self.assertEqual((piece.trim_start, piece.length), (0.0, 70.0))

    def test_speakers_enter_the_grid_in_first_appearance_order(self):
        cameras = {OLGA: [camera(1, 10.0, 10.0, OLGA)], IVAN: [camera(2, 0.0, 20.0, IVAN)]}
        plan = video.plan(20.0, cameras)
        tiles = next(i for i in plan.intervals if i.start == 10.0).tiles
        self.assertEqual([piece.stream.speaker for piece in tiles], [IVAN, OLGA])

    def test_zero_length_streams_are_ignored(self):
        plan = video.plan(10.0, {IVAN: [camera(1, 0.0, 0.0)]})
        self.assertEqual(plan.intervals, ())


class ShareTest(unittest.TestCase):
    def test_share_takes_the_main_area_and_cameras_the_strip(self):
        plan = video.plan(100.0, {IVAN: [camera(1, 0.0, 100.0)]}, [stream(3, 20.0, 30.0)])
        with_share = next(i for i in plan.intervals if i.share)
        self.assertEqual((with_share.start, with_share.end), (20.0, 50.0))
        boxes = video._boxes(with_share)
        self.assertEqual((boxes[0][1].x, boxes[0][1].y, boxes[0][1].w, boxes[0][1].h), (0, 0, 1280, 600))
        self.assertEqual((boxes[1][1].x, boxes[1][1].y, boxes[1][1].w, boxes[1][1].h), (0, 600, 128, 120))

    def test_two_overlapping_shares_show_the_latest_one(self):
        plan = video.plan(50.0, {}, [stream(1, 0.0, 20.0), stream(2, 10.0, 20.0)])
        shown = next(i for i in plan.intervals if i.start == 10.0).share
        self.assertEqual(shown.stream.id, 2)

    def test_the_strip_hides_tiles_beyond_the_limit(self):
        cameras = {Speaker(i, f"user {i}"): [camera(i, 0.0, 10.0, Speaker(i, f"user {i}"))] for i in range(11)}
        plan = video.plan(10.0, cameras, [stream(99, 0.0, 10.0)])
        self.assertEqual(len(plan.intervals[0].tiles), video.STRIP_TILES)
        self.assertIn("only 10 of 11", " ".join(plan.warnings))


class GridTest(unittest.TestCase):
    def test_the_grid_hides_tiles_beyond_the_limit(self):
        cameras = {Speaker(i, f"user {i}"): [camera(i, 0.0, 10.0, Speaker(i, f"user {i}"))] for i in range(26)}
        plan = video.plan(10.0, cameras)
        self.assertEqual(len(plan.intervals[0].tiles), video.MAX_TILES)
        self.assertIn("only 25 of 26", " ".join(plan.warnings))


class BlackTest(unittest.TestCase):
    def test_a_fully_black_interval_hides_the_tile(self):
        plan = video.plan(100.0, {IVAN: [camera(1, 0.0, 100.0)]}, black={1: [(0.0, 100.0)]})
        self.assertEqual(plan.intervals[0].tiles, ())

    def test_a_partly_black_interval_keeps_the_tile(self):
        plan = video.plan(100.0, {IVAN: [camera(1, 0.0, 100.0)]}, black={1: [(0.0, 50.0)]})
        self.assertEqual(len(plan.intervals[0].tiles), 1)

    def test_a_stray_picture_does_not_glue_a_black_camera_into_the_grid(self):
        plan = video.plan(10.0, {IVAN: [camera(1, 0.0, 10.0)]}, black={1: [(0.3, 10.0)]})
        self.assertEqual(plan.intervals[0].tiles, ())

    def test_the_grid_limit_counts_the_tiles_that_have_picture(self):
        cameras = {Speaker(i, f"user {i}"): [camera(i, 0.0, 10.0, Speaker(i, f"user {i}"))] for i in range(30)}
        black = {i: [(0.0, 10.0)] for i in range(10)}
        plan = video.plan(10.0, cameras, black=black)
        self.assertEqual(len(plan.intervals[0].tiles), 20)


class BoxesTest(unittest.TestCase):
    def test_one_tile_fills_the_canvas(self):
        boxes = video._boxes(interval(tiles=[piece()]))
        self.assertEqual((boxes[0][1].x, boxes[0][1].y, boxes[0][1].w, boxes[0][1].h), (0, 0, 1280, 720))

    def test_three_tiles_make_a_two_by_two_grid(self):
        boxes = video._boxes(interval(tiles=[piece(1), piece(2), piece(3)]))
        self.assertEqual([(box.x, box.y, box.w, box.h) for _, box in boxes],
                         [(0, 0, 640, 360), (640, 0, 640, 360), (0, 360, 640, 360)])

    def test_a_full_five_by_five_grid(self):
        tiles = [piece(index) for index in range(25)]
        boxes = video._boxes(interval(tiles=tiles))
        self.assertEqual((boxes[-1][1].x, boxes[-1][1].y, boxes[-1][1].w, boxes[-1][1].h), (1024, 576, 256, 144))

    def test_an_empty_interval_has_no_boxes(self):
        self.assertEqual(video._boxes(interval()), [])


class SegmentArgsTest(unittest.TestCase):
    def run_args(self, interval):
        return video._segment_args(interval, {1: "a.mp4", 2: "b.mp4", 3: "c.mp4"}, "seg.mp4")

    @staticmethod
    def graph_of(args):
        return args[args.index("-filter_complex") + 1]

    def test_inputs_and_filters(self):
        gap = video.TilePiece(stream(1, 0.0, 10.0), 9.0, 1.0, 2.0)
        live = video.TilePiece(stream(2, 5.0, 20.0), 0.0, 3.0, 0.0)
        share = video.TilePiece(stream(3, 0.0, 30.0), 1.0, 3.0, 0.0)
        args = self.run_args(interval(10.0, 13.0, share, [gap, live]))
        self.assertEqual(args[:24], [
            "-r", "25", "-ss", "1.000", "-t", "3.000", "-i", "c.mp4",
            "-r", "25", "-ss", "9.000", "-t", "1.000", "-i", "a.mp4",
            "-r", "25", "-ss", "0.000", "-t", "3.000", "-i", "b.mp4",
        ])
        graph = self.graph_of(args)
        self.assertIn("[0:v]scale=1280:600:force_original_aspect_ratio=decrease", graph)
        self.assertIn("[1:v]tpad=stop_mode=clone:stop_duration=2.000", graph)
        self.assertIn("scale=128:120", graph)
        self.assertIn("overlay=0:600", graph)
        self.assertEqual(args[args.index("-map") + 1:],
                         ["[v]", "-r", "25", "-t", "3.000", "-c:v", "h264_videotoolbox", "-b:v", video.VIDEO_BITRATE,
                          "-pix_fmt", "yuv420p", "seg.mp4"])

    def test_a_piece_without_a_gap_has_no_tpad(self):
        args = self.run_args(interval(tiles=[piece()]))
        graph = self.graph_of(args)
        self.assertIn("[0:v]scale=1280:720", graph)
        self.assertNotIn("tpad", graph)

    def test_an_empty_interval_maps_the_black_base(self):
        args = self.run_args(interval())
        self.assertEqual(args[args.index("-map") + 1], "[base]")
