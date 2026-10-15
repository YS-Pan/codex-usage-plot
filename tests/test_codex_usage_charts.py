import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from codex_usage.charts import (
    COMPOSITION_COLUMNS,
    RAW_TOKEN_COLUMNS,
    aggregate_daily,
    aggregate_recent_hourly,
    aggregate_threads,
    breakdown_color_map,
    collect_usage_events,
    cumulative_model_tokens,
    filter_date_range,
    plot_cumulative_usage,
    plot_weekly_breakdown,
    resolve_thread_roots,
    weekly_pivot,
)
from codex_usage.cost import (
    OFFICIAL_PRICES,
    auto_review_scenarios,
    build_daily_costs,
    calculate_event_costs,
    plot_cumulative_cost,
    summarize_official_models,
)

import pandas as pd
import matplotlib.pyplot as plt


class CodexUsageChartsTests(unittest.TestCase):
    def test_thread_ranking_includes_all_subagent_levels_and_header_only_bridges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            sessions = codex_home / "sessions"
            archived = codex_home / "archived_sessions"
            sessions.mkdir()
            archived.mkdir()
            titles = {"main": "主对话", "fork": "独立分叉", "child-1": "子代理标题"}
            (codex_home / "session_index.jsonl").write_text(
                "".join(json.dumps({"id": key, "thread_name": title}) + "\n"
                        for key, title in titles.items()), encoding="utf-8",
            )
            definitions = [
                ("main", "", 10, "2026-10-04T01:00:00Z", False),
                ("child-1", "main", 20, "2026-10-05T01:00:00Z", True),
                ("child-2", "child-1", 30, "2026-10-06T01:00:00Z", False),
                ("child-3", "child-2", 40, "2026-10-07T01:00:00Z", False),
                ("bridge", "main", 0, "2026-10-01T01:00:00Z", False),
                ("leaf", "bridge", 50, "2026-10-07T02:00:00Z", True),
                ("fork", "", 120, "2026-10-07T03:00:00Z", False),
            ]
            for session_id, parent, tokens, timestamp, legacy in definitions:
                meta = {"id": session_id, "session_id": session_id, "originator": "Codex Desktop"}
                if parent:
                    meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
                    if not legacy:
                        meta["parent_thread_id"] = parent
                if session_id == "fork":
                    meta["forked_from_id"] = "main"
                items = [{"type": "session_meta", "timestamp": timestamp, "payload": meta}]
                if tokens:
                    items.extend([
                        {"type": "turn_context", "timestamp": timestamp,
                         "payload": {"turn_id": f"turn-{session_id}", "model": "gpt-test"}},
                        {"type": "event_msg", "timestamp": timestamp,
                         "payload": {"type": "token_count", "info": {"last_token_usage": {
                             "input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens,
                         }}}},
                    ])
                text = "".join(json.dumps(item) + "\n" for item in items)
                folder = archived if session_id == "bridge" else sessions
                (folder / f"{session_id}.jsonl").write_text(text, encoding="utf-8")
                if session_id == "child-2":
                    (archived / "duplicate-child-2.jsonl").write_text(text, encoding="utf-8")

            events, stats = collect_usage_events(codex_home, ZoneInfo("Asia/Shanghai"), progress=False)
            threads = aggregate_threads(events)
            by_id = threads.set_index("thread_id")

            self.assertEqual(stats.duplicate_events_removed, 1)
            self.assertEqual(len(events), 6)
            self.assertEqual(threads["thread_id"].tolist(), ["main", "fork"])
            self.assertEqual(events.loc[events["session_id"] != "fork", "thread_id"].unique().tolist(), ["main"])
            self.assertEqual(events.loc[events["session_id"] != "fork", "title"].unique().tolist(), ["主对话"])
            self.assertEqual(by_id.loc["main", "total_tokens"], 150)
            self.assertEqual(by_id.loc["main", "main_agent_tokens"], 10)
            self.assertEqual(by_id.loc["main", "subagent_tokens"], 140)
            self.assertEqual(by_id.loc["main", "subagent_sessions"], 4)
            self.assertEqual(by_id.loc["main", "requests"], 5)
            self.assertEqual(by_id.loc["fork", "total_tokens"], 120)
            self.assertEqual(by_id.loc["fork", "subagent_tokens"], 0)
            self.assertEqual(int(threads["total_tokens"].sum()), int(events["total_tokens"].sum()))
            self.assertEqual(events.loc[events["session_id"] == "child-1", "parent_thread_id"].iloc[0], "main")
            self.assertEqual(events.loc[events["session_id"] == "child-3", "parent_thread_id"].iloc[0], "child-2")

            # The parent has no activity in this range; children still keep its ID and title.
            selected = filter_date_range(events, pd.Timestamp("2026-10-05").date(), None)
            selected_main = aggregate_threads(selected).set_index("thread_id").loc["main"]
            self.assertEqual(selected_main["total_tokens"], 140)
            self.assertEqual(selected_main["main_agent_tokens"], 0)
            self.assertEqual(selected_main["subagent_tokens"], 140)
            self.assertEqual(selected_main["title"], "主对话")

    def test_thread_roots_handle_missing_parents_self_links_and_cycles(self) -> None:
        roots = resolve_thread_roots({
            "child": "missing-parent", "a": "b", "b": "a", "c": "b",
            "self": "self", "standalone": "",
        })
        self.assertEqual(roots["child"], "missing-parent")
        self.assertEqual([roots[key] for key in ("a", "b", "c")], ["a", "a", "a"])
        self.assertEqual(roots["self"], "self")
        self.assertEqual(roots["standalone"], "standalone")

    def test_weekly_models_keep_low_volume_names_and_local_week_boundaries(self) -> None:
        # Historical models fill the old top-six limit; a new model would
        # otherwise disappear into "other" despite dominating a recent week.
        records = [
            {"timestamp": "2026-08-03T00:00:00Z", "model": f"historical-{index}",
             "total_tokens": 10_000 + index}
            for index in range(6)
        ] + [
            {"timestamp": "2026-09-20T15:59:59Z", "model": "gpt-new", "total_tokens": 10},
            {"timestamp": "2026-09-20T16:00:00Z", "model": "gpt-new", "total_tokens": 20},
            {"timestamp": "2026-09-22T00:00:00Z", "model": "gpt-new", "total_tokens": 30},
            {"timestamp": "2026-09-22T00:00:00Z", "model": "unknown", "total_tokens": 1},
        ]
        events = pd.DataFrame(records)
        events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True).dt.tz_convert("Asia/Shanghai")

        pivot = weekly_pivot(events, "model")

        self.assertEqual(set(pivot.columns), set(events["model"]))
        self.assertNotIn("other", pivot.columns)
        self.assertEqual(pivot.columns[-2:].tolist(), ["gpt-new", "unknown"])
        self.assertEqual(pivot.loc[pd.Timestamp("2026-09-14", tz="Asia/Shanghai"), "gpt-new"], 10)
        self.assertEqual(pivot.loc[pd.Timestamp("2026-09-21", tz="Asia/Shanghai"), "gpt-new"], 50)
        self.assertEqual(pivot.loc[pd.Timestamp("2026-08-03", tz="Asia/Shanghai"), "gpt-new"], 0)
        expected = events.groupby("model")["total_tokens"].sum()
        pd.testing.assert_series_equal(pivot.sum().sort_index(), expected.sort_index(), check_names=False)
        self.assertEqual(int(pivot.sum().sum()), int(events["total_tokens"].sum()))

    def test_weekly_sources_still_collapse_low_volume_categories(self) -> None:
        events = pd.DataFrame([
            {"timestamp": pd.Timestamp("2026-09-21", tz="Asia/Shanghai"),
             "source": f"source-{index}", "total_tokens": index + 1}
            for index in range(8)
        ])

        pivot = weekly_pivot(events, "source")

        self.assertEqual(pivot.columns.tolist(), [f"source-{index}" for index in range(7, 1, -1)] + ["other"])
        self.assertEqual(pivot.iloc[0]["other"], 3)
        self.assertEqual(int(pivot.sum().sum()), int(events["total_tokens"].sum()))

    def test_weekly_model_plot_handles_empty_data(self) -> None:
        events = pd.DataFrame({
            "timestamp": pd.DatetimeIndex([], tz="Asia/Shanghai"),
            "model": pd.Series(dtype=str),
            "total_tokens": pd.Series(dtype=int),
        })
        with tempfile.TemporaryDirectory() as directory:
            path = plot_weekly_breakdown(
                events, "model", "Weekly token usage by model",
                "08_weekly_by_model", Path(directory), "png", 80,
            )
            self.assertGreater(path.stat().st_size, 0)

    def test_cache_miss_rates_weight_tokens_and_use_calendar_days(self) -> None:
        events = pd.DataFrame([
            {"timestamp": "2026-08-29T00:00:00+08:00", "thread_id": "a",
             "input_tokens": 900, "cached_input_tokens": 900, "output_tokens": 10_000},
            {"timestamp": "2026-08-29T01:00:00+08:00", "thread_id": "b",
             "input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 0},
            {"timestamp": "2026-08-30T00:00:00+08:00", "thread_id": "a",
             "input_tokens": 9_000, "cached_input_tokens": 4_500, "output_tokens": 0},
        ]).reindex(columns=["timestamp", "thread_id", *RAW_TOKEN_COLUMNS, *COMPOSITION_COLUMNS], fill_value=0)
        events["timestamp"] = pd.to_datetime(events["timestamp"])
        events["total_tokens"] = events["input_tokens"] + events["output_tokens"]
        dates = pd.date_range("2026-08-28", "2026-09-07", tz="Asia/Shanghai")

        daily = aggregate_daily(events, dates)

        # Unequal request sizes give 10%, not the mean request miss rate of 50%.
        self.assertAlmostEqual(daily.loc[1, "cache_miss_percent"], 10)
        self.assertAlmostEqual(daily.loc[2, "cache_miss_percent"], 50)
        # Across both dates, 4,600 of 10,000 input tokens missed (not 30%).
        self.assertAlmostEqual(daily.loc[2, "rolling_7d_cache_miss_percent"], 46)
        self.assertAlmostEqual(daily.loc[7, "rolling_7d_cache_miss_percent"], 46)
        self.assertAlmostEqual(daily.loc[8, "rolling_7d_cache_miss_percent"], 50)
        self.assertTrue(pd.isna(daily.loc[0, "cache_miss_percent"]))
        self.assertTrue(pd.isna(daily.loc[3, "cache_miss_percent"]))
        self.assertTrue(pd.isna(daily.loc[0, "rolling_7d_cache_miss_percent"]))
        self.assertTrue(pd.isna(daily.loc[9, "rolling_7d_cache_miss_percent"]))

    def test_cache_miss_rate_handles_full_hits_cache_writes_and_zero_input(self) -> None:
        events = pd.DataFrame([
            {"timestamp": "2026-08-29T00:00:00+08:00", "thread_id": "a",
             "input_tokens": 100, "cached_input_tokens": 100,
             "cache_write_input_tokens": 0, "output_tokens": 1_000},
            {"timestamp": "2026-08-30T00:00:00+08:00", "thread_id": "a",
             "input_tokens": 100, "cached_input_tokens": 0,
             "cache_write_input_tokens": 100, "output_tokens": 0},
            {"timestamp": "2026-08-31T00:00:00+08:00", "thread_id": "a",
             "input_tokens": 0, "cached_input_tokens": 0,
             "cache_write_input_tokens": 0, "output_tokens": 50},
        ]).reindex(columns=["timestamp", "thread_id", *RAW_TOKEN_COLUMNS, *COMPOSITION_COLUMNS], fill_value=0)
        events["timestamp"] = pd.to_datetime(events["timestamp"])
        events["total_tokens"] = events["input_tokens"] + events["output_tokens"]

        daily = aggregate_daily(events)

        self.assertEqual(daily.loc[0, "cache_miss_percent"], 0)
        self.assertEqual(daily.loc[1, "cache_miss_percent"], 100)
        self.assertTrue(pd.isna(daily.loc[2, "cache_miss_percent"]))
        self.assertEqual(daily.loc[2, "rolling_7d_cache_miss_percent"], 50)

    def test_filtered_empty_string_threads_keep_zero_counts_and_missing_ratios(self) -> None:
        events = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-08-29T01:00:00+08:00"]),
            "thread_id": pd.Series(["synthetic"], dtype="string"),
            **{column: [0] for column in [*RAW_TOKEN_COLUMNS, *COMPOSITION_COLUMNS]},
        })
        dates = pd.date_range("2026-09-01", "2026-09-03", tz="Asia/Shanghai")
        selected = filter_date_range(events, dates[0].date(), dates[-1].date())
        daily = aggregate_daily(selected, dates)
        self.assertEqual(len(daily), 3)
        self.assertTrue((daily[["total_tokens", "requests", "threads"]] == 0).all().all())
        self.assertEqual(str(daily["threads"].dtype), "int64")
        self.assertTrue(daily["cache_miss_percent"].isna().all())
        self.assertTrue(daily["rolling_7d_cache_miss_percent"].isna().all())

    def test_api_cost_applies_cache_and_long_context_multipliers(self) -> None:
        events = pd.DataFrame(
            [
                {
                    "input_tokens": 300_000,
                    "cached_input_tokens": 200_000,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 1_000,
                    "total_tokens": 301_000,
                }
            ]
        )
        result = calculate_event_costs(events, OFFICIAL_PRICES["gpt-5.6-sol"])

        # 100K uncached * $4 * 2 + 200K cached * $0.40 * 2
        # + 1K output * $20 * 1.5 = $0.99.
        self.assertAlmostEqual(result.loc[0, "total_cost_usd"], 0.99)
        self.assertTrue(result.loc[0, "long_context"])

    def test_new_models_use_official_prices_at_long_context_boundary(self) -> None:
        sol_events = pd.DataFrame([
            {"input_tokens": 272_000, "cached_input_tokens": 200_000,
             "cache_write_input_tokens": 50_000, "output_tokens": 1_000},
            {"input_tokens": 272_001, "cached_input_tokens": 200_000,
             "cache_write_input_tokens": 50_000, "output_tokens": 1_000},
        ])
        sol = calculate_event_costs(sol_events, OFFICIAL_PRICES["gpt-6-sol"])
        self.assertEqual(sol["long_context"].tolist(), [False, True])
        self.assertAlmostEqual(sol.loc[0, "total_cost_usd"], 0.219)
        self.assertAlmostEqual(sol.loc[1, "total_cost_usd"], 0.433004)

        luna_events = pd.DataFrame([{
            "input_tokens": 100_000, "cached_input_tokens": 20_000,
            "cache_write_input_tokens": 10_000, "output_tokens": 10_000,
        }])
        luna = calculate_event_costs(luna_events, OFFICIAL_PRICES["gpt-6-luna"])
        self.assertAlmostEqual(luna.loc[0, "total_cost_usd"], 0.01345)

    def test_gpt_6_1_sol_cache_prices_and_long_context_boundary(self) -> None:
        events = pd.DataFrame([
            {"input_tokens": 272_000, "cached_input_tokens": 200_000,
             "cache_write_input_tokens": 50_000, "output_tokens": 1_000},
            {"input_tokens": 272_001, "cached_input_tokens": 200_000,
             "cache_write_input_tokens": 50_000, "output_tokens": 1_000},
        ])
        price = OFFICIAL_PRICES["gpt-6.1-sol"]
        result = calculate_event_costs(events, price)

        self.assertEqual(result["long_context"].tolist(), [False, True])
        self.assertEqual(result["uncached_input_tokens"].tolist(), [22_000, 22_001])
        self.assertAlmostEqual(result.loc[0, "cached_input_cost_usd"], 0.02)
        self.assertAlmostEqual(result.loc[1, "cached_input_cost_usd"], 0.04)
        self.assertAlmostEqual(result.loc[0, "cache_write_cost_usd"], 0.125)
        self.assertAlmostEqual(result.loc[1, "cache_write_cost_usd"], 0.25)
        self.assertAlmostEqual(result.loc[0, "total_cost_usd"], 0.199)
        self.assertAlmostEqual(result.loc[1, "total_cost_usd"], 0.393004)
        self.assertEqual(price.verified_on, "2026-10-07")
        self.assertEqual(OFFICIAL_PRICES["gpt-6-sol"].verified_on, "2026-09-23")

    def test_new_models_enter_official_subtotal(self) -> None:
        events = pd.DataFrame([
            {"model": model, "timestamp": pd.Timestamp("2026-09-23T10:00:00+08:00"),
             "input_tokens": 10_000, "cached_input_tokens": 0,
             "cache_write_input_tokens": 0, "output_tokens": 1_000,
             "total_tokens": 11_000}
            for model in ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "codex-auto-review")
        ])
        detail, priced = summarize_official_models(events)
        statuses = detail.set_index("model")["pricing_status"].to_dict()
        self.assertEqual(statuses["gpt-6.1-sol"], "official")
        self.assertEqual(statuses["gpt-6-sol"], "official")
        self.assertEqual(statuses["gpt-6-luna"], "official")
        self.assertEqual(statuses["codex-auto-review"], "no_public_api_price")
        self.assertEqual(set(priced["model"]), {"gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"})
        priced_subtotal = float(priced["total_cost_usd"].sum())
        self.assertAlmostEqual(priced_subtotal, 0.0615)
        scenarios, scenario_events = auto_review_scenarios(events, priced_subtotal)
        daily = build_daily_costs(priced, scenario_events)
        self.assertAlmostEqual(daily.loc[0, "cumulative_official_models_cost_usd"], 0.0615)
        for row in scenarios.to_dict(orient="records"):
            self.assertAlmostEqual(row["officially_priced_subtotal_usd"], 0.0615)
            self.assertAlmostEqual(
                daily.loc[0, f"cumulative_{row['scenario']}"], row["all_usage_total_usd"]
            )

    def test_daily_model_costs_keep_local_days_idle_dates_and_total(self) -> None:
        records = [
            ("gpt-6.1-sol", "2026-10-05T15:59:59Z"),
            ("gpt-6-astra", "2026-10-05T01:00:00Z"),
            ("gpt-6.1-sol", "2026-10-05T16:00:00Z"),
            ("gpt-6-sol", "2026-10-07T16:00:00Z"),
            ("codex-auto-review", "2026-10-06T12:00:00Z"),
        ]
        events = pd.DataFrame([
            {"model": model, "timestamp": timestamp,
             "input_tokens": 10_000, "cached_input_tokens": 0,
             "cache_write_input_tokens": 0, "output_tokens": 1_000,
             "total_tokens": 11_000}
            for model, timestamp in records
        ])
        events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True).dt.tz_convert("Asia/Shanghai")
        _, priced = summarize_official_models(events)
        daily = build_daily_costs(priced, {})

        self.assertEqual(daily["date"].dt.strftime("%Y-%m-%d").tolist(), [
            "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
        ])
        self.assertEqual(str(daily["date"].dt.tz), "Asia/Shanghai")
        for index, expected in enumerate([0.18, 0.03, 0.0, 0.03]):
            self.assertAlmostEqual(daily.loc[index, "official_models_cost_usd"], expected)
        self.assertEqual(daily.loc[2, "model_gpt-6.1-sol_cost_usd"], 0)
        self.assertAlmostEqual(daily.loc[2, "cumulative_model_gpt-6.1-sol_cost_usd"], 0.06)
        self.assertNotIn("model_codex-auto-review_cost_usd", daily)
        model_columns = [column for column in daily if column.startswith("model_")]
        cumulative_columns = [column for column in daily if column.startswith("cumulative_model_")]
        pd.testing.assert_series_equal(
            daily[model_columns].sum(axis=1), daily["official_models_cost_usd"], check_names=False,
        )
        pd.testing.assert_series_equal(
            daily[cumulative_columns].sum(axis=1), daily["cumulative_official_models_cost_usd"],
            check_names=False,
        )

    def test_cumulative_model_tokens_keep_local_days_idle_dates_and_all_models(self) -> None:
        events = pd.DataFrame([
            {"model": "gpt-6.1-sol", "timestamp": "2026-10-05T15:59:59Z", "total_tokens": 100},
            {"model": "gpt-6.1-sol", "timestamp": "2026-10-05T16:00:00Z", "total_tokens": 200},
            {"model": "codex-auto-review", "timestamp": "2026-10-05T16:01:00Z", "total_tokens": 50},
            {"model": "unknown", "timestamp": "2026-10-07T16:00:00Z", "total_tokens": 25},
        ])
        events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True).dt.tz_convert("Asia/Shanghai")
        dates = pd.date_range("2026-10-04", "2026-10-09", tz="Asia/Shanghai")

        cumulative = cumulative_model_tokens(events, dates)

        self.assertTrue(cumulative.index.equals(dates))
        self.assertEqual(cumulative["gpt-6.1-sol"].tolist(), [0, 100, 300, 300, 300, 300])
        self.assertEqual(cumulative["codex-auto-review"].tolist(), [0, 0, 50, 50, 50, 50])
        self.assertEqual(cumulative["unknown"].tolist(), [0, 0, 0, 0, 25, 25])
        self.assertEqual(cumulative.sum(axis=1).tolist(), [0, 100, 350, 350, 375, 375])
        self.assertEqual(int(cumulative.iloc[-1].sum()), int(events["total_tokens"].sum()))

    def test_cumulative_charts_match_weekly_colors_and_cost_omits_unpriced_models(self) -> None:
        models = ["unknown", "codex-auto-review", *OFFICIAL_PRICES]
        events = pd.DataFrame([
            {"model": model,
             "timestamp": pd.Timestamp("2026-10-07T10:00:00+08:00") - pd.Timedelta(minutes=index),
             "input_tokens": (len(models) - index) * 10_000, "cached_input_tokens": 0,
             "cache_write_input_tokens": 0, "output_tokens": 0,
             "total_tokens": (len(models) - index) * 10_000}
            for index, model in enumerate(models)
        ])
        _, priced = summarize_official_models(events)
        daily = build_daily_costs(priced, {})
        colors = breakdown_color_map(weekly_pivot(events, "model").columns)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "chart.png"
            with patch("codex_usage.charts.save_figure", return_value=output) as weekly_save:
                plot_weekly_breakdown(events, "model", "Models", "08", Path(directory), "png", 80)
            weekly_figure = weekly_save.call_args.args[0]
            with patch("codex_usage.cost.save_figure", return_value=output) as cost_save:
                plot_cumulative_cost(daily, output, 80, colors)
            cost_figure = cost_save.call_args.args[0]
            with patch("codex_usage.charts.save_figure", return_value=output) as token_save:
                plot_cumulative_usage(daily, events, Path(directory), "png", 80)
            token_figure = token_save.call_args.args[0]
            try:
                weekly_colors = {
                    container.get_label(): container.patches[0].get_facecolor()[:3]
                    for container in weekly_figure.axes[0].containers
                }
                cost_colors = {
                    area.get_label().split(":")[0]: tuple(area.get_facecolor()[0, :3])
                    for area in cost_figure.axes[0].collections
                }
                self.assertEqual(set(cost_colors), set(OFFICIAL_PRICES))
                self.assertFalse(cost_figure.axes[0].lines)
                for area in cost_figure.axes[0].collections:
                    self.assertEqual(area.get_facecolor()[0, 3], 1.0)
                for model, color in cost_colors.items():
                    self.assertEqual(color, weekly_colors[model])
                token_colors = {
                    area.get_label().split(":")[0]: tuple(area.get_facecolor()[0, :3])
                    for area in token_figure.axes[0].collections
                }
                # Other models follow first appearance; auto-review stays on top.
                expected_order = [model for model in reversed(models) if model != "codex-auto-review"]
                expected_order.append("codex-auto-review")
                self.assertEqual(list(token_colors), expected_order)
                for model, color in token_colors.items():
                    self.assertEqual(color, weekly_colors[model])
            finally:
                plt.close(weekly_figure)
                plt.close(cost_figure)
                plt.close(token_figure)

    def test_gpt_5_5_cache_writes_have_no_extra_charge(self) -> None:
        events = pd.DataFrame([{
            "input_tokens": 100_000, "cached_input_tokens": 10_000,
            "cache_write_input_tokens": 20_000, "output_tokens": 1_000,
        }])
        result = calculate_event_costs(events, OFFICIAL_PRICES["gpt-5.5"])
        self.assertAlmostEqual(result.loc[0, "cache_write_cost_usd"], 0.10)
        self.assertAlmostEqual(result.loc[0, "total_cost_usd"], 0.485)

    def test_collects_counts_without_message_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            sessions = codex_home / "sessions" / "2026" / "08" / "29"
            sessions.mkdir(parents=True)
            (codex_home / "session_index.jsonl").write_text(
                json.dumps({"id": "thread-1", "thread_name": "测试任务"}) + "\n",
                encoding="utf-8",
            )
            events = [
                {
                    "timestamp": "2026-08-29T00:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "thread-1",
                        "session_id": "thread-1",
                        "originator": "Codex Desktop",
                        "cwd": "D:\\project",
                    },
                },
                {
                    "timestamp": "2026-08-29T00:01:00Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "turn-1", "model": "gpt-test"},
                },
                {
                    "timestamp": "2026-08-29T00:02:00Z",
                    "type": "response_item",
                    "payload": {"type": "message", "content": "must not be exported"},
                },
                {
                    "timestamp": "2026-08-29T00:03:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 40,
                                "output_tokens": 20,
                                "reasoning_output_tokens": 5,
                                "total_tokens": 120,
                            },
                            "last_token_usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 40,
                                "output_tokens": 20,
                                "reasoning_output_tokens": 5,
                                "total_tokens": 120,
                            },
                        },
                    },
                },
            ]
            rollout = sessions / "rollout-test.jsonl"
            rollout.write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
            )

            frame, stats = collect_usage_events(
                codex_home, ZoneInfo("Asia/Shanghai"), progress=False
            )

            self.assertEqual(len(frame), 1)
            self.assertEqual(frame.loc[0, "title"], "测试任务")
            self.assertEqual(frame.loc[0, "source"], "desktop")
            self.assertEqual(frame.loc[0, "total_tokens"], 120)
            self.assertEqual(frame.loc[0, "cached_input_component"], 40)
            self.assertEqual(frame.loc[0, "reasoning_output_component"], 5)
            self.assertEqual(
                int(frame.loc[0, COMPOSITION_COLUMNS].sum()),
                int(frame.loc[0, "total_tokens"]),
            )
            self.assertNotIn("content", frame.columns)
            self.assertEqual(stats.files_parsed, 1)

    def test_removes_exact_fork_duplicates_and_filters_dates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            session_dir = codex_home / "sessions"
            archived_dir = codex_home / "archived_sessions"
            session_dir.mkdir()
            archived_dir.mkdir()
            common = [
                {
                    "timestamp": "2026-08-28T16:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "thread-1", "session_id": "thread-1"},
                },
                {
                    "timestamp": "2026-08-28T16:01:00Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "turn-1", "model": "gpt-test"},
                },
                {
                    "timestamp": "2026-08-28T16:02:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 10,
                                "output_tokens": 2,
                                "total_tokens": 12,
                            }
                        },
                    },
                },
            ]
            text = "".join(json.dumps(event) + "\n" for event in common)
            (session_dir / "one.jsonl").write_text(text, encoding="utf-8")
            (archived_dir / "copy.jsonl").write_text(text, encoding="utf-8")

            frame, stats = collect_usage_events(
                codex_home, ZoneInfo("Asia/Shanghai"), progress=False
            )
            self.assertEqual(len(frame), 1)
            self.assertEqual(stats.duplicate_events_removed, 1)
            hourly = aggregate_recent_hourly(frame, ZoneInfo("Asia/Shanghai"), frame.loc[0, "timestamp"].date())
            self.assertEqual(int(hourly["total_tokens"].sum()), 12)
            self.assertEqual(int(hourly["requests"].sum()), 1)
            self.assertEqual(len(filter_date_range(frame, None, None)), 1)
            self.assertEqual(
                len(filter_date_range(frame, frame.loc[0, "timestamp"].date(), None)), 1
            )


