#!/usr/bin/env python3
"""Estimate hypothetical OpenAI API token cost from a local events.csv export.

Standard text-token prices were checked against OpenAI Docs per model,
most recently on 2026-10-29.
This calculator intentionally excludes tool-call charges and does not invent a
price for the internal ``codex-auto-review`` model.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FuncFormatter, NullFormatter

from .charts import (
    COMPOSITION_COLUMNS,
    COMPOSITION_LABELS,
    breakdown_color_map,
    configure_matplotlib,
    format_tokens,
    model_stack_colors,
    save_figure,
    setup_date_axis,
    setup_recent_hourly_axis,
    weekly_pivot,
)
from .model_prices import (
    AUTO_REVIEW_MODEL, AUTO_REVIEW_SCENARIO_MODELS, ModelPrice, PricePeriod,
    OFFICIAL_PRICES, OFFICIAL_PRICE_HISTORY, PRICE_DATE,
)
from .progress import ProgressReporter, report_progress


MILLION = 1_000_000

COST_COMPOSITION_COLUMNS = [
    "uncached_input_cost_usd",
    "cached_input_cost_usd",
    "cache_write_cost_usd",
    "visible_output_cost_usd",
    "reasoning_output_cost_usd",
]
COST_COMPOSITION_TOKENS = dict(zip(COST_COMPOSITION_COLUMNS, COMPOSITION_COLUMNS))


def official_price_history() -> dict[str, list[dict[str, Any]]]:
    """JSON-ready provenance for the timeline used by all cost entry points."""
    return {
        model: [asdict(period) for period in periods]
        for model, periods in OFFICIAL_PRICE_HISTORY.items()
    }


def _event_price_periods(
    events: pd.DataFrame, model: str,
) -> tuple[tuple[PricePeriod, ...], np.ndarray]:
    periods = OFFICIAL_PRICE_HISTORY[model]
    if not periods or periods[0].effective_from is not None:
        raise ValueError(f"{model}: price history must start with a compatibility baseline")
    selected = np.zeros(len(events), dtype=int)
    if len(periods) == 1:
        return periods, selected

    timestamps = pd.to_datetime(
        events.get("timestamp", pd.Series(pd.NaT, index=events.index)),
        format="mixed", utc=True, errors="coerce",
    )
    previous = None
    for index, period in enumerate(periods[1:], start=1):
        start = pd.Timestamp(period.effective_from)
        if pd.isna(start):
            raise ValueError(f"{model}: dated price periods must have a valid start")
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        if previous is not None and start <= previous:
            raise ValueError(f"{model}: price periods must be in strictly increasing order")
        selected[(timestamps >= start).to_numpy()] = index
        previous = start
    return periods, selected


def calculate_model_event_costs(events: pd.DataFrame, model: str) -> pd.DataFrame:
    """Price each event at its UTC instant; unavailable times use the baseline.

    Naive timestamps (including date-only strings) mean UTC. Comparison uses a
    temporary parsed series so original timestamps, row order and indices remain
    intact. The explicit-price calculate_event_costs interface remains available.
    """
    periods, selected = _event_price_periods(events, model)
    result = calculate_event_costs(events, periods[0].price)
    # Positional assignment also supports non-unique and non-monotonic indices.
    for index in np.unique(selected):
        if index == 0:
            continue
        positions = np.flatnonzero(selected == index)
        calculated = calculate_event_costs(events.iloc[positions], periods[index].price)
        for column in (
            "long_context", "uncached_input_cost_usd", "cached_input_cost_usd",
            "cache_write_cost_usd", "output_cost_usd", "total_cost_usd",
        ):
            result.iloc[positions, result.columns.get_loc(column)] = calculated[column].to_numpy()
    return result


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


def aggregate_thread_model_costs(events: pd.DataFrame) -> pd.DataFrame:
    """Estimate each task/model from individual requests, excluding auto-review."""
    selected = events.loc[
        (events["model"] != AUTO_REVIEW_MODEL) & (events.get("source", "") != "auto_review")
    ].copy()
    selected["total_cost_usd"] = np.nan
    selected["priced_tokens"] = 0
    breakdown_tokens = selected["input_tokens"] + selected["output_tokens"]
    for model in OFFICIAL_PRICES:
        # A total-only snapshot has no input/output split to price.
        mask = (selected["model"] == model) & (
            (breakdown_tokens > 0) | (selected["total_tokens"] == 0)
        )
        if not mask.any():
            continue
        calculated = calculate_model_event_costs(selected.loc[mask], model)
        selected.loc[mask, "total_cost_usd"] = calculated["total_cost_usd"]
        selected.loc[mask, "priced_tokens"] = np.minimum(
            selected.loc[mask, "total_tokens"], breakdown_tokens.loc[mask],
        )
    selected["unpriced_tokens"] = (
        selected["total_tokens"] - selected["priced_tokens"]
    ).clip(lower=0)
    return (
        selected.groupby(["thread_id", "model"])
        [["total_cost_usd", "priced_tokens", "unpriced_tokens"]]
        .sum(min_count=1)
        .reset_index()
    )


def _has_cost_breakdown(events: pd.DataFrame) -> pd.Series:
    """A positive total without input/output details cannot be priced."""
    return ((events["input_tokens"] + events["output_tokens"] > 0)
            | (events["total_tokens"] == 0))


def summarize_official_models(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    priced_events: list[pd.DataFrame] = []
    for model in OFFICIAL_PRICES:
        subset = events.loc[events["model"] == model].copy()
        if subset.empty:
            continue
        eligible = subset.loc[_has_cost_breakdown(subset)]
        calculated = calculate_model_event_costs(eligible, model)
        periods, selected = _event_price_periods(eligible, model)
        used_prices = [periods[index].price for index in np.unique(selected)]
        price = used_prices[0] if len(used_prices) == 1 else None
        priced_events.append(calculated)
        rows.append(
            {
                "model": model,
                "pricing_status": "official",
                "requests": int(len(subset)),
                "unpriced_requests": int(len(subset) - len(eligible)),
                "unpriced_tokens": int((subset["total_tokens"] - (subset["input_tokens"] + subset["output_tokens"]).clip(upper=subset["total_tokens"])).clip(lower=0).sum()),
                "input_tokens": int(subset["input_tokens"].sum()),
                "cached_input_tokens": int(subset["cached_input_tokens"].sum()),
                "uncached_input_tokens": int(calculated["uncached_input_tokens"].sum()),
                "cache_write_input_tokens": int(
                    calculated["cache_write_input_tokens"].sum()
                ),
                "output_tokens": int(calculated["output_tokens"].sum()),
                "total_tokens": int(subset["total_tokens"].sum()),
                "long_context_requests": int(calculated["long_context"].sum()),
                "input_rate_per_million": price.input_per_million if price else None,
                "cached_input_rate_per_million": price.cached_input_per_million if price else None,
                "output_rate_per_million": price.output_per_million if price else None,
                "price_period_count": len(used_prices),
                "uncached_input_cost_usd": calculated[
                    "uncached_input_cost_usd"
                ].sum() if len(calculated) else None,
                "cached_input_cost_usd": calculated["cached_input_cost_usd"].sum() if len(calculated) else None,
                "cache_write_cost_usd": calculated["cache_write_cost_usd"].sum() if len(calculated) else None,
                "output_cost_usd": calculated["output_cost_usd"].sum() if len(calculated) else None,
                "total_cost_usd": calculated["total_cost_usd"].sum() if len(calculated) else None,
                "official_source": "; ".join(dict.fromkeys(item.source for item in used_prices)),
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
                "unpriced_requests": int(len(subset)),
                "unpriced_tokens": int(subset["total_tokens"].sum()),
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
                "price_period_count": 0,
                "uncached_input_cost_usd": None,
                "cached_input_cost_usd": None,
                "cache_write_cost_usd": None,
                "output_cost_usd": None,
                "total_cost_usd": None,
                "official_source": "",
            }
        )

    detail = pd.DataFrame(rows, columns=[
        "model", "pricing_status", "requests", "unpriced_requests", "unpriced_tokens",
        "input_tokens", "cached_input_tokens", "uncached_input_tokens", "cache_write_input_tokens",
        "output_tokens", "total_tokens", "long_context_requests", "input_rate_per_million",
        "cached_input_rate_per_million", "output_rate_per_million", "price_period_count",
        "uncached_input_cost_usd", "cached_input_cost_usd", "cache_write_cost_usd",
        "output_cost_usd", "total_cost_usd", "official_source",
    ]).sort_values(
        ["pricing_status", "total_cost_usd"], ascending=[False, False]
    )
    all_priced = (pd.concat(priced_events, ignore_index=True) if priced_events
                  else calculate_event_costs(events.iloc[:0], next(iter(OFFICIAL_PRICES.values()))))
    return detail, all_priced


def auto_review_scenarios(
    events: pd.DataFrame, priced_subtotal: float
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    auto_events = events.loc[(events["model"] == AUTO_REVIEW_MODEL) & _has_cost_breakdown(events)].copy()
    rows: list[dict[str, Any]] = []
    calculated_events: dict[str, pd.DataFrame] = {}
    for assumed_model in AUTO_REVIEW_SCENARIO_MODELS:
        calculated = calculate_model_event_costs(auto_events, assumed_model)
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
    scenario_daily = {
        name: frame.set_index("timestamp")["total_cost_usd"].resample("D").sum()
        for name, frame in scenario_events.items()
    }
    all_days = daily.index
    for values in scenario_daily.values():
        all_days = all_days.union(values.index)
    if len(all_days):
        all_days = pd.date_range(all_days.min(), all_days.max(), freq="D", name="timestamp")
        daily = daily.reindex(all_days, fill_value=0.0)
    model_daily = priced_events.groupby(
        [pd.Grouper(key="timestamp", freq="D"), "model"]
    )["total_cost_usd"].sum().unstack("model", fill_value=0)
    model_daily = model_daily.reindex(daily.index, fill_value=0)
    for model in model_daily.columns:
        daily[f"model_{model}_cost_usd"] = model_daily[model]
    for scenario, values in scenario_daily.items():
        daily[scenario] = daily["official_models_cost_usd"].add(
            values, fill_value=0
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


def aggregate_daily_cost_composition(
    events: pd.DataFrame, dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Price events first, then sum mutually exclusive costs on local calendar days."""
    daily = pd.DataFrame(0.0, index=dates, columns=[
        *COST_COMPOSITION_COLUMNS, "official_models_cost_usd",
    ])
    daily.index.name = "date"
    daily["unpriced_requests"] = 0
    daily["unpriced_tokens"] = 0
    local_events = events.assign(timestamp=events["timestamp"].dt.tz_convert(dates.tz))
    selected = local_events.loc[
        (local_events["timestamp"] >= dates[0])
        & (local_events["timestamp"] < dates[-1] + pd.DateOffset(days=1))
    ] if len(dates) else local_events.iloc[:0]
    for model in OFFICIAL_PRICES:
        subset = selected.loc[(selected["model"] == model) & _has_cost_breakdown(selected)]
        if subset.empty:
            continue
        calculated = calculate_model_event_costs(subset, model)
        output_tokens = calculated["output_tokens"]
        reasoning_tokens = calculated.get(
            "reasoning_output_tokens", pd.Series(0, index=calculated.index),
        ).clip(lower=0, upper=output_tokens)
        reasoning_share = reasoning_tokens.div(output_tokens.where(output_tokens > 0)).fillna(0)
        calculated["reasoning_output_cost_usd"] = calculated["output_cost_usd"] * reasoning_share
        calculated["visible_output_cost_usd"] = (
            calculated["output_cost_usd"] - calculated["reasoning_output_cost_usd"]
        )
        values = calculated.set_index("timestamp")[COST_COMPOSITION_COLUMNS].resample("D").sum()
        daily[COST_COMPOSITION_COLUMNS] += values.reindex(dates, fill_value=0.0)
    daily["official_models_cost_usd"] = daily[COST_COMPOSITION_COLUMNS].sum(axis=1)
    eligible = selected["model"].isin(OFFICIAL_PRICES) & _has_cost_breakdown(selected)
    unpriced = selected.assign(
        unpriced_request=(~eligible).astype("int64"),
        unpriced_token=np.where(
            eligible,
            (selected["total_tokens"] - selected["input_tokens"] - selected["output_tokens"]).clip(lower=0),
            selected["total_tokens"],
        ),
    ).set_index("timestamp")
    for target, source in [("unpriced_requests", "unpriced_request"), ("unpriced_tokens", "unpriced_token")]:
        daily[target] = unpriced[source].resample("D").sum().reindex(dates, fill_value=0).astype("int64")
    return daily.reset_index()


