"""Worker progress follows completed work without changing exported results."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
import io
import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
from PIL import Image

from codex_usage import animations, cost, progress


class WorkerProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.zone = ZoneInfo("UTC")
        self.options = animations.AnimationOptions(
            start=date(2026, 10, 19), end=date(2026, 10, 21), dpi=20,
        )
        self.observed = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
        self.errors = io.StringIO()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {progress.PROGRESS_ENV: "json"}).start()
        patch.object(progress, "monotonic", side_effect=itertools.count()).start()

    def events(self):
        return [event for line in self.errors.getvalue().splitlines()
                if (event := progress.parse_progress(line)) is not None]

    def export_animation(self, writer, *, enabled=True, options=None):
        with patch.object(animations, "ffmpeg_executable", return_value="synthetic-encoder"), \
                patch.object(animations, "FrameRenderer") as renderer, \
                patch.object(animations, "write_mp4", side_effect=writer), \
                redirect_stderr(self.errors):
            renderer.return_value.model_colors = {}
            renderer.return_value.render.side_effect = lambda *_: Image.new("RGB", (20, 20))
            return animations.export_animations(
                pd.DataFrame(), [], self.root, self.zone, date(2026, 10, 19),
                options or self.options, observed_at=self.observed, progress=enabled,
            )

    def test_animation_counts_frames_only_after_encoder_accepts_them(self):
        counts_at_delivery = []

        def writer(frames, *_):
            for image in frames:
                counts_at_delivery.append(self.events()[-1].completed)
                image.close()
            self.assertEqual(self.events()[-1].stage, "animation-save-2")
            self.assertNotIn("已保存", self.events()[-1].label)

        results = self.export_animation(writer)
        self.assertEqual(counts_at_delivery, [0, 1, 2])
        frames = [event for event in self.events() if event.stage == "animation-2"]
        self.assertEqual([event.completed for event in frames], [0, 1, 2, 3])
        self.assertTrue(all(event.total == 3 for event in frames))
        self.assertIn("已保存", self.events()[-1].label)
        self.assertEqual(results[0]["frames"], 3)

    def test_encoder_failure_after_last_frame_never_reports_saved(self):
        def writer(frames, *_):
            for image in frames:
                image.close()
            raise RuntimeError("synthetic encoder failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic encoder failure"):
            self.export_animation(writer)
        self.assertEqual(self.events()[-1].stage, "animation-save-2")
        self.assertEqual(self.events()[-1].label, "正在保存动画 02")
        self.assertFalse(any("已保存" in event.label for event in self.events()))

    def test_quiet_animation_and_disabled_animations_emit_no_progress(self):
        def writer(frames, *_):
            for image in frames:
                image.close()

        self.export_animation(writer, enabled=False)
        self.assertEqual(self.errors.getvalue(), "")
        result = self.export_animation(writer, options=animations.AnimationOptions(charts=()))
        self.assertEqual(result, [])
        self.assertEqual(self.errors.getvalue(), "")

    def test_gif_conversion_has_its_own_stage_and_failure_never_reports_gif_saved(self):
        options = animations.AnimationOptions(start=self.options.start, end=self.options.end, gif=True)

        def writer(frames, *_):
            for image in frames:
                image.close()

        with patch.object(animations, "convert_mp4_to_gif", side_effect=RuntimeError("synthetic GIF failure")), \
                self.assertRaisesRegex(RuntimeError, "synthetic GIF failure"):
            self.export_animation(writer, options=options)
        self.assertEqual(self.events()[-1].stage, "animation-gif-2")
        self.assertIsNone(self.events()[-1].total)
        self.assertEqual(self.events()[-1].label, "正在转换 GIF 动画 02")

    def write_cost_events(self):
        path = self.root / "events.csv"
        pd.DataFrame([{
            "timestamp": "2026-10-21T01:00:00+00:00", "model": "gpt-6.1-sol",
            "input_tokens": 1000, "cached_input_tokens": 200, "cache_write_input_tokens": 0,
            "output_tokens": 100, "total_tokens": 1100,
        }]).to_csv(path, index=False)
        return path

    def test_cost_progress_covers_saved_files_and_image_before_summary(self):
        path = self.write_cost_events()

        def image_writer(*_):
            self.assertEqual(self.events()[-1].stage, "cost-image")
            self.assertEqual(self.events()[-1].completed, 0)
            self.assertTrue((self.root / "daily_api_cost.csv").is_file())
            self.assertFalse((self.root / "api_cost_estimate.json").exists())

        output = io.StringIO()
        with patch.object(cost, "plot_cumulative_cost", side_effect=image_writer), \
                redirect_stderr(self.errors), redirect_stdout(output):
            self.assertEqual(cost.main(["--events", str(path), "--output", str(self.root)]), 0)
        self.assertEqual(list(dict.fromkeys(event.stage for event in self.events())),
                         ["cost-read", "cost-calculate", "cost-data", "cost-image", "cost-summary"])
        files = [event.completed for event in self.events() if event.stage == "cost-data"]
        self.assertEqual(files, [0, 1, 2, 3])
        images = [event.completed for event in self.events() if event.stage == "cost-image"]
        self.assertEqual(images, [0, 1])
        summary = json.loads((self.root / "api_cost_estimate.json").read_text(encoding="utf-8"))
        self.assertGreater(summary["priced_subtotal_usd"], 0)
        self.assertIn("Officially priced subtotal:", output.getvalue())

    def test_cost_image_failure_does_not_report_image_complete_or_save_summary(self):
        path = self.write_cost_events()
        with patch.object(cost, "plot_cumulative_cost", side_effect=OSError("synthetic image failure")), \
                redirect_stderr(self.errors), self.assertRaisesRegex(OSError, "synthetic image failure"):
            cost.main(["--events", str(path), "--output", str(self.root)])
        self.assertEqual(self.events()[-1].stage, "cost-image")
        self.assertEqual(self.events()[-1].completed, 0)
        self.assertFalse((self.root / "api_cost_estimate.json").exists())

    def test_cost_quiet_preserves_final_output_without_progress(self):
        path = self.write_cost_events()
        output = io.StringIO()
        with patch.object(cost, "plot_cumulative_cost"), redirect_stderr(self.errors), redirect_stdout(output):
            self.assertEqual(cost.main(["--events", str(path), "--output", str(self.root), "--quiet"]), 0)
        self.assertEqual(self.errors.getvalue(), "")
        self.assertIn("Officially priced subtotal:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
