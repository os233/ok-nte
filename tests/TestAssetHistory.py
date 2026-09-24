"""资产历史记录器与趋势报告的测试。

覆盖 src/utils/asset_history.py 的追加写入、增减计算、损坏行容错, 以及
tools/asset_report.py 的按天聚合与报告生成。

不需要游戏窗口、截图或音频; 所有落盘都写在 TemporaryDirectory 内。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.utils.asset_history import (
    SOURCE_MAIN_SCREEN,
    AssetHistoryRecorder,
    AssetRecord,
    read_records,
)


class _TempHistoryMixin:
    """为用例提供一个临时历史文件路径, 结束即清理。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.history_path = Path(self._tmp.name) / "nested" / "asset_history.jsonl"

    def tearDown(self):
        self._tmp.cleanup()


class TestAssetHistoryRecorder(_TempHistoryMixin, unittest.TestCase):
    def test_first_record_has_no_delta_then_computes_change(self):
        """首条没有可比前值, 之后每条都要给出相对上一条的增减。

        增减是报告里「这一轮赚了多少」的唯一来源, 算错会直接误导判断。
        """
        recorder = AssetHistoryRecorder(self.history_path)

        first = recorder.record(1_000)
        second = recorder.record(1_500)
        third = recorder.record(1_200)

        self.assertIsNotNone(first)
        self.assertIsNone(first.delta)
        self.assertEqual(second.delta, 500)
        self.assertEqual(third.delta, -300)

    def test_recorder_creates_missing_parent_directories(self):
        """默认路径是 data/asset_history.jsonl, 目录可能不存在, 必须自动创建。"""
        self.assertFalse(self.history_path.parent.exists())

        recorder = AssetHistoryRecorder(self.history_path)
        self.assertIsNotNone(recorder.record(42))

        self.assertTrue(self.history_path.exists())

    def test_records_append_instead_of_overwriting(self):
        """每次记录都是追加, 早期观测不能被后来的覆盖。

        这是选 JSONL 而不是整体 JSON 的全部理由。
        """
        recorder = AssetHistoryRecorder(self.history_path)
        for value in (10, 20, 30):
            recorder.record(value)

        records = read_records(self.history_path)
        self.assertEqual([r.value for r in records], [10, 20, 30])

    def test_delta_survives_recorder_recreation(self):
        """进程重启后新建记录器, 第一条的 delta 要基于文件里已有的末值。

        任务是逐轮跑的、随时可能重启, 不读回历史会让每轮重启后的第一条都丢增减。
        """
        AssetHistoryRecorder(self.history_path).record(1_000)

        fresh = AssetHistoryRecorder(self.history_path)
        record = fresh.record(1_400)

        self.assertEqual(record.delta, 400)

    def test_corrupt_line_is_skipped_without_losing_others(self):
        """单行损坏(半截 JSON / 手工改坏)不能让整个历史不可读。"""
        recorder = AssetHistoryRecorder(self.history_path)
        recorder.record(100)
        with open(self.history_path, "ab") as f:
            f.write(b'{"timestamp": 1, "value": bro\n')
        recorder.record(200)

        records = read_records(self.history_path)
        self.assertEqual([r.value for r in records], [100, 200])

    def test_record_missing_required_fields_are_dropped(self):
        """缺少 value 或 timestamp 的行没有分析价值, 应被丢弃而不是猜一个默认值。"""
        lines = [
            {"timestamp": 1_700_000_000, "value": 500},
            {"timestamp": 1_700_000_001},  # 缺 value
            {"value": 700},  # 缺 timestamp
            {"timestamp": "not-a-number", "value": 800},
            "not-a-dict",
        ]
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

        records = read_records(self.history_path)
        self.assertEqual([r.value for r in records], [500])

    def test_write_failure_returns_none_instead_of_raising(self):
        """落盘失败时返回 None, 不抛异常。

        记录资产是附加观测; 磁盘满或目录只读时把异常抛上去, 会让已经成功结算的
        拍卖轮次被记成失败, 损失远大于少一条统计。
        """
        recorder = AssetHistoryRecorder(self.history_path)
        recorder.path = Path(self._tmp.name)  # 路径是目录, open() 必然失败

        self.assertIsNone(recorder.record(100))

    def test_useful_state_kept_when_one_write_fails(self):
        """一次写入失败后, 基准值不应被污染, 下次成功时增减仍以最后一次成功为准。"""
        recorder = AssetHistoryRecorder(self.history_path)
        recorder.record(1_000)

        recorder.path = Path(self._tmp.name)
        self.assertIsNone(recorder.record(9_999))

        recorder.path = self.history_path
        record = recorder.record(1_100)

        self.assertEqual(record.delta, 100)


