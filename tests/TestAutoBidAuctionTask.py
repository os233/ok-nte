import time
import unittest
from types import SimpleNamespace

from ok import Box, WaitFailedException

from src.tasks.AutoBidAuctionTask import AutoBidAuctionTask
from src.tasks.mixin.RoundMixin import RoundState


class TestAutoBidAuctionTask(unittest.TestCase):
    """覆盖自动拍卖任务的纯逻辑部分, 不依赖游戏窗口。"""

    def _make_task(self, **config):
        task = object.__new__(AutoBidAuctionTask)
        task.config = {
            AutoBidAuctionTask.CONF_FIXED_PRICE: 1,
            AutoBidAuctionTask.CONF_AUTO_RAISE: False,
            AutoBidAuctionTask.CONF_RAISE_MODE: "倍数",
            AutoBidAuctionTask.CONF_RAISE_VALUE: "1.6",
            AutoBidAuctionTask.CONF_RAISE_ROUND: 2,
            AutoBidAuctionTask.CONF_SPECIAL_ROUND: False,
            AutoBidAuctionTask.CONF_SPECIAL_ROUNDS: ["5"],
            AutoBidAuctionTask.CONF_SPECIAL_ROUND_PRICE: "66666",
        }
        task.config.update(config)
        task.current_bid_count = 0
        task.last_bid_price = None
        task.logs = []
        task.log_info = lambda message, **kwargs: task.logs.append(("info", message))
        task.log_warning = lambda message, **kwargs: task.logs.append(("warning", message))
        task.log_debug = lambda message, **kwargs: task.logs.append(("debug", message))
        return task

    # --- 资产解析 ---
    def test_parse_asset_value_normalizes_fullwidth_and_ocr_errors(self):
        parse = AutoBidAuctionTask._parse_asset_value

        self.assertEqual(parse("1,234"), 1234)
        self.assertEqual(parse("１，２３４"), 1234)
        self.assertEqual(parse("0"), 0)
        self.assertEqual(parse("l23"), 123)
        self.assertEqual(parse("I23"), 123)
        self.assertEqual(parse("O23"), 23)
        self.assertEqual(parse("１２O0"), 1200)

    def test_parse_asset_value_returns_none_without_digits(self):
        parse = AutoBidAuctionTask._parse_asset_value

        self.assertIsNone(parse(""))
        self.assertIsNone(parse("我的资产"))
        self.assertIsNone(parse("，,"))

    # --- 价格按键序列 ---
    def test_price_key_sequence_uses_multi_zero_shortcuts(self):
        keys = AutoBidAuctionTask._price_key_sequence

        self.assertEqual(keys("1"), ["1"])
        self.assertEqual(keys("123456"), ["1", "2", "3", "4", "5", "6"])
        self.assertEqual(keys("200"), ["2", "00"])
        self.assertEqual(keys("50000"), ["5", "0000"])
        self.assertEqual(keys("100000"), ["1", "0", "0000"])
        self.assertEqual(keys("1200"), ["1", "2", "00"])

    def test_price_key_sequence_handles_pure_zero_and_empty_input(self):
        keys = AutoBidAuctionTask._price_key_sequence

        self.assertEqual(keys(""), [])
        self.assertEqual(keys("0"), ["0"])
        self.assertEqual(keys("00"), ["00"])
        self.assertEqual(keys("0000"), ["0000"])
        # 3 / 5 个 0 都要拆成数字键盘上真实存在的按键组合.
        self.assertEqual(keys("000"), ["0", "00"])
        self.assertEqual(keys("00000"), ["0", "0000"])

    def test_price_key_sequence_keeps_non_zero_digits_intact(self):
        keys = AutoBidAuctionTask._price_key_sequence

        # 只有末尾的整串 0 才可能走快捷键, 中间不受影响.
        self.assertEqual(keys("1010"), ["1", "0", "1", "0"])
        self.assertEqual(keys("90000"), ["9", "0000"])

    # --- 价格计算 ---
    def test_fixed_price_is_used_when_auto_raise_disabled(self):
        task = self._make_task(**{AutoBidAuctionTask.CONF_FIXED_PRICE: 5000})
        task.current_bid_count = 3

        self.assertEqual(task._calculate_auction_price(), 5000)

    def test_invalid_base_price_falls_back_to_one(self):
        task = self._make_task(**{AutoBidAuctionTask.CONF_FIXED_PRICE: "abc"})

        self.assertEqual(task._calculate_auction_price(), 1)

    def test_multiple_mode_grows_exponentially_from_configured_round(self):
        task = self._make_task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 100,
                AutoBidAuctionTask.CONF_AUTO_RAISE: True,
                AutoBidAuctionTask.CONF_RAISE_MODE: "倍数",
                AutoBidAuctionTask.CONF_RAISE_VALUE: "2",
                AutoBidAuctionTask.CONF_RAISE_ROUND: 1,
            }
        )

        task.current_bid_count = 0
        self.assertEqual(task._calculate_auction_price(), 200)
        task.current_bid_count = 2
        self.assertEqual(task._calculate_auction_price(), 800)

    def test_percent_and_custom_modes_grow_linearly(self):
        percent_task = self._make_task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 100,
                AutoBidAuctionTask.CONF_AUTO_RAISE: True,
                AutoBidAuctionTask.CONF_RAISE_MODE: "百分比",
                AutoBidAuctionTask.CONF_RAISE_VALUE: "10",
                AutoBidAuctionTask.CONF_RAISE_ROUND: 0,
            }
        )
        custom_task = self._make_task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 100,
                AutoBidAuctionTask.CONF_AUTO_RAISE: True,
                AutoBidAuctionTask.CONF_RAISE_MODE: "自定义",
                AutoBidAuctionTask.CONF_RAISE_VALUE: "50",
                AutoBidAuctionTask.CONF_RAISE_ROUND: 0,
            }
        )

        percent_task.current_bid_count = 1
        self.assertEqual(percent_task._calculate_auction_price(), 120)
        custom_task.current_bid_count = 2
        self.assertEqual(custom_task._calculate_auction_price(), 250)

    def test_raise_round_delays_first_increase(self):
        task = self._make_task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 100,
                AutoBidAuctionTask.CONF_AUTO_RAISE: True,
                AutoBidAuctionTask.CONF_RAISE_MODE: "自定义",
                AutoBidAuctionTask.CONF_RAISE_VALUE: "10",
                AutoBidAuctionTask.CONF_RAISE_ROUND: 3,
            }
        )

        task.current_bid_count = 0
        self.assertEqual(task._calculate_auction_price(), 100)
        task.current_bid_count = 1
        self.assertEqual(task._calculate_auction_price(), 100)
        task.current_bid_count = 2
        self.assertEqual(task._calculate_auction_price(), 110)

    def test_special_round_price_overrides_other_modes(self):
        task = self._make_task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 100,
                AutoBidAuctionTask.CONF_AUTO_RAISE: True,
                AutoBidAuctionTask.CONF_SPECIAL_ROUND: True,
                AutoBidAuctionTask.CONF_SPECIAL_ROUNDS: ["5"],
                AutoBidAuctionTask.CONF_SPECIAL_ROUND_PRICE: "66666",
            }
        )

        task.current_bid_count = 4
        self.assertEqual(task._calculate_auction_price(), 66666)
        # 非指定回合: 加价回合数默认 2, 第 1 次出价还没到加价回合, 应为基础价.
        task.current_bid_count = 0
        self.assertEqual(task._calculate_auction_price(), 100)

    def test_special_round_config_is_ignored_when_invalid(self):
        task = self._make_task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 100,
                AutoBidAuctionTask.CONF_SPECIAL_ROUND: True,
                AutoBidAuctionTask.CONF_SPECIAL_ROUNDS: ["5", "abc"],
                AutoBidAuctionTask.CONF_SPECIAL_ROUND_PRICE: "66666",
            }
        )
        task.current_bid_count = 4

        self.assertEqual(task._calculate_auction_price(), 100)

    def test_special_round_price_must_be_positive(self):
        """指定回合价格 <= 0 时应视为未配置, 回落到基础价。"""
        task = self._make_task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 100,
                AutoBidAuctionTask.CONF_SPECIAL_ROUND: True,
                AutoBidAuctionTask.CONF_SPECIAL_ROUNDS: ["5"],
                AutoBidAuctionTask.CONF_SPECIAL_ROUND_PRICE: "0",
            }
        )
        task.current_bid_count = 4

        self.assertEqual(task._calculate_auction_price(), 100)

    def test_invalid_raise_value_falls_back_to_base_price(self):
        """倍数模式下加价数值非法会算出 0, 应回退到基础价并告警, 而不是出价 0。"""
        task = self._make_task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 100,
                AutoBidAuctionTask.CONF_AUTO_RAISE: True,
                AutoBidAuctionTask.CONF_RAISE_MODE: "倍数",
                AutoBidAuctionTask.CONF_RAISE_VALUE: "abc",
                AutoBidAuctionTask.CONF_RAISE_ROUND: 0,
            }
        )
        task.current_bid_count = 0

        self.assertEqual(task._calculate_auction_price(), 100)
        self.assertTrue(
            any(level == "warning" for level, _ in task.logs),
            f"应记录一条告警, 实际日志: {task.logs}",
        )

    # --- 上轮出价复用 ---
    def test_can_reuse_last_bid_needs_matching_price_and_no_auto_raise(self):
        task = self._make_task()

        task.last_bid_price = None
        self.assertFalse(task._can_reuse_last_bid(5000))

        task.last_bid_price = 5000
        self.assertTrue(task._can_reuse_last_bid(5000))
        self.assertFalse(task._can_reuse_last_bid(6000))

        # 启用自动加价后不复用上轮出价, 避免快捷输入带来的不确定性.
        task.config[AutoBidAuctionTask.CONF_AUTO_RAISE] = True
        self.assertFalse(task._can_reuse_last_bid(5000))

    # --- 配置读取 ---
    def test_config_int_falls_back_and_warns(self):
        task = self._make_task(**{AutoBidAuctionTask.CONF_SELL_INTERVAL: "abc"})

        value = task._config_int(
            AutoBidAuctionTask.CONF_SELL_INTERVAL, 0, warn="出售间隔次数配置无效, 按 0 处理"
        )

        self.assertEqual(value, 0)
        self.assertIn(("warning", "出售间隔次数配置无效, 按 0 处理"), task.logs)

    def test_config_int_list_returns_empty_on_invalid_item(self):
        task = self._make_task(**{AutoBidAuctionTask.CONF_SPECIAL_ROUNDS: ["1", "x"]})

        self.assertEqual(task._config_int_list(AutoBidAuctionTask.CONF_SPECIAL_ROUNDS), [])
        self.assertEqual(task._config_int_list("不存在的配置"), [])

    def test_config_float_falls_back_and_parses_values(self):
        task = self._make_task(**{AutoBidAuctionTask.CONF_RAISE_VALUE: "abc"})
        key = AutoBidAuctionTask.CONF_RAISE_VALUE

        self.assertEqual(task._config_float(key, 0.0), 0.0)
        self.assertEqual(task._config_float("不存在的配置", 1.5), 1.5)

        task.config[key] = "1.6"
        self.assertAlmostEqual(task._config_float(key, 0.0), 1.6)
        task.config[key] = 2
        self.assertAlmostEqual(task._config_float(key, 0.0), 2.0)

    # --- 超时辅助 ---
    def test_remaining_timeout_clamps_to_deadline_and_raises_when_expired(self):
        task = self._make_task()

        self.assertEqual(task._remaining_timeout(time.monotonic() + 10, 5), 5)
        self.assertLessEqual(task._remaining_timeout(time.monotonic() + 10, 0.01), 0.01)
        with self.assertRaises(WaitFailedException):
            task._remaining_timeout(time.monotonic() - 1, 5)

    def test_bounded_timeout_keeps_limit_when_deadline_missing(self):
        task = self._make_task()

        self.assertEqual(task._bounded_timeout(None, 5), 5)
        self.assertEqual(task._bounded_timeout(time.monotonic() + 10, 5), 5)
        with self.assertRaises(WaitFailedException):
            task._bounded_timeout(time.monotonic() - 1, 5)

    # --- 轮次结果处理 ---
    def _make_round_task(self, round_result: bool):
        task = self._make_task()
        task._round_state = RoundState(total=0)
        task.calls = []

        def fake_exec(boxes):
            task.calls.append(("round", round_result))
            return round_result

        task._exec_auction_round = fake_exec
        task.add_success = lambda: task.calls.append(("success",))
        task.add_failed = lambda reason: task.calls.append(("failed", reason))
        task._sell_collections_on_interval = lambda boxes: task.calls.append(("sell",))
        return task

    def test_run_single_round_skips_periodic_sell_when_still_bidding(self):
        """进入下一轮出价(未回主界面)时不得触发定期出售, 否则仓库入口会白等超时。"""
        task = self._make_round_task(False)
        task._run_single_round(boxes=None)

        self.assertNotIn(("sell",), task.calls)
        self.assertIn(("failed", "结果阶段进入下一轮出价"), task.calls)
        self.assertNotIn(("success",), task.calls)

    def test_run_single_round_triggers_periodic_sell_after_finish(self):
        task = self._make_round_task(True)
        task._run_single_round(boxes=None)

        self.assertIn(("sell",), task.calls)
        self.assertIn(("success",), task.calls)
        self.assertNotIn(("failed", "结果阶段进入下一轮出价"), task.calls)

    # --- 价格校验与出价尝试 ---
    def test_verify_input_price_rejects_range_hint_even_when_price_equals_asset(self):
        """未输入时价格区显示 "可输入范围0~<资产>" 提示, 即使目标价恰等于资产也不能误判通过。"""
        task = self._make_task()
        task.wait_ocr = lambda **kwargs: [Box(0, 0, 1, 1, name="可输入范围0~16,155,238")]
        boxes = SimpleNamespace(price_result=1)

        with self.assertRaises(WaitFailedException):
            task._verify_input_price(boxes, 16155238, None)

    def test_verify_input_price_accepts_typed_price(self):
        task = self._make_task()
        task.wait_ocr = lambda **kwargs: [Box(0, 0, 1, 1, name="1,000,000")]
        boxes = SimpleNamespace(price_result=1)

        task._verify_input_price(boxes, 1000000, None)

    def test_verify_input_price_rejects_mismatched_price(self):
        task = self._make_task()
        task.wait_ocr = lambda **kwargs: [Box(0, 0, 1, 1, name="500")]
        boxes = SimpleNamespace(price_result=1)

        with self.assertRaises(WaitFailedException):
            task._verify_input_price(boxes, 1000000, None)

    def _make_bid_task(self, reads):
        """构造可离线驱动 _attempt_bid 的最小桩, reads 依次作为资产识别返回值。"""
        task = self._make_task()
        task.calls = []
        read_iter = iter(reads)

        def fake_read(box, timeout):
            value = next(read_iter)
            task.calls.append(("read", value))
            return value

        def fake_click(*args, **kwargs):
            task.calls.append(("click", args[0] if args else None))
            return True

        task._read_asset_value = fake_read
        task.operate_click = fake_click
        task.sleep = lambda t: task.calls.append(("sleep",))
        task._remaining_timeout = lambda deadline, limit: limit
        task.wait_click_ocr = lambda **kwargs: task.calls.append(("wait_click",)) or [object()]
        task.wait_ocr = lambda **kwargs: task.calls.append(("wait_ocr",)) or [object()]
        task.wait_until = lambda *args, **kwargs: task.calls.append(("wait_until",)) or True
        task._input_fixed_price = lambda *args, **kwargs: task.calls.append(("input",))
        return task

    def test_attempt_bid_abandons_only_after_two_zero_reads(self):
        task = self._make_bid_task([0, 0])
        boxes = SimpleNamespace(asset_value=1, abandon=2, abandon_confirm=3, bid=4, bid_confirm=5)

        task._attempt_bid(boxes, time.monotonic() + 60)

        self.assertEqual([c for c in task.calls if c[0] == "click"], [("click", 2), ("click", 3)])
        self.assertNotIn(("input",), task.calls)

    def test_attempt_bid_continues_when_second_read_is_not_zero(self):
        """首次读到 0 但二次读数正常时, 应继续出价而不是放弃。"""
        task = self._make_bid_task([0, 7000])
        boxes = SimpleNamespace(asset_value=1, abandon=2, abandon_confirm=3, bid=4, bid_confirm=5)

        task._attempt_bid(boxes, time.monotonic() + 60)

        self.assertEqual([c for c in task.calls if c[0] == "click"], [])
        self.assertIn(("read", 7000), task.calls)
        self.assertIn(("input",), task.calls)

    # --- 出价循环记账 ---
    def test_stage_bid_loop_does_not_count_abandon_as_successful_bid(self):
        task = self._make_task()
        task.current_bid_count = 0
        attempts = iter([False])
        outcomes = iter([True])
        task._attempt_bid = lambda boxes, deadline: next(attempts)
        task._wait_bid_outcome = lambda boxes, deadline: next(outcomes)
        task._remaining_timeout = lambda deadline, limit: limit

        task._stage_bid_loop(boxes=None, deadline=time.monotonic() + 60)

        self.assertEqual(task.current_bid_count, 0)
        self.assertFalse(any("出价成功" in msg for _, msg in task.logs))

    def test_stage_bid_loop_counts_successful_bid(self):
        task = self._make_task()
        task.current_bid_count = 0
        attempts = iter([True])
        outcomes = iter([True])
        task._attempt_bid = lambda boxes, deadline: next(attempts)
        task._wait_bid_outcome = lambda boxes, deadline: next(outcomes)
        task._remaining_timeout = lambda deadline, limit: limit

        task._stage_bid_loop(boxes=None, deadline=time.monotonic() + 60)

        self.assertEqual(task.current_bid_count, 1)
        self.assertTrue(any("第 1 次出价成功" in msg for _, msg in task.logs))

    # --- 低保金与价格配置 ---
    def test_try_claim_welfare_skips_gracefully_when_button_missing(self):
        """低保金按钮未出现(如当日已领取)只跳过领取, 不得拖垮已成功的拍卖轮次。"""
        task = self._make_task()
        task._wait_click_optional = lambda *args, **kwargs: False
        boxes = SimpleNamespace(welfare_btn=1, claim=2, cancel=3)

        self.assertFalse(task._try_claim_welfare(boxes, None))

    def test_try_claim_welfare_propagates_deadline_expiry(self):
        task = self._make_task()

        def raise_timeout(*args, **kwargs):
            raise WaitFailedException("单轮拍卖超时")

        task._wait_click_optional = raise_timeout
        boxes = SimpleNamespace(welfare_btn=1, claim=2, cancel=3)

        with self.assertRaises(WaitFailedException):
            task._try_claim_welfare(boxes, time.monotonic() - 1)

    def test_validate_price_config_accepts_valid_values(self):
        task = self._make_task(**{AutoBidAuctionTask.CONF_FIXED_PRICE: "5000"})

        task._validate_price_config()

    def test_validate_price_config_rejects_non_positive_base_price(self):
        task = self._make_task(**{AutoBidAuctionTask.CONF_FIXED_PRICE: "-5"})

        with self.assertRaises(ValueError):
            task._validate_price_config()

    def test_validate_price_config_rejects_unparseable_base_price(self):
        task = self._make_task(**{AutoBidAuctionTask.CONF_FIXED_PRICE: "1.5"})

        with self.assertRaises(ValueError):
            task._validate_price_config()

    def test_validate_price_config_rejects_non_finite_raise_value(self):
        task = self._make_task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: "100",
                AutoBidAuctionTask.CONF_AUTO_RAISE: True,
                AutoBidAuctionTask.CONF_RAISE_VALUE: "inf",
            }
        )

        with self.assertRaises(ValueError):
            task._validate_price_config()


if __name__ == "__main__":
    unittest.main()
