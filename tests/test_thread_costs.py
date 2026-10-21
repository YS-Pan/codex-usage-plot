"""Task costs price requests before aggregation and exclude auto-review usage."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import matplotlib.pyplot as plt
from matplotlib.text import Annotation
import numpy as np
import pandas as pd

from codex_usage import charts
from codex_usage.cost import aggregate_thread_model_costs


TASK_TEMP = Path(__file__).resolve().parents[1] / ".task-tmp"


def events(records: list[dict]) -> pd.DataFrame:
    defaults = {
        "timestamp": "2026-10-07T00:00:00Z", "thread_id": "task", "session_id": "task",
        "title": "Synthetic task", "model": "gpt-6.1-sol", "source": "desktop", "cwd": "",
        **dict.fromkeys(charts.RAW_TOKEN_COLUMNS, 0),
    }
    frame = pd.DataFrame([{**defaults, **record} for record in records], columns=list(defaults))
    if records:
        frame["total_tokens"] = [
            record.get("total_tokens", row["input_tokens"] + row["output_tokens"])
            for record, (_, row) in zip(records, frame.iterrows())
        ]
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert("Asia/Shanghai")
    for column in charts.RAW_TOKEN_COLUMNS:
        frame[column] = frame[column].astype("int64")
    return frame


class ThreadCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TASK_TEMP.mkdir(exist_ok=True)

    def capture(self, frame: pd.DataFrame, top_n: int = 15):
        with patch("codex_usage.charts.save_figure", return_value=Path("06_top_threads.png")) as saved:
            charts.plot_top_threads(charts.aggregate_threads(frame), frame, top_n, Path("."), "png", 80)
        figure = saved.call_args.args[0]
        self.addCleanup(plt.close, figure)
        return figure.axes[0]

    def test_prices_requests_and_subagents_before_summing_and_excludes_auto_review(self):
        frame = events([
            {"input_tokens": 200_000, "cached_input_tokens": 20_000,
             "cache_write_input_tokens": 10_000, "output_tokens": 1_000},
            {"input_tokens": 200_000, "cached_input_tokens": 20_000,
             "cache_write_input_tokens": 10_000, "output_tokens": 1_000, "session_id": "child"},
            {"input_tokens": 300_000, "cached_input_tokens": 20_000,
             "cache_write_input_tokens": 10_000, "output_tokens": 1_000, "session_id": "nested-child"},
            {"model": "gpt-6-luna", "input_tokens": 100_000, "output_tokens": 1_000},
            {"model": "codex-auto-review", "input_tokens": 9_000_000},
            {"model": "gpt-6-sol", "source": "auto_review", "input_tokens": 9_000_000},
            {"thread_id": "other", "model": "gpt-6-luna", "input_tokens": 20_000},
        ])
        estimates = aggregate_thread_model_costs(frame).set_index(["thread_id", "model"])
        sol = estimates.loc[("task", "gpt-6.1-sol")]
        self.assertAlmostEqual(sol["total_cost_usd"], 1.903)
        self.assertEqual(sol["priced_tokens"], 703_000)
        self.assertEqual(sol["unpriced_tokens"], 0)
        self.assertAlmostEqual(estimates.loc[("task", "gpt-6-luna"), "total_cost_usd"], 0.0105)
        self.assertNotIn("codex-auto-review", estimates.index.get_level_values("model"))
        self.assertNotIn("gpt-6-sol", estimates.index.get_level_values("model"))
        self.assertAlmostEqual(estimates.loc[("other", "gpt-6-luna"), "total_cost_usd"], 0.002)

    def test_missing_rates_and_breakdowns_remain_unpriced_and_zero_denominator_is_valid(self):
        frame = events([
            {"input_tokens": 100_000},
            {"total_tokens": 900_000},  # Same model, total-only snapshot.
            {"model": "unknown", "input_tokens": 500_000},
            {"model": "new-model", "total_tokens": 700_000},
            {"thread_id": "zero"},
        ])
        estimates = aggregate_thread_model_costs(frame).set_index(["thread_id", "model"])
        known = estimates.loc[("task", "gpt-6.1-sol")]
        self.assertAlmostEqual(known["total_cost_usd"], 0.2)
        self.assertEqual(known["priced_tokens"], 100_000)
        self.assertEqual(known["unpriced_tokens"], 900_000)
        self.assertTrue(pd.isna(estimates.loc[("task", "unknown"), "total_cost_usd"]))
        self.assertTrue(pd.isna(estimates.loc[("task", "new-model"), "total_cost_usd"]))
        self.assertEqual(estimates.loc[("zero", "gpt-6.1-sol"), "total_cost_usd"], 0)
        zero_axis = self.capture(frame.loc[frame["thread_id"] == "zero"])
        zero_texts = [line.removesuffix(" / 1M") for text in zero_axis.texts for line in text.get_text().splitlines()]
        self.assertIn("$0.00", zero_texts)
        self.assertIn("N/A", zero_texts)
        self.assertFalse(any("nan" in value.lower() or "inf" in value.lower() for value in zero_texts))

    def test_plot_centers_model_prices_and_uses_weighted_task_average_without_auto_review(self):
        frame = events([
            {"input_tokens": 100_000},
            {"model": "gpt-6-luna", "input_tokens": 200_000, "session_id": "child"},
            {"model": "codex-auto-review", "input_tokens": 1_000_000},
            {"thread_id": "smaller", "model": "gpt-6-astra", "input_tokens": 1_000},
        ])
        axis = self.capture(frame, 1)
        containers = {container.get_label(): container for container in axis.containers}
        for model, label in [("gpt-6.1-sol", "$0.20"), ("gpt-6-luna", "$0.02")]:
            bar = containers[model].patches[0]
            center = bar.get_x() + bar.get_width() / 2
            self.assertTrue(any(
                text.get_text() == label and text.get_position() == (center, 0)
                and text.get_ha() == "center" and text.get_va() == "center"
                for text in axis.texts
            ))
        # ($0.20 + $0.02) / 300K * 1M = $0.73, rather than averaging model rates.
        summaries = [text for text in axis.texts if isinstance(text, Annotation)]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].get_text(), "$0.22\n$0.73 / 1M")
        self.assertEqual(summaries[0].xy, (1_300_000, 0))
        np.testing.assert_allclose(sum(container.patches[0].get_width() for container in axis.containers), 1_300_000)
        self.assertEqual(set(containers), {"gpt-6.1-sol", "gpt-6-luna", "codex-auto-review"})
        self.assertFalse(any(text.get_text().endswith("*") for text in axis.texts))

    def test_plot_keeps_priced_average_and_all_unpriced_is_unavailable(self):
        frame = events([
            {"input_tokens": 100_000},
            {"total_tokens": 1_000_000},
            {"model": "unknown", "input_tokens": 3_000_000},
            {"model": "codex-auto-review", "input_tokens": 5_000_000},
        ])
        axis = self.capture(frame)
        labels = [line.removesuffix(" / 1M") for text in axis.texts for line in text.get_text().splitlines()]
        self.assertIn("$0.20", labels)
        self.assertIn("$2.00", labels)
        self.assertIn("N/A", labels)
        unpriced_axis = self.capture(frame.loc[frame["model"] == "unknown"])
        self.assertEqual(sum(
            line.removesuffix(" / 1M") == "N/A"
            for text in unpriced_axis.texts for line in text.get_text().splitlines()
        ), 3)
        self.assertFalse(any(text.get_text() == "$0.00" for text in unpriced_axis.texts))

    def test_empty_and_narrow_stacks_render_in_all_supported_formats(self):
        cases = [
            events([]),
            events([{"model": "codex-auto-review", "input_tokens": 100}]),
            events([{"model": "unknown", "source": "auto_review", "input_tokens": 100}]),
            events([
                {"input_tokens": 1_000_000},
                {"model": "gpt-6-luna", "input_tokens": 1},
                {"model": "unknown", "input_tokens": 1},
            ]),
        ]
        with tempfile.TemporaryDirectory(dir=TASK_TEMP) as directory:
            for case, frame in enumerate(cases):
                for image_format in ("png", "svg", "pdf"):
                    with self.subTest(case=case, image_format=image_format):
                        path = charts.plot_top_threads(
                            charts.aggregate_threads(frame), frame, 5, Path(directory), image_format, 40,
                        )
                        self.assertTrue(path.is_file())
                        self.assertGreater(path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