def plot_daily_cost_composition(
    daily: pd.DataFrame, output_dir: Path, image_format: str, dpi: int,
    *, component_colors: dict[str, Any] | None = None,
) -> Path:
    """Draw each day's estimated token costs as stacked daily bars."""
    configure_matplotlib()
    figure, axis = plt.subplots(figsize=(13, 5.8))
    dates = daily["date"].dt.tz_localize(None)
    active = [COST_COMPOSITION_TOKENS[column] for column in COST_COMPOSITION_COLUMNS if daily[column].sum() > 0]
    colors = component_colors if component_colors is not None else breakdown_color_map(active)
    fallback_colors = breakdown_color_map(COMPOSITION_COLUMNS)
    bottom = np.zeros(len(daily), dtype=float)
    for column in COST_COMPOSITION_COLUMNS:
        values = daily[column].to_numpy(dtype=float)
        if not values.any():
            continue
        token_column = COST_COMPOSITION_TOKENS[column]
        axis.bar(
            dates, values, width=0.82, bottom=bottom,
            color=colors.get(token_column, fallback_colors[token_column]),
            alpha=1.0, linewidth=0, edgecolor="none",
            label=f"{COMPOSITION_LABELS[token_column]}: ${values.sum():,.2f}",
        )
        bottom += values
    total = float(daily["official_models_cost_usd"].sum())
    axis.set(
        title=f"Daily estimated API cost by token type — total ${total:,.2f}",
        ylabel="Estimated cost (USD)", yscale="linear",
    )
    axis.set_ylim(bottom=0)
    axis.yaxis.set_major_formatter(FuncFormatter(
        lambda value, _pos: f"${value:,.2f}" if axis.get_ylim()[1] < 10 else f"${value:,.0f}"
    ))
    setup_date_axis(axis)
    if len(dates):
        # A single day still needs a visible horizontal time span.
        axis.set_xlim(dates.iloc[0] - pd.Timedelta(hours=12), dates.iloc[-1] + pd.Timedelta(hours=12))
    if axis.containers:
        axis.legend(frameon=False, ncol=2, loc="upper left", fontsize=9)
    else:
        axis.set_ylim(0, 1)
        axis.set_yticks([0, 1])
        axis.text(
            0.5, 0.5, "No estimated cost for models with recorded API rates",
            transform=axis.transAxes, ha="center", va="center", color="#64748B",
        )
        if not len(dates):
            axis.xaxis.set_major_formatter(NullFormatter())
            axis.set_xticks([])
    excluded = int(daily["unpriced_requests"].sum())
    excluded_tokens = int(daily["unpriced_tokens"].sum())
    axis.set_title(
        f"{axis.get_title()}\nRecorded API rates; unpriced usage excluded"
        f" ({excluded:,} events / {format_tokens(excluded_tokens)} tokens)", fontsize=11,
    )
    return save_figure(figure, output_dir, "14_daily_api_cost_composition", image_format, dpi)


