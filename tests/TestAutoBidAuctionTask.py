import itertools
import re
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

from ok import TaskDisabledException, WaitFailedException
from ok.core.config_schema import build_config_fields

import src.tasks.auction.recovery as auction_recovery
import src.tasks.auction.welfare as auction_welfare
import src.tasks.AutoBidAuctionTask as auction_module
from src.scene.PositionMap import PositionMap
from src.tasks.AutoBidAuctionTask import (
    RE_CANCEL,
    RE_CLAIM,
    RE_MAIN_TITLE,
    RE_ONE_CLICK_SELL,
    RE_POPUP_CLOSE_HINT,
    RE_WELFARE_COUNTER,
    AuctionState,
    AutoBidAuctionTask,
    PostRoundState,
)
from src.tasks.BaseNTETask import BaseNTETask


def _config(**values) -> Mock:
    """构造只认识给定键的配置对象, 未给出的键返回调用方传入的默认值。"""
    return Mock(get=lambda key, default=None: values.get(key, default))


def _make_task(config: dict | None = None) -> AutoBidAuctionTask:
    """构造跳过 ok 框架初始化的任务实例, 只保留被测方法需要的依赖。"""
    task = AutoBidAuctionTask.__new__(AutoBidAuctionTask)
    task.log_info = Mock()
    task.log_debug = Mock()
    task.log_warning = Mock()
    task.log_error = Mock()
    task.sleep = Mock()
    task.next_frame = Mock()
    task.config = _config(**(config or {}))
    task.ocr = Mock(return_value=[])
    task.wait_ocr = Mock(return_value=[])
    task.wait_until = Mock(return_value=True)
    task.operate_click = Mock(return_value=True)
    task.box_of_screen = Mock(return_value=Mock())
    task.find_monthly_card = Mock(return_value=None)
    task.check_monthly_card = Mock(return_value=False)
    task.handle_monthly_card = Mock()
    task.info_set = Mock()
    # 位置表由 BaseNTETask.__init__ 建立, 桩实例要自己补一份, 否则 self.pos.* 会 AttributeError。
    task.pos = PositionMap(task)
    # 当日低保领取记录由 __init__ 建立, 桩实例要自己补一份, 否则
    # _rollover_welfare_day / _welfare_quota_exhausted 会 AttributeError。
    # 默认「没读到过弹窗读数」= 今日低保未领完, 与真机首次运行的保守口径一致。
    task._welfare_day = None
    task._welfare_claims_today = 0
    task._welfare_daily_limit = None
    # 贴边阈值按屏幕宽度换算(见 ESTIMATE_EDGE_MARGIN_RATIO), 而框架的 width 属性会一路
    # 走到 executor.method.width, executor 又读 _executor —— 桩实例没有它, 必须在这里给
    # 一个默认屏幕宽。需要验证高分辨率行为的用例用
    # patch.object(AutoBidAuctionTask, "width", property(...)) 自行覆盖
    # (见 TestAuctionEstimateEdgeMargin)。
    task._executor = Mock(method=Mock(width=1920))
    # 掉线回场靠基类的 in_team_and_world() 判定大世界(= is_in_team() and in_world())。
    # 两个输入都桩掉、让基类的合取逻辑真跑, 默认「在队伍里但不在大世界」; 需要「在大世界」
    # 的用例只覆盖 in_world, 需要覆盖误报的用例覆盖 is_in_team。
    task.is_in_team = Mock(return_value=True)
    task.in_world = Mock(return_value=False)
    # 框架的 wait_click_ocr 直接走 click_box, 不会保存和还原鼠标位置,
    # 后台执行时会把用户的鼠标留在游戏窗口内; 任务内任何调用都视为回归。
    task.wait_click_ocr = Mock(side_effect=AssertionError("不应使用 wait_click_ocr"))
    task.current_bid_count = 0
    task.last_bid_price = None
    task._post_round_state = PostRoundState()
    task._sell_failures = 0
    task._inventory_stuck = False
    return task


class _FakeTime:
    """可控时钟: 让 sleep 推进时间, 以便断言重试时机而不用真的等待。"""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


def _make_configured_task() -> AutoBidAuctionTask:
    """只跑完 __init__ 的配置装配, 不触碰 ok 框架初始化。"""
    task = AutoBidAuctionTask.__new__(AutoBidAuctionTask)
    task.default_config = {}
    task.config_description = {}
    task.add_rounds_config = Mock()
    task.add_exit_after_config = Mock()
    with patch.object(BaseNTETask, "__init__", return_value=None):
        AutoBidAuctionTask.__init__(task)
    return task


def _option_subsets(options: list[str]) -> list[list[str]]:
    """多选框的全部取值组合, 空选也算一种。"""
    subsets: list[list[str]] = [[]]
    for option in options:
        subsets += [subset + [option] for subset in subsets]
    return subsets


def _stub_round_loop(task: AutoBidAuctionTask, total_polls: int) -> None:
    """把轮次循环收敛到固定次数, 避免依赖真实配置和 info 面板。"""
    task.start_rounds = Mock()
    task._validate_price_config = Mock()
    task._build_boxes = Mock(return_value=Mock())
    task.finish_rounds = Mock()
    task.add_failed = Mock()
    task.begin_round = Mock(return_value=True)

    polls = {"count": 0}

    def has_remaining_rounds():
        polls["count"] += 1
        return polls["count"] <= total_polls

    task.has_remaining_rounds = has_remaining_rounds


class TestAuctionSleepCheckHook(unittest.TestCase):
    def test_sleep_check_closes_monthly_card_popup(self):
        """月卡时间窗内出现弹窗时, 睡眠钩子先关闭弹窗再继续当前轮次。"""
        task = _make_task()
        task.check_monthly_card.return_value = True

        task.sleep_check()

        task.handle_monthly_card.assert_called_once()

    def test_sleep_check_is_noop_outside_window(self):
        """不在月卡时间窗内时睡眠钩子没有额外动作, 避免每次 sleep 都白点一次。"""
        task = _make_task()

        task.sleep_check()

        task.handle_monthly_card.assert_not_called()

    @patch.object(BaseNTETask, "__init__", return_value=None)
    def test_init_enables_sleep_check_interval(self, _base_init):
        """ok-script 只在 sleep_check_interval >= 0 时调用钩子, 默认 -1 会让月卡处理静默失效。"""
        task = AutoBidAuctionTask.__new__(AutoBidAuctionTask)
        task.default_config = {}
        task.config_description = {}
        task.add_rounds_config = Mock()
        task.add_exit_after_config = Mock()

        AutoBidAuctionTask.__init__(task)

        self.assertGreaterEqual(task.sleep_check_interval, 0)
        self.assertEqual(task.sleep_check_interval, AutoBidAuctionTask.SLEEP_CHECK_INTERVAL)


class TestAuctionBlockingPopupRecovery(unittest.TestCase):
    def test_recover_blocking_popup_closes_popup_found_by_feature(self):
        """时间窗已过(任务在 5 点后才启动)时, 靠弹窗特征兜底关闭, 避免每轮空转到超时。"""
        task = _make_task()
        task.find_monthly_card.return_value = object()

        task._recover_blocking_popup()

        task.handle_monthly_card.assert_called_once()

    def test_recover_blocking_popup_dismisses_notice_popups(self):
        """入场费/异常出价/满仓提示共用一套模板, 整轮失败后也要兜一次。"""
        task = _make_task()
        task._dismiss_notice_popup = Mock()

        task._recover_blocking_popup(Mock())

        task._dismiss_notice_popup.assert_called_once()

    def test_recover_blocking_popup_is_noop_without_popup(self):
        task = _make_task()

        task._recover_blocking_popup()

        task.handle_monthly_card.assert_not_called()

    def test_recover_blocking_popup_swallows_handler_failure(self):
        """兜底处理本身失败时只告警, 不能把异常抛出去中止整个任务。"""
        task = _make_task()
        task.find_monthly_card.return_value = object()
        task.handle_monthly_card.side_effect = WaitFailedException("月卡关闭失败")

        task._recover_blocking_popup()

        task.log_warning.assert_called_once()

    def test_recover_blocking_popup_propagates_task_disabled(self):
        task = _make_task()
        task.find_monthly_card.return_value = object()
        task.handle_monthly_card.side_effect = TaskDisabledException("disabled")

        with self.assertRaises(TaskDisabledException):
            task._recover_blocking_popup()


class TestAuctionRoundFailureHandling(unittest.TestCase):
    def test_failed_round_triggers_popup_recovery(self):
        """整轮失败后按弹窗特征兜底一次, 覆盖月卡时间窗已过的情况。"""
        task = _make_task()
        _stub_round_loop(task, total_polls=1)
        task._run_single_round = Mock(side_effect=WaitFailedException("boom"))
        task._recover_blocking_popup = Mock()

        task.do_run()

        task.add_failed.assert_called_once()
        task._recover_blocking_popup.assert_called_once()

    def test_successful_round_does_not_trigger_popup_recovery(self):
        task = _make_task()
        _stub_round_loop(task, total_polls=1)
        task._run_single_round = Mock(return_value=None)
        task._recover_blocking_popup = Mock()

        task.do_run()

        task.add_failed.assert_not_called()
        task._recover_blocking_popup.assert_not_called()

    def test_task_disabled_is_not_treated_as_round_failure(self):
        task = _make_task()
        _stub_round_loop(task, total_polls=1)
        task._run_single_round = Mock(side_effect=TaskDisabledException("disabled"))
        task._recover_blocking_popup = Mock()

        with self.assertRaises(TaskDisabledException):
            task.do_run()

        task.add_failed.assert_not_called()

    def test_invalid_config_still_finishes_the_rounds(self):
        """入场校验抛错时轮次汇总和结束通知不能跟着一起丢。

        校验若放在 try 之外, finish_rounds 不会执行: 用户收不到任务结束通知,
        info 面板也停在「进行中」。
        """
        task = _make_task()
        _stub_round_loop(task, total_polls=1)
        task._run_single_round = Mock()
        task._validate_price_config = Mock(side_effect=ValueError("基础价必须为正整数"))

        with self.assertRaises(ValueError):
            task.do_run()

        task._run_single_round.assert_not_called()
        task.finish_rounds.assert_called_once()

    def test_run_warns_about_contradictory_sell_config(self):
        """入场告警必须在 do_run 里真的被调用, 光有方法不算。

        告警只在启动时说一次, 漏调不会让任何流程失败, 因此最容易被静默删掉。
        """
        task = _make_task()
        _stub_round_loop(task, total_polls=1)
        task._run_single_round = Mock()
        task._warn_if_no_sellable_quality = Mock()

        task.do_run()

        task._warn_if_no_sellable_quality.assert_called_once()

    def test_warnings_run_after_price_validation(self):
        """校验失败时不该再报出售配置的告警 —— 先处理真正会拦下任务的问题。"""
        task = _make_task()
        _stub_round_loop(task, total_polls=1)
        task._run_single_round = Mock()
        task._validate_price_config = Mock(side_effect=ValueError("基础价必须为正整数"))
        task._warn_if_no_sellable_quality = Mock()

        with self.assertRaises(ValueError):
            task.do_run()

        task._warn_if_no_sellable_quality.assert_not_called()

    def test_run_confirms_auction_entry_every_round(self):
        """入口确认必须每轮都跑一次, 且排在该轮拍卖之前。

        漏调不会让任何用例失败(人不在拍卖界面时仍会由 _stage_match 空转兜底),
        因此最容易被静默删掉 —— 代价是后续每轮白等 MATCH_TIMEOUT(120 秒), 只有实测
        才看得出来。只在循环外调一次同样算漏: 上一轮掉线或异常退出时人可能已经不在
        拍卖界面了, 后续每轮都得再兜一次。
        """
        task = _make_task()
        _stub_round_loop(task, total_polls=2)
        order: list[str] = []
        task._ensure_auction_entry = Mock(side_effect=lambda _b: order.append("entry"))

        def single_round(_boxes):
            order.append("round")

        task._run_single_round = single_round

        task.do_run()

        self.assertEqual(task._ensure_auction_entry.call_count, 2)
        self.assertEqual(order, ["entry", "round", "entry", "round"])

    def test_round_start_closes_a_leftover_warehouse_before_bidding(self):
        """每轮开头先收起残留的藏品仓库, 再做入口确认与出价。

        轮末的仓库检查只有 _run_single_round 正常返回才会执行; 启动前残留的仓库
        (上次进程被杀 / 手动开着)会让每轮 _stage_match 空烧 MATCH_TIMEOUT(120 秒)
        后抛异常, 轮末检查永远轮不到 —— 只能在每轮开头兜。
        """
        task = _make_task()
        _stub_round_loop(task, total_polls=2)
        task._run_single_round = Mock(side_effect=WaitFailedException("匹配阶段超时"))
        task._recover_blocking_popup = Mock()
        task._is_warehouse_open = Mock(side_effect=[True, False, False])
        task._close_warehouse = Mock()

        task.do_run()

        task._close_warehouse.assert_called_once()
        self.assertEqual(task._run_single_round.call_count, 2)

    def test_uncloseable_warehouse_stops_the_rounds_at_round_start(self):
        """仓库收不掉时立即停止后续轮次, 不把剩余轮次烧在匹配超时上。"""
        task = _make_task()
        _stub_round_loop(task, total_polls=2)
        task._run_single_round = Mock()
        task._recover_blocking_popup = Mock()
        task._is_warehouse_open = Mock(return_value=True)
        task._close_warehouse = Mock()

        task.do_run()

        task._close_warehouse.assert_called_once()
        task._run_single_round.assert_not_called()
        task.log_error.assert_called()


class TestAuctionWelfareDialogClose(unittest.TestCase):
    """低保金每日次数用尽(5/5)时弹窗仍会打开但没有领取按钮, 必须继续关闭。"""

    def _boxes(self) -> Mock:
        return Mock()

    def test_claim_button_missing_still_closes_dialog(self):
        task = _make_task()
        task._wait_click_optional = Mock(side_effect=[True, False])
        task._close_welfare_dialog = Mock(return_value=True)

        claimed = task._try_claim_welfare(self._boxes(), None)

        self.assertTrue(claimed)
        task._close_welfare_dialog.assert_called_once()

    def test_claim_success_also_closes_dialog(self):
        task = _make_task()
        task._wait_click_optional = Mock(return_value=True)
        task._close_welfare_dialog = Mock(return_value=True)

        claimed = task._try_claim_welfare(self._boxes(), None)

        self.assertTrue(claimed)
        task._close_welfare_dialog.assert_called_once()

    def test_welfare_button_missing_skips_everything(self):
        task = _make_task()
        task._wait_click_optional = Mock(return_value=False)
        task._close_welfare_dialog = Mock(return_value=True)

        claimed = task._try_claim_welfare(self._boxes(), None)

        self.assertFalse(claimed)
        task._close_welfare_dialog.assert_not_called()

    def test_close_dialog_returns_true_when_already_closed(self):
        task = _make_task()
        task._is_welfare_dialog_open = Mock(return_value=False)
        task._wait_click_optional = Mock()

        self.assertTrue(task._close_welfare_dialog(self._boxes(), None))
        task._wait_click_optional.assert_not_called()

    def test_close_dialog_retries_cancel_until_closed(self):
        """第一次点击取消没生效时必须继续重试, 否则弹窗会一直挡住拍卖界面。"""
        task = _make_task()
        # 循环内每次点击取消后各查一次: 第 1 轮仍开着, 第 2 轮关闭。
        task._is_welfare_dialog_open = Mock(side_effect=[True, True, True, False])
        task._wait_click_optional = Mock(return_value=True)

        self.assertTrue(task._close_welfare_dialog(self._boxes(), None))
        self.assertEqual(task._wait_click_optional.call_count, 2)

    def test_close_dialog_reports_failure_after_retries(self):
        task = _make_task()
        task._is_welfare_dialog_open = Mock(return_value=True)
        task._wait_click_optional = Mock(return_value=True)

        self.assertFalse(task._close_welfare_dialog(self._boxes(), None))
        self.assertEqual(
            task._wait_click_optional.call_count, AutoBidAuctionTask.WELFARE_CLOSE_RETRIES
        )

    def test_recover_blocking_popup_closes_stuck_welfare_dialog(self):
        """整轮失败后按界面特征兜底关闭残留的低保金弹窗。"""
        task = _make_task()
        task._is_welfare_dialog_open = Mock(return_value=True)
        task._close_welfare_dialog = Mock(return_value=True)

        task._recover_blocking_popup(self._boxes())

        task._close_welfare_dialog.assert_called_once()
        task.handle_monthly_card.assert_not_called()


class TestAuctionWelfareQuota(unittest.TestCase):
    """弹窗读数「今日已领取次数：N/5」决定按哪个出售清单卖。

    旧实现把「本轮领到低保」当成放开条件, 但卖藏品会抬高资产、资产高于 10 万就领不到
    下一次低保 —— 「领一次卖一次」会把当天剩下的低保全部堵死。现在只有把当日次数领满
    才切到「已领完」清单, 所以这组用例守的是「次数状态从哪来、什么时候清零」。
    """

    def _boxes(self) -> Mock:
        return Mock()

    @staticmethod
    def _text_box(text: str) -> Mock:
        # 不能用 Mock(name=text): name 是 Mock 的保留参数, 读 box.name 拿到的是子 Mock.
        box = Mock()
        box.name = text
        return box

    def _task_with_counter(self, *texts: str) -> AutoBidAuctionTask:
        task = _make_task()
        task.ocr = Mock(return_value=[self._text_box(t) for t in texts])
        return task

    def test_counter_reads_claims_and_limit(self):
        task = self._task_with_counter("今日已领取次数：3/5")

        task._read_welfare_counter(self._boxes())

        self.assertEqual(task._welfare_claims_today, 3)
        self.assertEqual(task._welfare_daily_limit, 5)
        self.assertFalse(task._welfare_quota_exhausted())

    def test_counter_regex_needs_the_label(self):
        """正则必须锚在「次数」上。

        弹窗附近还有「当前资产：(12,904,567)」这类数字, 也有别的界面文案里出现 N/M 的
        可能。丢掉标签锚点后任意 "N/M" 都会把状态判成「已领满」, 于是任务在还能领低保
        的时候切到「已领完」清单 —— 资产被抬高, 低保就领不到了。
        """
        self.assertIsNotNone(RE_WELFARE_COUNTER.search("今日已领取次数：5/5"))
        for decoy in ("当前资产：(12,904,567)", "1/5", "已领取 5 次", "出售价值 3/5"):
            with self.subTest(decoy=decoy):
                self.assertIsNone(RE_WELFARE_COUNTER.search(decoy))

    def test_counter_accepts_fullwidth_digits(self):
        task = self._task_with_counter("今日已领取次数：５/５")

        task._read_welfare_counter(self._boxes())

        self.assertTrue(task._welfare_quota_exhausted())

    def test_counter_survives_the_line_being_split_in_two(self):
        """检测模型可能把标签和数值拆成两个框, 拼接后仍要能解析。"""
        task = self._task_with_counter("今日已领取次数：", "2/5")

        task._read_welfare_counter(self._boxes())

        self.assertEqual(task._welfare_claims_today, 2)

    def test_counter_uses_ocr_not_wait_ocr(self):
        """必须走 `ocr(match=None)` 拿全量文本。

        `wait_ocr(match=RE_XXX)` 会按 match 过滤返回值, 把「今日已领取次数：」这段标签
        滤掉, 只剩数值框, 解析必然失败。这条用例守着别把它换回去。
        """
        task = self._task_with_counter("今日已领取次数：1/5")

        task._read_welfare_counter(self._boxes())

        task.wait_ocr.assert_not_called()
        self.assertIsNone(task.ocr.call_args.kwargs.get("match"))

    def test_counter_read_failure_keeps_previous_state(self):
        """读不出时必须保持原值 —— 「已领完」是放开出售的开关, 读不到就得保守。"""
        task = self._task_with_counter()
        task._welfare_claims_today = 2
        task._welfare_daily_limit = 5

        task._read_welfare_counter(self._boxes())

        self.assertEqual(task._welfare_claims_today, 2)
        self.assertEqual(task._welfare_daily_limit, 5)

    def test_counter_retries_when_the_frame_is_blank(self):
        """弹窗淡入中的空白帧不能把这次读数吃掉。

        这次读数一旦落空, 资产涨过 10 万后弹窗就不再打开, 当天再也读不到 —— 阶段永远
        停在「未领完」, 高价值品质静默不卖。
        """
        task = _make_task()
        task.ocr = Mock(side_effect=[[], [self._text_box("今日已领取次数：2/5")]])

        task._read_welfare_counter(self._boxes())

        self.assertEqual(task._welfare_claims_today, 2)
        self.assertEqual(task.ocr.call_count, AutoBidAuctionTask.WELFARE_COUNTER_READS)
        task.next_frame.assert_called_once()

    def test_successful_claim_rereads_the_counter_after_the_click(self):
        """点击领取后要重读弹窗次数, 而不是盲目本地 +1。

        点击已发出不等于领取已生效: 盲目 +1 会在点击落空时虚增当日次数, 提前按
        「已领完」放开出售抬高资产, 弹窗从此不再打开, 虚计再无读数可纠偏。弹窗还
        开着时重读权威读数, 读数整体覆盖本地值。
        """
        task = _make_task()
        task.ocr = Mock(
            side_effect=[
                [self._text_box("今日已领取次数：4/5")],
                [self._text_box("今日已领取次数：5/5")],
            ]
        )
        task._wait_click_optional = Mock(return_value=True)
        task._close_welfare_dialog = Mock(return_value=True)

        task._try_claim_welfare(self._boxes(), None)

        self.assertEqual(task._welfare_claims_today, 5)
        self.assertEqual(task._welfare_daily_limit, 5)
        self.assertTrue(task._welfare_quota_exhausted())

    def test_failed_claim_click_keeps_the_pre_claim_count(self):
        """点击落空时本地计数保持领取前的值, 往保守方向兜。

        多记的代价是卖掉高价值品质把资产抬过 10 万、当天剩余低保领不到; 少记的代价
        只是少卖几件藏品。两个方向不对称, 读不到领取后的新读数就不能 +1。
        """
        task = _make_task()
        # 领取前读到 4/5, 领取后两次换帧重读全部落空(如弹窗领取后立即关闭)。
        task.ocr = Mock(side_effect=[[self._text_box("今日已领取次数：4/5")], [], []])
        task._wait_click_optional = Mock(return_value=True)
        task._close_welfare_dialog = Mock(return_value=True)

        task._try_claim_welfare(self._boxes(), None)

        self.assertEqual(task._welfare_claims_today, 4)
        self.assertFalse(task._welfare_quota_exhausted())

    def test_missing_claim_button_does_not_increment(self):
        task = _make_task()
        task._read_welfare_counter = Mock()
        task._wait_click_optional = Mock(side_effect=[True, False])
        task._close_welfare_dialog = Mock(return_value=True)

        task._try_claim_welfare(self._boxes(), None)

        self.assertEqual(task._welfare_claims_today, 0)

    def test_quota_exhausted_needs_the_dialog_reading(self):
        task = _make_task()
        task._welfare_claims_today = 99
        task._welfare_daily_limit = None

        self.assertFalse(task._welfare_quota_exhausted())

    def test_quota_exhausted_boundary(self):
        task = _make_task()
        task._welfare_daily_limit = 5

        for claims, expected in ((4, False), (5, True), (6, True)):
            task._welfare_claims_today = claims
            with self.subTest(claims=claims):
                self.assertEqual(task._welfare_quota_exhausted(), expected)

    def _rollover_task(self, now) -> AutoBidAuctionTask:
        task = _make_task()
        task._welfare_claims_today = 5
        task._welfare_daily_limit = 5
        fake_datetime = Mock()
        fake_datetime.now.return_value = now
        # 跨日重置已迁至 auction/welfare.py, 补丁要打在实现所在模块上才能控制时钟。
        patcher = patch.object(auction_welfare, "datetime", fake_datetime)
        patcher.start()
        self.addCleanup(patcher.stop)
        return task

    def test_rollover_resets_after_the_daily_refresh_hour(self):
        task = self._rollover_task(datetime(2026, 9, 25, 5, 1))

        task._rollover_welfare_day()

        self.assertEqual(task._welfare_claims_today, 0)
        self.assertIsNone(task._welfare_daily_limit)

    def test_rollover_keeps_yesterdays_records_before_the_refresh_hour(self):
        """0~5 点这段仍算前一天: 按自然日午夜切会让这几轮误以为低保又能领了。"""
        task = self._rollover_task(datetime(2026, 9, 25, 4, 59))
        task._welfare_day = date(2026, 9, 24)

        task._rollover_welfare_day()

        self.assertEqual(task._welfare_claims_today, 5)
        self.assertEqual(task._welfare_daily_limit, 5)

    def test_rollover_is_a_noop_within_the_same_day(self):
        task = self._rollover_task(datetime(2026, 9, 25, 5, 1))
        task._welfare_day = date(2026, 9, 25)

        task._rollover_welfare_day()

        self.assertEqual(task._welfare_claims_today, 5)

    def test_every_round_checks_the_day_rollover(self):
        """挂在每轮上: 只在任务启动时算一次的话, 跨天后仍按昨天的「已领完」放开出售。"""
        task = _make_task()
        task._inventory_stuck = False
        task._rollover_welfare_day = Mock()
        task._exec_auction_round = Mock(return_value=True)
        task.add_success = Mock()
        task.add_failed = Mock()
        task._sell_collections_on_interval = Mock()
        # current_round 是只读属性, 读的是 _round_state.index.
        task._round_state = Mock(index=1, total_text="10")

        task._run_single_round(Mock())
        task._run_single_round(Mock())

        self.assertEqual(task._rollover_welfare_day.call_count, 2)


