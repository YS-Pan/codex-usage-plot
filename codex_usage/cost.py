#!/usr/bin/env python3
"""Estimate hypothetical OpenAI API token cost from a local events.csv export.

Standard text-token prices were checked against OpenAI Docs per model,
most recently on 2026-10-07.
This calculator intentionally excludes tool-call charges and does not invent a
price for the internal ``codex-auto-review`` model.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FuncFormatter, NullFormatter

from .charts import (
    breakdown_color_map,
    configure_matplotlib,
    save_figure,
    setup_date_axis,
    weekly_pivot,
)


PRICE_DATE = "2026-10-07"
MILLION = 1_000_000
AUTO_REVIEW_MODEL = "codex-auto-review"


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    cached_input_per_million: float
    output_per_million: float
    cache_write_multiplier: float = 1.25
    long_context_threshold: int = 272_000
    long_input_multiplier: float = 2.0
    long_output_multiplier: float = 1.5
    source: str = ""
    # Retain the original verification date for existing model prices.
    verified_on: str = "2026-09-23"


OFFICIAL_PRICES: dict[str, ModelPrice] = {
    "gpt-6-astra": ModelPrice(
        10.00,
        1.00,
        50.00,
        source="https://developers.openai.com/api/docs/models/gpt-6-astra",
    ),
    "gpt-6.1-sol": ModelPrice(
        2.00,
        0.10,
        10.00,
        source="https://developers.openai.com/api/docs/models/gpt-6.1-sol",
        verified_on=PRICE_DATE,
    ),
    "gpt-6-sol": ModelPrice(
        2.00,
        0.20,
        10.00,
        source="https://developers.openai.com/api/docs/models/gpt-6-sol",
    ),
    "gpt-6-luna": ModelPrice(
        0.10,
        0.01,
        0.50,
        source="https://developers.openai.com/api/docs/models/gpt-6-luna",
    ),
    "gpt-5.6-sol": ModelPrice(
        4.00,
        0.40,
        20.00,
        source="https://developers.openai.com/api/docs/models/gpt-5.6-sol",
    ),
    "gpt-5.6-terra": ModelPrice(
        2.00,
        0.20,
        12.00,
        source="https://developers.openai.com/api/docs/models/gpt-5.6-terra",
    ),
    "gpt-5.6-luna": ModelPrice(
        0.20,
        0.02,
        1.20,
        source="https://developers.openai.com/api/docs/models/gpt-5.6-luna",
    ),
    "gpt-5.5": ModelPrice(
        5.00,
        0.50,
        30.00,
        cache_write_multiplier=1.0,
        # The local data contains no GPT-5.5 request above 272K, but the
        # long-context multiplier is retained for future exports. GPT-5.5
        # has no additional cache-write charge, unlike GPT-5.6 and later.
        source="https://developers.openai.com/api/docs/models/gpt-5.5",
    ),
}

REQUIRED_COLUMNS = {
    "timestamp",
    "model",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "total_tokens",
}


def calculate_event_costs(events: pd.DataFrame, price: ModelPrice) -> pd.DataFrame:
    result = events.copy()
    result["uncached_input_tokens"] = (
        result["input_tokens"]
        - result["cached_input_tokens"]
        - result["cache_write_input_tokens"]
    ).clip(lower=0)
    long_context = result["input_tokens"] > price.long_context_threshold
    input_multiplier = np.where(long_context, price.long_input_multiplier, 1.0)
    output_multiplier = np.where(long_context, price.long_output_multiplier, 1.0)

    result["long_context"] = long_context
    result["uncached_input_cost_usd"] = (
        result["uncached_input_tokens"]
        / MILLION
        * price.input_per_million
        * input_multiplier
    )
    result["cached_input_cost_usd"] = (
        result["cached_input_tokens"]
        / MILLION
        * price.cached_input_per_million
        * input_multiplier
    )
    result["cache_write_cost_usd"] = (
        result["cache_write_input_tokens"]
        / MILLION
        * price.input_per_million
        * price.cache_write_multiplier
        * input_multiplier
    )
    result["output_cost_usd"] = (
        result["output_tokens"]
        / MILLION
        * price.output_per_million
        * output_multiplier
    )
    result["total_cost_usd"] = result[
        [
            "uncached_input_cost_usd",
            "cached_input_cost_usd",
            "cache_write_cost_usd",
            "output_cost_usd",
        ]
    ].sum(axis=1)
    return result


def summarize_official_models(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    priced_events: list[pd.DataFrame] = []
    for model, price in OFFICIAL_PRICES.items():
        subset = events.loc[events["model"] == model].copy()
        if subset.empty:
            continue
        calculated = calculate_event_costs(subset, price)
        priced_events.append(calculated)
        rows.append(
            {
                "model": model,
                "pricing_status": "official",
                "requests": int(len(calculated)),
                "input_tokens": int(calculated["input_tokens"].sum()),
                "cached_input_tokens": int(calculated["cached_input_tokens"].sum()),
                "uncached_input_tokens": int(calculated["uncached_input_tokens"].sum()),
                "cache_write_input_tokens": int(
                    calculated["cache_write_input_tokens"].sum()
                ),
                "output_tokens": int(calculated["output_tokens"].sum()),
                "total_tokens": int(calculated["total_tokens"].sum()),
                "long_context_requests": int(calculated["long_context"].sum()),
                "input_rate_per_million": price.input_per_million,
                "cached_input_rate_per_million": price.cached_input_per_million,
                "output_rate_per_million": price.output_per_million,
                "uncached_input_cost_usd": calculated[
                    "uncached_input_cost_usd"
                ].sum(),
                "cached_input_cost_usd": calculated["cached_input_cost_usd"].sum(),
                "cache_write_cost_usd": calculated["cache_write_cost_usd"].sum(),
                "output_cost_usd": calculated["output_cost_usd"].sum(),
                "total_cost_usd": calculated["total_cost_usd"].sum(),
                "official_source": price.source,
            }
        )

    unpriced = events.loc[~events["model"].isin(OFFICIAL_PRICES)].copy()
    for model, subset in unpriced.groupby("model"):
        uncached = (
            subset["input_tokens"]
            - subset["cached_input_tokens"]
            - subset["cache_write_input_tokens"]
        ).clip(lower=0)
        rows.append(
            {
                "model": model,
                "pricing_status": "no_public_api_price",
                "requests": int(len(subset)),
                "input_tokens": int(subset["input_tokens"].sum()),
                "cached_input_tokens": int(subset["cached_input_tokens"].sum()),
                "uncached_input_tokens": int(uncached.sum()),
                "cache_write_input_tokens": int(
                    subset["cache_write_input_tokens"].sum()
                ),
                "output_tokens": int(subset["output_tokens"].sum()),
                "total_tokens": int(subset["total_tokens"].sum()),
                "long_context_requests": None,
                "input_rate_per_million": None,
                "cached_input_rate_per_million": None,
                "output_rate_per_million": None,
                "uncached_input_cost_usd": None,
                "cached_input_cost_usd": None,
                "cache_write_cost_usd": None,
                "output_cost_usd": None,
                "total_cost_usd": None,
                "official_source": "",
            }
        )

    detail = pd.DataFrame(rows).sort_values(
        ["pricing_status", "total_cost_usd"], ascending=[False, False]
    )
    all_priced = pd.concat(priced_events, ignore_index=True)
    return detail, all_priced


def auto_review_scenarios(
    events: pd.DataFrame, priced_subtotal: float
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    auto_events = events.loc[events["model"] == AUTO_REVIEW_MODEL].copy()
    rows: list[dict[str, Any]] = []
    calculated_events: dict[str, pd.DataFrame] = {}
    for assumed_model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"):
        price = OFFICIAL_PRICES[assumed_model]
        calculated = calculate_event_costs(auto_events, price)
        scenario_name = f"auto_review_as_{assumed_model}"
        calculated_events[scenario_name] = calculated
        auto_cost = float(calculated["total_cost_usd"].sum())
        rows.append(
            {
                "scenario": scenario_name,
                "assumed_price_model": assumed_model,
                "auto_review_cost_usd": auto_cost,
                "officially_priced_subtotal_usd": priced_subtotal,
                "all_usage_total_usd": priced_subtotal + auto_cost,
                "note": "Scenario only; codex-auto-review has no published API price.",
            }
        )
    return pd.DataFrame(rows), calculated_events


def build_daily_costs(
    priced_events: pd.DataFrame,
    scenario_events: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    daily = (
        priced_events.set_index("timestamp")["total_cost_usd"]
        .resample("D")
        .sum()
        .rename("official_models_cost_usd")
        .to_frame()
    )
    model_daily = priced_events.groupby(
        [pd.Grouper(key="timestamp", freq="D"), "model"]
    )["total_cost_usd"].sum().unstack("model", fill_value=0)
    model_daily = model_daily.reindex(daily.index, fill_value=0)
    for model in model_daily.columns:
        daily[f"model_{model}_cost_usd"] = model_daily[model]
    for scenario, events in scenario_events.items():
        scenario_daily = events.set_index("timestamp")["total_cost_usd"].resample("D").sum()
        daily[scenario] = daily["official_models_cost_usd"].add(
            scenario_daily, fill_value=0
        )
    for column in list(daily.columns):
        daily[f"cumulative_{column}"] = daily[column].cumsum()
    return daily.reset_index().rename(columns={"timestamp": "date"})


def plot_cumulative_cost(
    daily: pd.DataFrame,
    output: Path,
    dpi: int,
    model_colors: dict[str, Any],
) -> None:
    configure_matplotlib()
    figure, axis = plt.subplots(figsize=(13, 5.8))
    dates = daily["date"].dt.tz_localize(None)
    bottom = np.zeros(len(daily), dtype=float)
    for model, color in model_colors.items():
        column = f"cumulative_model_{model}_cost_usd"
        if model not in OFFICIAL_PRICES or column not in daily.columns or daily.empty:
            continue
        values = daily[column].to_numpy(dtype=float)
        top = bottom + values
        axis.fill_between(
            dates, bottom, top, color=color, alpha=1.0, linewidth=0, edgecolor="none",
            label=f"{model}: ${values[-1]:,.2f}",
        )
        bottom = top
    total = float(bottom[-1]) if len(bottom) else 0.0
    axis.set(title=f"Cumulative estimated API cost by model — total ${total:,.2f}", ylabel="USD")
    axis.set_ylim(bottom=0)
    axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"${value:,.0f}"))
    setup_date_axis(axis)
    if axis.collections:
        axis.legend(frameon=False, ncol=2, loc="upper left", fontsize=9)
        if total == 0:
            axis.set_ylim(0, 1)
            axis.set_yticks([0, 1])
    else:
        axis.text(
            0.5, 0.5, "No publicly priced model usage",
            transform=axis.transAxes, ha="center", va="center", color="#64748B",
        )
        axis.xaxis.set_major_formatter(NullFormatter())
        axis.set_xticks([])
        axis.set_yticks([])
    save_figure(figure, output.parent, output.stem, output.suffix.lstrip("."), dpi)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codex_usage cost",
        description="根据用量数据 events.csv 估算 API token 费用（非实际账单）。"
    )
    parser.add_argument(
        "--events",
        type=Path,
        default=Path("codex_usage_report/events.csv"),
        help="用量数据文件 events.csv 路径。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("codex_usage_report"),
        help="结果目录，默认 codex_usage_report。",
    )
    parser.add_argument("--dpi", type=int, default=160, help="图片 DPI，默认 160。")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    events_path = args.events.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()
    if not events_path.is_file():
        parser.error(f"找不到 {events_path}；请先生成用量数据，或用 --events 指定 events.csv。")
    if args.dpi <= 0:
        parser.error("--dpi 必须大于 0。")

    events = pd.read_csv(events_path)
    missing = REQUIRED_COLUMNS.difference(events.columns)
    if missing:
        parser.error(f"events.csv 缺少列：{sorted(missing)}；请重新生成用量数据。")
    # ``format='mixed'`` handles ISO timestamps whose fractional-second width
    # varies between rows while preserving their recorded UTC offset.
    events["timestamp"] = pd.to_datetime(events["timestamp"], format="mixed")
    output_dir.mkdir(parents=True, exist_ok=True)

    model_detail, priced_events = summarize_official_models(events)
    priced_subtotal = float(priced_events["total_cost_usd"].sum())
    scenarios, scenario_events = auto_review_scenarios(events, priced_subtotal)
    daily = build_daily_costs(priced_events, scenario_events)
    # Use every model, including unpriced ones, to reproduce chart 08's colors.
    model_colors = breakdown_color_map(weekly_pivot(events, "model").columns)
    # Stack earlier models below later ones, using the exact first timestamp.
    model_order = events.groupby("model")["timestamp"].min().sort_values(kind="stable").index
    model_colors = {model: model_colors[model] for model in model_order}

    model_detail.to_csv(
        output_dir / "api_cost_by_model.csv", index=False, encoding="utf-8-sig"
    )
    scenarios.to_csv(
        output_dir / "api_cost_scenarios.csv", index=False, encoding="utf-8-sig"
    )
    daily.to_csv(output_dir / "daily_api_cost.csv", index=False, encoding="utf-8-sig")
    plot_cumulative_cost(daily, output_dir / "09_cumulative_api_cost.png", args.dpi, model_colors)

    auto_events = events.loc[events["model"] == AUTO_REVIEW_MODEL]
    total_only_snapshot_tokens = int(
        (
            events["total_tokens"]
            - events["input_tokens"]
            - events["output_tokens"]
        )
        .clip(lower=0)
        .sum()
    )
    report = {
        "price_date": PRICE_DATE,
        "currency": "USD",
        "priced_subtotal_usd": priced_subtotal,
        "usage_scope": {
            "reported_total_tokens": int(events["total_tokens"].sum()),
            "tokens_with_input_output_breakdown": int(
                events["input_tokens"].sum() + events["output_tokens"].sum()
            ),
            "total_only_snapshot_tokens_excluded_from_cost": total_only_snapshot_tokens,
        },
        "unpriced_model": AUTO_REVIEW_MODEL,
        "unpriced_model_usage": {
            "requests": int(len(auto_events)),
            "input_tokens": int(auto_events["input_tokens"].sum()),
            "cached_input_tokens": int(auto_events["cached_input_tokens"].sum()),
            "output_tokens": int(auto_events["output_tokens"].sum()),
            "total_tokens": int(auto_events["total_tokens"].sum()),
        },
        "scenarios": scenarios.to_dict(orient="records"),
        "official_prices": {
            model: asdict(price) for model, price in OFFICIAL_PRICES.items()
        },
        "assumptions": [
            "Cached input is a subset of input_tokens and is not double-counted.",
            "Reasoning output is a subset of output_tokens and is not double-counted.",
            "Requests over 272K input tokens use 2x input and 1.5x output rates.",
            "GPT-5.6 and later cache writes use 1.25x the uncached input rate; GPT-5.5 has no additional cache-write charge.",
            "Tool-call, storage, regional-processing, Batch, Flex, and priority-processing charges are excluded.",
            "Rows that report total_tokens but zero input/output breakdown are state snapshots and are excluded from API cost.",
            "codex-auto-review scenarios are illustrations, not an official price mapping.",
        ],
    }
    with (output_dir / "api_cost_estimate.json").open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(f"Officially priced subtotal: ${priced_subtotal:,.2f}")
    for row in report["scenarios"]:
        print(
            f"{row['assumed_price_model']}: auto-review ${row['auto_review_cost_usd']:,.2f}; "
            f"all usage ${row['all_usage_total_usd']:,.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
