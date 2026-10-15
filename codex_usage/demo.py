#!/usr/bin/env python3
"""Generate a reproducible report from synthetic metadata, never local logs."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import random
import tempfile
from zoneinfo import ZoneInfo

from .charts import collect_usage_data, export_report
from .cost import main as estimate_cost


START = date(2026, 9, 14)
END = START + timedelta(days=27)


def write_demo_logs(home: Path) -> None:
    """Write invented IDs, titles, timings and counters without content fields."""
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    rng = random.Random(42)
    definitions = [
        ("demo-desktop", "Demo / Desktop", "Codex Desktop", "", "gpt-6.1-sol"),
        ("demo-cli", "Demo / CLI", "codex_cli_rs", "", "gpt-5.5"),
        ("demo-child", "Demo / Subagent", "Codex Desktop", "demo-desktop", "gpt-6-luna"),
        ("demo-review", "Demo / Review", "Codex Desktop", "demo-desktop", "codex-auto-review"),
    ]
    index = [{"id": item[0], "thread_name": item[1]} for item in definitions]
    (home / "session_index.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in index), encoding="utf-8")
    for number, (session_id, _, originator, parent, model) in enumerate(definitions):
        records = []

        def add(kind: str, stamp: datetime, **payload) -> None:
            records.append({"type": kind, "timestamp": stamp.isoformat(), "payload": payload})

        first = datetime(START.year, START.month, START.day, tzinfo=timezone.utc)
        add("session_meta", first, id=session_id, originator=originator,
            parent_thread_id=parent, source="cli" if number == 1 else "desktop",
            thread_source="subagent" if parent else "")
        for day in range(28):
            if day % 7 == 6:  # Preserve idle calendar days in the example.
                continue
            for block in range(3):
                stamp = first + timedelta(days=day, hours=1 + block * 4, minutes=number * 9)
                turn = f"{session_id}-{day}-{block}"
                add("turn_context", stamp, turn_id=turn, model=model)
                add("event_msg", stamp, type="task_started", turn_id=turn)
                for request in range(4):
                    inputs = rng.randint(8_000, 95_000) * (1 + day // 10)
                    outputs = rng.randint(600, 8_000)
                    cached = int(inputs * (0.45 + 0.5 * rng.random()))
                    usage = {"input_tokens": inputs, "cached_input_tokens": cached,
                             "output_tokens": outputs, "reasoning_output_tokens": outputs // 3,
                             "total_tokens": inputs + outputs}
                    add("event_msg", stamp + timedelta(minutes=5 + request * 12),
                        type="token_count", info={"last_token_usage": usage})
                add("event_msg", stamp + timedelta(minutes=50 + number * 5),
                    type="task_complete", turn_id=turn, duration_ms=(50 + number * 5) * 60_000)
        (sessions / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m codex_usage demo", description="用完全合成的日志体验图片生成，不读取本机 Codex 数据。")
    parser.add_argument("--output", type=Path, default=Path("codex_usage_demo"), help="演示结果目录，默认 codex_usage_demo。")
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    if output.exists() and (not output.is_dir() or (any(output.iterdir()) and not (output / "DEMO.txt").is_file())):
        parser.error("输出位置已有非演示文件，请指定一个空目录，以免覆盖真实图片与数据文件。")
    output.mkdir(parents=True, exist_ok=True)
    (output / "DEMO.txt").write_text(
        "此目录由 python -m codex_usage demo 生成，所有会话、时间和用量均为合成数据。\n"
        "费用也仅用于演示，不代表任何人的实际用量或账单。\n", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="codex-usage-demo-") as directory:
        home = Path(directory)
        write_demo_logs(home)
        target_tz = ZoneInfo("Asia/Shanghai")
        events, intervals, stats = collect_usage_data(home, target_tz, progress=False)
        export_report(events, stats, output, target_tz, "png", 120, 10,
                      running_intervals=intervals, start=START, end=END)
    estimate_cost(["--events", str(output / "events.csv"), "--output", str(output), "--dpi", "120"])
    print(f"\n演示图片与数据文件已生成：{output}\n所有数据均为合成数据，未读取本机日志或统计缓存。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