class TestAuctionInventoryFullSell(unittest.TestCase):
    """满仓会挡住拍卖(库存不足无法出价), 不能只等轮次间隔。"""

    def setUp(self):
        # current_round 是只读 property, 需要覆盖到类上才能构造不同轮次。
        self._round_patcher = patch.object(
            AutoBidAuctionTask, "current_round", new_callable=PropertyMock
        )
        self.current_round = self._round_patcher.start()
        self.addCleanup(self._round_patcher.stop)

    def _task(self, *, interval: int, current_round: int, mode: str | None = None):
        task = _make_task()
        task.config = _config(
            **{
                AutoBidAuctionTask.CONF_SELL_MODE: (
                    AutoBidAuctionTask.SELL_MODE_INTERVAL if mode is None else mode
                ),
                AutoBidAuctionTask.CONF_SELL_INTERVAL: interval,
                AutoBidAuctionTask.CONF_SELL_BEFORE_WELFARE: [],
                AutoBidAuctionTask.CONF_SELL_AFTER_WELFARE: [],
            }
        )
        self.current_round.return_value = current_round
        task._sell_collections = Mock(return_value=True)
        return task

    def test_sells_when_inventory_full_before_interval(self):
        """间隔设为 3 时, 第 2 轮满仓必须立刻出售而不是等到第 3 轮。"""
        task = self._task(interval=3, current_round=2)

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=True))

        task._sell_collections.assert_called_once()

    def test_sells_on_interval_when_inventory_not_full(self):
        task = self._task(interval=3, current_round=3)

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=False))

        task._sell_collections.assert_called_once()

    def test_skips_when_neither_condition_met(self):
        task = self._task(interval=3, current_round=2)

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=False))

        task._sell_collections.assert_not_called()

    def test_off_mode_never_sells(self):
        """模式为「不出售」时, 满仓和间隔都不该触发出售。"""
        task = self._task(interval=3, current_round=3, mode=AutoBidAuctionTask.SELL_MODE_OFF)

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=True))

        task._sell_collections.assert_not_called()

    def test_invalid_interval_falls_back_to_inventory_cleanup(self):
        """「按间隔出售」下间隔为 0 是无效配置, 退化成满仓清理而不是完全不出售。"""
        task = self._task(interval=0, current_round=2)

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=True))

        task._sell_collections.assert_called_once()

    def test_invalid_interval_does_not_sell_on_round_without_full(self):
        task = self._task(interval=0, current_round=3)

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=False))

        task._sell_collections.assert_not_called()

    def test_full_mode_ignores_interval(self):
        """「满仓时清理」不看轮次: 间隔命中也不出售。"""
        task = self._task(interval=3, current_round=3, mode=AutoBidAuctionTask.SELL_MODE_FULL)

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=False))

        task._sell_collections.assert_not_called()

    def test_full_mode_sells_when_inventory_full(self):
        task = self._task(interval=3, current_round=2, mode=AutoBidAuctionTask.SELL_MODE_FULL)

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=True))

        task._sell_collections.assert_called_once()

    def test_unknown_mode_is_treated_as_off(self):
        """脏配置(拼写错误/旧值残留)必须按「不出售」处理, 不能意外清空仓库。"""
        task = self._task(interval=3, current_round=3, mode="随便写的模式")

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=True))

        task._sell_collections.assert_not_called()

    def test_uses_collection_sell_follows_config(self):
        task = self._task(interval=0, current_round=1, mode=AutoBidAuctionTask.SELL_MODE_OFF)
        self.assertFalse(task._uses_collection_sell())

        task.config = _config(
            **{AutoBidAuctionTask.CONF_SELL_MODE: AutoBidAuctionTask.SELL_MODE_INTERVAL}
        )
        self.assertTrue(task._uses_collection_sell())

        task.config = _config(
            **{AutoBidAuctionTask.CONF_SELL_MODE: AutoBidAuctionTask.SELL_MODE_FULL}
        )
        self.assertTrue(task._uses_collection_sell())

    def _post_round_task(self, mode: str, *, inventory_full: bool):
        task = self._task(interval=3, current_round=2, mode=mode)
        task._dismiss_notice_popup = Mock()
        task._detect_inventory_full = Mock(return_value=inventory_full)
        task._sell_collections_with_escalation = Mock(return_value=True)
        return task

    def test_post_round_actions_only_observe_and_never_sell(self):
        """结算后处理只观测, 出售统一由轮次末尾决定。

        两处都动手会让同一轮卖两次: 第二次面对已被卖空的仓库读到「出售价值 0」,
        白白累计失败次数, 最终触发「放宽保留品质」把用户要保留的藏品也卖掉。
        """
        task = self._post_round_task(AutoBidAuctionTask.SELL_MODE_FULL, inventory_full=True)

        task._run_post_round_actions(Mock(), None)

        task._sell_collections_with_escalation.assert_not_called()
        self.assertTrue(task._post_round_state.observed)
        self.assertTrue(task._post_round_state.inventory_full)

    def test_post_round_actions_record_the_observation(self):
        task = self._post_round_task(AutoBidAuctionTask.SELL_MODE_INTERVAL, inventory_full=False)

        task._run_post_round_actions(Mock(), None)

        task._sell_collections_with_escalation.assert_not_called()
        self.assertTrue(task._post_round_state.observed)
        self.assertFalse(task._post_round_state.inventory_full)

    def test_off_mode_does_not_even_probe_the_inventory(self):
        """「不出售」模式不该为满仓检测花时间。"""
        task = self._post_round_task(AutoBidAuctionTask.SELL_MODE_OFF, inventory_full=True)

        task._run_post_round_actions(Mock(), None)

        task._sell_collections_with_escalation.assert_not_called()
        task._detect_inventory_full.assert_not_called()


class TestAuctionConditionalSell(unittest.TestCase):
    """出售清单按当日低保阶段自动切换, 两个清单都是「勾选即出售」。"""

    def _task(self, before=(), after=(), mode=None) -> AutoBidAuctionTask:
        task = _make_task()
        values = {
            AutoBidAuctionTask.CONF_SELL_BEFORE_WELFARE: list(before),
            AutoBidAuctionTask.CONF_SELL_AFTER_WELFARE: list(after),
        }
        if mode is not None:
            values[AutoBidAuctionTask.CONF_SELL_MODE] = mode
        task.config = _config(**values)
        return task

    def test_before_quota_uses_the_before_list(self):
        task = self._task(before=["品质白"], after=["品质白", "品质紫"])

        self.assertEqual(task._sell_qualities(), ["品质白"])

    def test_after_quota_uses_the_after_list(self):
        task = self._task(before=["品质白"], after=["品质白", "品质紫"])
        task._welfare_claims_today = 5
        task._welfare_daily_limit = 5

        self.assertEqual(task._sell_qualities(), ["品质白", "品质紫"])

    def test_regression_claiming_once_does_not_switch_the_list(self):
        """回归: 领过低保、但今天还没领满时必须仍用「未领完」清单。

        旧实现在每次领取成功后就追加出售, 而卖藏品会抬高资产 —— 资产高于 10 万就领不到
        下一次低保, 于是「领一次就卖一次」把当天剩下的低保全部堵死。
        """
        task = self._task(before=["品质白"], after=["品质白", "品质紫"])
        task._welfare_claims_today = 4
        task._welfare_daily_limit = 5

        self.assertEqual(task._sell_qualities(), ["品质白"])

    def test_missing_dialog_reading_keeps_the_conservative_list(self):
        """没读到过弹窗读数时必须保守: 判不准就按「未领完」清单卖。"""
        task = self._task(before=["品质白"], after=["品质白", "品质紫"])
        task._welfare_claims_today = 99
        task._welfare_daily_limit = None

        self.assertEqual(task._sell_qualities(), ["品质白"])

    def test_inventory_full_still_uses_the_before_list(self):
        """满仓只代表必须腾空间, 不代表低保已无望 —— 清单仍按低保阶段选。

        满仓改用「已领完」清单会立刻卖掉高价值品质, 把资产顶过 10 万, 当天剩下的低保
        就领不到了; 真腾不出空间有放宽机制兜底(6 个品质全卖)。
        """
        task = self._task(
            before=["品质白"],
            after=["品质白", "品质紫"],
            mode=AutoBidAuctionTask.SELL_MODE_FULL,
        )
        task._detect_inventory_full = Mock(return_value=True)
        task._sell_collections_with_escalation = Mock(return_value=True)

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=True))

        self.assertEqual(task._sell_collections_with_escalation.call_args.args[2], ["品质白"])

    def test_legacy_quality_keys_are_gone(self):
        """旧的两个品质键已彻底废弃: 不再注册、不再迁移, 升级用户直接吃默认清单。

        残留旧键或残留迁移会让「全部按默认值来」的约定失效 —— 老配置里只要还有旧键,
        面板上显示的默认值与实际生效的清单就会不一致。
        """
        task = _make_configured_task()

        for legacy in ("保留藏品品质", "满仓或领低保后追加出售品质"):
            with self.subTest(key=legacy):
                self.assertNotIn(legacy, task.default_config)
                self.assertNotIn(legacy, task.config_type)
                self.assertNotIn(legacy, task.config_description)

    def test_quality_filters_only_click_the_listed_qualities(self):
        """勾选即出售: 只有清单里的品质被点选, 其余全部保留。"""
        task = self._task()

        clicked_count = task._select_quality_filters(None, ["品质紫"])

        clicked = [tuple(call.args) for call in task.box_of_screen.call_args_list]
        self.assertEqual(clicked_count, 1)
        self.assertEqual(clicked, [AutoBidAuctionTask.QUALITY_BOXES[3]])  # 品质紫

    def test_quality_filters_keep_everything_when_the_list_is_empty(self):
        """空清单 = 什么都不卖, 连品质按钮都不点。"""
        task = self._task()

        clicked_count = task._select_quality_filters(None)

        self.assertEqual(clicked_count, 0)
        task.box_of_screen.assert_not_called()


class TestAuctionBidMode(unittest.TestCase):
    """新增出价模式: 每轮指定价格 / 按系统估价。"""

    def _task(self, **values) -> AutoBidAuctionTask:
        task = _make_task()
        task.config = _config(**values)
        return task

    # --- 每轮指定价格 ---
    @staticmethod
    def _prices(*values: int) -> dict[str, int]:
        """按出价顺序把价格映射到 6 个「第N次出价价格」配置项。"""
        return dict(zip(AutoBidAuctionTask.CONF_BID_PRICES, values))

    def test_each_round_uses_its_own_price(self):
        """6 次出价各自取自己的价格: 第1次 99999 / 第2次 123456 / 第3次 886 / 第4次 1314520。"""
        prices = (99999, 123456, 886, 1314520, 50000, 60000)
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_LIST,
                **self._prices(*prices),
            }
        )

        for bid_index, expected in enumerate(prices):
            with self.subTest(bid=bid_index + 1):
                task.current_bid_count = bid_index
                self.assertEqual(task._calculate_auction_price(), expected)

    def test_unset_round_reuses_previous_price(self):
        """只填前 3 次时, 后面的回合沿用第 3 次的价格。"""
        task = self._task(**self._prices(100, 200, 300))

        self.assertEqual(task._resolve_bid_prices(), [100, 200, 300, 300, 300, 300])

    def test_blank_round_in_the_middle_reuses_previous_price(self):
        """中间漏填的回合沿用上一次的价格, 而不是被跳过。"""
        task = self._task(**self._prices(100, 0, 300))

        self.assertEqual(task._resolve_bid_prices(), [100, 100, 300, 300, 300, 300])

    def test_first_bid_price_is_required(self):
        """第 1 次出价没有价格时无法出价, 必须在任务入口拦截。"""
        task = self._task(**self._prices(0, 500))

        self.assertEqual(task._resolve_bid_prices(), [])

    def test_bid_price_accepts_string_values(self):
        """配置界面可能把数字存成字符串, 解析要容忍。"""
        task = self._task(**self._prices("99999", "abc"))

        self.assertEqual(task._resolve_bid_prices(), [99999, 99999, 99999, 99999, 99999, 99999])

    def test_list_mode_reuses_last_price_beyond_configured_rounds(self):
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_LIST,
                **self._prices(100, 200),
            }
        )

        task.current_bid_count = 5
        self.assertEqual(task._calculate_auction_price(), 200)

    def test_list_mode_ignores_auto_raise(self):
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_LIST,
                AutoBidAuctionTask.CONF_AUTO_RAISE: True,
                AutoBidAuctionTask.CONF_RAISE_MODE: AutoBidAuctionTask.RAISE_MODE_MULTIPLE,
                AutoBidAuctionTask.CONF_RAISE_VALUE: "1.6",
                AutoBidAuctionTask.CONF_FIXED_PRICE: 1000,
                **self._prices(100),
            }
        )

        task.current_bid_count = 2
        self.assertEqual(task._calculate_auction_price(), 100)

    # --- 按系统估价 ---
    def test_estimate_mode_uses_estimate_times_ratio(self):
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_ESTIMATE,
                AutoBidAuctionTask.CONF_ESTIMATE_RATIO: "1.5",
                AutoBidAuctionTask.CONF_FIXED_PRICE: 1,
            }
        )
        task._read_estimate_value = Mock(return_value=(35301, False))

        self.assertEqual(task._calculate_auction_price(Mock(), None), 52952)

    def test_estimate_mode_falls_back_to_base_price(self):
        """估价被遮挡识别不到时不能中断整场拍卖, 只回退到基础价。"""
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_ESTIMATE,
                AutoBidAuctionTask.CONF_ESTIMATE_RATIO: "1",
                AutoBidAuctionTask.CONF_FIXED_PRICE: 777,
            }
        )
        task._read_estimate_value = Mock(return_value=(None, False))
        # 稳定读取会重试到 ESTIMATE_STABLE_TIMEOUT, 用假时钟避免测试真的等 10 秒。
        clock = _FakeTime()
        task.sleep = Mock(side_effect=clock.sleep)

        with patch.object(auction_module, "time", clock):
            self.assertEqual(task._calculate_auction_price(Mock(), None), 777)

    def test_custom_mode_keeps_legacy_behavior(self):
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_CUSTOM,
                AutoBidAuctionTask.CONF_FIXED_PRICE: 1000,
                AutoBidAuctionTask.CONF_AUTO_RAISE: False,
            }
        )

        task.current_bid_count = 3
        self.assertEqual(task._calculate_auction_price(), 1000)

    # --- 入口校验 ---
    def test_validate_rejects_missing_first_bid_price(self):
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_LIST,
                **self._prices(0, 200),
            }
        )

        with self.assertRaises(ValueError):
            task._validate_price_config()

    def test_validate_accepts_list_mode_without_base_price(self):
        """每轮指定价格不使用基础价, 基础价非法不应拦住任务。"""
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_LIST,
                AutoBidAuctionTask.CONF_FIXED_PRICE: "abc",
                **self._prices(100, 200, 300, 400, 500, 600),
            }
        )

        task._validate_price_config()

    # --- 第 6 次必须高于第 5 次 ---
    def test_validate_rejects_sixth_bid_lower_than_fifth(self):
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_LIST,
                **self._prices(100, 200, 300, 400, 500, 400),
            }
        )

        with self.assertRaises(ValueError):
            task._validate_price_config()

    def test_validate_rejects_sixth_bid_equal_to_fifth(self):
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_LIST,
                **self._prices(100, 200, 300, 400, 500, 500),
            }
        )

        with self.assertRaises(ValueError):
            task._validate_price_config()

    def test_validate_rejects_unset_sixth_bid(self):
        """第 6 次留 0 会沿用第 5 次的价格, 与第 5 次相等, 同样要被拦截。"""
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_LIST,
                **self._prices(100, 200, 300),
            }
        )

        with self.assertRaises(ValueError) as ctx:
            task._validate_price_config()

        # 报错必须点名是哪个配置项没填, 否则用户不知道该改哪里。
        self.assertIn(AutoBidAuctionTask.CONF_BID_PRICES[5], str(ctx.exception))

    def test_validate_accepts_sixth_bid_higher_than_fifth(self):
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_LIST,
                **self._prices(100, 200, 300, 400, 500, 501),
            }
        )

        task._validate_price_config()

    def test_ladder_rule_only_applies_to_the_last_bid(self):
        """规则只针对最后一次出价, 前面的回合允许相等或任意填写。"""
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_LIST,
                **self._prices(100, 100, 100, 100, 100, 101),
            }
        )

        task._validate_price_config()

    def test_validate_rejects_non_positive_ratio(self):
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_ESTIMATE,
                AutoBidAuctionTask.CONF_ESTIMATE_RATIO: "0",
            }
        )

        with self.assertRaises(ValueError):
            task._validate_price_config()

    def test_validate_rejects_invalid_base_price_in_custom_mode(self):
        task = self._task(**{AutoBidAuctionTask.CONF_FIXED_PRICE: 0})

        with self.assertRaises(ValueError):
            task._validate_price_config()

    def test_validate_rejects_invalid_raise_value_when_auto_raise_on(self):
        """自动加价开启时, 加价数值非法必须拦下任务而不是静默按 0 处理。

        校验若用 _config_float, 非法字符串会回退成 0.0 顺利通过; 运行时
        _config_decimal 同样回退 0 —— 自定义/百分比模式每次出价都按基础价,
        零告警, 用户以为配了加价实际没生效。
        """
        for raise_mode in AutoBidAuctionTask.RAISE_MODES:
            for raw in ("abc", ""):
                with self.subTest(mode=raise_mode, value=raw):
                    task = self._task(
                        **{
                            AutoBidAuctionTask.CONF_FIXED_PRICE: 1000,
                            AutoBidAuctionTask.CONF_AUTO_RAISE: True,
                            AutoBidAuctionTask.CONF_RAISE_MODE: raise_mode,
                            AutoBidAuctionTask.CONF_RAISE_VALUE: raw,
                        }
                    )
                    with self.assertRaises(ValueError):
                        task._validate_price_config()

    def test_validate_accepts_decimal_raise_value_with_whitespace(self):
        """合法的小数加价数值(带首尾空格)不能被收紧后的校验误拦。"""
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 1000,
                AutoBidAuctionTask.CONF_AUTO_RAISE: True,
                AutoBidAuctionTask.CONF_RAISE_VALUE: " 1.6 ",
            }
        )

        task._validate_price_config()

    def test_validate_rejects_non_positive_raise_value_when_auto_raise_on(self):
        """加价数值为 0 或负数时必须在入口拦下。

        它们能通过 is_finite 校验, 运行时每口出价都触发回退告警(倍率模式偶数次偏移
        还会先算出天文数字再回退); 加价的语义就是往上加, 非法配置不该等到出价阶段
        才以每轮告警的方式暴露。
        """
        for raise_mode in AutoBidAuctionTask.RAISE_MODES:
            for raw in ("0", "-1"):
                with self.subTest(mode=raise_mode, value=raw):
                    task = self._task(
                        **{
                            AutoBidAuctionTask.CONF_FIXED_PRICE: 1000,
                            AutoBidAuctionTask.CONF_AUTO_RAISE: True,
                            AutoBidAuctionTask.CONF_RAISE_MODE: raise_mode,
                            AutoBidAuctionTask.CONF_RAISE_VALUE: raw,
                        }
                    )
                    with self.assertRaises(ValueError):
                        task._validate_price_config()

    def test_estimate_mode_warns_when_fallback_base_price_is_invalid(self):
        """估价模式下「基础价」只作回退价, 非正整数不拦启动但必须提前告警。

        回退价非法时, 估价一旦读不出, 那次出价会以「非法价格」连续失败 3 次丢掉
        整轮 —— 用户只在日志里看到出价失败, 看不出是配置问题。正常配置下(估价
        可读)基础价根本用不到, 拦启动会误伤能正常跑的配置, 所以只告警。
        """
        for raw in (0, "abc", None):
            with self.subTest(value=raw):
                task = self._task(
                    **{
                        AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_ESTIMATE,
                        AutoBidAuctionTask.CONF_ESTIMATE_RATIO: "1",
                        AutoBidAuctionTask.CONF_FIXED_PRICE: raw,
                    }
                )

                task._validate_price_config()

                self.assertTrue(task.log_warning.called)

    def test_estimate_mode_is_quiet_with_a_valid_fallback_base_price(self):
        """默认基础价(1)是合法回退价, 不能误报告警。"""
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_BID_MODE: AutoBidAuctionTask.BID_MODE_ESTIMATE,
                AutoBidAuctionTask.CONF_ESTIMATE_RATIO: "1",
                AutoBidAuctionTask.CONF_FIXED_PRICE: 1,
            }
        )

        task._validate_price_config()

        self.assertFalse(task.log_warning.called)


