"""资产变化趋势报告: 读取 data/asset_history.jsonl, 输出控制台摘要和 Markdown 报告。

用法(在仓库根目录执行):

    .\\.venv\\Scripts\\python.exe tools\\asset_report.py
    .\\.venv\\Scripts\\python.exe tools\\asset_report.py --days 7
    .\\.venv\\Scripts\\python.exe tools\\asset_report.py --out docs/asset_trend.md

`--days` 同时作用于控制台摘要和 Markdown 报告(见 filter_recent)。

数据来源是 ``AutoBidAuctionTask`` 每轮回到主界面时 OCR 到的资产值, 由
``src/utils/asset_history.py`` 追加落盘。脚本只读该文件, 不修改任何数据。
"""

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 允许直接以脚本方式运行时导入 src 包。
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.utils.asset_history import (  # noqa: E402
    DEFAULT_HISTORY_PATH,
    AssetRecord,
    read_records,
)


@dataclass
class DaySummary:
    """按天聚合的结果。"""

    date: str
    first: int
    last: int
    samples: int

    @property
    def change(self) -> int:
        return self.last - self.first


def summarize_by_day(records: list[AssetRecord]) -> list[DaySummary]:
    """按本地日期聚合记录, 返回按日期升序的汇总。

    同一天的第一条与最后一条之差就是当天净变化; 中间可能跨过多次任务重启,
    所以还带上样本数, 样本很少时这个差值参考价值有限。
    """
    buckets: dict[str, list[AssetRecord]] = defaultdict(list)
    for record in records:
        day = datetime.fromtimestamp(record.timestamp).strftime("%Y-%m-%d")
        buckets[day].append(record)

    summaries = []
    for day in sorted(buckets):
        day_records = buckets[day]
        summaries.append(
            DaySummary(
                date=day,
                first=day_records[0].value,
                last=day_records[-1].value,
                samples=len(day_records),
            )
        )
    return summaries


def format_number(value: int) -> str:
    return f"{value:,}"


def format_change(value: int) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:,}"


def filter_recent(records: list[AssetRecord], days: int | None) -> list[AssetRecord]:
    """只保留最近 days 天的记录。days 为 None 或非正数时原样返回。

    过滤放在报告生成之外: 控制台摘要和 Markdown 必须看到同一份记录, 否则
    `--days 7` 不加 `--out` 时用户会以为过滤生效了, 实际控制台打印的是全部历史。
    """
    if days is None or days <= 0:
        return records
    cutoff = datetime.now().timestamp() - days * 86400
    return [r for r in records if r.timestamp >= cutoff]


def build_markdown(records: list[AssetRecord]) -> str:
    """生成 Markdown 报告。records 应已由调用方完成时间过滤。"""
    if not records:
        return "# 资产变化报告\n\n暂无记录。\n"

    summaries = summarize_by_day(records)
    first, last = records[0], records[-1]
    total_change = last.value - first.value

    lines = [
        "# 资产变化报告",
        "",
        f"- 记录区间: {first.time_text} ~ {last.time_text}",
        f"- 样本数: {len(records)} 次观测, 覆盖 {len(summaries)} 天",
        f"- 区间首值: {format_number(first.value)}",
        f"- 区间末值: {format_number(last.value)}",
        f"- 区间净变化: {format_change(total_change)}",
    ]
    if first.value > 0:
        lines.append(f"- 变化幅度: {total_change / first.value * 100:+.2f}%")

    lines += [
        "",
        "## 按天汇总",
        "",
        "| 日期 | 当日首值 | 当日末值 | 当日变化 | 观测次数 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for summary in summaries:
        lines.append(
            f"| {summary.date} | {format_number(summary.first)} | "
            f"{format_number(summary.last)} | {format_change(summary.change)} | "
            f"{summary.samples} |"
        )

    lines += [
        "",
        "## 逐轮明细",
        "",
        "| 时间 | 轮次 | 资产值 | 相对上次 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for record in records:
        delta = "—" if record.delta is None else format_change(record.delta)
        lines.append(
            f"| {record.time_text} | {record.round_index} | "
            f"{format_number(record.value)} | {delta} |"
        )

    lines.append("")
    return "\n".join(lines)


def print_console_summary(records: list[AssetRecord]) -> None:
    if not records:
        print("暂无资产记录。请先运行一次「自动拍卖」任务。")
        return

    summaries = summarize_by_day(records)
    first, last = records[0], records[-1]
    print("资产变化概览")
    print("=" * 44)
    print(f"记录区间 : {first.time_text} ~ {last.time_text}")
    print(f"样本数   : {len(records)} 次观测, 覆盖 {len(summaries)} 天")
    print(f"区间首值 : {format_number(first.value)}")
    print(f"区间末值 : {format_number(last.value)}")
    print(f"净变化   : {format_change(last.value - first.value)}")
    print()
    print("按天:")
    for summary in summaries:
        print(
            f"  {summary.date}  {format_number(summary.first):>12} -> "
            f"{format_number(summary.last):>12}  "
            f"{format_change(summary.change):>12}  (n={summary.samples})"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成异环拍卖资产变化报告")
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_HISTORY_PATH,
        help=f"历史文件路径, 默认 {DEFAULT_HISTORY_PATH}",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="只统计最近 N 天, 默认全部",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="同时把 Markdown 报告写到该路径",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    records = filter_recent(read_records(args.path), args.days)
    print_console_summary(records)

    if args.out is not None:
        markdown = build_markdown(records)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown, encoding="utf-8")
        print()
        print(f"Markdown 报告已写入: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
