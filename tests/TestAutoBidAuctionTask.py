import time
import unittest

from ok import WaitFailedException

from src.tasks.AutoBidAuctionTask import AutoBidAuctionTask


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


if __name__ == "__main__":
    unittest.main()