class TestAuctionCursorRestore(unittest.TestCase):
    """后台执行时点击必须还原鼠标位置, 否则鼠标会留在游戏窗口内。"""

    def test_confirm_bid_price_uses_cursor_restoring_click(self):
        task = _make_task()
        # 第一次给「确认出价」, 第二次给弹窗探测(未命中弹窗, 不该多点一次).
        task.wait_ocr = Mock(side_effect=[[Mock()], []])

        task._confirm_bid_price(Mock(), None)

        task.operate_click.assert_called_once()

    def test_wait_click_optional_uses_cursor_restoring_click(self):
        task = _make_task()
        task.wait_ocr = Mock(return_value=[Mock()])

        self.assertTrue(task._wait_click_optional(Mock(), RE_CANCEL, None, 3, "取消按钮"))
        task.operate_click.assert_called_once()

    def test_wait_operate_click_returns_false_without_target(self):
        task = _make_task()
        task.wait_ocr = Mock(return_value=[])

        self.assertFalse(task._wait_operate_click(Mock(), RE_CLAIM, 3))
        task.operate_click.assert_not_called()


class TestAuctionSellModeGuard(unittest.TestCase):
    """进入出售模式与品质勾选都要校验, 否则会点错按钮并谎报成功。

    仓库初始视图的「出售」圆钮与出售模式的「取消」圆钮位置相同, 上一次出售中途失败会
    把仓库留在出售模式; 此时再点一次「出售」等于取消, 后续品质勾选与确认出售全部落空,
    日志却仍会打印「藏品出售完成」。
    """

    def _task(self, sell_mode: bool) -> AutoBidAuctionTask:
        task = _make_task()
        task.wait_ocr = Mock(return_value=[Mock()])
        task._is_sell_mode = Mock(return_value=sell_mode)
        task._select_quality_filters = Mock(return_value=5)
        task._read_sell_value = Mock(return_value=12345)
        return task

    def test_already_in_sell_mode_skips_the_sell_button(self):
        task = self._task(sell_mode=True)
        boxes = Mock()

        self.assertTrue(task._sell_collections(boxes, None))

        clicked = [call.args[0] for call in task.operate_click.call_args_list]
        self.assertNotIn(boxes.sell, clicked)
        self.assertIn(boxes.confirm_sell, clicked)

    def test_initial_view_still_clicks_the_sell_button(self):
        task = self._task(sell_mode=False)
        boxes = Mock()

        self.assertTrue(task._sell_collections(boxes, None))

        clicked = [call.args[0] for call in task.operate_click.call_args_list]
        self.assertIn(boxes.sell, clicked)
        self.assertIn(boxes.confirm_sell, clicked)

    def test_sell_flow_reports_failure_when_no_quality_is_selected(self):
        """勾选没生效时不能再报告成功, 否则满仓问题会被日志掩盖。"""
        task = self._task(sell_mode=True)
        task._read_sell_value = Mock(return_value=0)

        self.assertFalse(task._sell_collections(Mock(), None))
        self.assertIn("出售", str(task.log_warning.call_args))

    def test_sell_mode_detection_reads_the_sell_value_label(self):
        task = _make_task()
        boxes = Mock()

        task.wait_ocr = Mock(return_value=[Mock()])
        self.assertTrue(task._is_sell_mode(boxes, 1))
        self.assertIs(task.wait_ocr.call_args.kwargs["box"], boxes.sell_label)

    def test_sell_mode_detection_returns_false_without_raising(self):
        task = _make_task()
        task.wait_ocr = Mock(return_value=[])

        self.assertFalse(task._is_sell_mode(Mock(), 1))
        self.assertFalse(task.wait_ocr.call_args.kwargs["raise_if_not_found"])


class TestAuctionQualitySelection(unittest.TestCase):
    """品质勾选是「勾选即出售」: 清单里的点选, 其余保留, 返回点击次数。"""

    def _task(self) -> AutoBidAuctionTask:
        return _make_task()

    def test_selection_clicks_every_quality_when_all_are_listed(self):
        task = self._task()

        self.assertEqual(task._select_quality_filters(None, AutoBidAuctionTask.QUALITY_KEYS), 6)
        self.assertEqual(task.operate_click.call_count, 6)

    def test_selection_uses_the_default_gap(self):
        task = self._task()

        task._select_quality_filters(None, ["品质紫"])

        task.sleep.assert_called_with(AutoBidAuctionTask.SELL_QUALITY_GAP)

    def test_selection_follows_the_panel_order(self):
        """清单顺序不影响点击顺序, 一律按面板上的品质顺序走。"""
        task = self._task()

        task._select_quality_filters(None, ["品质红", "品质白"])

        clicked = [tuple(call.args) for call in task.box_of_screen.call_args_list]
        self.assertEqual(
            clicked,
            [AutoBidAuctionTask.QUALITY_BOXES[0], AutoBidAuctionTask.QUALITY_BOXES[5]],
        )


class TestAuctionNoSellableQualityWarning(unittest.TestCase):
    """两个出售清单都为空 + 开了出售模式是自相矛盾的配置, 要在入场就说清楚。

    这种配置下出售会一直报成功却清不出空间, 满仓后每轮出价都失败, 提前告警更好排查。
    """

    def _task(self, mode: str, before=(), after=()) -> AutoBidAuctionTask:
        task = _make_task()
        task.config = _config(
            **{
                AutoBidAuctionTask.CONF_SELL_MODE: mode,
                AutoBidAuctionTask.CONF_SELL_BEFORE_WELFARE: list(before),
                AutoBidAuctionTask.CONF_SELL_AFTER_WELFARE: list(after),
            }
        )
        return task

    def test_warns_when_both_lists_are_empty(self):
        task = self._task(AutoBidAuctionTask.SELL_MODE_FULL)

        task._warn_if_no_sellable_quality()

        message = str(task.log_warning.call_args)
        self.assertIn(AutoBidAuctionTask.CONF_SELL_BEFORE_WELFARE, message)
        self.assertIn(AutoBidAuctionTask.CONF_SELL_AFTER_WELFARE, message)

    def test_stays_quiet_when_something_can_be_sold(self):
        task = self._task(AutoBidAuctionTask.SELL_MODE_FULL, ("品质白",))

        task._warn_if_no_sellable_quality()

        task.log_warning.assert_not_called()

    def test_after_list_alone_is_enough(self):
        """只在「已领完」清单里勾了品质也算有配置 —— 领满后照样会卖。"""
        task = self._task(AutoBidAuctionTask.SELL_MODE_FULL, (), ("品质紫",))

        task._warn_if_no_sellable_quality()

        task.log_warning.assert_not_called()

    def test_off_mode_never_warns(self):
        task = self._task(AutoBidAuctionTask.SELL_MODE_OFF)

        task._warn_if_no_sellable_quality()

        task.log_warning.assert_not_called()


class TestAuctionQualityListCleaning(unittest.TestCase):
    """脏配置(手改 JSON)不能把出售流程带崩。

    多选框的配置值本该是列表, 但用户可能手工改成字符串、数字或 null。清洗集中在
    _quality_list: 非序列一律当空清单, 未知名称与重复项直接丢掉 —— 否则会去点
    不存在的按钮, 或者在迭代 int / None 时直接抛 TypeError。
    """

    def _task(self, key: str, value) -> AutoBidAuctionTask:
        task = _make_task()
        task.config = _config(**{key: value})
        return task

    def test_dirty_values_are_treated_as_empty(self):
        for dirty in ("品质白", 5, None, {"品质白": 1}):
            with self.subTest(dirty=dirty):
                task = self._task(AutoBidAuctionTask.CONF_SELL_BEFORE_WELFARE, dirty)

                self.assertEqual(task._sell_qualities(), [])

    def test_unknown_names_are_dropped(self):
        task = self._task(AutoBidAuctionTask.CONF_SELL_BEFORE_WELFARE, ["品质不存在", "品质白"])

        self.assertEqual(task._sell_qualities(), ["品质白"])

    def test_duplicates_are_collapsed(self):
        """重复项会让同一个品质被点两次 —— 第二次是取消勾选。"""
        task = self._task(AutoBidAuctionTask.CONF_SELL_BEFORE_WELFARE, ["品质白", "品质白"])

        self.assertEqual(task._sell_qualities(), ["品质白"])

    def test_after_list_is_cleaned_too(self):
        task = _make_task()
        task.config = _config(
            **{
                AutoBidAuctionTask.CONF_SELL_BEFORE_WELFARE: "品质白",
                AutoBidAuctionTask.CONF_SELL_AFTER_WELFARE: ["品质紫", "品质不存在"],
            }
        )
        task._welfare_claims_today = 5
        task._welfare_daily_limit = 5

        self.assertEqual(task._sell_qualities(), ["品质紫"])


class TestAuctionOneClickSell(unittest.TestCase):
    """「拍卖成功一键出售」: 结算界面点游戏自带的按钮, 再点空白区域关掉「获得物品」提示。

    这条路径不碰藏品仓库, 所以既不做满仓检测, 也不筛品质 —— 它走的是另一条独立分支。
    """

    def _task(self, mode: str | None = None) -> AutoBidAuctionTask:
        task = _make_task()
        task.config = _config(
            **{
                AutoBidAuctionTask.CONF_SELL_MODE: (
                    AutoBidAuctionTask.SELL_MODE_ONE_CLICK if mode is None else mode
                )
            }
        )
        return task

    def test_clicks_the_button_then_the_blank_area(self):
        task = self._task()
        boxes = Mock()
        task._wait_operate_click = Mock(return_value=True)
        task.wait_ocr = Mock(return_value=[Mock()])

        task._sell_on_settlement_screen(boxes, auction_module.time.monotonic() + 60)

        task._wait_operate_click.assert_called_once()
        self.assertIs(task._wait_operate_click.call_args.args[0], boxes.one_click_sell)
        self.assertIs(task.operate_click.call_args.args[0], boxes.popup_blank)

    def test_detects_the_popup_without_clicking_its_text(self):
        """关闭要点空白区域, 不是点提示文字本身。"""
        task = self._task()
        boxes = Mock()
        task._wait_operate_click = Mock(return_value=True)
        task.wait_ocr = Mock(return_value=[Mock()])

        task._sell_on_settlement_screen(boxes, auction_module.time.monotonic() + 60)

        self.assertIs(task.wait_ocr.call_args.kwargs["box"], boxes.popup_close_hint)
        self.assertIs(task.wait_ocr.call_args.kwargs["match"], RE_POPUP_CLOSE_HINT)
        self.assertIsNot(task.operate_click.call_args.args[0], boxes.popup_close_hint)

    def test_button_regex_tolerates_the_dropped_first_glyph(self):
        """首字「一」是单笔画, 检测模型裁剪偏紧时会直接丢掉它, 只读出「键出售」。

        实测同一张 1920x1080 截图放大 2 倍能读出完整文字、放大 3/4 倍读不出 ——
        这是检测本身的抖动, 不是截图差异, 所以正则必须同时接受两种写法。
        """
        self.assertIsNotNone(RE_ONE_CLICK_SELL.search("一键出售"))
        self.assertIsNotNone(RE_ONE_CLICK_SELL.search("键出售"))

    def test_popup_regex_matches_the_close_hint(self):
        """只匹配「点击空白」: 整句较长, 尾部被认坏时仍要能命中。"""
        self.assertIsNotNone(RE_POPUP_CLOSE_HINT.search("点击空白区域关闭"))

    def test_missing_button_is_not_a_failure(self):
        """流拍时按钮不出现, 不该因此让整轮失败, 也不该去点空白区域。"""
        task = self._task()
        task._wait_operate_click = Mock(return_value=False)
        task.wait_ocr = Mock(return_value=[Mock()])

        task._sell_on_settlement_screen(Mock(), auction_module.time.monotonic() + 60)

        task.operate_click.assert_not_called()

    def test_missing_popup_warns_and_does_not_click(self):
        task = self._task()
        task._wait_operate_click = Mock(return_value=True)
        task.wait_ocr = Mock(return_value=[])

        task._sell_on_settlement_screen(Mock(), auction_module.time.monotonic() + 60)

        task.operate_click.assert_not_called()
        self.assertIn("获得物品", str(task.log_warning.call_args))

    def test_exhausted_deadline_skips_instead_of_raising(self):
        """结算已经完成, 时间不够只能跳过, 不能抛异常把整轮判成失败。"""
        task = self._task()
        task._wait_operate_click = Mock(return_value=True)

        task._sell_on_settlement_screen(Mock(), auction_module.time.monotonic() - 1)

        task._wait_operate_click.assert_not_called()
        task.operate_click.assert_not_called()
        self.assertIn("时间已用尽", str(task.log_warning.call_args))

    def test_runs_between_skip_animation_and_exit(self):
        """必须插在「跳过动画」之后、「退出拍卖」之前: 按钮只在结算界面存在。"""
        task = self._task()
        order: list[str] = []
        task.operate_click = Mock(side_effect=lambda *a, **kw: order.append("skip"))
        task._sell_on_settlement_screen = Mock(side_effect=lambda *a: order.append("sell"))
        task._wait_operate_click = Mock(side_effect=lambda *a, **kw: order.append("exit") or True)
        task.wait_ocr = Mock(return_value=[])

        task._finish_auction(Mock(), [Mock()], auction_module.time.monotonic() + 60)

        self.assertEqual(order, ["skip", "sell", "exit"])

    def test_other_modes_do_not_touch_the_settlement_screen(self):
        for mode in (
            AutoBidAuctionTask.SELL_MODE_OFF,
            AutoBidAuctionTask.SELL_MODE_FULL,
            AutoBidAuctionTask.SELL_MODE_INTERVAL,
        ):
            with self.subTest(mode=mode):
                task = self._task(mode)
                task._wait_operate_click = Mock(return_value=True)
                task.wait_ocr = Mock(return_value=[])
                task._sell_on_settlement_screen = Mock()

                task._finish_auction(Mock(), [Mock()], auction_module.time.monotonic() + 60)

                task._sell_on_settlement_screen.assert_not_called()

    def test_one_click_mode_does_not_use_the_warehouse_flow(self):
        """一键出售不碰仓库: 不做满仓检测, 也不在轮次末尾出售。"""
        task = self._task()
        task._sell_collections = Mock(return_value=True)
        task._detect_inventory_full = Mock(return_value=False)

        self.assertFalse(task._uses_collection_sell())

        task._sell_collections_on_interval(Mock())

        task._sell_collections.assert_not_called()
        task._detect_inventory_full.assert_not_called()

    def test_one_click_mode_never_sells_even_when_the_state_says_full(self):
        """即使调用点带着「满仓」状态进来, 也不能去卖仓库。

        「满仓卡住」那条重试分支是无条件调用 _sell_collections_on_interval 的(它不先问
        模式), 所以这条守卫只能靠函数自己拦 —— 拦不住就会在一键出售模式下突然跑去开仓库。
        """
        task = self._task()
        task._sell_collections = Mock(return_value=True)

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=True))

        task._sell_collections.assert_not_called()

    def test_one_click_mode_never_warns_about_empty_lists(self):
        """它不筛选品质, 所以空清单对它不是矛盾配置。"""
        task = _make_task()
        task.config = _config(
            **{
                AutoBidAuctionTask.CONF_SELL_MODE: AutoBidAuctionTask.SELL_MODE_ONE_CLICK,
                AutoBidAuctionTask.CONF_SELL_BEFORE_WELFARE: [],
                AutoBidAuctionTask.CONF_SELL_AFTER_WELFARE: [],
            }
        )

        task._warn_if_no_sellable_quality()

        task.log_warning.assert_not_called()


class TestAuctionSellValueCheck(unittest.TestCase):
    """勾完品质要读「出售价值」, 读到 0 说明点击全部落空。

    品质圆点每点一次界面都会重绘, 间隔太短会让后续点击落空, 表现为只卖掉一种品质;
    原流程不校验就直接点确认出售并报告成功, 满仓因此一直清不掉。

    「读不出」和「读到 0」要分开处理: 读不出是重绘空白态, 只能换帧重读, 重勾会把刚勾上
    的品质点掉(双重取反); 换帧后仍是 0 才说明这一遍真的一个都没勾上, 那时重勾一次是
    修正(仓库里残留着上一次异常退出留下的勾选, 被这一遍全点掉了), 不是取反。
    """

    def _task(self, values) -> AutoBidAuctionTask:
        task = _make_task()
        task._read_sell_value = Mock(side_effect=list(values))
        task._select_quality_filters = Mock(return_value=5)
        return task

    def test_positive_value_needs_no_retry(self):
        task = self._task([12345])

        self.assertEqual(task._ensure_sell_value(Mock(), None, 5), 12345)
        task._select_quality_filters.assert_not_called()
        task.log_warning.assert_not_called()

    def test_zero_value_is_retried_by_reading_again(self):
        task = self._task([0, 999])

        self.assertEqual(task._ensure_sell_value(Mock(), None, 5), 999)
        self.assertEqual(task._read_sell_value.call_count, 2)
        task._select_quality_filters.assert_not_called()
        task.log_warning.assert_called()

    def test_retry_switches_frame_before_reading_again(self):
        """必须换帧: 两次读取落在同一帧上会读到同样的空白值。"""
        task = self._task([0, 999])

        task._ensure_sell_value(Mock(), None, 5)

        task.next_frame.assert_called_once()

    def test_persistent_zero_reselects_once_and_gives_up(self):
        """换帧重读后仍是 0, 才按「残留勾选被这一遍点掉」处理, 重勾一次。

        仓库里残留着上一次异常退出留下的勾选时, 这一遍无条件点击会把它们全部点掉
        (出售价值读到 0); 再点一遍同一批品质能把状态拉回来。只重勾一次, 不无限重试。
        """
        task = self._task([0, 0, 0])

        self.assertEqual(task._ensure_sell_value(Mock(), None, 5), 0)
        self.assertEqual(task._select_quality_filters.call_count, 1)
        self.assertIn("重新勾选", str(task.log_warning.call_args))

    def test_reselect_recovers_the_value_when_the_warehouse_was_dirty(self):
        """残留勾选被点掉后, 重勾一次应当重新读到正数并判定生效。"""
        task = self._task([0, 0, 12345])

        self.assertEqual(task._ensure_sell_value(Mock(), None, 5), 12345)
        self.assertEqual(task._select_quality_filters.call_count, 1)

    def test_reselect_passes_the_sell_qualities(self):
        """重勾必须用同一批品质, 否则满仓放宽时会把要保留的品质重新勾回来。"""
        task = self._task([0, 0, 12345])

        task._ensure_sell_value(Mock(), None, 5, ["品质红"])

        self.assertEqual(task._select_quality_filters.call_args.args[1], ["品质红"])

    def test_unreadable_value_does_not_reselect(self):
        """读不出时只重读, 不重勾 —— 重勾会点成反选, 把已经勾上的品质取消掉。"""
        task = self._task([None, None])

        self.assertIsNone(task._ensure_sell_value(Mock(), None, 5))
        task._select_quality_filters.assert_not_called()
        task.log_warning.assert_called()

    def test_unreadable_value_is_retried_before_giving_up(self):
        """界面重绘期间数值区会短暂空白, 线上 4 次出售有 3 次是这样, 要重读一次。"""
        task = self._task([None, 4242])

        self.assertEqual(task._ensure_sell_value(Mock(), None, 5), 4242)
        self.assertEqual(task._read_sell_value.call_count, 2)
        task._select_quality_filters.assert_not_called()

    def test_nothing_selected_is_not_treated_as_failure(self):
        task = self._task([0])

        self.assertIsNone(task._ensure_sell_value(Mock(), None, 0))
        task._read_sell_value.assert_not_called()
        task.log_warning.assert_not_called()

    def test_sell_value_reader_reuses_the_asset_value_parser(self):
        task = _make_task()
        boxes = Mock()

        self.assertIsNone(task._read_sell_value(boxes, 1))
        self.assertIs(task.wait_ocr.call_args.kwargs["box"], boxes.sell_value)


