"""Animation dates, historical snapshots, stable colors and real MP4 encoding."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.colors import to_hex
import numpy as np
import pandas as pd
from PIL import Image

from codex_usage import animations, charts
from codex_usage.running_time import RunningInterval, aggregate_daily_running_time


def synthetic_events(records=()):
    events = pd.DataFrame(records, columns=["timestamp", "model", "total_tokens"])
    events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True).dt.tz_convert("Asia/Shanghai")
    for column in [*charts.RAW_TOKEN_COLUMNS, *charts.COMPOSITION_COLUMNS]:
        if column not in events:
            events[column] = 0
        events[column] = events[column].astype("int64")
    events["input_tokens"] = events["total_tokens"]
    events["uncached_input_tokens"] = events["total_tokens"]
    for column, value in {"thread_id": "001", "session_id": "001", "title": "Synthetic task",
                          "source": "cli", "cwd": "synthetic"}.items():
        events[column] = value
    return events


class AnimationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "动画 with spaces"
        self.zone = ZoneInfo("Asia/Shanghai")
        self.observed = datetime(2026, 10, 21, 2, tzinfo=timezone.utc)

    def test_default_window_ends_today_after_idle_days_and_uses_local_calendar(self):
        dates = animations.animation_dates(animations.AnimationOptions(), self.zone, self.observed)
        self.assertEqual(dates[0].date(), date(2026, 9, 21))
        self.assertEqual(dates[-1].date(), date(2026, 10, 21))
        self.assertEqual(len(dates), 31)
        ny = ZoneInfo("America/New_York")
        dates = animations.animation_dates(
            animations.AnimationOptions(start=date(2026, 10, 31), end=date(2026, 11, 2)), ny,
            datetime(2026, 11, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(len(dates), 3)
        self.assertEqual((dates[2] - dates[1]).total_seconds(), 25 * 3600)
        for options in (animations.AnimationOptions(start=date(2026, 11, 1)),
                        animations.AnimationOptions(fps=0), animations.AnimationOptions(dpi=-1)):
            with self.assertRaises(ValueError):
                animations.animation_dates(options, self.zone, self.observed)

    def test_snapshots_preserve_earlier_totals_and_clip_cross_midnight_turns(self):
        events = synthetic_events([
            ("2026-09-01T00:00:00Z", "old", 100),
            ("2026-10-20T15:59:59Z", "old", 200),
            ("2026-10-20T16:00:00Z", "new", 300),
        ])
        cutoff = datetime(2026, 10, 21, tzinfo=self.zone)
        interval = RunningInterval(datetime(2026, 10, 20, 23, tzinfo=self.zone),
                                   datetime(2026, 10, 21, 2, tzinfo=self.zone))
        visible, clipped = animations.snapshot_data(events, [interval], cutoff)
        self.assertEqual(int(visible["total_tokens"].sum()), 300)
        self.assertEqual(clipped[0].end, cutoff)
        self.assertEqual(interval.end.hour, 2)  # The original interval remains intact.
        daily = aggregate_daily_running_time(clipped, self.zone, date(2026, 10, 20), date(2026, 10, 21), cutoff)
        self.assertEqual(daily["summed_running_hours"].tolist(), [1, 0])

    def test_model_colors_and_order_stay_fixed_when_models_appear_and_overtake(self):
        events = synthetic_events([
            ("2026-09-01T00:00:00Z", "old", 100),
            ("2026-10-19T00:00:00Z", "old", 200),
            ("2026-10-20T00:00:00Z", "new", 900),
            ("2026-10-20T00:10:00Z", "codex-auto-review", 50),
        ])
        options = animations.AnimationOptions(start=date(2026, 10, 19), end=date(2026, 10, 21), dpi=35)
        dates = animations.animation_dates(options, self.zone, self.observed)
        renderer = animations.FrameRenderer(events, [], self.zone, date(2026, 9, 1), dates, options, 5, self.observed)
        original_save = charts.save_figure
        snapshots = []

        def inspect(figure, *args):
            axis = figure.axes[0]
            snapshots.append((axis.get_title(), {
                artist.get_label().split(":")[0]: to_hex(artist.get_facecolor()[0]) for artist in axis.collections
            }))
            return original_save(figure, *args)

        with patch.object(charts, "save_figure", side_effect=inspect):
            for stamp in dates:
                renderer.render(2, stamp).close()
        expected = {model: to_hex(color) for model, color in charts.model_stack_colors(events).items()}
        self.assertEqual(list(expected), ["old", "new", "codex-auto-review"])
        self.assertIn("total 300", snapshots[0][0])
        self.assertIn("total 1.2K", snapshots[-1][0])
        for _, colors in snapshots:
            self.assertTrue(colors)
            self.assertEqual(colors, {model: expected[model] for model in colors})
            self.assertEqual(list(colors), [model for model in expected if model in colors])

    def test_cumulative_axes_follow_each_snapshot_without_using_future_usage(self):
        events = synthetic_events([
            ("2026-10-01T00:00:00Z", "old", 100),
            ("2026-10-19T00:00:00Z", "old", 200),
            ("2026-10-20T00:00:00Z", "new", 900),
            ("2026-10-21T03:00:00Z", "future", 1_000_000_000),
        ])
        options = animations.AnimationOptions(start=date(2026, 10, 19), end=date(2026, 10, 21), dpi=30)
        dates = animations.animation_dates(options, self.zone, self.observed)
        renderer = animations.FrameRenderer(events, [], self.zone, date(2026, 10, 1), dates, options, 3, self.observed)
        original_save = charts.save_figure
        snapshots = []

        def inspect(figure, *args):
            # The sink applies the video layout inside save_figure.
            result = original_save(figure, *args)
            axis = figure.axes[0]
            snapshots.append((axis.get_xlim(), axis.get_ylim(), axis.get_position().bounds))
            return result

        with patch.object(charts, "save_figure", side_effect=inspect):
            for stamp in dates:
                with renderer.render(2, stamp) as image:
                    self.assertEqual(image.size, (420, 240))
        self.assertEqual([mdates.num2date(item[0][1]).date() for item in snapshots],
                         [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)])
        self.assertGreater(snapshots[0][1][1], 300)
        self.assertLess(snapshots[0][1][1], 1200)
        self.assertGreater(snapshots[1][1][1], 1200)
        self.assertLess(snapshots[1][1][1], 2000)
        # An idle date advances time without expanding the token scale. The
        # future request after today's cutoff must not influence either frame.
        self.assertEqual(snapshots[1][1], snapshots[2][1])
        self.assertTrue(all(item[1][0] == 0 for item in snapshots))
        self.assertTrue(all(item[2] == snapshots[0][2] for item in snapshots))

    def test_all_numbered_charts_render_empty_and_populated_frames_without_still_files(self):
        events = synthetic_events([("2026-10-20T00:00:00Z", "gpt-6.1-sol", 1000)])
        options = animations.AnimationOptions(start=date(2026, 10, 19), end=date(2026, 10, 20), dpi=30)
        dates = animations.animation_dates(options, self.zone, self.observed)
        renderer = animations.FrameRenderer(events, [], self.zone, date(2026, 10, 19), dates, options, 3, self.observed)
        for number in animations.CHART_NAMES:
            for stamp in dates:
                with self.subTest(number=number, day=stamp.date()), renderer.render(number, stamp) as image:
                    self.assertEqual(image.mode, "RGB")
                    self.assertGreater(image.width, 100)
        self.assertFalse(plt.get_fignums())

    def test_daily_cost_frames_keep_daily_values_idle_days_and_fixed_component_colors(self):
        from codex_usage import cost

        events = synthetic_events([
            ("2026-10-19T00:00:00Z", "gpt-6.1-sol", 100_000),
            ("2026-10-20T00:00:00Z", "gpt-6.1-sol", 200_000),
            ("2026-10-21T03:00:00Z", "gpt-6.1-sol", 1_000_000_000),
        ])
        events.loc[0, ["cached_input_tokens", "cached_input_component"]] = 100_000
        events.loc[0, "uncached_input_tokens"] = 0
        options = animations.AnimationOptions(charts=(14,), start=date(2026, 10, 19),
                                              end=date(2026, 10, 21), dpi=30)
        dates = animations.animation_dates(options, self.zone, self.observed)
        renderer = animations.FrameRenderer(events, [], self.zone, dates[0].date(), dates, options, 3, self.observed)
        original_plot = cost.plot_daily_cost_composition
        snapshots = []

        def inspect(daily, *args, **kwargs):
            snapshots.append((daily.copy(), dict(kwargs["component_colors"])))
            return original_plot(daily, *args, **kwargs)

        with patch.object(cost, "plot_daily_cost_composition", side_effect=inspect):
            for stamp in dates:
                renderer.render(14, stamp).close()
        expected_values = [[0.01], [0.01, 0.4], [0.01, 0.4, 0.0]]
        for (daily, colors), expected in zip(snapshots, expected_values):
            np.testing.assert_allclose(daily["official_models_cost_usd"], expected)
            self.assertEqual(colors, renderer.component_colors)
            self.assertEqual(to_hex(colors["cached_input_component"]), "#14b8a6")
        self.assertEqual(snapshots[-1][0]["date"].dt.date.tolist(), [stamp.date() for stamp in dates])

    def test_real_mp4_round_trip_and_atomic_preservation_on_render_error(self):
        import imageio_ffmpeg

        events = synthetic_events([("2026-10-20T00:00:00Z", "old", 1000)])
        options = animations.AnimationOptions(start=date(2026, 10, 19), end=date(2026, 10, 21), dpi=30, fps=3)
        with redirect_stdout(io.StringIO()):
            results = animations.export_animations(events, [], self.output, self.zone, date(2026, 10, 1), options, observed_at=self.observed)
        self.assertEqual([item["file"] for item in results], ["02_cumulative_tokens.mp4"])
        self.assertEqual(results[0]["frames"], 3)
        self.assertEqual(results[0]["encoded_frames"], 6)
        self.assertEqual(results[0]["end_hold_seconds"], 1)
        self.assertEqual(results[0]["duration_seconds"], 2)
        path = self.output / results[0]["file"]
        reader = imageio_ffmpeg.read_frames(str(path))
        try:
            metadata = next(reader)
            self.assertIn("h264", metadata["codec"])
            self.assertEqual(metadata["size"], (420, 240))
            self.assertEqual(len(list(reader)), 6)
            self.assertAlmostEqual(metadata["duration"], 2)
        finally:
            reader.close()
        previous = path.read_bytes()

        def broken_frames():
            yield Image.new("RGB", (40, 20), "white")
            raise ValueError("synthetic rendering error")

        with self.assertRaisesRegex(ValueError, "synthetic rendering error"):
            animations.write_mp4(broken_frames(), path, (40, 20), 4, animations.ffmpeg_executable())
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(list(self.output.glob(".animation-*")), [])

    def test_final_image_is_held_for_an_extra_second_at_different_frame_rates(self):
        import imageio_ffmpeg

        self.output.mkdir(parents=True)
        for fps in (1, 4):
            with self.subTest(fps=fps):
                path = self.output / f"hold-{fps}.mp4"
                frames = (Image.new("RGB", (40, 20), color) for color in ("red", "blue"))
                animations.write_mp4(frames, path, (40, 20), fps, animations.ffmpeg_executable())
                reader = imageio_ffmpeg.read_frames(str(path))
                try:
                    metadata = next(reader)
                    decoded = [np.frombuffer(frame, dtype=np.uint8).astype(np.int16) for frame in reader]
                finally:
                    reader.close()
                self.assertEqual(len(decoded), 2 + fps)
                self.assertAlmostEqual(metadata["duration"], 2 / fps + 1)
                self.assertGreater(np.abs(decoded[0] - decoded[1]).mean(), 100)
                for frame in decoded[2:]:
                    self.assertLess(np.abs(frame - decoded[1]).mean(), 1)

    def test_optional_gif_loops_with_the_mp4_frame_count_and_preserves_old_file_on_failure(self):
        events = synthetic_events([("2026-10-20T00:00:00Z", "old", 1000)])
        options = animations.AnimationOptions(start=date(2026, 10, 19), end=date(2026, 10, 21),
                                               dpi=30, fps=4, gif=True)
        with redirect_stdout(io.StringIO()):
            results = animations.export_animations(events, [], self.output, self.zone, date(2026, 10, 1),
                                                   options, observed_at=self.observed)
        self.assertEqual([item["format"] for item in results], ["mp4", "gif"])
        gif_path = self.output / "02_cumulative_tokens.gif"
        with Image.open(gif_path) as image:
            self.assertEqual(image.format, "GIF")
            self.assertTrue(image.is_animated)
            self.assertEqual(image.n_frames, 7)
            self.assertEqual(image.info["loop"], 0)
            self.assertEqual(image.info["duration"], 250)
            self.assertEqual(image.size, (420, 240))
            decoded = []
            durations = []
            for index in range(image.n_frames):
                image.seek(index)
                durations.append(image.info["duration"])
                decoded.append(np.asarray(image.convert("RGB")).astype(np.int16))
            self.assertEqual(sum(durations), 1750)
            for frame in decoded[3:]:
                self.assertLess(np.abs(frame - decoded[2]).mean(), 1)
        self.assertEqual(results[1]["duration_seconds"], 1.75)
        self.assertEqual(results[1]["end_hold_seconds"], 1)
        self.assertEqual(results[0]["model_colors"], results[1]["model_colors"])
        previous = gif_path.read_bytes()
        failure = subprocess.CalledProcessError(1, "synthetic-ffmpeg", stderr=b"synthetic encoder error")
        with patch.object(animations.subprocess, "run", side_effect=failure), \
                self.assertRaisesRegex(RuntimeError, "synthetic encoder error"):
            animations.convert_mp4_to_gif(self.output / "02_cumulative_tokens.mp4", gif_path,
                                          animations.ffmpeg_executable())
        self.assertEqual(gif_path.read_bytes(), previous)
        self.assertEqual(list(self.output.glob(".animation-*")), [])

    def test_default_report_generates_only_chart_two_animation_and_opt_out_skips_encoder(self):
        events = synthetic_events([("2026-10-20T00:00:00Z", "old", 1000)])
        with redirect_stdout(io.StringIO()), patch.object(animations, "write_mp4") as writer:
            summary = charts.export_report(events, charts.ParseStats(), self.output, self.zone, "png", 20, 3,
                                           running_intervals=[], start=date(2026, 10, 20), end=date(2026, 10, 20))
        self.assertEqual(writer.call_count, 1)
        self.assertEqual([item["chart"] for item in summary["animations"]], [2])
        self.assertEqual(summary["animations"][0]["frames"], 31)
        self.assertEqual(summary["animations"][0]["end_date"], datetime.now(self.zone).date().isoformat())
        self.assertEqual(len(summary["charts"]), 13)
        with patch.object(animations, "ffmpeg_executable") as encoder:
            self.assertEqual(animations.export_animations(events, [], self.output, self.zone, date(2026, 10, 20),
                                                         animations.AnimationOptions(charts=())), [])
        encoder.assert_not_called()

    def test_runtime_only_report_generates_an_empty_default_token_animation(self):
        events = charts.frame_from_records([], {}, {}, self.zone)
        interval = RunningInterval(datetime(2026, 10, 20, 1, tzinfo=timezone.utc),
                                   datetime(2026, 10, 20, 2, tzinfo=timezone.utc))
        options = animations.AnimationOptions(start=date(2026, 10, 20), end=date(2026, 10, 20), dpi=30)
        with redirect_stdout(io.StringIO()):
            summary = charts.export_report(events, charts.ParseStats(), self.output, self.zone, "png", 30, 3,
                                           running_intervals=[interval], start=date(2026, 10, 20),
                                           end=date(2026, 10, 20), animation=options)
        self.assertEqual(summary["total_tokens"], 0)
        self.assertEqual(summary["running_time"]["total_running_hours"], 1)
        self.assertEqual(summary["animations"][0]["frames"], 1)
        self.assertTrue((self.output / "02_cumulative_tokens.mp4").is_file())

    def test_palette_does_not_switch_when_the_ninth_model_appears(self):
        events = synthetic_events([
            (f"2026-10-{19 if number < 8 else 20}T{number:02d}:00:00Z", f"model-{number}", 10 * (number + 1))
            for number in range(9)
        ])
        options = animations.AnimationOptions(start=date(2026, 10, 19), end=date(2026, 10, 20), dpi=30)
        dates = animations.animation_dates(options, self.zone, self.observed)
        renderer = animations.FrameRenderer(events, [], self.zone, dates[0].date(), dates, options, 3, self.observed)
        original_plot = charts.plot_cumulative_usage
        colors = []

        def inspect(*args, **kwargs):
            colors.append(dict(kwargs["model_colors"]))
            return original_plot(*args, **kwargs)

        with patch.object(charts, "plot_cumulative_usage", side_effect=inspect):
            for stamp in dates:
                renderer.render(2, stamp).close()
        self.assertEqual(colors[0], colors[1])
        self.assertEqual(len(colors[0]), 9)
        early = events.loc[events["timestamp"].dt.date == dates[0].date()]
        self.assertNotEqual(to_hex(colors[0]["model-0"]), to_hex(charts.model_stack_colors(early)["model-0"]))

    def test_bad_cli_dates_or_frame_rate_stop_before_scanning_logs(self):
        for arguments in (["--animation-fps", "0"], ["--animation-dpi", "-1"],
                          ["--animation-start", "2026-10-20", "--animation-end", "2026-10-19"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), \
                    patch.object(charts, "collect_usage_data") as scan:
                self.assertEqual(charts.main(list(arguments)), 2)
            scan.assert_not_called()

    def test_standalone_command_reads_existing_exports_and_preserves_stills_and_string_ids(self):
        events = synthetic_events([("2026-10-20T00:00:00Z", "gpt-6.1-sol", 1000)])
        with redirect_stdout(io.StringIO()):
            charts.export_report(events, charts.ParseStats(), self.output, self.zone, "png", 20, 3,
                                 running_intervals=[], start=date(2026, 10, 20), end=date(2026, 10, 20),
                                 animation=animations.AnimationOptions(charts=()))
        before = {path.name: path.read_bytes() for path in self.output.iterdir() if path.suffix in (".png", ".csv")}
        restored, _, _, _, _ = animations.read_report(self.output)
        self.assertEqual(restored.iloc[0]["thread_id"], "001")
        with redirect_stdout(io.StringIO()):
            result = animations.main(["--chart", "6", "--output", str(self.output), "--start", "2026-10-20",
                                      "--end", "2026-10-20", "--dpi", "30", "--gif"])
        self.assertEqual(result, 0)
        self.assertTrue((self.output / "06_top_threads.mp4").is_file())
        self.assertTrue((self.output / "06_top_threads.gif").is_file())
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.output.iterdir() if path.name in before})
        summary = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual([item["chart"] for item in summary["animations"]], [6, 6])
        with redirect_stderr(io.StringIO()) as errors:
            result = animations.main(["--chart", "2", "--output", str(self.output / "missing")])
        self.assertEqual(result, 1)
        self.assertIn("report", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
