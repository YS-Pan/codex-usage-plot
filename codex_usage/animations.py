"""Daily snapshots of the existing plots, encoded locally as MP4 and optional GIF."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Iterable

# Select the noninteractive backend before importing pyplot.
from . import charts

import matplotlib.pyplot as plt
from matplotlib.colors import to_hex
from matplotlib.ticker import MaxNLocator
import pandas as pd
from PIL import Image, ImageOps

from .running_time import RunningInterval, aggregate_daily_running_time, local_midnight


CHART_NAMES = {
    1: "01_daily_tokens", 2: "02_cumulative_tokens", 3: "03_daily_composition",
    4: "04_weekly_by_source", 5: "05_weekday_hour_heatmap", 6: "06_top_threads",
    7: "07_request_sizes_over_time", 8: "08_weekly_by_model", 9: "09_cumulative_api_cost",
    10: "10_daily_running_time", 11: "11_daily_cache_miss_rate",
    12: "12_recent_hourly_tokens", 13: "13_recent_hourly_api_cost",
}
END_HOLD_SECONDS = 1


@dataclass(frozen=True)
class AnimationOptions:
    charts: tuple[int, ...] = (2,)
    start: date | None = None
    end: date | None = None
    fps: int = 4
    dpi: int = 100
    gif: bool = False


def animation_dates(
    options: AnimationOptions, target_tz: tzinfo, observed_at: datetime | None = None,
) -> pd.DatetimeIndex:
    """Use local calendar days, even across DST and when recent logs are idle."""
    if options.fps <= 0 or options.dpi <= 0:
        raise ValueError("动画帧率和 DPI 必须大于 0。")
    if any(number not in CHART_NAMES for number in options.charts):
        raise ValueError("动画图片编号必须在 01–13 之间。")
    today = (observed_at or datetime.now(timezone.utc)).astimezone(target_tz).date()
    end = min(options.end or today, today)
    start = options.start or end - timedelta(days=30)
    if start > end:
        raise ValueError("动画起始日期不能晚于结束日期或今天。")
    return pd.date_range(start, end, freq="D", tz=target_tz)


def snapshot_data(
    events: pd.DataFrame, intervals: list[RunningInterval], cutoff: datetime,
) -> tuple[pd.DataFrame, list[RunningInterval]]:
    """Never include future requests or the future portion of a running turn."""
    visible = events.loc[events["timestamp"] < cutoff].copy()
    clipped = [replace(item, end=min(item.end, cutoff)) for item in intervals if item.start < cutoff]
    return visible, clipped


def ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg
    except ModuleNotFoundError:
        executable = shutil.which("ffmpeg")
        if executable:
            return executable
        raise RuntimeError(
            "找不到 MP4 编码器。请运行 python -m codex_usage setup 安装动画依赖，"
            "或使用 report --no-animations 仅更新图片。"
        ) from None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except RuntimeError as exc:
        raise RuntimeError("找不到 MP4 编码器，请重新运行 python -m codex_usage setup。") from exc


def write_mp4(
    frames: Iterable[Image.Image], path: Path, size: tuple[int, int], fps: int,
    executable: str,
) -> None:
    """Stream RGB frames, hold the last image, and replace only on success."""
    staged: Path | None = None
    process: subprocess.Popen | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".animation-", suffix=".mp4", dir=path.parent, delete=False) as handle:
            staged = Path(handle.name)
        with tempfile.TemporaryFile(dir=path.parent) as errors:
            process = subprocess.Popen(
                [executable, "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}",
                 "-framerate", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
                 "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", str(staged)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                count = 0
                final_frame: bytes | None = None
                resampling = getattr(Image, "Resampling", Image)  # Pillow 8 is allowed by Matplotlib 3.8.
                for frame in frames:
                    with frame:
                        fitted = ImageOps.pad(frame, size, color="white", method=resampling.LANCZOS)
                        try:
                            final_frame = fitted.tobytes()
                            process.stdin.write(final_frame)
                        finally:
                            fitted.close()
                    count += 1
                if final_frame is not None:
                    for _ in range(fps * END_HOLD_SECONDS):
                        process.stdin.write(final_frame)
                process.stdin.close()
                process.wait(timeout=30)
                if process.returncode or count == 0:
                    raise RuntimeError("MP4 编码失败。")
            except (BrokenPipeError, subprocess.TimeoutExpired, RuntimeError) as exc:
                if process.poll() is None:
                    process.kill()
                process.wait()
                errors.seek(0)
                detail = errors.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"MP4 编码失败：{detail or str(exc)}") from exc
        staged.replace(path)
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        if staged is not None:
            staged.unlink(missing_ok=True)


def convert_mp4_to_gif(source: Path, target: Path, executable: str) -> None:
    """Use a shared palette for all frames and preserve the MP4's timing."""
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".animation-", suffix=".gif", dir=target.parent, delete=False) as handle:
            staged = Path(handle.name)
        with tempfile.TemporaryDirectory(prefix=".animation-palette-", dir=target.parent) as directory:
            palette = Path(directory) / "palette.png"
            commands = [
                ["-i", str(source), "-vf", "palettegen=reserve_transparent=0", "-frames:v", "1", "-threads", "1", str(palette)],
                ["-i", str(source), "-i", str(palette), "-filter_complex", "[0:v][1:v]paletteuse=dither=none", "-loop", "0", str(staged)],
            ]
            for arguments in commands:
                try:
                    subprocess.run(
                        [executable, "-hide_banner", "-loglevel", "error", "-y", *arguments],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        check=True, timeout=60,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                    detail = exc.stderr.decode("utf-8", errors="replace").strip() if exc.stderr else str(exc)
                    raise RuntimeError(f"GIF 编码失败：{detail}") from exc
        staged.replace(target)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


class FrameRenderer:
    """Freeze full-report colors once, then reuse the normal plotting functions."""

    def __init__(
        self, events: pd.DataFrame, intervals: list[RunningInterval], target_tz: tzinfo,
        history_start: date, dates: pd.DatetimeIndex, options: AnimationOptions,
        top_threads: int, observed_at: datetime,
    ) -> None:
        self.events, self.intervals, self.target_tz = events, intervals, target_tz
        self.history_start = min(history_start, dates[0].date())
        self.options = options
        self.top_threads, self.observed_at = top_threads, observed_at
        self.model_colors = charts.model_stack_colors(events)
        self.source_colors = charts.breakdown_color_map(charts.weekly_pivot(events, "source").columns)
        self.point_colors = charts.breakdown_color_map(sorted(events["source"].unique()))
        active = [column for column in charts.COMPOSITION_COLUMNS if events[column].sum() > 0]
        self.component_colors = charts.breakdown_color_map(active)

    def render(self, number: int, stamp: pd.Timestamp) -> Image.Image:
        from . import cost

        day = stamp.date()
        cutoff = min(local_midnight(day + timedelta(days=1), self.target_tz), self.observed_at)
        events, intervals = snapshot_data(self.events, self.intervals, cutoff)
        dates = pd.date_range(self.history_start, day, tz=self.target_tz)
        daily = charts.aggregate_daily(events, dates)
        output = Path(".")  # The figure sink consumes the plot; no still file is written.
        arguments = (output, "png", self.options.dpi)
        image: Image.Image | None = None

        def capture(figure: plt.Figure) -> None:
            nonlocal image
            if number == 2:
                # Keep the plot at the same pixels even before the first model
                # appears; tight cropping would move axes when tick labels change.
                figure.set_size_inches(14, 8)
                figure.subplots_adjust(left=0.095, right=0.975, bottom=0.13, top=0.86)
                axis = figure.axes[0]
                axis.set_xlim(pd.Timestamp(self.history_start), stamp.tz_localize(None) + pd.Timedelta(days=1))
                axis.set_ylim(0, max(float(daily["cumulative_tokens"].iloc[-1]) * 1.12, 1))
                axis.yaxis.set_major_locator(MaxNLocator(nbins=7, integer=True))
                charts.setup_date_axis(axis)
            figure.text(
                0.99, 0.99, f"Through {day:%Y-%m-%d} ({self.target_tz})",
                ha="right", va="top", fontsize=10, color="#475569",
            )
            with BytesIO() as buffer:
                figure.savefig(buffer, format="png", dpi=self.options.dpi,
                               bbox_inches=None if number == 2 else "tight")
                buffer.seek(0)
                with Image.open(buffer) as source:
                    image = source.convert("RGB")

        with charts.figure_sink(capture):
            if number == 1:
                charts.plot_daily_usage(daily, events, *arguments, model_colors=self.model_colors)
            elif number == 2:
                charts.plot_cumulative_usage(daily, events, *arguments, model_colors=self.model_colors)
            elif number == 3:
                charts.plot_daily_composition(daily, *arguments, component_colors=self.component_colors)
            elif number in (4, 8):
                category = "source" if number == 4 else "model"
                colors = self.source_colors if number == 4 else self.model_colors
                charts.plot_weekly_breakdown(
                    events, category, f"Weekly token usage by {category}", CHART_NAMES[number],
                    *arguments, category_colors=colors,
                )
            elif number == 5:
                charts.plot_activity_heatmap(events, *arguments)
            elif number == 6:
                charts.plot_top_threads(charts.aggregate_threads(events), events, self.top_threads,
                                       *arguments, model_colors=self.model_colors)
            elif number == 7:
                charts.plot_request_sizes_over_time(events, *arguments, source_colors=self.point_colors)
            elif number == 9:
                parts = [cost.calculate_event_costs(events.loc[events["model"] == model], price)
                         for model, price in cost.OFFICIAL_PRICES.items() if (events["model"] == model).any()]
                priced = pd.concat(parts) if parts else events.iloc[:0].assign(total_cost_usd=pd.Series(dtype=float))
                costs = cost.build_daily_costs(priced, {})
                cost.plot_cumulative_cost(costs, output / "09_cumulative_api_cost.png", self.options.dpi, self.model_colors)
            elif number == 10:
                running = aggregate_daily_running_time(intervals, self.target_tz, self.history_start, day, cutoff)
                charts.plot_daily_running_time(running, output, self.target_tz, "png", self.options.dpi)
            elif number == 11:
                charts.plot_daily_cache_miss_rate(daily, *arguments)
            elif number in (12, 13):
                hourly = charts.aggregate_recent_hourly(events, self.target_tz, day)
                if number == 12:
                    charts.plot_recent_hourly_usage(hourly, events, *arguments, model_colors=self.model_colors)
                else:
                    costs = cost.aggregate_recent_hourly_cost(events, pd.DatetimeIndex(hourly["hour_start"]))
                    cost.plot_recent_hourly_cost(costs, events, *arguments, model_colors=self.model_colors)
        if image is None:
            raise RuntimeError(f"图片 {number:02d} 未生成动画帧。")
        return image


def export_animations(
    events: pd.DataFrame, intervals: list[RunningInterval], output_dir: Path,
    target_tz: tzinfo, history_start: date, options: AnimationOptions,
    *, top_threads: int = 15, observed_at: datetime | None = None,
) -> list[dict[str, Any]]:
    if not options.charts:
        return []
    observed_at = observed_at or datetime.now(timezone.utc)
    dates = animation_dates(options, target_tz, observed_at)
    executable = ffmpeg_executable()
    charts.configure_matplotlib()
    renderer = FrameRenderer(events, intervals, target_tz, history_start, dates, options, top_threads, observed_at)
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for number in dict.fromkeys(options.charts):
        path = output_dir / f"{CHART_NAMES[number]}.mp4"
        width = int(14 * options.dpi) // 2 * 2
        height = int(max(8, 0.58 * top_threads + 2.8) * options.dpi if number == 6 else 8 * options.dpi) // 2 * 2
        print(f"Animation {number:02d}: {dates[0]:%Y-%m-%d} → {dates[-1]:%Y-%m-%d} "
              f"({len(dates)} snapshots + {END_HOLD_SECONDS}s final hold)", flush=True)
        frames = (renderer.render(number, stamp) for stamp in dates)
        write_mp4(frames, path, (width, height), options.fps, executable)
        results.append({
            "chart": number, "file": path.name, "format": "mp4", "codec": "h264",
            "start_date": dates[0].date().isoformat(), "end_date": dates[-1].date().isoformat(),
            "timezone": str(target_tz), "frames": len(dates), "fps": options.fps,
            "encoded_frames": len(dates) + options.fps * END_HOLD_SECONDS,
            "end_hold_seconds": END_HOLD_SECONDS,
            "duration_seconds": len(dates) / options.fps + END_HOLD_SECONDS,
            "model_colors": {model: to_hex(color) for model, color in renderer.model_colors.items()},
        })
        if options.gif:
            gif_path = path.with_suffix(".gif")
            convert_mp4_to_gif(path, gif_path, executable)
            results.append({**results[-1], "file": gif_path.name, "format": "gif", "codec": "gif"})
    return results


def read_report(directory: Path) -> tuple[pd.DataFrame, list[RunningInterval], dict[str, Any], tzinfo, date]:
    """Read exported metadata only; preserve string IDs and variable UTC offsets."""
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    try:
        target_tz = charts.resolve_timezone(summary["timezone"])
    except ValueError:
        target_tz = datetime.fromisoformat(summary["coverage_start"]).tzinfo
        if target_tz is None:
            raise ValueError("汇总文件缺少时区，请重新生成用量数据。") from None
    text_columns = ["session_id", "thread_id", "parent_thread_id", "turn_id", "title", "model", "source", "originator", "cwd", "log_file", "project_name"]
    events = pd.read_csv(directory / "events.csv", keep_default_na=False, dtype={key: str for key in text_columns})
    required = {"timestamp", "model", "source", "thread_id", "session_id", "title", "cwd",
                *charts.RAW_TOKEN_COLUMNS, *charts.COMPOSITION_COLUMNS}
    if required.difference(events.columns):
        raise ValueError("events.csv 缺少动画所需字段，请重新生成用量数据。")
    events["timestamp"] = pd.to_datetime(events["timestamp"], format="mixed", utc=True).dt.tz_convert(target_tz)
    for column in [*charts.RAW_TOKEN_COLUMNS, *charts.COMPOSITION_COLUMNS]:
        events[column] = pd.to_numeric(events[column], errors="raise").astype("int64")
    if "thread_started_at" in events:
        events["thread_started_at"] = pd.to_datetime(events["thread_started_at"], format="mixed", utc=True).dt.tz_convert(target_tz)
    intervals = []
    interval_path = directory / "running_intervals.csv"
    if interval_path.is_file():
        raw = pd.read_csv(interval_path, keep_default_na=False, dtype=str)
        for item in raw.to_dict("records"):
            intervals.append(RunningInterval(
                start=datetime.fromisoformat(item["start"]), end=datetime.fromisoformat(item["end"]),
                **{key: item[key] for key in ("session_id", "turn_id", "status", "timing_source", "log_file")},
            ))
    history_start = date.fromisoformat(summary["running_time"]["coverage_start_date"])
    return events, intervals, summary, target_tz, history_start


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m codex_usage animate", description="从已有数据文件按编号生成 MP4 动画，可同时导出 GIF，不重新扫描日志或更新静态图片。")
    parser.add_argument("--chart", type=int, nargs="+", choices=range(1, 14), required=True, metavar="NUMBER", help="图片编号，例如 --chart 6 或 --chart 2 8。")
    parser.add_argument("--output", type=Path, default=Path("codex_usage_report"), help="已有 summary.json、events.csv 所在的结果目录，也是动画输出目录。")
    parser.add_argument("--start", help="第一帧本地日期，YYYY-MM-DD；默认结束日期的 30 天前。")
    parser.add_argument("--end", help="最后一帧本地日期，YYYY-MM-DD；默认今天。")
    parser.add_argument("--fps", type=int, default=4, help="每秒帧数，默认 4。")
    parser.add_argument("--dpi", type=int, default=100, help="动画 DPI，默认 100。")
    parser.add_argument("--gif", action="store_true", help="同时生成循环播放的 GIF 动图，默认只生成 MP4。")
    parser.add_argument("--top-threads", type=int, default=15, help="06 图任务数量，默认 15。")
    args = parser.parse_args(argv)
    try:
        options = AnimationOptions(
            charts=tuple(dict.fromkeys(args.chart)), start=charts.parse_date_argument(args.start, "--start"),
            end=charts.parse_date_argument(args.end, "--end"), fps=args.fps, dpi=args.dpi,
            gif=args.gif,
        )
        if args.top_threads <= 0:
            raise ValueError("--top-threads 必须大于 0。")
        directory = args.output.expanduser().resolve()
        events, intervals, summary, target_tz, history_start = read_report(directory)
        if 10 in options.charts and not (directory / "running_intervals.csv").is_file():
            raise ValueError("缺少 running_intervals.csv，请重新生成用量数据后再生成 10 图动画。")
        results = export_animations(events, intervals, directory, target_tz, history_start, options, top_threads=args.top_threads)
        updated = {item["file"]: item for item in summary.get("animations", [])}
        updated.update({item["file"]: item for item in results})
        summary["animations"] = list(updated.values())
        staged: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".animation-summary-", suffix=".json", dir=directory, delete=False) as handle:
                staged = Path(handle.name)
                json.dump(summary, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
            staged.replace(directory / "summary.json")
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
        print(f"已生成 {len(results)} 个动画文件：{directory}")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        charts.eprint(f"错误：{exc}；请确认 --output 指向已有结果目录，必要时先运行 report。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