class TestAuctionSelectionConfirmed(unittest.TestCase):
    """勾选是否生效必须三态判断: 读到正数 / 读到 0 / 读不出。

    线上 4 次出售有 3 次读不出「出售价值」(界面重绘中的空白态), 原判据
    `not (selected > 0 and value == 0)` 把 None 也算成功 —— 等于根本没有校验。
    """

    def test_positive_value_confirms_the_selection(self):
        self.assertTrue(AutoBidAuctionTask._is_selection_confirmed(5, 12345))

    def test_zero_value_means_the_selection_failed(self):
        self.assertFalse(AutoBidAuctionTask._is_selection_confirmed(5, 0))

    def test_unreadable_value_is_unconfirmed_not_success(self):
        self.assertIsNone(AutoBidAuctionTask._is_selection_confirmed(5, None))

    def test_nothing_selected_is_not_a_failure(self):
        self.assertTrue(AutoBidAuctionTask._is_selection_confirmed(0, None))

    def test_nothing_selected_is_a_failure_when_a_sale_is_required(self):
        """满仓时一个品质都没勾上, 不能算成功。

        否则会把满仓标记清掉, 仓库一件没腾却报告「藏品出售完成」, 之后每轮出价都失败。
        """
        self.assertFalse(AutoBidAuctionTask._is_selection_confirmed(0, None, require_sale=True))

    def test_require_sale_does_not_change_the_other_verdicts(self):
        self.assertTrue(AutoBidAuctionTask._is_selection_confirmed(5, 12345, require_sale=True))
        self.assertFalse(AutoBidAuctionTask._is_selection_confirmed(5, 0, require_sale=True))
        self.assertIsNone(AutoBidAuctionTask._is_selection_confirmed(5, None, require_sale=True))


class TestAuctionSellUnconfirmed(unittest.TestCase):
    """读不出出售价值时不能打印「藏品出售完成」。"""

    def _task(self) -> AutoBidAuctionTask:
        task = _make_task()
        task.wait_ocr = Mock(return_value=[Mock()])
        task._is_sell_mode = Mock(return_value=True)
        task._select_quality_filters = Mock(return_value=6)
        task._ensure_sell_value = Mock(return_value=None)
        return task

    def test_unconfirmed_sell_returns_none_not_failure(self):
        """读不出时必须返回 None(结果未知)而不是 False(确认失败)。

        False 会让上层把这次计入满仓失败并置 _inventory_stuck; 而确认出售在此之前
        已经点击, 出售可能已生效 —— 仓库实际清空后, 这个误置会让任务永远跳过拍卖
        去重试一个空仓库的清理。None 才能把「未知」交给上层与超时同等处理。
        """
        task = self._task()

        self.assertIsNone(task._sell_collections(Mock(), None))
        self.assertNotIn("藏品出售完成", str(task.log_info.call_args_list))

    def test_unconfirmed_sell_says_it_could_not_be_confirmed(self):
        """日志要区分「读不出」和「勾选没生效」, 否则又回到排查时看不出原因。

        这两条告警是支持流程唯一的信息来源(用户把日志发过来定位问题),
        文案本身就是契约。
        """
        task = self._task()

        task._sell_collections(Mock(), None)

        self.assertIn("未读出", str(task.log_warning.call_args))

    def test_zero_value_sell_says_nothing_was_cleared(self):
        task = self._task()
        task._ensure_sell_value = Mock(return_value=0)

        task._sell_collections(Mock(), None)

        self.assertIn("未生效", str(task.log_warning.call_args))

    def test_unconfirmed_sell_still_clicks_confirm_and_closes(self):
        """读不出也要把确认出售和关闭点掉, 否则会卡在出售模式影响下一次。"""
        task = self._task()
        boxes = Mock()

        task._sell_collections(boxes, None)

        clicked = [call.args[0] for call in task.operate_click.call_args_list]
        self.assertIn(boxes.confirm_sell, clicked)
        self.assertIn(boxes.close, clicked)

    def test_confirmed_sell_still_reports_success(self):
        task = self._task()
        task._ensure_sell_value = Mock(return_value=888)

        self.assertTrue(task._sell_collections(Mock(), None))
        self.assertIn("藏品出售完成", str(task.log_info.call_args_list))

    def test_required_sale_with_nothing_selected_does_not_claim_success(self):
        """满仓时一个品质都没勾上(保留列表覆盖全部品质), 不能报告出售完成。"""
        task = self._task()
        task._select_quality_filters = Mock(return_value=0)
        task._ensure_sell_value = Mock(return_value=None)

        self.assertFalse(task._sell_collections(Mock(), None, require_sale=True))
        self.assertNotIn("藏品出售完成", str(task.log_info.call_args_list))
        self.assertIn("没有勾选任何品质", str(task.log_warning.call_args))

    def test_optional_sale_with_nothing_selected_is_not_a_failure(self):
        """不要求出售时(未满仓), 没有可卖的东西不算失败, 也不谎报出售完成。"""
        task = self._task()
        task._select_quality_filters = Mock(return_value=0)
        task._ensure_sell_value = Mock(return_value=None)

        self.assertTrue(task._sell_collections(Mock(), None))
        self.assertNotIn("藏品出售完成", str(task.log_info.call_args_list))


class TestAuctionSellAbortClosesWarehouse(unittest.TestCase):
    """出售流程无论成功还是异常退出, 都必须把仓库关掉。

    把「出售模式 + 已勾选的品质」留给下一轮, 下次进来会检测到「已在出售模式」而跳过
    点「出售」, 然后无条件再点一遍同一批品质 —— 全部取反成未勾选; 满仓放宽时还会
    连带卖掉用户明确要保留的品质。关闭按钮无文字图标, 在主界面点它是空操作。
    """

    def _task(self, error: Exception) -> AutoBidAuctionTask:
        task = _make_task()
        task.wait_ocr = Mock(return_value=[Mock()])
        task._is_sell_mode = Mock(return_value=False)
        task._select_quality_filters = Mock(side_effect=error)
        return task

    def test_timeout_abort_still_closes_the_warehouse(self):
        task = self._task(WaitFailedException("单轮拍卖超时"))
        boxes = Mock()

        with self.assertRaises(WaitFailedException):
            task._sell_collections(boxes, None)

        self.assertIn(boxes.close, [call.args[0] for call in task.operate_click.call_args_list])

    def test_unexpected_error_abort_still_closes_the_warehouse(self):
        task = self._task(RuntimeError("boom"))
        boxes = Mock()

        self.assertFalse(task._sell_collections(boxes, None))

        self.assertIn(boxes.close, [call.args[0] for call in task.operate_click.call_args_list])


class TestAuctionWarehouseCloseRetry(unittest.TestCase):
    """点「关闭」必须确认仓库真的收起, 一次不成要重试。

    关闭按钮是无文字图标, 点击可能因为动画/焦点没生效; 而仓库连同「出售模式 + 已勾选的
    品质」留给下一轮时, 下次进来会检测到「已在出售模式」而跳过点「出售」, 然后无条件再点
    一遍同一批品质 —— 全部取反成未勾选, 满仓放宽时还会卖掉用户明确要保留的品质。
    """

    def setUp(self):
        self._boxes = Mock()

    def _task_with(self, open_readings):
        task = _make_task()
        task.operate_click = Mock()
        readings = list(open_readings)
        task._is_warehouse_open = Mock(
            side_effect=lambda boxes: readings.pop(0) if readings else False
        )
        return task

    def test_closes_on_first_click_without_retrying(self):
        task = self._task_with([False])

        task._close_warehouse(self._boxes)

        self.assertEqual(task.operate_click.call_count, 1)

    def test_retries_until_the_warehouse_is_gone(self):
        """前两次点击没生效时, 第三次关掉就应停止重试。"""
        task = self._task_with([True, True, False])

        task._close_warehouse(self._boxes)

        self.assertEqual(task.operate_click.call_count, 3)

    def test_gives_up_after_the_retry_limit_without_raising(self):
        """这里是异常收尾路径, 关不掉只能告警, 再抛会盖掉真正的失败原因。"""
        task = self._task_with([True] * 10)

        task._close_warehouse(self._boxes)

        self.assertEqual(
            task.operate_click.call_count,
            AutoBidAuctionTask.WAREHOUSE_CLOSE_RETRIES,
        )
        self.assertTrue(task.log_warning.called)


class TestAuctionSellFailureEscalation(unittest.TestCase):
    """出售连续失败要升级处理: 满仓卖不掉会让后续出价全部失败。"""

    def _task(self, results) -> AutoBidAuctionTask:
        task = _make_task()
        task._sell_collections = Mock(side_effect=list(results))
        return task

    def test_success_resets_the_failure_counter(self):
        task = self._task([False, True])
        task._sell_failures = AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER - 1
        task._inventory_stuck = True

        self.assertTrue(
            task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        )
        self.assertEqual(task._sell_failures, 0)
        self.assertFalse(task._inventory_stuck)

    def test_reaching_the_threshold_starts_with_the_escalated_set(self):
        """计数已达阈值时直接用放宽集合开局, 不再先白试一次未放宽的。

        满仓时第一次调用就可能耗尽出售预算并抛异常, 若仍先试一次未放宽的, 放宽分支
        永远走不到, 计数累到阈值也没有用。
        """
        task = self._task([True])
        task._sell_failures = AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER
        task._inventory_stuck = True

        self.assertTrue(
            task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        )
        self.assertEqual(task._sell_collections.call_count, 1)
        self.assertEqual(
            set(task._sell_collections.call_args.args[2]),
            set(AutoBidAuctionTask.QUALITY_KEYS),
        )
        self.assertEqual(task._sell_failures, 0)
        self.assertFalse(task._inventory_stuck)

    def test_sell_timeout_does_not_feed_the_escalation_counter(self):
        """出售超出预算时结果未知, 既不计入放宽计数也不置 _inventory_stuck。

        超时点无法区分在「确认出售」之前还是之后: 收尾的 _bounded_sleep 在点完
        confirm_sell 之后也会抛, 那次出售可能已经生效。误计会让放宽提前触发(卖掉用户
        明确保留的品质), 误置 _inventory_stuck 会让下一轮跳过拍卖并记一次失败。
        """
        task = self._task([WaitFailedException("藏品出售超出预算")])
        task._sell_failures = 0
        task._inventory_stuck = False

        with self.assertRaises(WaitFailedException):
            task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)

        self.assertEqual(task._sell_collections.call_count, 1)
        self.assertEqual(task._sell_failures, 0)
        self.assertFalse(task._inventory_stuck)

    def test_failure_below_threshold_does_not_escalate(self):
        task = self._task([False])

        self.assertFalse(
            task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        )
        self.assertEqual(task._sell_collections.call_count, 1)
        self.assertTrue(task._inventory_stuck)

    def test_repeated_failure_escalates_by_dropping_kept_qualities(self):
        """满仓时把仓库腾空优先于保留配置里的品质。"""
        task = self._task([False, True])
        task._sell_failures = AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER - 1

        self.assertTrue(
            task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        )

        escalated = task._sell_collections.call_args.args[2]
        self.assertEqual(set(escalated), set(AutoBidAuctionTask.QUALITY_KEYS))

    def test_escalation_keeps_the_extra_qualities(self):
        task = self._task([False, True])
        task._sell_failures = AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER - 1

        task._sell_collections_with_escalation(Mock(), None, ["品质红"], inventory_full=True)

        escalated = task._sell_collections.call_args.args[2]
        self.assertIn("品质红", escalated)

    def test_escalated_confirmed_zero_restores_the_auction(self):
        """放宽集合确认读数为 0 = 仓库无可卖藏品, 满仓前提失效, 必须复位恢复拍卖。

        活锁回归: 上一轮出售结果未确认(实际已清空仓库)会让 _inventory_stuck 置位,
        之后每轮跳过拍卖重试清理, 而空仓库的出售价值永远读到 0, 满仓标记永远清不掉
        —— 任务从此不再拍卖, 每轮记一次「满仓未清理」失败。唯一的出路是把
        「全品质勾选 + 要求出售 + 确认读到 0」当作满仓结论已被推翻的证据。
        """
        task = self._task([False])
        task._sell_failures = AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER
        task._inventory_stuck = True

        self.assertFalse(
            task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        )
        # 直接以放宽集合开局, 确认 0 后复位: 下一轮不再跳过拍卖。
        self.assertEqual(task._sell_collections.call_count, 1)
        self.assertFalse(task._inventory_stuck)
        # 没有东西可卖不是清理失败, 不推进放宽计数。
        self.assertEqual(task._sell_failures, AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER)

    def test_unconfirmed_outcome_does_not_feed_the_counter_but_keeps_cleanup(self):
        """结果未知(None)与超时同等对待: 不计数, 但满仓时仍置位让下一轮重试清理。

        确认出售在返回前就可能已经点击, 结果未知; 无证据的失败不该驱动放宽(会把
        要保留的品质一起卖掉)。可仓库若真的还满, 不置位的话下一轮会去空烧匹配阶段。
        """
        task = self._task([None])

        self.assertFalse(
            task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        )
        self.assertEqual(task._sell_failures, 0)
        self.assertTrue(task._inventory_stuck)

    def test_unconfirmed_outcome_does_not_stick_when_not_full(self):
        task = self._task([None])

        self.assertFalse(
            task._sell_collections_with_escalation(Mock(), None, (), inventory_full=False)
        )
        self.assertEqual(task._sell_failures, 0)
        self.assertFalse(task._inventory_stuck)

    def test_escalated_retry_unconfirmed_keeps_stuck_for_another_cleanup(self):
        """放宽后的结果同样未知: 首次失败证据已计数, 置位等下一轮的确认读数。"""
        task = self._task([False, None])
        task._sell_failures = AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER - 1

        self.assertFalse(
            task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        )
        self.assertEqual(task._sell_failures, AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER)
        self.assertTrue(task._inventory_stuck)

    def test_unconfirmed_sale_then_empty_warehouse_finally_restores_the_auction(self):
        """端到端活锁回归: 未确认出售 → 空仓库反复确认 0 → 满仓标记必须复位。

        修复前的序列是: 未确认出售被计为失败并置位 → 空仓库每轮确认 0 → 计数涨到
        阈值后放宽开局 → 放宽确认 0 仍置位 → 永远跳过拍卖。修复后第三步起读到的
        确认 0 会把满仓前提推翻, 任务恢复正常拍卖。
        """
        task = self._task([None, False, False, False])

        # 第 1 轮: 出售结果未确认(实际已清空仓库) -> 跳过下一轮拍卖去清理。
        task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        self.assertTrue(task._inventory_stuck)

        # 第 2 轮: 空仓库, 未放宽清单确认读到 0 -> 仍按满仓失败计数。
        task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        self.assertTrue(task._inventory_stuck)

        # 第 3 轮: 先按未放宽清单确认 0(计满阈值), 同一轮内放宽重试再确认 0
        # -> 满仓前提失效, 恢复拍卖。
        task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        self.assertFalse(task._inventory_stuck)

    def test_not_full_does_not_mark_the_inventory_as_stuck(self):
        """没满仓时出售失败不该让下一轮跳过拍卖。"""
        task = self._task([False])

        task._sell_collections_with_escalation(Mock(), None, (), inventory_full=False)

        self.assertFalse(task._inventory_stuck)

    def test_full_warehouse_requires_the_sale_to_succeed(self):
        """满仓时要把 require_sale 传下去, 否则「一件没卖」会被当成成功。

        满仓清理报成功后 _inventory_stuck 被清掉, 下一轮不会再重试清理, 而出价
        在满仓下必然失败 —— 只能靠这条把「没清掉」如实报成失败。
        """
        task = self._task([False])

        task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)

        self.assertTrue(task._sell_collections.call_args.kwargs["require_sale"])

    def test_escalated_attempt_also_requires_the_sale(self):
        task = self._task([False, True])
        task._sell_failures = AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER - 1

        task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)

        self.assertTrue(task._sell_collections.call_args.kwargs["require_sale"])

    def test_not_full_does_not_require_the_sale(self):
        task = self._task([False])

        task._sell_collections_with_escalation(Mock(), None, (), inventory_full=False)

        self.assertFalse(task._sell_collections.call_args.kwargs["require_sale"])

    def test_not_full_never_drops_the_kept_qualities(self):
        """非满仓的失败多半是界面读数抖动, 不该白白卖掉用户明确要保留的品质。"""
        task = self._task([False, False])
        task._sell_failures = AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER - 1

        self.assertFalse(
            task._sell_collections_with_escalation(Mock(), None, (), inventory_full=False)
        )
        # 只应尝试一次, 没有放宽后的第二次调用.
        self.assertEqual(task._sell_collections.call_count, 1)

    def test_not_full_failure_does_not_feed_the_escalation_counter(self):
        """未满仓的失败不该计入放宽计数, 否则满仓的首次失败就会立刻放开放宽。

        阈值 SELL_FAILURE_ESCALATE_AFTER 的含义是「满仓**连续**失败几次后放宽」。
        若非满仓的读数抖动也往上累积, 阈值会被历史抖动提前填满, 之后满仓第一次失败
        就触发 escalated = extra_sell | QUALITY_KEYS —— 6 个品质全卖, 包含用户
        明确保留的那些。实测修复前: 非满仓失败 5 次后紧接一次满仓失败即放宽。
        """
        task = self._task([False, False, True])
        task._sell_failures = AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER - 1

        task._sell_collections_with_escalation(Mock(), None, (), inventory_full=False)

        self.assertEqual(task._sell_failures, AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER - 1)
        self.assertFalse(task._inventory_stuck)

    def test_not_full_failures_after_the_threshold_keep_the_escalated_start(self):
        """已达阈值后发生非满仓失败, 下一次满仓出售仍以放宽集合开局。

        钉住现状: 非满仓失败对计数既不累加也不清零 (口径是「满仓连续失败」)。
        若将来把口径改成「非满仓失败清零计数」, 这条会红 —— 届时需要先确认
        「满仓失败 → 手动清仓 → 长时间抖动 → 再次满仓」场景愿意从零重新累计,
        不能顺手改掉。
        """
        task = self._task([False, True])
        task._sell_failures = AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER

        task._sell_collections_with_escalation(Mock(), None, (), inventory_full=False)
        self.assertEqual(task._sell_failures, AutoBidAuctionTask.SELL_FAILURE_ESCALATE_AFTER)

        self.assertTrue(
            task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        )
        # 第 1 次调用是非满仓尝试(未放宽), 第 2 次满仓直接以放宽集合开局, 不再有第三次。
        self.assertEqual(task._sell_collections.call_count, 2)
        self.assertEqual(
            set(task._sell_collections.call_args.args[2]),
            set(AutoBidAuctionTask.QUALITY_KEYS),
        )

    def test_full_failure_starting_from_zero_still_escalates_after_the_threshold(self):
        """满仓连续失败到阈值仍必须放宽, 反写计数语义不能把这条能力一起拆掉。"""
        task = self._task([False, False, True])

        task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        self.assertEqual(task._sell_failures, 1)
        # 满仓且没卖成 -> 下一轮要先跳过拍卖去清理仓库.
        self.assertTrue(task._inventory_stuck)

        self.assertTrue(
            task._sell_collections_with_escalation(Mock(), None, (), inventory_full=True)
        )
        escalated = task._sell_collections.call_args.args[2]
        self.assertEqual(set(escalated), set(AutoBidAuctionTask.QUALITY_KEYS))
        self.assertEqual(task._sell_failures, 0)


class TestAuctionNoticePopup(unittest.TestCase):
    """提示类弹窗(入场费确认/异常出价/满仓提示)共用一套模板, 一个区域兜住。"""

    def test_popup_is_clicked_when_the_confirm_button_is_seen(self):
        task = _make_task()
        task.wait_ocr = Mock(return_value=[Mock()])
        boxes = Mock()

        self.assertTrue(task._dismiss_notice_popup(boxes, None, "测试"))
        self.assertIs(task.wait_ocr.call_args.kwargs["box"], boxes.exception_area)
        task.operate_click.assert_called_once_with(boxes.exception_area, after_sleep=0.3)

    def test_popup_probe_uses_the_given_budget(self):
        """每次出价都要查一遍, 热路径要能用更小的预算。"""
        task = _make_task()
        task.wait_ocr = Mock(return_value=[])

        task._dismiss_notice_popup(Mock(), None, "测试", timeout=0.5)

        self.assertEqual(task.wait_ocr.call_args.kwargs["time_out"], 0.5)

    def test_expired_deadline_skips_the_probe(self):
        task = _make_task()
        task.wait_ocr = Mock(return_value=[Mock()])

        expired = auction_module.time.monotonic() - 1
        self.assertFalse(task._dismiss_notice_popup(Mock(), expired, "测试"))
        task.wait_ocr.assert_not_called()


class TestAuctionInventoryFullProbe(unittest.TestCase):
    """满仓检测是可选观测步骤, 没有时间时不能抛异常拖垮结算后处理。"""

    def test_expired_deadline_returns_none_without_raising(self):
        """没时间时必须返回 None(未检测), 不能返回 False(确定没满仓)。

        返回 False 会被写进 PostRoundState, 轮次末尾的 _sell_collections_on_interval
        看到「不是 None」就不再补测 —— 满仓被静默漏掉, 之后每轮都卡在「开始匹配」上。
        """
        task = _make_task()
        task.wait_ocr = Mock(return_value=[Mock()])

        self.assertIsNone(task._detect_inventory_full(Mock(), 0))
        task.wait_ocr.assert_not_called()

    def test_detects_the_insufficient_banner(self):
        task = _make_task()
        task.wait_ocr = Mock(return_value=[Mock()])

        self.assertTrue(task._detect_inventory_full(Mock(), 3))

    def test_not_full_is_false_not_none(self):
        """有时间但没读到提示时是「确定没满仓」, 必须返回 False 而不是 None。"""
        task = _make_task()
        task.wait_ocr = Mock(return_value=[])

        self.assertFalse(task._detect_inventory_full(Mock(), 3))

    def test_round_end_probe_covers_undetected_state(self):
        """结算后没测出结论时, 轮次末尾必须补测一次, 否则满仓会被漏掉。"""
        task = _make_task({AutoBidAuctionTask.CONF_SELL_MODE: AutoBidAuctionTask.SELL_MODE_FULL})
        task._detect_inventory_full = Mock(return_value=True)
        task._sell_collections_with_escalation = Mock(return_value=True)

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=None))

        task._detect_inventory_full.assert_called_once()
        task._sell_collections_with_escalation.assert_called_once()

    def test_round_end_probe_skips_known_state(self):
        """已经有结论时不该重复 OCR。"""
        task = _make_task({AutoBidAuctionTask.CONF_SELL_MODE: AutoBidAuctionTask.SELL_MODE_FULL})
        task._detect_inventory_full = Mock(return_value=True)
        task._sell_collections_with_escalation = Mock(return_value=True)

        task._sell_collections_on_interval(Mock(), state=PostRoundState(inventory_full=False))

        task._detect_inventory_full.assert_not_called()
        task._sell_collections_with_escalation.assert_not_called()