class TestAssetHistoryDefaults(unittest.TestCase):
    def test_default_source_is_main_screen(self):
        """默认来源必须是主界面: 出价面板读到的资产语义不同, 混在一起会误导分析。"""
        self.assertEqual(SOURCE_MAIN_SCREEN, "main_screen")


class TestAssetReport(_TempHistoryMixin, unittest.TestCase):
    def _write(self, entries):
        """entries 为 (时间戳, 数值) 序列。"""
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "w", encoding="utf-8") as f:
            for timestamp, value in entries:
                f.write(
                    json.dumps({"timestamp": timestamp, "value": value}) + "\n"
                )

    def test_daily_summary_uses_first_and_last_of_each_day(self):
        """按天汇总取当天首末之差, 中间的波动不能影响结果。"""
        from tools.asset_report import summarize_by_day

        day = 86_400
        base = 1_700_000_000 - (1_700_000_000 % day)
        self._write(
            [
                (base + 60, 1_000),
                (base + 3_600, 5_000),  # 中间冲高
                (base + 7_200, 1_200),
                (base + day + 60, 2_000),
                (base + day + 3_600, 2_500),
            ]
        )

        summaries = summarize_by_day(read_records(self.history_path))

        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[0].change, 200)
        self.assertEqual(summaries[1].change, 500)
        self.assertEqual(summaries[0].samples, 3)

    def test_daily_summary_follows_timestamps_not_file_order(self):
        """净变化必须按时间戳取首末, 不能按物理行序取。

        文件是追加写的, 但用户手工整理数据、日志回放或多任务交错都会让行序变乱;
        按物理首末行算会给出**符号相反**的净变化且不报任何错
        (实测同日先写 t=200/100、再写 t=100/500 时算出 +400, 按时间序应为 -400)。
        """
        from tools.asset_report import summarize_by_day

        day = 86_400
        base = 1_700_000_000 - (1_700_000_000 % day)
        self._write(
            [
                (base + 3_600, 100),  # 物理第一行, 但时间上更晚
                (base + 60, 500),  # 物理第二行, 时间上更早
            ]
        )

        summaries = summarize_by_day(read_records(self.history_path))

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].first, 500)
        self.assertEqual(summaries[0].last, 100)
        self.assertEqual(summaries[0].change, -400)

    def test_markdown_reports_negative_change_with_sign(self):
        """资产减少时报告要显式带负号, 不能只靠数字大小去猜。"""
        from tools.asset_report import build_markdown

        base = 1_700_000_000
        self._write([(base, 5_000), (base + 60, 4_000)])

        markdown = build_markdown(read_records(self.history_path))

        self.assertIn("-1,000", markdown)
        self.assertIn("5,000", markdown)

    def test_markdown_on_empty_history_does_not_crash(self):
        """任务还没跑过时报告脚本仍要能正常输出, 而不是报错退出。"""
        from tools.asset_report import build_markdown

        markdown = build_markdown([])

        self.assertIn("暂无记录", markdown)

    def test_days_filter_excludes_old_records(self):
        """--days 过滤要真的生效, 否则长期积累后报告会被历史数据淹没。"""
        from tools.asset_report import build_markdown, filter_recent

        now = time.time()
        self._write([(now - 10 * 86_400, 1_000), (now - 60, 2_000)])

        records = filter_recent(read_records(self.history_path), 7)
        markdown = build_markdown(records)

        self.assertIn("2,000", markdown)
        self.assertNotIn("1,000\n", markdown)
        self.assertIn("1 次观测", markdown)

    def test_days_filter_is_a_noop_for_none_and_non_positive(self):
        """不传 --days 或传 0/负数时不过滤, 避免意外丢掉全部历史。"""
        from tools.asset_report import filter_recent

        now = time.time()
        self._write([(now - 10 * 86_400, 1_000), (now - 60, 2_000)])
        records = read_records(self.history_path)

        self.assertEqual(len(filter_recent(records, None)), 2)
        self.assertEqual(len(filter_recent(records, 0)), 2)
        self.assertEqual(len(filter_recent(records, -1)), 2)

    def test_days_filter_applies_to_console_output_too(self):
        """--days 必须同时作用于控制台摘要, 不能只在 --out 时才生效。

        回归: 过滤原先写在 build_markdown 内部, 而 print_console_summary 收的是未过滤的
        全量记录。用户执行 `--days 7` 但不加 `--out` 时, 界面上的数字与 help 描述不符,
        会让人以为几天前的旧数据还在。
        """
        import contextlib
        import io

        from tools.asset_report import main

        now = time.time()
        self._write([(now - 10 * 86_400, 1_234_567), (now - 60, 7_654_321)])

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main(["--path", str(self.history_path), "--days", "7"])

        output = buffer.getvalue()
        self.assertIn("7,654,321", output)
        self.assertNotIn("1,234,567", output)
        self.assertIn("1 次观测", output)