def aggregate_recent_hourly_cost(events: pd.DataFrame, hours: pd.DatetimeIndex) -> pd.DataFrame:
    """Price individual requests before summing on chart 12's actual local hours."""
    local_events = events.assign(timestamp=events["timestamp"].dt.tz_convert(hours.tz))
    selected = local_events.loc[
        (local_events["timestamp"] >= hours[0])
        & (local_events["timestamp"] < hours[-1] + pd.Timedelta(hours=1))
    ]
    hourly = pd.DataFrame(index=hours)
    hourly.index.name = "hour_start"
    hourly["official_models_cost_usd"] = 0.0
    for model in OFFICIAL_PRICES:
        subset = selected.loc[(selected["model"] == model) & _has_cost_breakdown(selected)]
        if subset.empty:
            continue
        calculated = calculate_model_event_costs(subset, model)
        values = (
            calculated.set_index("timestamp")["total_cost_usd"]
            .resample("h", origin=hours[0]).sum().reindex(hours, fill_value=0.0)
        )
        hourly[f"model_{model}_cost_usd"] = values
        hourly["official_models_cost_usd"] += values
    eligible = selected["model"].isin(OFFICIAL_PRICES) & _has_cost_breakdown(selected)
    unpriced = selected.assign(
        unpriced_request=(~eligible).astype("int64"),
        unpriced_token=np.where(
            eligible,
            (selected["total_tokens"] - selected["input_tokens"] - selected["output_tokens"]).clip(lower=0),
            selected["total_tokens"],
        ),
    ).set_index("timestamp")
    hourly["unpriced_requests"] = (
        unpriced["unpriced_request"].resample("h", origin=hours[0]).sum().reindex(hours, fill_value=0)
    ).astype("int64")
    hourly["unpriced_tokens"] = (
        unpriced["unpriced_token"].resample("h", origin=hours[0]).sum().reindex(hours, fill_value=0)
    ).astype("int64")
    return hourly.reset_index()