class TestAuctionInventoryStuckRound(unittest.TestCase):
    """满仓且出售未成功时不该继续空转出价。"""

    def _task(self) -> AutoBidAuctionTask:
        task = _make_task()
        task._exec_auction_round = Mock(return_value=True)
        task.add_success = Mock()
        task.add_failed = Mock()
        task._sell_collections_on_interval = Mock()
        # current_round 是只读属性, 读的是 _round_state.index.
        task._round_state = Mock(index=3, total_text="10")
        return task

    def test_stuck_inventory_skips_the_auction_round(self):
        task = self._task()
        task._inventory_stuck = True

        task._run_single_round(Mock())

        task._exec_auction_round.assert_not_called()
        task.add_failed.assert_called_once()
        task._sell_collections_on_interval.assert_called_once()

    def test_stuck_retry_does_not_require_detecting_full_again(self):
        """满仓是上一轮已经判定过的结论, 重试清理不该再赌一次 OCR。

        满仓提示条会被弹窗遮住, 重新检测失败就什么都不做, 于是每轮都走这条分支却
        一件藏品都清不掉, 变成无限跳过拍卖。
        """
        task = self._task()
        task._inventory_stuck = True
        task._sell_collections_with_escalation = Mock(return_value=True)
        task._detect_inventory_full = Mock(return_value=None)

        task._run_single_round(Mock())

        state = task._sell_collections_on_interval.call_args.kwargs["state"]
        self.assertTrue(state.inventory_full)
        # 上面那条只断言了「调用方传了什么」—— 传了 True 不等于被调方真的没再去检测.
        # 「不该再赌一次 OCR」这条意图必须靠下面的断言钉住: 预置结论已足够,
        # _sell_collections_on_interval 不能再调 _detect_inventory_full 补测一次.
        task._detect_inventory_full.assert_not_called()

    def test_stuck_retry_sells_with_the_round_budget(self):
        """轮次末尾的出售必须带上自己的预算, 不能是无界调用。

        两处调用原本都不传 deadline, _bounded_timeout(deadline=None, ...) 会原样返回
        limit, 于是整条出售流程不受任何上级预算约束(逐分支等待下限约 35 秒,
        连续失败放宽时约 71 秒), 而它不消耗 ROUND_TIMEOUT —— 「满仓时清理」每轮都走,
        仓库入口读不到时表现为「任务在跑但几乎不出价」。
        """
        task = self._task()
        task._inventory_stuck = True

        task._run_single_round(Mock())

        deadline = task._sell_collections_on_interval.call_args[0][1]
        self.assertIsNotNone(deadline)
        self.assertGreater(deadline, 0)

    def test_sell_timeout_does_not_fail_the_round(self):
        """出售超预算只放弃出售, 不能把本轮已记下的结果改掉。"""
        task = self._task()
        task._post_round_state = PostRoundState(observed=True)
        task._inventory_stuck = False
        task._exec_auction_round = Mock(return_value=True)
        task._sell_collections_on_interval = Mock(
            side_effect=WaitFailedException("藏品出售超出预算")
        )

        task._run_single_round(Mock())

        task.add_success.assert_called_once()
        task.add_failed.assert_not_called()

    def test_normal_round_still_runs_the_auction(self):
        task = self._task()
        task._inventory_stuck = False

        task._run_single_round(Mock())

        task._exec_auction_round.assert_called_once()
        task.add_success.assert_called_once()

    def test_normal_round_does_not_sell_without_an_observation(self):
        """结算后处理没跑到时画面状态未知, 不能去点仓库入口白等超时。"""
        task = self._task()
        task._inventory_stuck = False

        task._run_single_round(Mock())

        task._sell_collections_on_interval.assert_not_called()

    def test_normal_round_sells_after_a_successful_observation(self):
        task = self._task()
        task._inventory_stuck = False

        def exec_round(_boxes):
            # 模拟 _finish_auction 走完结算后观测再返回.
            task._post_round_state = PostRoundState(inventory_full=True, observed=True)
            return True

        task._exec_auction_round = Mock(side_effect=exec_round)

        task._run_single_round(Mock())

        task._sell_collections_on_interval.assert_called_once()


class TestAuctionEstimateStableRead(unittest.TestCase):
    """当前估价在界面刚出现时会跳动几次, 第一次识别到的不是最终值。

    用户反馈: 估价会跳动几个值, 第一次识别到的不是当前估价。
    单次 OCR 一命中就返回, 会把跳动中的中间值当成估价, 按它出价。
    """

    def setUp(self):
        self.clock = _FakeTime()
        patcher = patch.object(auction_module, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _task(self, values) -> AutoBidAuctionTask:
        task = _make_task()
        task.sleep = Mock(side_effect=self.clock.sleep)
        # 读数序列用完后一直重复最后一个值, 模拟"跳完就稳定"。
        # 桩点打在 _read_estimate_value 上: _read_stable_asset_value 现在通过它读值,
        # 返回值是 (值, 是否贴边) 二元组。这里统一给不贴边。
        task._read_estimate_value = Mock(
            side_effect=[(v, False) for v in list(values) + [list(values)[-1]] * 20]
        )
        return task

    def test_returns_only_after_consecutive_identical_reads(self):
        task = self._task([100, 200, 300, 300, 300])

        self.assertEqual(task._read_stable_asset_value(Mock(), 10, "当前估价"), 300)
        # 300 连续 3 次相同之后还要满足最短观察窗口, 所以读取次数多于 5 次。
        self.assertGreaterEqual(task._read_estimate_value.call_count, 5)

    def test_refetches_a_frame_between_reads(self):
        """不换帧时两次读取会落在同一帧上, 读到同样的中间值, 白等。"""
        task = self._task([100, 200, 300, 300, 300])

        task._read_stable_asset_value(Mock(), 10, "当前估价")

        # 每次重读前都要换帧, 采用的那一次不再换帧。
        self.assertEqual(task.next_frame.call_count, task._read_estimate_value.call_count - 1)

    def test_value_is_not_used_before_the_minimum_observe_window(self):
        """中间值会稳定停留到面板打开后 2.6 秒, 观察窗口不够长就会采信它。

        线上日志: 19:16 那局的中间值 35,365 一直持续到 2.63 秒才变成真值 37,979;
        19:18 那局恰好在 2.63 秒返回, 拿到了残缺的 197(同局真实估价数万)。
        """
        task = self._task([197, 197, 197, 5197, 5197, 5197])

        value = task._read_stable_asset_value(Mock(), 10, "当前估价", skip_zero=True)

        self.assertEqual(value, 5197)

    def test_leading_digit_dropped_by_ocr_is_not_used(self):
        """估价数字右对齐, OCR 会漏读最左侧首位数字, 且漏读状态因画面静止而连续出现。

        线上日志: 真实 `6,486` 的读数依次是 6486 -> 486 -> 486 -> 486(被采用);
        真实 `22,778` 的读数在 22,778 / 2,778 之间交替, 攒不满连续相同而超时。
        漏读只会让位数变少, 所以位数更少的读数不能顶掉已经读到的完整值。
        """
        task = self._task([6486, 486, 486, 486])

        self.assertEqual(task._read_stable_asset_value(Mock(), 10, "当前估价"), 6486)

    def test_unreadable_reads_do_not_discard_a_good_value(self):
        """残缺读数被过滤成未读出后, 之前读到的完整值仍然要保留并采用。

        线上日志 18:56 那局: 真实 `6,544`, 读数序列是 ",544" 连着 9 次加 "6,544" 两次。
        按「连续相同」判定会超时, 最后拿残缺的 544 出价。
        """
        task = self._task([None, None, 6544, None, None, None])

        value = task._read_stable_asset_value(Mock(), 10, "当前估价", skip_zero=True)

        self.assertEqual(value, 6544)

    def test_partial_number_text_is_detected(self):
        """千位分隔符前面空着说明首位数字被漏读, 这类文本不是完整读数。"""
        self.assertTrue(AutoBidAuctionTask._is_partial_number_text(",544"))
        self.assertTrue(AutoBidAuctionTask._is_partial_number_text("：,523"))
        self.assertFalse(AutoBidAuctionTask._is_partial_number_text("5,734"))
        self.assertFalse(AutoBidAuctionTask._is_partial_number_text("486"))

    def test_inconsistent_grouping_is_detected(self):
        """逗号位置不合千位规则说明 OCR 丢了或多读了字符。

        拦不住无逗号的 `643`(天然自洽), 所以它只是辅助防线, 主要防线是贴边告警。
        """
        self.assertTrue(AutoBidAuctionTask._has_inconsistent_grouping("1,23"))
        self.assertTrue(AutoBidAuctionTask._has_inconsistent_grouping("12,3,456"))
        self.assertFalse(AutoBidAuctionTask._has_inconsistent_grouping("1,234"))
        self.assertFalse(AutoBidAuctionTask._has_inconsistent_grouping("22,684"))
        self.assertFalse(AutoBidAuctionTask._has_inconsistent_grouping("643"))
        self.assertFalse(AutoBidAuctionTask._has_inconsistent_grouping(",643"))

    def test_fullwidth_comma_is_normalized_before_the_read_defenses(self):
        """全角逗号必须先归一成半角, 否则两条残缺读数防线同时失效。

        防线用 re.sub(r"[^\\d,]", "") 保留半角逗号来识别「首位漏读 / 分组不自洽」;
        全角逗号「，」不在保留范围里会被当噪声删掉, 「，643」就此洗成「643」,
        截断读数被当成完整值采纳后, 出价会按低一个数量级的价格算。每一对半角/全角
        输入的判定结果必须一致。
        """
        cases = [
            # (半角输入, 全角输入, 是否首位漏读, 是否分组不自洽)
            (",643", "，643", True, False),
            ("：,523", "：，523", True, False),
            (",1234", "，1234", True, False),
            ("1,23,456", "1，23，456", False, True),
            ("12,34", "12，34", False, True),
        ]
        for half, full, partial, inconsistent in cases:
            for text, expected_partial, expected_inconsistent in (
                (half, partial, inconsistent),
                (full, partial, inconsistent),
            ):
                with self.subTest(text=text):
                    self.assertEqual(
                        AutoBidAuctionTask._is_partial_number_text(text), expected_partial
                    )
                    self.assertEqual(
                        AutoBidAuctionTask._has_inconsistent_grouping(text), expected_inconsistent
                    )

    def test_read_asset_value_drops_partial_estimate_text(self):
        """估价区域读到残缺文本时按未读出处理, 交给上层重读; 稳定的数字区域保持原行为。"""
        task = _make_task()
        text_box = Mock()
        text_box.name = ",544"
        task.wait_ocr = Mock(return_value=[text_box])

        self.assertIsNone(task._read_asset_value(Mock(), 1, "当前估价", reject_partial=True))
        self.assertEqual(task._read_asset_value(Mock(), 1, "资产"), 544)

    def test_keeps_last_value_when_never_stable(self):
        """一直跳动时不能回退成基础价, 最后一次读数比丢弃更接近真实值。"""
        task = _make_task()
        task.sleep = Mock(side_effect=self.clock.sleep)
        task._read_estimate_value = Mock(
            side_effect=lambda *a, **k: (task._read_estimate_value.call_count * 100, False)
        )

        value = task._read_stable_asset_value(Mock(), 5, "当前估价")

        self.assertEqual(value, task._read_estimate_value.call_count * 100)
        self.assertGreater(task._read_estimate_value.call_count, 1)
        task.log_warning.assert_called()

    def test_returns_none_without_warning_when_never_readable(self):
        task = _make_task()
        task.sleep = Mock(side_effect=self.clock.sleep)
        task._read_estimate_value = Mock(return_value=(None, False))

        self.assertIsNone(task._read_stable_asset_value(Mock(), 5, "当前估价"))
        task.log_warning.assert_not_called()

    def test_zero_placeholder_is_skipped_until_the_real_value(self):
        """面板滚出数字前会先显示 0, 0 是占位读数不是估价。

        线上日志证据: 估价区域 OCR 到的是 `当前估价：` + `0`(conf 0.97), 连续 0.36 秒都是 0,
        随后才滚出真实数字。把 0 当结果会算出 0 元出价。
        """
        task = self._task([0, 0, 0, 35301, 35301, 35301])

        value = task._read_stable_asset_value(Mock(), 10, "当前估价", skip_zero=True)

        self.assertEqual(value, 35301)
        self.assertGreater(task._read_estimate_value.call_count, 3)

    def test_zero_does_not_reset_the_stability_counter(self):
        """中间夹一次 0 不应把已经攒够的连续次数清零, 否则真值永远攒不满。"""
        task = self._task([100, 0, 100, 100])

        self.assertEqual(task._read_stable_asset_value(Mock(), 10, "当前估价", skip_zero=True), 100)

    def test_zero_is_kept_when_skip_zero_is_off(self):
        """默认不跳过 0, 避免影响「出售价值为 0」这类把 0 当有效读数的调用方。"""
        task = self._task([0, 0, 0])

        self.assertEqual(task._read_stable_asset_value(Mock(), 10, "出售价值"), 0)

    def test_only_zero_reads_are_reported_as_unreadable(self):
        """只读到 0 时按未读出处理, 不能把 0 当成"稳定读数"交给调用方。"""
        task = self._task([0, 0, 0])

        self.assertIsNone(task._read_stable_asset_value(Mock(), 5, "当前估价", skip_zero=True))
        task.log_warning.assert_called()

    def _task_with_tight(self, reads) -> AutoBidAuctionTask:
        """reads 为 (值, 是否贴边) 序列, 用完后一直重复最后一帧。"""
        task = _make_task()
        task.sleep = Mock(side_effect=self.clock.sleep)
        task._read_estimate_value = Mock(side_effect=list(reads) + [list(reads)[-1]] * 40)
        return task

    def test_tight_reads_do_not_accumulate_the_stability_counter(self):
        """贴边帧不能被当成「没有新信息」来累积稳定计数。

        回归: 贴边帧原本走 `if value is None: same += 1` 分支, 于是「先读到一次正常值 +
        随后连续贴边」会被攒成 stable 并返回那个旧值 —— 而旧值正是在裁框不够用的帧上读的,
        可信度不该随贴边次数的增加而上升。更糟的是 tight_seen 的告警只在 last is None
        时才走, 贴边信号被静默吞掉 (2026-09-23 审查发现)。
        """
        task = self._task_with_tight([(26643, False)] + [(2643, True)] * 8)

        value = task._read_stable_asset_value(Mock(), 5, "当前估价")

        # 不得把它当成「读数稳定」返回; 允许走超时兜底, 但必须有贴边告警。
        self.assertFalse(any("读数稳定" in str(c.args[0]) for c in task.log_info.call_args_list))
        self.assertTrue(
            any("贴住识别区域边界" in str(c.args[0]) for c in task.log_warning.call_args_list)
        )
        self.assertNotEqual(value, 2643)

    def test_all_tight_reads_return_none_with_edge_warning(self):
        """全程贴边说明末位可能一直是被裁的, 按未读出处理并指向裁框常量。"""
        task = self._task_with_tight([(2643, True)] * 10)

        self.assertIsNone(task._read_stable_asset_value(Mock(), 5, "当前估价"))
        self.assertTrue(
            any("BOX_ESTIMATE 右边界" in str(c.args[0]) for c in task.log_warning.call_args_list)
        )

    def test_recovery_after_a_transient_tight_read(self):
        """贴边可能只是数字滚动中的瞬时抖动, 之后的正常读数必须能正常稳定下来。"""
        task = self._task_with_tight([(197, True)] + [(300, False)] * 8)

        self.assertEqual(task._read_stable_asset_value(Mock(), 10, "当前估价"), 300)


class TestAuctionEstimateLabelAnchor(unittest.TestCase):
    """估价数字必须按「估价」标签右边沿取, 不能只靠裁框宽度。

    线上证据 2026-09-23: 区域里有多个文本时直接 `"".join()` 会把两串数字粘成一个。
    BOX_ESTIMATE 右边界落进「我的资产」数值里时, 资产数字被截尾后与估价粘成一串,
    表现成「估价少一位」(2,643 读成 ,643 / 1,912 读成 912)。

    另一个坑(2026-09-23 21:49 线上炸过一次): 取文本必须用 `self.ocr(match=None)`,
    不能用 `wait_ocr(match=RE_NUMBER)` —— 框架会用 match **过滤返回值**, 标签框不在里面,
    于是 `label_right` 永远是 None、估价永远读不出。这里桩的就是 `task.ocr`。
    """

    @staticmethod
    def _text_box(name: str, x: int, width: int, y: int = 150, height: int = 40) -> Mock:
        box = Mock()
        box.name = name
        box.x = x
        box.y = y
        box.width = width
        box.height = height
        return box

    def _task(self, boxes) -> AutoBidAuctionTask:
        task = _make_task()
        task.ocr = Mock(return_value=boxes)
        return task

    def test_reads_all_texts_not_only_number_matches(self):
        """必须读到标签框: 它不匹配 RE_NUMBER, 但正是定位数字的依据。

        回归 2026-09-23 21:49 的线上故障 —— 那时用 wait_ocr(match=RE_NUMBER), 框架把返回
        列表过滤成只含数字, 标签丢失, 每一帧都判「标签未读到」, 出价一直回退基础价 1。
        """
        task = self._task(
            [
                self._text_box("当前估价：", 1502, 118, y=149, height=34),
                self._text_box("13,875", 1640, 90, y=153, height=28),
            ]
        )

        value, tight = task._read_estimate_value(Mock(x=1493, y=143, width=369, height=53), 1)

        self.assertEqual(value, 13875)
        self.assertFalse(tight)
        # 必须是不带 match 的调用, 否则框架会再把标签过滤掉。
        self.assertIsNone(task.ocr.call_args.kwargs.get("match"))

    def test_takes_number_right_of_the_label(self):
        """标签右边的数字才是估价; 标签左边残留的数字要被排除。"""
        task = self._task(
            [
                self._text_box("当前估价：", 1900, 130),
                self._text_box("2,643", 2060, 120),
            ]
        )

        value, tight = task._read_estimate_value(Mock(x=1850, y=140, width=450, height=60), 1)

        self.assertEqual(value, 2643)
        self.assertFalse(tight)

    def test_asset_number_to_the_right_is_not_glued_in(self):
        """同一裁框里出现第二个数字栏时, 拼接结果会被判为残缺读数而不是当成估价。

        这是本次线上故障的形态: `RE_NUMBER` 把区域内所有命中文本 `"".join()` 拼接, 估价的
        右边界一旦伸进「我的资产」数值, 两串数字就粘成 `2,64322,684`。这类读数分组不自洽,
        会被 `_has_inconsistent_grouping` 拦下, 按未读出处理 —— 交给调用方重读一帧, 而不是
        按错误价格出价。真正让裁框内只剩估价一个数字的是 BOX_ESTIMATE 的右边界(见该常量注释)。
        """
        task = self._task(
            [
                self._text_box("当前估价：", 1900, 130),
                self._text_box("2,643", 2060, 120),
                self._text_box("22,684", 2450, 100),
            ]
        )

        value, _ = task._read_estimate_value(Mock(x=1850, y=140, width=800, height=60), 1)

        self.assertIsNone(value)
        # 必须是因为「分组不自洽」被拒, 而不是因为标签没读到之类的原因。
        self.assertTrue(any("不自洽" in str(c.args[0]) for c in task.log_debug.call_args_list))

    def test_partial_text_right_of_the_label_is_rejected(self):
        """标签右边读到 `,643` 说明首位被裁, 按未读出处理。"""
        task = self._task(
            [
                self._text_box("当前估价：", 1900, 130),
                self._text_box(",643", 2060, 90),
            ]
        )

        self.assertIsNone(task._read_estimate_value(Mock(), 1)[0])

    def test_label_missing_gives_no_value(self):
        """标签没读出来时不猜数字, 交给调用方重读一帧。"""
        task = self._task([self._text_box("2,643", 2060, 120)])

        self.assertIsNone(task._read_estimate_value(Mock(), 1)[0])

    def test_estimate_box_right_edge_leaves_the_asset_number_outside(self):
        """BOX_ESTIMATE 右边界必须落在「我的资产」数值左边, 不能覆盖它。

        回归 coderabbit 审查: 右边界原本是 0.9700, 而「我的资产」数值右端实测在 0.9594,
        即裁框把资产数字圈了进来。标签一旦漏读(OCR 抖动、界面过渡帧), 区域内就剩两个数字,
        `_read_estimate_value` 的按标签过滤会退化成拼接整段数字 —— 拼接结果可能恰好满足
        千位规则而逃过残缺校验, 直接进 `_estimate_bid_price` 变成一次错误出价。
        把右边界收到 0.9200 后, 即使标签漏读, 区域里也只剩估价一个数字。
        """
        left, _, right, _ = AutoBidAuctionTask.BOX_ESTIMATE
        # 实测数字: 估价右端最靠右 0.9052, 我的资产右端 0.9594。
        estimate_right_edge = 0.9052
        asset_left_edge = 0.9594

        self.assertGreater(right, estimate_right_edge, "右边界必须留出估价末位的余量")
        self.assertLess(right, asset_left_edge, "右边界不能伸进我的资产数值")

    def test_number_touching_the_right_edge_is_flagged(self):
        """数字右端贴住裁框边界说明末位可能被切掉, 用贴边标志暴露给调用方。"""
        box = Mock(x=1850, y=140, width=350, height=60)
        task = self._task(
            [
                self._text_box("当前估价：", 1900, 130),
                # 右端 2196 距裁框右边界 2200 只剩 4px。
                self._text_box("2,643", 2060, 136),
            ]
        )

        value, tight = task._read_estimate_value(box, 1)

        self.assertEqual(value, 2643)
        self.assertTrue(tight)


class TestAuctionKeypadScreenDetection(unittest.TestCase):
    """数字键盘弹出后是第三套界面状态, 必须能认出它, 否则空等到超时。

    键盘弹窗整体覆盖约 0.30~0.90 / 0.44~0.80, 盖住 BOX_BID, 那时 BOX_BID 的 all_boxes
    是空的, 用 RE_BID 判定会永远为假。线上 2026-09-23 18:44 因此空转 60 秒报
    「等待出价界面超时」。
    """

    def _task(self, hit_keypad: bool, hit_bid: bool) -> AutoBidAuctionTask:
        task = _make_task()
        task.ocr = Mock(side_effect=[hit_keypad, hit_bid])
        return task

    def test_keypad_markup_is_recognized_as_bid_screen(self):
        task = self._task(hit_keypad=True, hit_bid=False)
        boxes = Mock(bid_keypad=Mock(), bid=Mock())

        self.assertTrue(task._is_bid_screen(boxes))

    def test_bid_button_still_works_without_keypad(self):
        task = self._task(hit_keypad=False, hit_bid=True)
        boxes = Mock(bid_keypad=Mock(), bid=Mock())

        self.assertTrue(task._is_bid_screen(boxes))

    def test_keypad_is_checked_before_the_bid_button(self):
        """键盘态是更具体的形态, 命中就不必再读 BOX_BID(那个区域此时是空的)。"""
        task = self._task(hit_keypad=True, hit_bid=False)
        boxes = Mock(bid_keypad=Mock(), bid=Mock())

        task._is_bid_screen(boxes)

        self.assertEqual(task.ocr.call_count, 1)


class TestAuctionEstimateBidPrice(unittest.TestCase):
    """按系统估价出价必须走稳定读取, 并在读不到时回退基础价。"""

    def _task(self, **values) -> AutoBidAuctionTask:
        task = _make_task()
        task.config = _config(**values)
        return task

    def test_estimate_mode_uses_the_stable_reader(self):
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 1,
                AutoBidAuctionTask.CONF_ESTIMATE_RATIO: "2",
            }
        )
        task._read_stable_asset_value = Mock(return_value=300)
        boxes = Mock()

        self.assertEqual(task._estimate_bid_price(boxes, None, 1), 600)
        self.assertIs(task._read_stable_asset_value.call_args.args[0], boxes.estimate)

    def test_estimate_read_skips_zero_placeholders(self):
        """估价读取必须跳过 0 占位读数, 否则 0 会被当成结果。"""
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 1,
                AutoBidAuctionTask.CONF_ESTIMATE_RATIO: "2",
            }
        )
        task._read_stable_asset_value = Mock(return_value=300)
        boxes = Mock()

        task._estimate_bid_price(boxes, None, 1)

        self.assertTrue(task._read_stable_asset_value.call_args.kwargs["skip_zero"])

    def test_zero_reading_does_not_become_a_zero_bid(self):
        """估价为 0 时不能按 0 元出价, 应回退到基础价。"""
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 7,
                AutoBidAuctionTask.CONF_ESTIMATE_RATIO: "1",
            }
        )
        task._read_stable_asset_value = Mock(return_value=0)

        self.assertEqual(task._estimate_bid_price(Mock(), None, 1), 7)
        task.log_warning.assert_called()

    def test_falls_back_to_base_price_when_estimate_unreadable(self):
        task = self._task(
            **{
                AutoBidAuctionTask.CONF_FIXED_PRICE: 7,
                AutoBidAuctionTask.CONF_ESTIMATE_RATIO: "1",
            }
        )
        task._read_stable_asset_value = Mock(return_value=None)

        self.assertEqual(task._estimate_bid_price(Mock(), None, 1), 7)
        task.log_warning.assert_called()