class RecentHourlyUsageTests(unittest.TestCase):
    @staticmethod
    def events(records: list[tuple[str, str, int]]) -> pd.DataFrame:
        frame = pd.DataFrame(records, columns=["timestamp", "thread_id", "total_tokens"])
        frame = frame.reindex(
            columns=["timestamp", "thread_id", *RAW_TOKEN_COLUMNS, *COMPOSITION_COLUMNS],
            fill_value=0,
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame["input_tokens"] = frame["total_tokens"]
        frame["uncached_input_tokens"] = frame["total_tokens"]
        return frame

    def test_local_hour_boundaries_seven_dates_and_idle_hours(self) -> None:
        events = self.events([
            ("2026-10-01T15:59:59Z", "a", 1_000),
            ("2026-10-01T16:00:00Z", "a", 10),
            ("2026-10-01T16:59:59Z", "b", 20),
            ("2026-10-01T17:00:00Z", "a", 30),
            ("2026-10-02T01:05:00Z", "a", 40),
            ("2026-10-02T01:45:00Z", "b", 50),
            ("2026-10-08T15:59:59Z", "b", 60),
            ("2026-10-08T16:00:00Z", "a", 2_000),
        ])
        hourly = aggregate_recent_hourly(events, ZoneInfo("Asia/Shanghai"), pd.Timestamp("2026-10-08").date())
        self.assertEqual(len(hourly), 168)
        self.assertEqual(str(hourly["hour_start"].dt.tz), "Asia/Shanghai")
        self.assertEqual(hourly.iloc[0]["hour_start"].isoformat(), "2026-10-02T00:00:00+08:00")
        self.assertEqual(hourly.iloc[-1]["hour_start"].isoformat(), "2026-10-08T23:00:00+08:00")
        self.assertEqual(hourly.loc[0, ["total_tokens", "requests", "threads"]].tolist(), [30, 2, 2])
        self.assertEqual(hourly.loc[1, "total_tokens"], 30)
        self.assertEqual(hourly.loc[9, "total_tokens"], 90)
        self.assertEqual(hourly.loc[167, "total_tokens"], 60)
        self.assertEqual(int(hourly["total_tokens"].sum()), 210)
        self.assertEqual(int(hourly["requests"].sum()), 6)
        idle = hourly.loc[hourly["hour_start"].dt.day == 4]
        self.assertEqual(len(idle), 24)
        self.assertTrue((idle.drop(columns="hour_start") == 0).all().all())
        self.assertEqual(int(hourly[COMPOSITION_COLUMNS].sum().sum()), 210)

    def test_empty_and_out_of_window_events_keep_zero_hours(self) -> None:
        for records in ([], [("2026-09-01T01:00:00Z", "a", 100)]):
            with self.subTest(records=records):
                hourly = aggregate_recent_hourly(
                    self.events(records), ZoneInfo("Asia/Shanghai"), pd.Timestamp("2026-10-08").date(),
                )
                self.assertEqual(len(hourly), 168)
                self.assertTrue((hourly.drop(columns="hour_start") == 0).all().all())

    def test_dst_repeated_hours_stay_separate_and_missing_hours_are_skipped(self) -> None:
        fall = aggregate_recent_hourly(self.events([
            ("2026-11-01T05:30:00Z", "a", 11),
            ("2026-11-01T06:30:00Z", "b", 22),
        ]), ZoneInfo("America/New_York"), pd.Timestamp("2026-11-01").date())
        self.assertEqual(len(fall), 169)
        repeated = fall.loc[(fall["hour_start"].dt.day == 1) & (fall["hour_start"].dt.hour == 1)]
        self.assertEqual(repeated["hour_start"].dt.strftime("%z").tolist(), ["-0400", "-0500"])
        self.assertEqual(repeated["total_tokens"].tolist(), [11, 22])
        self.assertEqual(int(fall["total_tokens"].sum()), 33)

        spring = aggregate_recent_hourly(self.events([
            ("2026-03-08T06:30:00Z", "a", 11),
            ("2026-03-08T07:30:00Z", "b", 22),
        ]), ZoneInfo("America/New_York"), pd.Timestamp("2026-03-08").date())
        self.assertEqual(len(spring), 167)
        transition_day = spring.loc[spring["hour_start"].dt.day == 8]
        self.assertNotIn(2, transition_day["hour_start"].dt.hour.tolist())
        self.assertEqual(int(spring["total_tokens"].sum()), 33)


if __name__ == "__main__":
    unittest.main()