def plot_recent_hourly_cost(
    hourly: pd.DataFrame,
    events: pd.DataFrame,
    output_dir: Path,
    image_format: str,
    dpi: int,
    *, model_colors: dict[str, Any] | None = None,
) -> Path:
    configure_matplotlib()
    figure, axis = plt.subplots(figsize=(14, 5.5))
    hours = hourly["hour_start"]
    bottom = np.zeros(len(hourly), dtype=float)
    for model, color in (model_colors if model_colors is not None else model_stack_colors(events)).items():
        column = f"model_{model}_cost_usd"
        if column not in hourly:
            continue
        values = hourly[column].to_numpy(dtype=float)
        if not values.any():
            continue
        axis.bar(
            hours, values, width=0.86 / 24, align="edge", bottom=bottom,
            color=color, linewidth=0, label=model,
        )
        bottom += values
    total = float(hourly["official_models_cost_usd"].sum())
    setup_recent_hourly_axis(
        axis, hours, "Hourly estimated API cost by model — last 7 calendar days",
        "Estimated cost (USD)", f"${total:,.2f}",
    )
    # Preserve cents for low-cost windows without crowding larger dollar ticks.
    axis.yaxis.set_major_formatter(FuncFormatter(
        lambda value, _pos: f"${value:,.2f}" if axis.get_ylim()[1] < 10 else f"${value:,.0f}"
    ))
    excluded = int(hourly["unpriced_requests"].sum())
    excluded_tokens = int(hourly["unpriced_tokens"].sum())
    axis.set_title(
        f"{axis.get_title()}\nRecorded API rates; unpriced usage excluded"
        f" ({excluded:,} events / {format_tokens(excluded_tokens)} tokens)",
        fontsize=11,
    )
    if axis.containers:
        axis.legend(frameon=False, ncol=4, loc="upper left", bbox_to_anchor=(0, -0.22), fontsize=9)
    if total == 0:
        axis.set_ylim(0, 1)
        axis.set_yticks([0, 1])
        axis.text(
            0.5, 0.5, "No estimated cost for models with recorded API rates in this 7-day window",
            transform=axis.transAxes, ha="center", va="center", color="#64748B",
        )
    return save_figure(figure, output_dir, "13_recent_hourly_api_cost", image_format, dpi)


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
    parser.add_argument("--quiet", action="store_true", help="隐藏进度，保留最终结果。")
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

    report_progress("cost-read", "正在读取费用估算数据", enabled=not args.quiet)
    events = pd.read_csv(events_path)
    missing = REQUIRED_COLUMNS.difference(events.columns)
    if missing:
        parser.error(f"events.csv 缺少列：{sorted(missing)}；请重新生成用量数据。")
    # ``format='mixed'`` handles ISO timestamps whose fractional-second width
    # varies between rows while preserving their recorded UTC offset.
    events["timestamp"] = pd.to_datetime(events["timestamp"], format="mixed")
    output_dir.mkdir(parents=True, exist_ok=True)

    report_progress("cost-calculate", "正在计算费用估算", enabled=not args.quiet)
    model_detail, priced_events = summarize_official_models(events)
    priced_subtotal = float(priced_events["total_cost_usd"].sum())
    scenarios, scenario_events = auto_review_scenarios(events, priced_subtotal)
    daily = build_daily_costs(priced_events, scenario_events)
    # Use every model, including unpriced ones, to reproduce chart 08's colors.
    model_colors = breakdown_color_map(weekly_pivot(events, "model").columns)
    # Stack earlier models below later ones, using the exact first timestamp.
    model_order = events.groupby("model")["timestamp"].min().sort_values(kind="stable").index
    model_colors = {model: model_colors[model] for model in model_order}

    data_progress = ProgressReporter("cost-data", "保存费用数据文件", total=3,
                                     unit="个", enabled=not args.quiet)
    data_progress.update(0)
    model_detail.to_csv(
        output_dir / "api_cost_by_model.csv", index=False, encoding="utf-8-sig"
    )
    data_progress.update(1, "api_cost_by_model.csv")
    scenarios.to_csv(
        output_dir / "api_cost_scenarios.csv", index=False, encoding="utf-8-sig"
    )
    data_progress.update(2, "api_cost_scenarios.csv")
    daily.to_csv(output_dir / "daily_api_cost.csv", index=False, encoding="utf-8-sig")
    data_progress.update(3, "daily_api_cost.csv")
    image_progress = ProgressReporter("cost-image", "生成费用图片 09", total=1,
                                      unit="张", enabled=not args.quiet)
    image_progress.update(0)
    plot_cumulative_cost(daily, output_dir / "09_cumulative_api_cost.png", args.dpi, model_colors)
    image_progress.update(1, "09_cumulative_api_cost.png")

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
        "official_price_history": official_price_history(),
        "assumptions": [
            "Prices are selected by each event's UTC instant; missing or invalid timestamps use the compatibility baseline, and naive timestamps mean UTC.",
            "Cached input is a subset of input_tokens and is not double-counted.",
            "Reasoning output is a subset of output_tokens and is not double-counted.",
            "Requests over 272K input tokens use 2x input and 1.5x output rates.",
            "GPT-5.6 and later cache writes use 1.25x the uncached input rate; GPT-5.5 has no additional cache-write charge.",
            "Tool-call, storage, regional-processing, Batch, Flex, and priority-processing charges are excluded.",
            "Rows that report total_tokens but zero input/output breakdown are state snapshots and are excluded from API cost.",
            "codex-auto-review scenarios are illustrations, not an official price mapping.",
        ],
    }
    report_progress("cost-summary", "正在保存费用估算汇总", enabled=not args.quiet)
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