class TestAuctionMainScreenTitle(unittest.TestCase):
    """拍卖结束的「回到主界面」判据从「我的资产」改成「即刻落槌」。

    「我的资产」在结算等界面也会出现, 用它判"已回主界面"会提前放行;
    「即刻落槌」只在拍卖主界面出现, 位置与藏品仓库标题是同一个槽位。
    """

    def test_main_title_regex_matches_only_the_auction_hall(self):
        self.assertTrue(RE_MAIN_TITLE.search("即刻落槌"))
        self.assertFalse(RE_MAIN_TITLE.search("我的资产"))
        self.assertFalse(RE_MAIN_TITLE.search("藏品仓库"))

    def _task(self, title_found: bool) -> AutoBidAuctionTask:
        task = _make_task()
        # wait_ocr 第一次给 _wait_operate_click(退出按钮), 第二次给主界面标题。
        task.wait_ocr = Mock(side_effect=[[Mock()], [Mock()] if title_found else []])
        task._run_post_round_actions = Mock()
        return task

    def test_title_seen_runs_post_round_actions(self):
        task = self._task(title_found=True)
        boxes = Mock()

        task._finish_auction(boxes, [Mock()], auction_module.time.monotonic() + 60)

        self.assertIs(task.wait_ocr.call_args_list[1].kwargs["box"], boxes.main_title)
        self.assertIs(task.wait_ocr.call_args_list[1].kwargs["match"], RE_MAIN_TITLE)
        task._run_post_round_actions.assert_called_once()

    def test_missing_title_skips_post_round_actions(self):
        task = self._task(title_found=False)

        task._finish_auction(Mock(), [Mock()], auction_module.time.monotonic() + 60)

        task._run_post_round_actions.assert_not_called()
        self.assertIn("即刻落槌", str(task.log_warning.call_args))