class TestTaskRecordsAssetEachRound(unittest.TestCase):
    """任务侧接线: 读到资产值时必须调用记录器, 且记录失败不能影响流程。"""

    def _make_task(self):
        from src.tasks.AutoBidAuctionTask import AutoBidAuctionTask
        from src.tasks.mixin.RoundMixin import RoundState

        task = AutoBidAuctionTask.__new__(AutoBidAuctionTask)
        task.log_info = Mock()
        task.log_debug = Mock()
        task.log_warning = Mock()
        task.info_set = Mock()
        # current_round 是只读 property, 读取轮次状态, 直接给状态对象赋值。
        task._round_state = RoundState(total=0, index=3)
        task._asset_history = Mock()
        # 用真实的记录对象而不是裸 Mock: _record_asset_value 会读 value/delta 拼日志。
        task._asset_history.record = Mock(
            return_value=AssetRecord(
                timestamp=0.0,
                time_text="1970-01-01 00:00:00",
                value=12_345,
                round_index=3,
                delta=250,
            )
        )
        return task

    def test_asset_value_is_recorded(self):
        """每轮读到资产就落盘一次, 这是整条记录链路的起点。"""
        task = self._make_task()

        task._record_asset_value(12_345)

        task._asset_history.record.assert_called_once_with(12_345, round_index=3)

    def test_observe_records_asset_regardless_of_welfare_threshold(self):
        """资产观测读到值就必须落盘, 与低保金阈值无关。

        资产高于 10 万时(绝大多数正常轮次)低保金分支会直接返回, 若把记录调用放在
        阈值判断之后, 这些轮次一条数据都写不出来, 而任务本身运行完全正常,
        不会有任何报错提示数据丢了。
        """
        task = self._make_task()
        task._read_asset_value = Mock(return_value=500_000)
        task.WELFARE_ASSET_THRESHOLD = 100_000

        value = task._observe_main_asset(Mock(), time.monotonic() + 10)

        self.assertEqual(value, 500_000)
        task._asset_history.record.assert_called_once_with(500_000, round_index=3)

    def test_record_failure_does_not_raise(self):
        """记录器抛异常时必须被吞掉 —— 不能把已成功结算的轮次判成失败。"""
        task = self._make_task()
        task._asset_history.record = Mock(side_effect=OSError("disk full"))

        task._record_asset_value(12_345)  # 不应抛出

        task.log_warning.assert_called_once()

    def test_returning_none_is_reported_as_failure(self):
        """写入被拒(返回 None)也要留痕, 否则数据静默丢失无人察觉。"""
        task = self._make_task()
        task._asset_history.record = Mock(return_value=None)

        task._record_asset_value(12_345)

        task.log_warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