class TestAuctionMatchClickProbe(unittest.TestCase):
    """点击开始匹配后界面毫无变化时立刻重试, 不再白等 MATCH_CLICK_TIMEOUT。

    历史日志里点击成功后界面切换只要 0.6 秒, 而失败时要等满 30 秒才重试,
    09-14 一天因此白等约 11 分钟。
    """

    def setUp(self):
        self.clock = _FakeTime()
        patcher = patch.object(auction_module, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _task(self, *, match: bool, confirm=False, bid=False, skip=False) -> AutoBidAuctionTask:
        task = _make_task()
        task._is_match_screen = Mock(return_value=match)
        task._is_confirm_screen = Mock(return_value=confirm)
        task._is_bid_screen = Mock(return_value=bid)
        task._is_skip_screen = Mock(return_value=skip)
        task._wait_operate_click = Mock(return_value=True)
        task.sleep = Mock(side_effect=self.clock.sleep)
        return task

    def test_retries_immediately_when_button_never_left(self):
        """按钮仍在原位说明点击没生效, 应在 MATCH_PROBE_TIMEOUT 内交回上层重试。"""
        task = self._task(match=True)
        started = self.clock.now

        self.assertIsNone(task._handle_match_click(Mock(), self.clock.now + 120))

        elapsed = self.clock.now - started
        self.assertLessEqual(elapsed, AutoBidAuctionTask.MATCH_PROBE_TIMEOUT)
        self.assertLess(elapsed, AutoBidAuctionTask.MATCH_CLICK_TIMEOUT)

    def test_waits_full_timeout_while_screen_is_loading(self):
        """按钮已消失(处于加载动画)但目标界面未出现时, 仍应等满 MATCH_CLICK_TIMEOUT。"""
        task = self._task(match=False)
        started = self.clock.now

        self.assertIsNone(task._handle_match_click(Mock(), self.clock.now + 120))

        self.assertGreaterEqual(self.clock.now - started, AutoBidAuctionTask.MATCH_CLICK_TIMEOUT)

    def test_probe_waits_before_giving_up(self):
        """按钮残留时要先观察 MATCH_PROBE_TIMEOUT 秒, 不能第一帧就判点击失败。"""
        task = self._task(match=True)

        self.assertIsNone(task._handle_match_click(Mock(), self.clock.now + 120))

        expected_polls = AutoBidAuctionTask.MATCH_PROBE_TIMEOUT / AutoBidAuctionTask.POLL_INTERVAL
        self.assertGreaterEqual(task.sleep.call_count, expected_polls)

    def test_returns_confirm_as_soon_as_it_appears(self):
        task = self._task(match=False, confirm=True)

        self.assertEqual(
            task._handle_match_click(Mock(), self.clock.now + 120), AuctionState.CONFIRM
        )
        self.assertEqual(task.sleep.call_count, 0)

    def test_returns_skip_state(self):
        task = self._task(match=False, skip=True)

        self.assertEqual(task._handle_match_click(Mock(), self.clock.now + 120), AuctionState.SKIP)

    def test_missing_button_returns_none_without_waiting(self):
        task = self._task(match=True)
        task._wait_operate_click = Mock(return_value=False)
        started = self.clock.now

        self.assertIsNone(task._handle_match_click(Mock(), self.clock.now + 120))

        self.assertEqual(self.clock.now, started)

    def test_stage_budget_expiry_reports_match_stage(self):
        """匹配阶段的局部预算用尽时整轮往往还剩几百秒, 异常消息不能是「单轮拍卖超时」。"""
        task = self._task(match=True)

        with self.assertRaises(WaitFailedException) as ctx:
            task._handle_match_click(Mock(), self.clock.now)

        self.assertIn("匹配阶段", str(ctx.exception))
        self.assertNotIn("单轮拍卖超时", str(ctx.exception))


class TestAuctionWorldDropRecovery(unittest.TestCase):
    """掉线被踢回大世界后自动回场。

    网络不稳时点「开始匹配」后会被踢回大世界, 拍卖界面的四种状态判定全不命中, 原逻辑
    每轮空转到 MATCH_TIMEOUT(120 秒) 才按本轮失败处理。这里覆盖掉线检测、回场路径
    与「只回场一次」的边界。
    """

    def setUp(self):
        self.clock = _FakeTime()
        # 回场流程已拆到 auction/recovery.py, 假时钟要同时控制任务侧与回场侧的 time。
        patcher = patch.object(auction_module, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(auction_recovery, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _task(self, *, world: bool = False, match: bool = False) -> AutoBidAuctionTask:
        task = _make_task()
        task.in_world = Mock(return_value=world)
        task._is_match_screen = Mock(return_value=match)
        task._is_confirm_screen = Mock(return_value=False)
        task._is_bid_screen = Mock(return_value=False)
        task._is_skip_screen = Mock(return_value=False)
        task._wait_operate_click = Mock(return_value=True)
        task.sleep = Mock(side_effect=self.clock.sleep)
        return task

    def test_world_screen_forwards_to_in_team_and_world(self):
        """大世界判定走基类的 in_team_and_world(), 组队血条与小地图箭头都要查。"""
        task = self._task(world=True)

        self.assertTrue(task._is_world_screen())
        task.in_world.assert_called_once()
        task.is_in_team.assert_called_once()

    def test_world_screen_rejects_bright_screen_without_team_ui(self):
        """小地图箭头命中但组队血条不在时不算大世界。

        回归用例: 小地图箭头是 chamfer 打分, 没有「场景饱和」惩罚 —— 搜索区整片偏亮时
        coverage 与 distance_score 双双为 1, 纯白画面直接满分。实测「都市大亨」面板得
        1.000、「仪器组合」面板得 0.841, 都越过 0.75 阈值, 与真箭头(0.997)分不开。
        加上 is_in_team() 的组队血条判定才能挡住这类画面。
        """
        task = self._task(world=True)
        task.is_in_team = Mock(return_value=False)

        self.assertFalse(task._is_world_screen())

    def test_world_screen_swallows_detection_errors(self):
        """大世界判定依赖模板匹配, 判定失败不能打断拍卖主流程。"""
        task = self._task()
        task.in_world = Mock(side_effect=RuntimeError("no frame"))

        self.assertFalse(task._is_world_screen())

    def test_match_click_reports_world_instead_of_loading_forever(self):
        """按钮已消失却没进后续界面, 且检测到大世界时判定为掉线。"""
        task = self._task(match=False, world=True)
        started = self.clock.now

        self.assertEqual(task._handle_match_click(Mock(), self.clock.now + 120), AuctionState.WORLD)
        self.assertLess(self.clock.now - started, AutoBidAuctionTask.MATCH_CLICK_TIMEOUT)

    def test_stage_match_recovers_when_world_detected_before_timeout(self):
        """空转到超时前检测到大世界时走回场, 而不是抛「匹配阶段超时」。"""
        task = self._task(world=True)
        task._recover_quota = AutoBidAuctionTask.RECOVER_MAX_PER_ROUND
        task._recover_from_world = Mock(return_value=AuctionState.BID)

        result = task._stage_match(Mock(), self.clock.now)

        self.assertEqual(result, AuctionState.BID)
        task._recover_from_world.assert_called_once()

    def test_recover_reruns_match(self):
        """回场成功后重跑匹配阶段, 把本轮接着走完。"""
        task = self._task()
        task._return_to_auction = Mock(return_value=True)
        task._read_current_venue = Mock(return_value="当前：海贝场")
        task._stage_match = Mock(return_value=AuctionState.CONFIRM)

        result = task._recover_from_world(Mock(), self.clock.now + 600)

        self.assertEqual(result, AuctionState.CONFIRM)
        task._stage_match.assert_called_once()
        # 重跑走的是同一份轮次 deadline, 不会另开预算。
        self.assertEqual(task._stage_match.call_args.args[1], self.clock.now + 600)

    def test_recover_raises_when_return_to_auction_fails(self):
        task = self._task()
        task._return_to_auction = Mock(return_value=False)

        with self.assertRaises(WaitFailedException) as ctx:
            task._recover_from_world(Mock(), self.clock.now + 600)

        self.assertIn("未能回到拍卖界面", str(ctx.exception))

    def test_second_drop_fails_round_instead_of_recovering_again(self):
        """配额用完后再次掉线按本轮失败结束, 不能无限回场。"""
        task = self._task()
        task._recover_quota = 0
        task._recover_from_world = Mock()

        with self.assertRaises(WaitFailedException) as ctx:
            task._resume_after_world_drop(Mock(), self.clock.now)

        self.assertIn("再次掉线", str(ctx.exception))
        task._recover_from_world.assert_not_called()

    def test_each_recover_consumes_the_round_quota(self):
        """回场一次就扣一次配额, 配额用完后再掉线直接判本轮失败。"""
        task = self._task()
        task._recover_quota = 1
        task._recover_from_world = Mock(return_value=AuctionState.BID)

        self.assertEqual(task._resume_after_world_drop(Mock(), self.clock.now), AuctionState.BID)
        self.assertEqual(task._recover_quota, 0)

        with self.assertRaises(WaitFailedException):
            task._resume_after_world_drop(Mock(), self.clock.now)
        self.assertEqual(task._recover_from_world.call_count, 1)

    def test_confirm_failure_rerun_shares_the_same_quota(self):
        """确认失败后重跑匹配阶段时, 不能把本轮回场配额重置回去。

        回归 coderabbit 审查: 原实现把「能否回场」放在 `allow_recover` 参数上逐层传递,
        而 `_ensure_confirm_stage` 的确认失败分支重新调 `_stage_match(boxes, deadline)`,
        默认值 `True` 会把配额悄悄恢复 —— 于是一轮里可以回场多次, 每次都重走一遍
        「F5 → 都市闲趣 → 即刻落槌」的面板动画, 把整轮 deadline 耗光。

        这里直接盯住配额: 第一次匹配时消耗掉本轮回场配额, 之后确认失败触发重跑,
        重跑进 `_stage_match` 时配额必须已经是 0, 而不是被恢复成 RECOVER_MAX_PER_ROUND。
        """
        task = self._task()
        task._stage_confirm = Mock(return_value=False)
        seen_quota: list[int] = []

        def stage_match(boxes, deadline):
            seen_quota.append(task._recover_quota)
            if len(seen_quota) == 1:
                # 第一次匹配: 模拟掉线回场, 把本轮配额用掉。
                task._recover_quota -= 1
                return AuctionState.CONFIRM
            # 确认失败后的重跑: 从此处抛出让整轮提前结束, 只看配额够了。
            raise WaitFailedException("重跑: 测试到此为止")

        task._stage_match = Mock(side_effect=stage_match)

        with self.assertRaises(WaitFailedException) as ctx:
            task._exec_auction_round(Mock())

        self.assertIn("重跑", str(ctx.exception))
        self.assertEqual(seen_quota[0], AutoBidAuctionTask.RECOVER_MAX_PER_ROUND)
        # 关键断言: 重跑时配额没有被恢复, 否则同一轮又能再回场一次。
        self.assertEqual(seen_quota[1], 0)

    def test_return_to_auction_walks_f5_city_fun_and_card(self):
        """回场顺序: 大世界 → F5 都市大亨 → 都市闲趣 → 即刻落槌 → 主界面标题。"""
        task = self._task()
        task.ensure_main = Mock()
        task.openF5panel = Mock()
        task.wait_ocr = Mock(return_value=[Mock()])
        task._click_instant_lot = Mock(return_value=True)

        self.assertTrue(task._return_to_auction(Mock(), self.clock.now + 90))

        task.ensure_main.assert_called()
        task.openF5panel.assert_called_once()
        task.operate_click.assert_called_once_with(*task.pos.panels.f5.hobbies)
        task._click_instant_lot.assert_called_once()

    def test_return_to_auction_fails_when_city_fun_panel_missing(self):
        task = self._task()
        task.ensure_main = Mock()
        task.openF5panel = Mock()
        task.wait_ocr = Mock(return_value=[])
        task._click_instant_lot = Mock(return_value=True)

        self.assertFalse(task._return_to_auction(Mock(), self.clock.now + 90))
        task._click_instant_lot.assert_not_called()

    def test_return_to_auction_attempts_exactly_twice(self):
        """回场整条路径只重试一次, 即总共走两次。

        回归: retry_on_action 的循环是 `while not result and count <= attempt`, attempt 是
        「额外重试次数」, 实际执行 attempt + 1 次。原来传 attempt=2 会走三遍, 每次都要重看
        一遍面板动画, 把整轮 deadline 耗光 —— 与此处注释声称的「重试一次」不符。
        """
        task = self._task()
        task.ensure_main = Mock()
        task.openF5panel = Mock()
        task.wait_ocr = Mock(return_value=[])
        task._click_instant_lot = Mock(return_value=True)

        self.assertFalse(task._return_to_auction(Mock(), self.clock.now + 90))

        self.assertEqual(task.openF5panel.call_count, 2)

    def test_instant_lot_clicks_without_scrolling_when_visible(self):
        task = self._task()
        task.scroll = Mock()

        self.assertTrue(task._click_instant_lot(self.clock.now + 90))

        task.scroll.assert_not_called()

    def test_instant_lot_scrolls_then_gives_up(self):
        """「即刻落槌」在面板最后一页, 找不到时要滚动重试, 用尽次数后放弃。"""
        task = self._task()
        task._wait_operate_click = Mock(return_value=False)
        task.scroll = Mock()

        self.assertFalse(task._click_instant_lot(self.clock.now + 90))

        self.assertEqual(task.scroll.call_count, AutoBidAuctionTask.RECOVER_SCROLL_STEPS)

    def test_current_venue_read_failure_is_not_fatal(self):
        """会场文字只用于日志留痕, 读不出不能影响回场结果。"""
        task = self._task()
        task.ocr = Mock(side_effect=RuntimeError("no frame"))

        self.assertEqual(task._read_current_venue(), "")


class TestAuctionEntryRecover(unittest.TestCase):
    """启动时的入口回场: 从大世界直接启动也要能进拍卖界面。

    原来只能靠第一轮 `_stage_match` 空转到 MATCH_TIMEOUT 才由末尾的大世界兜底触发回场,
    首轮白等约一分钟。`_ensure_auction_entry` 复用同一条回场路径把这个等待提到启动时。
    """

    def setUp(self):
        self.clock = _FakeTime()
        # 回场流程已拆到 auction/recovery.py, 假时钟要同时控制任务侧与回场侧的 time。
        patcher = patch.object(auction_module, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(auction_recovery, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _task(self, *, in_auction: bool, world: bool = False) -> AutoBidAuctionTask:
        task = _make_task()
        # wait_ocr 同时被「读主界面标题」和回场内部调用; 用 side_effect 区分:
        # 首次调用是入口探测, 由 in_auction 决定命中与否。
        task.wait_ocr = Mock(side_effect=[Mock() if in_auction else [], Mock()])
        task.in_world = Mock(return_value=world)
        task._return_to_auction = Mock(return_value=True)
        return task

    def test_auction_screen_wins_over_world_probe(self):
        """已在拍卖界面时直接返回, 不再去探测大世界。

        「主界面标题命中」必须排在「大世界判定」之前: 拍卖界面本身也带小地图(在大世界
        图层之上), `in_world` 可能为真。若先判大世界就会把已在拍卖界面的用户再走一遍
        F5 流程。这里让 in_world 返回 True, 断言它不被查询。
        """
        task = self._task(in_auction=True, world=True)

        task._ensure_auction_entry(Mock())

        task.in_world.assert_not_called()
        task._return_to_auction.assert_not_called()

    def test_world_start_triggers_recover(self):
        """启动时人在大世界: 走回场路径进入拍卖界面。"""
        task = self._task(in_auction=False, world=True)

        task._ensure_auction_entry(Mock())

        task._return_to_auction.assert_called_once()
        # 回场预算与掉线回场一致。
        deadline = task._return_to_auction.call_args.args[1]
        self.assertEqual(deadline, self.clock.now + AutoBidAuctionTask.ENTRY_RECOVER_TIMEOUT)

    def test_not_in_world_leaves_flow_alone(self):
        """既不在拍卖界面也不在大世界时(登录页/加载页等)不接管, 交给原有流程报错。"""
        task = self._task(in_auction=False, world=False)

        task._ensure_auction_entry(Mock())

        task._return_to_auction.assert_not_called()

    def test_recover_failure_is_not_fatal(self):
        """入口回场失败只记警告, 让第一轮按界面异常继续走既有失败路径。"""
        task = self._task(in_auction=False, world=True)
        task._return_to_auction = Mock(return_value=False)

        task._ensure_auction_entry(Mock())

        self.assertTrue(
            any("启动回场未成功" in str(call.args[0]) for call in task.log_warning.call_args_list)
        )

    def test_does_not_consume_round_recover_quota(self):
        """入口回场发生在轮次之外, 不能吃掉「每轮掉线可回场一次」的配额。"""
        task = self._task(in_auction=False, world=True)
        task._recover_quota = AutoBidAuctionTask.RECOVER_MAX_PER_ROUND

        task._ensure_auction_entry(Mock())

        self.assertEqual(task._recover_quota, AutoBidAuctionTask.RECOVER_MAX_PER_ROUND)


class TestAuctionReturnBudget(unittest.TestCase):
    """回场的每一步都必须受「剩余预算」约束, 不能走各处的默认超时。

    `ensure_main` 默认 `time_out` 是 30 秒, 但登录态丢失时会被抬到 600 秒
    (见 `BaseNTETask.ensure_main`)。`RECOVER_TIMEOUT` 只有 90 秒, 不把剩余时间传进去,
    单是这一步就能把整个回场预算连同本轮 deadline 一起耗光 —— 后面的 F5、「都市闲趣」
    入口、「即刻落槌」根本轮不到执行, 而日志只会显示回场失败。
    """

    def setUp(self):
        self.clock = _FakeTime()
        # 回场流程已拆到 auction/recovery.py, 假时钟要同时控制任务侧与回场侧的 time。
        patcher = patch.object(auction_module, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(auction_recovery, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _task(self) -> AutoBidAuctionTask:
        task = _make_task()
        task.sleep = Mock(side_effect=self.clock.sleep)
        task.ensure_main = Mock()
        task.openF5panel = Mock()
        task.operate_click = Mock()
        task._click_instant_lot = Mock(return_value=True)
        return task

    def test_ensure_main_receives_the_remaining_budget(self):
        """预算只剩多少, ensure_main 就最多等多少。"""
        task = self._task()
        # 回场整体成功, 让流程走到最后一步。
        task.wait_ocr = Mock(return_value=[Mock()])
        deadline = self.clock.now + 20

        task._return_to_auction(Mock(), deadline)

        self.assertTrue(task.ensure_main.called)
        leftover = task.ensure_main.call_args.kwargs["time_out"]
        self.assertLessEqual(leftover, 20)
        self.assertGreater(leftover, 0)

    def test_budget_exhausted_skips_the_rest_of_the_path(self):
        """预算耗尽时不再去动 F5 和面板 —— 那些步骤只会继续超时。"""
        task = self._task()
        task.wait_ocr = Mock(return_value=[Mock()])
        deadline = self.clock.now - 1  # 已经过期

        self.assertFalse(task._return_to_auction(Mock(), deadline))
        task.ensure_main.assert_not_called()
        task.openF5panel.assert_not_called()

    def test_retry_reset_also_respects_the_budget(self):
        """重试前的复位走的是同一份预算, 不能再用默认超时把预算吃干净。"""
        task = self._task()
        seen = []

        def fail_once(*args, **kwargs):
            seen.append(kwargs.get("time_out"))
            raise RuntimeError("模拟打开面板失败")

        task.wait_ocr = Mock(return_value=[Mock()])
        task.ensure_main = Mock(side_effect=fail_once)
        deadline = self.clock.now + 30

        task._return_to_auction(Mock(), deadline)

        # action 与 reset 各调一次, 且两次都带上了不超过剩余预算的 time_out。
        self.assertEqual(len(seen), 2)
        for time_out in seen:
            self.assertIsNotNone(time_out)
            self.assertLessEqual(time_out, 30)
            self.assertGreater(time_out, 0)


class TestAuctionTimeoutConstants(unittest.TestCase):
    """超时值必须是常量, 不能裸写数字。

    裸数字的问题是「改一处漏一处」: 同一个语义在两处各写一遍 10, 日后有人调其中一处,
    另一处静默不同步, 表现为「有时生效有时不生效」。所以凡是「有名字的等待」都应引用
    常量, 只有循环前探测 deadline 这类一次性极小值可以裸写。

    例外清单(白名单)按语义放行, 新增白名单项必须说明理由 —— 它们都是出价热路径上
    刻意调短的独立超时, 语义与任何现有常量都不同, 强行提取只会造出无人复用的常量。
    """

    # 允许裸写的位置 -> (出现次数上限, 理由)。
    #
    # 记次数而不是单纯记「这个数字可以用」: 否则把某个常量改回裸数字时, 只要该数字
    # 恰好也在白名单里就抓不出来 —— 变异验证实测过这个漏洞(把 WAREHOUSE_LOAD_TIMEOUT
    # 改回裸 10, 因为白名单里有 10 而放行)。带上限后, 每次新增裸写都会让计数超标。
    ALLOWED_BARE = {
        # 循环体入口的 deadline 探测: 只要一个「还没到点」的判定, 不等待。
        "0.1": (3, "deadline 探测, 一次性极小值"),
        # 单帧/快检: 只确认当前帧的界面状态, 不需要等它变化。
        "1": (1, "单帧快检(是否已在出售模式)"),
        # 等出价按钮出现: 与 WAREHOUSE_LOAD_TIMEOUT 数值相同, 但一个是出价热路径、
        # 一个是仓库加载, 合并会让两者被一起调歪。
        "10": (1, "出价热路径等按钮出现"),
        # 等控件出现: 与 ASSET_OCR_TIMEOUT 数值相同但场景不同(一个是等按钮、
        # 一个是读资产), 借用会造成错误的耦合。
        "15": (2, "各阶段等控件出现, 语义独立"),
        # 找卡片 / 读价格文本的短超时。
        "3": (2, "找卡片 / 读价格文本"),
        # 出价热路径上的短超时: 等按钮/面板/界面离开, 每次出价都要走一遍, 给太长会拖慢。
        "5": (6, "出价热路径的独立短超时"),
    }

    def _bare_timeout_calls(self) -> list[tuple[int, str]]:
        """扫描拍卖域源码, 返回所有以裸数字作超时的调用点 (行号, 数字文本)。

        出售等能力已按模块拆分到 src/tasks/auction/ 子包, 扫描必须覆盖整个拍卖域,
        否则搬迁出去的调用点会逃过检查, 白名单对应的约束就只剩半边。
        """
        paths = [Path(auction_module.__file__)]
        package_dir = Path(auction_module.__file__).parent / "auction"
        if package_dir.is_dir():
            paths.extend(sorted(package_dir.glob("*.py")))
        pattern = re.compile(
            r"(?:_remaining_timeout|_bounded_timeout|_timeout_or_zero)\(deadline,\s*([0-9.]+)\s*\)"
        )
        found = []
        for path in paths:
            source = path.read_text(encoding="utf-8")
            for index, line in enumerate(source.splitlines(), start=1):
                match = pattern.search(line)
                if match:
                    found.append((index, match.group(1)))
        return found

    def test_timeout_calls_use_named_constants(self):
        """所有超时调用要么引用常量, 要么在白名单里且没超过次数上限。"""
        counts: dict[str, int] = {}
        for _, value in self._bare_timeout_calls():
            counts[value] = counts.get(value, 0) + 1

        unknown = {
            value: count for value, count in counts.items() if value not in self.ALLOWED_BARE
        }
        self.assertEqual(
            unknown,
            {},
            f"这些超时值应提取成常量(或加入白名单并说明理由): {unknown}",
        )

        exceeded = {
            value: (count, self.ALLOWED_BARE[value][0])
            for value, count in counts.items()
            if count > self.ALLOWED_BARE[value][0]
        }
        self.assertEqual(
            exceeded,
            {},
            f"这些裸数字出现次数超过白名单上限(数值, 实际 vs 上限): {exceeded}",
        )

    def test_scan_actually_finds_the_known_call_sites(self):
        """扫描本身要有判别力: 至少能找到那些已知的白名单点位。

        否则正则写错(比如常量改成 self.XXX 后全部不匹配)时, 上一条用例会「零违规」
        而永远通过 —— 变异验证时正是靠这条抓住的。
        """
        found = self._bare_timeout_calls()
        self.assertGreaterEqual(len(found), 5)
        values = {value for _, value in found}
        # 白名单里的每类值都应真实存在, 不能是过期的残留名单。
        for value in self.ALLOWED_BARE:
            self.assertIn(value, values, f"白名单项 {value} 已不存在, 应删除")


class TestAuctionEstimateEdgeMarginScaling(unittest.TestCase):
    """估价贴边阈值必须随分辨率等比放大, 不能是固定像素。

    AGENTS.md 要求坐标用相对屏幕比例, 支持 1080p/1440p/2160p。贴边阈值原本硬编码 8px,
    那是 1080p 下的实测临界值; 高分辨率下 UI 与文字同步放大, 同一形态的末位残边也等比
    变宽 (2160p 下约 16px), 固定 8px 会漏判 —— 被裁的读数会被当成正常值采信, 正是
    2026-09-23 那次「2,643 读成 ,643」的同类故障。
    """

    BOX_RIGHT = 2200
    # 真实布局里「当前估价：」标签在数字左侧。
    LABEL_X = 1900

    @staticmethod
    def _text_box(name: str, x: int, width: int, y: int = 150, height: int = 40) -> Mock:
        box = Mock()
        box.name = name
        box.x = x
        box.y = y
        box.width = width
        box.height = height
        return box

    def _read(self, screen_width: int, digit_right_offset: int) -> tuple[int | None, bool]:
        """数字右端距裁框右边界 digit_right_offset 像素时的 (值, 是否贴边)。"""
        task = _make_task()
        task.ocr = Mock(
            return_value=[
                self._text_box("当前估价：", self.LABEL_X, 130),
                self._text_box("1,912", self.BOX_RIGHT - digit_right_offset - 90, 90),
            ]
        )
        with patch.object(AutoBidAuctionTask, "width", property(lambda self: screen_width)):
            return task._read_estimate_value(
                Mock(x=self.BOX_RIGHT - 350, y=140, width=350, height=60), 1.0
            )

    def test_1080p_known_failure_shape_is_tight(self):
        """1080p 下末位只剩 4px 竖边 —— 这是实际发生过的故障形态, 必须判贴边。"""
        _, tight = self._read(1920, 4)

        self.assertTrue(tight)

    def test_same_shape_scales_and_stays_tight_at_higher_resolution(self):
        """同一故障形态在 1440p/2160p 下残边同比变宽, 固定 8px 会漏判。"""
        ratio = AutoBidAuctionTask.ESTIMATE_EDGE_MARGIN_RATIO

        for width in (2560, 3840):
            with self.subTest(width=width):
                # 按 1080p 的 4px 等比换算出的残边宽度。
                offset = round(4 * width / 1920)
                expected_margin = max(1, round(width * ratio))

                self.assertGreater(expected_margin, 8)  # 固定阈值确实不够用
                _, tight = self._read(width, offset)

                self.assertTrue(tight)

    def test_read_is_not_flagged_when_far_from_the_edge(self):
        """离边界足够远时不报贴边, 正常读数继续被采信。"""
        ratio = AutoBidAuctionTask.ESTIMATE_EDGE_MARGIN_RATIO

        for width in (1920, 2560, 3840):
            with self.subTest(width=width):
                value, tight = self._read(width, max(1, round(width * ratio)) + 3)

                self.assertFalse(tight)
                self.assertEqual(value, 1912)


class TestAuctionBidModeConfigVisibility(unittest.TestCase):
    """出价模式下的价格配置不能有字段被永久隐藏, 也不能误藏无关配置。"""

    def _visible_keys(self, task: AutoBidAuctionTask, mode: str, **overrides) -> set[str]:
        config = dict(task.default_config)
        config[AutoBidAuctionTask.CONF_BID_MODE] = mode
        config.update(overrides)
        fields = build_config_fields(config, task.config_description, task.config_type)
        return {field["key"] for field in fields}

    def test_other_configs_are_never_hidden_by_bid_mode(self):
        """出价模式只应影响价格相关配置, 别把藏品/低保金配置一起藏掉。"""
        task = _make_configured_task()
        unrelated = {
            task.CONF_SELL_MODE,
            task.CONF_SELL_INTERVAL,
            task.CONF_SELL_BEFORE_WELFARE,
            task.CONF_SELL_AFTER_WELFARE,
            task.CONF_ASSIST_FEATURES,
        }
        # 出售子项由「出售藏品模式」控制可见性, 这里固定成会展示它们的模式。
        sell_on = {task.CONF_SELL_MODE: task.SELL_MODE_INTERVAL}

        for mode in (task.BID_MODE_CUSTOM, task.BID_MODE_LIST, task.BID_MODE_ESTIMATE):
            visible = self._visible_keys(task, mode, **sell_on)
            self.assertEqual(unrelated - visible, set(), f"{mode} 下配置项被误隐藏")


class TestAuctionSellModeConfigVisibility(unittest.TestCase):
    """出售相关配置收在「出售藏品模式」下, 只有选到对应模式才展开。"""

    def _visible_keys(self, task: AutoBidAuctionTask, mode: str, **overrides) -> set[str]:
        config = dict(task.default_config)
        config[AutoBidAuctionTask.CONF_SELL_MODE] = mode
        config.update(overrides)
        fields = build_config_fields(config, task.config_description, task.config_type)
        return {field["key"] for field in fields}

    def test_default_mode_keeps_the_panel_short(self):
        """默认「拍卖成功一键出售」时, 面板上只留一个模式下拉框。"""
        task = _make_configured_task()
        self.assertEqual(task.default_config[task.CONF_SELL_MODE], task.SELL_MODE_ONE_CLICK)
        visible = self._visible_keys(task, task.SELL_MODE_ONE_CLICK)

        self.assertIn(task.CONF_SELL_MODE, visible)
        self.assertNotIn(task.CONF_SELL_INTERVAL, visible)
        self.assertNotIn(task.CONF_SELL_BEFORE_WELFARE, visible)
        self.assertNotIn(task.CONF_SELL_AFTER_WELFARE, visible)

    def test_every_mode_declares_its_sub_configs(self):
        """新增模式时漏写 sub_configs 条目, 将来给它加子项会静默不显示。"""
        task = _make_configured_task()
        declared = task.config_type[task.CONF_SELL_MODE]["sub_configs"]

        self.assertEqual(set(declared), set(task.SELL_MODES))

    def test_sell_mode_is_declared_as_a_drop_down(self):
        """模式必须是下拉框, 否则 sub_configs 不会生效。"""
        task = _make_configured_task()
        fields = build_config_fields(task.default_config, task.config_description, task.config_type)
        mode_field = next(f for f in fields if f["key"] == task.CONF_SELL_MODE)

        self.assertEqual(mode_field["kind"], "select")
        self.assertEqual(set(mode_field["options"]), set(task.SELL_MODES))

    def test_legacy_auto_clear_key_is_gone(self):
        """旧开关合并进模式后不应再注册, 否则面板上会多出一个失效控件。

        用字面量而不是常量: 常量本身已被删除, 这里守的是「这个名字不许再出现在面板上」。
        """
        task = _make_configured_task()

        for legacy in ("启用自动清理藏品", "出售藏品间隔次数"):
            with self.subTest(key=legacy):
                self.assertNotIn(legacy, task.config_type)
        self.assertNotIn("启用自动清理藏品", task.default_config)
        self.assertNotIn("启用自动清理藏品", task.config_description)


class TestAuctionConfigReachability(unittest.TestCase):
    """穷举模式组合, 保证 default_config 的每个键都至少在一个组合下可见。

    旧的可达性用例手工列举键集合, 新增配置键忘了同步集合就失去保护;
    这里直接扫 default_config 的全部键, 新键自动纳入。
    """

    def test_every_default_config_key_is_reachable_in_some_combination(self):
        """sub_configs 里的键名写错会让字段在所有模式下都隐藏, 用户根本改不了。"""
        task = _make_configured_task()
        bid_modes = task.config_type[task.CONF_BID_MODE]["options"]
        sell_modes = task.config_type[task.CONF_SELL_MODE]["options"]
        raise_modes = task.config_type[task.CONF_RAISE_MODE]["options"]
        assist_subsets = _option_subsets(task.config_type[task.CONF_ASSIST_FEATURES]["options"])
        # 子配置的可见性还取决于父开关的当前值, 这里固定成全展开。
        toggles = {
            task.CONF_AUTO_RAISE: True,
            task.CONF_SPECIAL_ROUND: True,
        }

        reachable: set[str] = set()
        for bid_mode, sell_mode, raise_mode, assists in itertools.product(
            bid_modes, sell_modes, raise_modes, assist_subsets
        ):
            config = dict(task.default_config)
            config.update(toggles)
            config[task.CONF_BID_MODE] = bid_mode
            config[task.CONF_SELL_MODE] = sell_mode
            config[task.CONF_RAISE_MODE] = raise_mode
            config[task.CONF_ASSIST_FEATURES] = list(assists)
            fields = build_config_fields(config, task.config_description, task.config_type)
            reachable |= {field["key"] for field in fields}

        self.assertEqual(set(task.default_config) - reachable, set())


class TestAuctionAssistFeaturesConfig(unittest.TestCase):
    """「启用表情包 / 启用低保金」合并成一个多选框, 面板和判定都要跟着走。"""

    def test_assist_features_render_as_a_multi_selection(self):
        """ok-script 里 bool 只能渲染成开关按钮, 要出勾选框必须声明 multi_selection。"""
        task = _make_configured_task()
        fields = build_config_fields(task.default_config, task.config_description, task.config_type)
        field = next(f for f in fields if f["key"] == task.CONF_ASSIST_FEATURES)

        self.assertEqual(field["kind"], "multi_selection")
        self.assertEqual(set(field["options"]), set(task.ASSIST_FEATURES))

    def test_default_checks_the_welfare_feature(self):
        """默认勾选「低保金」, 表情包仍需用户主动开启。"""
        task = _make_configured_task()

        self.assertEqual(task.default_config[task.CONF_ASSIST_FEATURES], [task.ASSIST_WELFARE])

    def test_legacy_assist_switches_are_gone(self):
        """旧开关合并进多选框后不应再注册, 否则面板上会多出两个失效控件。"""
        task = _make_configured_task()

        for key in ("启用表情包", "启用低保金"):
            with self.subTest(key=key):
                self.assertNotIn(key, task.default_config)
                self.assertNotIn(key, task.config_type)
                self.assertNotIn(key, task.config_description)

    def _task_with(self, value) -> AutoBidAuctionTask:
        task = _make_task()
        task.config = _config(**{AutoBidAuctionTask.CONF_ASSIST_FEATURES: value})
        return task

    def test_feature_is_enabled_only_when_checked(self):
        task = self._task_with([AutoBidAuctionTask.ASSIST_EMOTE])

        self.assertTrue(task._assist_enabled(AutoBidAuctionTask.ASSIST_EMOTE))
        self.assertFalse(task._assist_enabled(AutoBidAuctionTask.ASSIST_WELFARE))

    def test_empty_selection_disables_everything(self):
        task = self._task_with([])

        self.assertFalse(task._assist_enabled(AutoBidAuctionTask.ASSIST_EMOTE))
        self.assertFalse(task._assist_enabled(AutoBidAuctionTask.ASSIST_WELFARE))

    def test_dirty_value_disables_everything(self):
        """旧 bool 或手工填的字符串一律按未勾选处理, 不能当成已启用去点不存在的按钮。"""
        for value in (None, True, False, "", "表情包"):
            task = self._task_with(value)
            with self.subTest(value=value):
                self.assertFalse(task._assist_enabled(AutoBidAuctionTask.ASSIST_EMOTE))

    def test_emote_is_sent_only_when_checked(self):
        for features in ([AutoBidAuctionTask.ASSIST_EMOTE], []):
            task = self._task_with(features)
            task._read_asset_value = Mock(return_value=1000)
            task._wait_operate_click = Mock(return_value=True)
            task.wait_ocr = Mock(return_value=object())
            task._input_fixed_price = Mock()
            task._is_bid_screen = Mock(return_value=False)
            task._send_emote = Mock()

            task._attempt_bid(Mock(), float("inf"))

            with self.subTest(features=features):
                self.assertEqual(task._send_emote.called, bool(features))

    def test_welfare_is_claimed_only_when_checked(self):
        for features in ([AutoBidAuctionTask.ASSIST_WELFARE], []):
            task = self._task_with(features)
            task._read_asset_value = Mock(return_value=1)
            task._try_claim_welfare = Mock(return_value=True)

            task._run_post_round_actions(Mock(), None)

            with self.subTest(features=features):
                # 资产低于阈值, 勾选了低保金才会真的去领取。
                self.assertEqual(task._try_claim_welfare.called, bool(features))

    def test_asset_is_observed_even_when_welfare_unchecked(self):
        """取消勾选「低保金」不能连带停掉资产读数的日志。

        资产观测是独立的长期观测; 曾经把观测挂在低保金领取流程里, 用户一旦取消勾选
        低保金(很常见的配置), 任务运行完全正常但日志里再也看不到资产值, 静默丢观测。
        """
        for features in ([], [AutoBidAuctionTask.ASSIST_WELFARE]):
            task = self._task_with(features)
            task._read_asset_value = Mock(return_value=8_844_793)

            with self.subTest(features=features):
                task._run_post_round_actions(Mock(), None)

                self.assertIn(
                    "当前资产: 8844793",
                    [str(call.args[0]) for call in task.log_info.call_args_list],
                )


class TestAuctionRaiseModeConfig(unittest.TestCase):
    """「加价方式」只接受当前三种取值, 无效配置按默认「倍率」处理。"""

    def test_default_value_is_one_of_the_options(self):
        """默认值不在 options 里, 全新用户的下拉框会直接显示空白。"""
        task = _make_configured_task()

        self.assertIn(task.default_config[task.CONF_RAISE_MODE], task.RAISE_MODES)

    def test_every_option_controls_the_raise_value_widget(self):
        """sub_configs 的键就是选项取值, 漏一个会让「加价数值」在该方式下永久隐藏。"""
        task = _make_configured_task()

        sub_configs = task.config_type[task.CONF_RAISE_MODE]["sub_configs"]

        self.assertEqual(set(sub_configs), set(task.RAISE_MODES))
        for mode, children in sub_configs.items():
            self.assertIn(task.CONF_RAISE_VALUE, children, mode)

    def _price(self, mode: str) -> int:
        task = _make_task()
        task.config = _config(
            **{
                AutoBidAuctionTask.CONF_RAISE_MODE: mode,
                AutoBidAuctionTask.CONF_RAISE_VALUE: "1.6",
                AutoBidAuctionTask.CONF_RAISE_ROUND: 0,
            }
        )
        return task._raise_price(100, 1)

    def test_invalid_value_uses_the_default_raise_mode(self):
        """迁移已删除, 老配置或手改坏的取值按默认「倍率」处理。"""
        self.assertEqual(
            self._price("手改坏了的取值"),
            self._price(AutoBidAuctionTask.RAISE_MODE_MULTIPLE),
        )

    def test_huge_exponent_falls_back_instead_of_raising(self):
        """倍率填得稍大时 `value ** offset` 会超出可表示范围。

        只用 float 时 `100000 * 10.0 ** 400` 抛 OverflowError; 改用 Decimal 后这步
        不再抛, 但结果有 405 位有效数字, 超过默认上下文精度 28 —— 量化那一步才是真正
        抛 InvalidOperation 的地方。所以量化必须留在被吞异常的区间内, 否则等于把
        OverflowError 换成同样会漏出的 InvalidOperation, 价格永远算不出来。
        """
        task = _make_task()
        task.config = _config(
            **{
                AutoBidAuctionTask.CONF_RAISE_MODE: AutoBidAuctionTask.RAISE_MODE_MULTIPLE,
                AutoBidAuctionTask.CONF_RAISE_VALUE: "10.0",
                AutoBidAuctionTask.CONF_RAISE_ROUND: 0,
            }
        )

        self.assertEqual(task._raise_price(100000, 400), 100000)
        task.log_warning.assert_called()

    def test_normal_magnitude_is_still_quantized_to_an_integer(self):
        """守住上一条的边界: 常规量级不能被「提前挡掉大数」的逻辑一起废掉。"""
        task = _make_task()
        task.config = _config(
            **{
                AutoBidAuctionTask.CONF_RAISE_MODE: AutoBidAuctionTask.RAISE_MODE_MULTIPLE,
                AutoBidAuctionTask.CONF_RAISE_VALUE: "1.6",
                AutoBidAuctionTask.CONF_RAISE_ROUND: 0,
            }
        )

        # raise_round=0 -> offset = bid_count = 2 -> 100 * 1.6 ** 2
        self.assertEqual(task._raise_price(100, 2), 256)

    def test_long_zeros_collapse_into_the_pad_shortcut(self):
        """按键序列要按最长前缀合并, 否则 1000000 会退化成逐位按 7 次。

        键盘快捷键有 0000 与 00 两档。贪心算法只看「剩余整串是否恰好是某个快捷键」,
        1000000 会拆成 ['1','0','0','0000'] —— 4 次按键, 而最优是 ['1','0000','00']。
        """
        self.assertEqual(
            AutoBidAuctionTask._price_key_sequence("1000000"),
            ["1", "0000", "00"],
        )

    def test_key_sequence_never_changes_the_price_text(self):
        """任何价格都要能被按键序列原样拼回来, 否则会输错金额。"""
        for price in (
            "1",
            "20",
            "100",
            "1000",
            "6600",
            "10000",
            "100000",
            "300000",
            "66666",
            "1000000",
            "166660",
        ):
            with self.subTest(price=price):
                self.assertEqual(
                    "".join(AutoBidAuctionTask._price_key_sequence(price)),
                    price,
                )


class TestAuctionConfigDescriptions(unittest.TestCase):
    """配置项描述要覆盖完整, 且遵守仓库的 ASCII 标点约定。"""

    def _keys(self, task: AutoBidAuctionTask) -> list[str]:
        # _ 开头的键不渲染到面板, 不需要描述.
        return [key for key in task.default_config if not str(key).startswith("_")]

    def test_every_config_key_has_a_description(self):
        """新增配置项时忘了写描述, 面板上会出现一个没有任何说明的控件。"""
        task = _make_configured_task()

        missing = [key for key in self._keys(task) if not task.config_description.get(key)]

        self.assertEqual(missing, [])

    def test_descriptions_use_ascii_punctuation(self):
        """AGENTS.md 要求源码字符串用 ASCII `,` `;`, 避免易混淆 Unicode 告警。"""
        task = _make_configured_task()
        full_width = set("，；：（）")

        offenders = {
            key: desc for key, desc in task.config_description.items() if full_width & set(desc)
        }

        self.assertEqual(offenders, {})

    def test_no_description_for_a_removed_config_key(self):
        """删掉配置项却留下描述, 会让人以为那个键还在。"""
        task = _make_configured_task()

        orphans = [key for key in task.config_description if key not in task.default_config]

        self.assertEqual(orphans, [])

    def test_bid_price_descriptions_match_their_index(self):
        """6 个价格共用一套模板, 写错序号会让用户按错误的规则填值。"""
        task = _make_configured_task()

        for index, key in enumerate(task.CONF_BID_PRICES, start=1):
            description = task.config_description[key]
            self.assertIn(f"第 {index} 次出价的价格", description)


class TestAuctionPostRoundTimeoutGrace(unittest.TestCase):
    """结算后处理属于可选的收尾, 单轮时间用尽不该把已经结算成功的轮次判成失败。

    同一个方法里的三个步骤曾经用两套超时口径: 满仓检测和弹窗兜底走 _timeout_or_zero
    (deadline 用尽返回 0), 低保金的资产读取走 _remaining_timeout(抛 WaitFailedException)。
    异常传播出去会连带丢掉写回 _post_round_state 的那一行, 于是本轮既被记为失败,
    轮次末尾又因为 observed 是 False 而跳过出售。
    """

    def _expired(self) -> float:
        return auction_module.time.monotonic() - 1.0

    def test_asset_observe_skips_when_deadline_used_up(self):
        task = _make_task()
        task._read_asset_value = Mock(return_value=1)

        self.assertIsNone(task._observe_main_asset(Mock(), self._expired()))
        task._read_asset_value.assert_not_called()

    def test_inventory_probe_and_notice_popup_share_the_same_grace(self):
        """四个步骤的口径必须一致: 任一在过期 deadline 上抛异常都会毁掉整轮。"""
        task = _make_task()
        task._read_asset_value = Mock(return_value=1)
        expired = self._expired()

        task._dismiss_notice_popup(Mock(), expired, "探针")
        task._detect_inventory_full(Mock(), task._timeout_or_zero(expired, 3))
        task._observe_main_asset(Mock(), expired)
        task._claim_welfare_if_needed(Mock(), expired, None)

    def test_post_round_actions_still_records_observation_on_welfare_timeout(self):
        """低保金领取超时时, 观测结果仍要写回, 否则轮次末尾会连带跳过出售。"""
        task = _make_task(
            {AutoBidAuctionTask.CONF_ASSIST_FEATURES: [AutoBidAuctionTask.ASSIST_WELFARE]}
        )
        task._dismiss_notice_popup = Mock(return_value=False)
        task._claim_welfare_if_needed = Mock(side_effect=WaitFailedException("单轮拍卖超时"))

        task._run_post_round_actions(Mock(), self._expired())

        self.assertTrue(task._post_round_state.observed)
        # 领取超时不能让状态变成「今日已领完」—— 那会在还能领低保时切到放开出售的清单。
        self.assertFalse(task._welfare_quota_exhausted())

    def test_finished_round_still_sells_after_post_round_timeout(self):
        """结算后处理超时不能让本轮变成失败, 也不能连带跳过出售。"""
        from src.tasks.mixin.RoundMixin import RoundState

        task = _make_task(
            {
                AutoBidAuctionTask.CONF_SELL_MODE: AutoBidAuctionTask.SELL_MODE_FULL,
                AutoBidAuctionTask.CONF_ASSIST_FEATURES: [AutoBidAuctionTask.ASSIST_WELFARE],
            }
        )
        task._round_state = RoundState(total=0, index=1)
        task._dismiss_notice_popup = Mock(return_value=False)
        task._detect_inventory_full = Mock(return_value=True)
        task._read_asset_value = Mock(return_value=1)
        task._try_claim_welfare = Mock(side_effect=WaitFailedException("单轮拍卖超时"))
        task._sell_collections_on_interval = Mock()
        task.add_success = Mock()
        task.add_failed = Mock()
        expired = self._expired()

        def fake_round(boxes):
            # _finish_auction 在结算成功后调用结算后处理, 这里模拟同样的顺序。
            task._run_post_round_actions(boxes, expired)
            return True

        task._exec_auction_round = Mock(side_effect=fake_round)

        task._run_single_round(Mock())

        self.assertTrue(task._post_round_state.observed)
        task.add_success.assert_called_once()
        task.add_failed.assert_not_called()
        task._sell_collections_on_interval.assert_called_once()


class TestAuctionReturnToMatchScreenObservation(unittest.TestCase):
    """_stage_result 的「返回匹配界面」分支不走 _finish_auction, 但结算后观测必须照做。

    那条分支下「跳过动画」和「退出拍卖」按钮都已经不存在, 所以不能复用 _finish_auction;
    可如果因此连观测也省掉, _post_round_state.observed 会一直是 False, 轮次末尾就不出售
    —— 满仓时后续每轮都卡在「开始匹配」上, 而 _inventory_stuck 永远不会置位,
    「满仓时清理」在这条路径上完全失效。
    """

    def _task(self, title_found: bool) -> AutoBidAuctionTask:
        task = _make_task()
        task._is_match_screen = Mock(return_value=True)
        task._is_bid_screen = Mock(return_value=False)
        task.ocr = Mock(return_value=[])
        task.wait_ocr = Mock(return_value=[Mock()] if title_found else [])
        task._finish_auction = Mock()
        task._run_post_round_actions = Mock()
        return task

    def test_return_to_match_screen_runs_post_round_actions(self):
        task = self._task(title_found=True)

        finished = task._stage_result(Mock(), auction_module.time.monotonic() + 60)

        self.assertTrue(finished)
        task._finish_auction.assert_not_called()
        task._run_post_round_actions.assert_called_once()

    def test_missing_main_title_skips_post_round_actions(self):
        """标题没识别到说明画面状态未知, 不能去点不存在的仓库入口白等超时。"""
        task = self._task(title_found=False)

        task._stage_result(Mock(), auction_module.time.monotonic() + 60)

        task._run_post_round_actions.assert_not_called()

    def test_expired_deadline_does_not_raise(self):
        """deadline 用尽时按「没时间」跳过, 不能把已经结束的拍卖判成失败。"""
        task = self._task(title_found=True)

        task._observe_post_round_on_main_screen(Mock(), auction_module.time.monotonic() - 1.0)

        task._run_post_round_actions.assert_not_called()


class TestAuctionResultStageBudget(unittest.TestCase):
    """结算阶段的 RESULT_TIMEOUT 只约束「等待结算」, 不能当成收尾动作的预算。

    修复前 _stage_result 把 result_deadline(= min(整轮 deadline, now + RESULT_TIMEOUT))
    传给了 _finish_auction 与 _observe_post_round_on_main_screen。于是结算阶段空转掉
    80 秒后, 退出按钮只剩 10 秒不到可用, 满仓检测和低保金读取直接按「没时间」跳过;
    空转满 90 秒时收尾动作会抛「单轮拍卖超时」—— 而整轮其实还剩 500 多秒, 一个已经
    拍成的轮次被记成失败, 而且 _post_round_state 的写回被跳过, 轮次末尾也不出售。
    """

    def setUp(self):
        self.clock = _FakeTime()
        patcher = patch.object(auction_module, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _task(self, spins: int, *, mode: str = "skip") -> AutoBidAuctionTask:
        task = _make_task({AutoBidAuctionTask.CONF_SELL_MODE: AutoBidAuctionTask.SELL_MODE_OFF})
        task.sleep = Mock(side_effect=self.clock.sleep)
        task.next_frame = Mock()
        task.wait_ocr = Mock(return_value=[Mock()])
        task.operate_click = Mock()
        task._is_bid_screen = Mock(return_value=False)
        task._is_skip_screen = Mock(return_value=False)
        task._is_match_screen = Mock(return_value=False)

        counter = {"n": 0}

        def tick(*args, **kw):
            counter["n"] += 1
            # mode="skip": 第 spins+1 轮才读到「跳过动画」
            # mode="match": 始终不命中「跳过动画」, 由 _is_match_screen 触发返回匹配界面分支
            return [Mock()] if (mode == "skip" and counter["n"] > spins) else []

        task.ocr = tick
        if mode == "match":
            # 用**循环轮次**而不是 tick 调用次数做判据: _is_match_screen 排在 skip 判定
            # 之前(见 AutoBidAuctionTask._stage_result), 同一轮里它先被调用, 此时 tick
            # 还没跑过, counter["n"] 仍是上一轮的值 —— 按调用次数判定会晚一轮才命中,
            # 空转到 result_deadline 之后循环先退出, 这个用例就永远走不到 match 分支
            # (它此前能通过, 纯靠 skip 分支意外兜住)。
            loop = {"n": 0}

            def match_screen(boxes):
                loop["n"] += 1
                return loop["n"] > spins

            task._is_match_screen = match_screen
        return task

    def test_finish_auction_receives_round_deadline(self):
        """结算空转 89 秒后, 收尾动作拿到的仍是整轮 deadline, 不是阶段 deadline。"""
        task = self._task(int(89.0 / AutoBidAuctionTask.POLL_INTERVAL))
        round_deadline = self.clock.now + AutoBidAuctionTask.ROUND_TIMEOUT
        task._finish_auction = Mock()

        task._stage_result(Mock(), round_deadline)

        task._finish_auction.assert_called_once()
        self.assertEqual(task._finish_auction.call_args[0][2], round_deadline)

    def test_return_to_match_branch_receives_round_deadline(self):
        """「返回匹配界面」分支同理, 否则结算后观测会被整段跳过。"""
        task = self._task(int(89.0 / AutoBidAuctionTask.POLL_INTERVAL), mode="match")
        round_deadline = self.clock.now + AutoBidAuctionTask.ROUND_TIMEOUT
        task._observe_post_round_on_main_screen = Mock()

        task._stage_result(Mock(), round_deadline)

        task._observe_post_round_on_main_screen.assert_called_once()
        self.assertEqual(task._observe_post_round_on_main_screen.call_args[0][1], round_deadline)

    def test_slow_settlement_still_finishes_the_round(self):
        """结算空转 89.5 秒后走完整个收尾不能抛异常(修复前抛「单轮拍卖超时」)。"""
        task = self._task(int(89.5 / AutoBidAuctionTask.POLL_INTERVAL))
        task._run_post_round_actions = Mock()
        round_deadline = self.clock.now + AutoBidAuctionTask.ROUND_TIMEOUT

        self.assertTrue(task._stage_result(Mock(), round_deadline))
        task._run_post_round_actions.assert_called_once()

    def test_skip_area_reading_does_not_shadow_the_match_branch(self):
        """skip 区域与主界面标题框重叠: 同一帧两者可能同时命中。

        BOX_SKIP(0.703,0.902,0.807,0.953) 与 BOX_MATCH(0.7427,0.8972,0.8360,0.9472)
        高度重叠, 所谓「跳过动画」其实是在主界面同一位置读到的文字。修复前 skip 判定
        排在 match 之前, 命中 skip 就去 _finish_auction —— 可画面已经在主界面, 找不到
        退出按钮而抛异常, _run_post_round_actions 被跳过, 满仓检测/低保金/轮次末尾出售
        全部丢失。所以 match 必须排在 skip 前面。
        """
        task = self._task(0, mode="match")
        # 让 skip 区域也读到内容: 旧顺序下会抢在 match 之前命中
        task.ocr = Mock(return_value=[Mock()])
        task._is_match_screen = Mock(return_value=True)
        task._finish_auction = Mock()
        task._observe_post_round_on_main_screen = Mock()
        round_deadline = self.clock.now + AutoBidAuctionTask.ROUND_TIMEOUT

        self.assertTrue(task._stage_result(Mock(), round_deadline))

        task._observe_post_round_on_main_screen.assert_called_once()
        task._finish_auction.assert_not_called()

    def test_slow_settlement_on_match_branch_keeps_observation(self):
        """「返回匹配界面」分支空转 89.5 秒后, 结算后观测仍要执行。"""
        task = self._task(int(89.5 / AutoBidAuctionTask.POLL_INTERVAL), mode="match")
        task._run_post_round_actions = Mock()
        round_deadline = self.clock.now + AutoBidAuctionTask.ROUND_TIMEOUT

        self.assertTrue(task._stage_result(Mock(), round_deadline))
        task._run_post_round_actions.assert_called_once()

    def test_settlement_wait_itself_is_still_bounded(self):
        """等待结算本身仍受 RESULT_TIMEOUT 约束, 修复不能把这条上限一起放开。

        循环体每轮耗时按 1.2 秒构造(真实环境里 next_frame + 三次 OCR 就是这样),
        否则 RESULT_MAX_LOOPS(180) x POLL_INTERVAL(0.5) 恰好也是 90 秒, 两个条件
        同时到点, 这条断言就分不出到底是时间上限还是次数上限在起作用。
        """
        task = self._task(0)
        task.ocr = Mock(return_value=[])
        task.sleep = Mock(side_effect=lambda seconds: self.clock.sleep(1.2))
        started = self.clock.now
        round_deadline = self.clock.now + AutoBidAuctionTask.ROUND_TIMEOUT

        with self.assertRaises(WaitFailedException):
            task._stage_result(Mock(), round_deadline)

        elapsed = self.clock.now - started
        self.assertLessEqual(elapsed, AutoBidAuctionTask.RESULT_TIMEOUT + 1.2)
        self.assertLess(elapsed, AutoBidAuctionTask.RESULT_MAX_LOOPS * 1.2)


class TestAuctionInstructions(unittest.TestCase):
    """任务卡上的「说明」按钮由 task.instructions 驱动。

    instructions 为空时框架会直接把按钮隐藏, 用户看不到任何说明; 内容过长又会让
    qfluentwidgets 的 Dialog 把唯一的「确定」按钮挤出屏幕(该 Dialog 没有滚动区也没有
    高度钳制)。所以这里同时守住「控件存在」「覆盖了面板的每个选项」「行数不超上限」。
    """

    # 遮罩尺寸 = 父窗口(src/config.py 的 window_size = 1200x800), 弹窗高于它就会居中
    # 溢出、上下一起被裁, 底部的「确定」按钮随之消失。实测每行约 16px + 185px 固定开销
    # (标题/边距/按钮行) -> 800px 父窗口的硬上限约 38 行, 留足余量后取 30。
    MAX_INSTRUCTION_LINES = 30

    def test_task_exposes_gui_instructions(self):
        task = _make_configured_task()

        self.assertTrue(task.instructions)

    def test_instructions_fit_the_dialog_height(self):
        """行数超上限时弹窗会高过父窗口, 内容下方的「确定」按钮被裁掉, 用户关不掉。"""
        lines = auction_module.INST.count("<br>") + 1

        self.assertLessEqual(lines, self.MAX_INSTRUCTION_LINES)

    def test_instructions_document_every_panel_option(self):
        text = auction_module.INST
        documented = [
            "循环次数",
            AutoBidAuctionTask.CONF_BID_MODE,
            AutoBidAuctionTask.BID_MODE_ESTIMATE,
            AutoBidAuctionTask.BID_MODE_CUSTOM,
            AutoBidAuctionTask.BID_MODE_LIST,
            AutoBidAuctionTask.CONF_ESTIMATE_RATIO,
            AutoBidAuctionTask.CONF_FIXED_PRICE,
            AutoBidAuctionTask.CONF_AUTO_RAISE,
            AutoBidAuctionTask.CONF_RAISE_MODE,
            AutoBidAuctionTask.CONF_RAISE_VALUE,
            AutoBidAuctionTask.CONF_RAISE_ROUND,
            AutoBidAuctionTask.CONF_SPECIAL_ROUND,
            AutoBidAuctionTask.CONF_SPECIAL_ROUND_PRICE,
            # 6 个出价价格在说明里以首尾两个标签概括, 不再逐条列出。
            AutoBidAuctionTask.CONF_BID_PRICES[0],
            AutoBidAuctionTask.CONF_BID_PRICES[-1],
            AutoBidAuctionTask.CONF_SELL_MODE,
            *AutoBidAuctionTask.SELL_MODES,
            AutoBidAuctionTask.CONF_SELL_INTERVAL,
            AutoBidAuctionTask.CONF_SELL_BEFORE_WELFARE,
            AutoBidAuctionTask.CONF_SELL_AFTER_WELFARE,
            AutoBidAuctionTask.CONF_ASSIST_FEATURES,
            *AutoBidAuctionTask.ASSIST_FEATURES,
        ]
        for label in documented:
            with self.subTest(label=label):
                self.assertIn(label, text)

    def test_instructions_warn_about_the_last_bid_round(self):
        """第 6 次必须高于第 5 次是唯一会让整轮出价被拒的硬约束, 必须在说明里点明。"""
        text = auction_module.INST

        self.assertIn(f"第 {AutoBidAuctionTask.MAX_BID_ROUNDS} 次", text)
        self.assertIn(f"第 {AutoBidAuctionTask.MAX_BID_ROUNDS - 1} 次", text)

    def test_instructions_warn_about_empty_sell_lists(self):
        """两个出售清单都不勾等于不卖任何藏品, 说明里必须把这个后果写出来。"""
        text = auction_module.INST

        self.assertIn("两个都不勾", text)
        self.assertIn("不卖任何藏品", text)

    def test_instructions_have_an_upgrade_notes_section(self):
        """废弃过配置键的版本必须交代旧键去向, 否则老用户升级后只会觉得功能丢了。

        用字面量而不是常量: 迁移已删除, 被废弃的键不再有对应常量, 这里守的是「说明里
        必须逐个点名」。
        """
        text = auction_module.INST

        self.assertIn("升级后必看", text)
        for legacy in (
            "启用自动清理藏品",
            "启用表情包",
            "启用低保金",
            "保留藏品品质",
            "满仓或领低保后追加出售品质",
        ):
            with self.subTest(legacy=legacy):
                self.assertIn(legacy, text)

    def test_instructions_point_at_the_reset_config_button(self):
        """要让本机现存配置回到默认只能靠面板的「重置配置」, 必须写明入口和代价。"""
        text = auction_module.INST

        self.assertIn("重置配置", text)
        self.assertIn("清掉", text)


if __name__ == "__main__":
    unittest.main()
