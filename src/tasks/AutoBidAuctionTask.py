import re
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from ok import Box, TaskDisabledException, WaitFailedException

from src.tasks.BaseNTETask import BaseNTETask
from src.tasks.NTEOneTimeTask import NTEOneTimeTask

# --- 拍卖界面 OCR 正则 ---
# 集中定义, 避免各阶段重复编译同一规则。
RE_MATCH = re.compile(r"开始匹配|开始|匹配")
RE_CONFIRM = re.compile(r"确\s*认")
RE_BID = re.compile(r"出\s*价")
RE_SKIP = re.compile(r"跳\s*过")
RE_EXIT = re.compile(r"退\s*出")
RE_BID_CONFIRM = re.compile(r"确认出价")
RE_BID_PANEL_READY = re.compile(r"确认出价|[0-9]")
RE_NUMBER = re.compile(r"[0-9\uff10-\uff19,]+")
RE_CONFIRM_ANY = re.compile(r"确认")
RE_MAIN_ASSET_TITLE = re.compile(r"我的资产")
RE_COLLECTION_INSUFFICIENT = re.compile(r"少于200格")
RE_WELFARE = re.compile(r"低保金")
RE_CLAIM = re.compile(r"领取")
RE_CANCEL = re.compile(r"取消")
RE_WAREHOUSE = re.compile(r"藏品仓库")

# 全角数字转半角, 用于统一资产与价格的 OCR 文本。
FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# 数字键盘上一次点击即可输入的快捷键, 需优先于逐位输入。
PAD_SHORTCUTS = ("0000", "00")


class AuctionState(Enum):
    """单轮拍卖过程中可检测到的界面状态。"""

    CONFIRM = "confirm"
    BID = "bid"
    SKIP = "skip"


@dataclass(frozen=True)
class AuctionBoxes:
    """单轮拍卖使用的 UI 区域, 在轮次开始时按相对比例一次性构建。"""

    match: Box
    confirm: Box
    bid: Box
    skip_area: Box
    exit: Box
    bid_confirm: Box
    abandon: Box
    abandon_confirm: Box
    asset_value: Box
    last_bid: Box
    clear: Box
    price_result: Box
    exception_area: Box
    main_asset_title: Box
    main_asset: Box
    insufficient: Box
    welfare_btn: Box
    claim: Box
    cancel: Box
    warehouse_btn: Box
    warehouse_title: Box
    sell: Box
    confirm_sell: Box
    blank: Box
    close: Box


class AutoBidAuctionTask(NTEOneTimeTask, BaseNTETask):
    """自动完成游戏内拍卖流程。

    功能包括: 匹配, 确认, 出价, 出价重试, 结算, 低保金领取, 表情包发送, 藏品出售。
    需要在拍卖主界面选择低级会场后开始执行。
    """

    # --- 拍卖配置 ---
    CONF_FIXED_PRICE = "基础价"
    CONF_SELL_INTERVAL = "出售藏品间隔次数"
    CONF_KEEP_QUALITIES = "保留藏品品质"

    # 自动加价配置.
    CONF_AUTO_RAISE = "启用自动加价"
    CONF_RAISE_MODE = "加价方式"
    CONF_RAISE_VALUE = "加价数值"
    CONF_RAISE_ROUND = "加价回合数"

    # 指定回合出价配置.
    CONF_SPECIAL_ROUND = "启用指定回合单独出价"
    CONF_SPECIAL_ROUNDS = "指定回合(可多选)"
    CONF_SPECIAL_ROUND_PRICE = "指定回合价格"

    # 拍卖辅助功能.
    CONF_USE_EMOTE = "启用表情包"
    CONF_USE_WELFARE = "启用低保金"
    CONF_AUTO_CLEAR_COLLECTIONS = "启用自动清理藏品"

    # --- UI 坐标 (相对比例) ---
    # 主界面按钮.
    BOX_MATCH = (0.7427, 0.8972, 0.8360, 0.9472)  # 开始匹配
    BOX_CONFIRM = (0.535, 0.633, 0.666, 0.681)  # 确认按钮
    BOX_BID = (0.882, 0.913, 0.930, 0.953)  # 出价按钮
    BOX_SKIP_AREA = (0.703, 0.902, 0.807, 0.953)  # 跳过区域
    BOX_EXIT = (0.853, 0.900, 0.961, 0.949)  # 退出拍卖
    BOX_BID_CONFIRM = (0.649, 0.868, 0.726, 0.911)  # 确认出价

    # 出价面板.
    BOX_ABANDON = (0.7276, 0.9083, 0.7833, 0.9583)  # 放弃按钮
    BOX_ABANDON_CONFIRM = (0.5474, 0.6389, 0.6714, 0.6861)  # 放弃确认
    BOX_ASSET_VALUE = (0.8583, 0.0426, 0.9870, 0.0806)  # 出价面板资产
    BOX_LAST_BID = (0.473, 0.733, 0.546, 0.807)  # 上轮出价
    BOX_CLEAR = (0.488, 0.859, 0.533, 0.917)  # 清除按钮
    BOX_PRICE_RESULT = (0.588, 0.685, 0.783, 0.747)  # 输入价格结果
    BOX_EXCEPTION_AREA = (0.579, 0.641, 0.634, 0.681)  # 异常确认框

    # 主界面 / 结算.
    BOX_MAIN_ASSET_TITLE = (0.555, 0.038, 0.617, 0.081)  # 主界面资产标题
    BOX_MAIN_ASSET = (0.670, 0.025, 0.830, 0.095)  # 主界面资产数值
    BOX_INSUFFICIENT = (0.240, 0.467, 0.747, 0.536)  # 库存不足提示

    # 低保金.
    BOX_WELFARE_BTN = (0.8266, 0.0398, 0.8984, 0.0778)
    BOX_CLAIM = (0.576, 0.636, 0.632, 0.685)
    BOX_CANCEL = (0.370, 0.637, 0.421, 0.684)

    # 藏品仓库.
    BOX_WAREHOUSE_BTN = (0.2109, 0.8583, 0.2740, 0.9713)
    BOX_WAREHOUSE_TITLE = (0.058, 0.032, 0.130, 0.081)
    BOX_SELL = (0.931, 0.860, 0.949, 0.900)
    BOX_CONFIRM_SELL = (0.862, 0.863, 0.886, 0.917)
    BOX_BLANK = (0.442, 0.851, 0.564, 0.917)
    BOX_CLOSE = (0.950, 0.045, 0.963, 0.073)

    # 品质按钮 (白, 绿, 蓝, 紫, 橙, 红), 与 QUALITY_KEYS 一一对应.
    QUALITY_KEYS = ["品质白", "品质绿", "品质蓝", "品质紫", "品质橙", "品质红"]
    QUALITY_BOXES = (
        (0.682, 0.799, 0.687, 0.819),
        (0.730, 0.799, 0.735, 0.813),
        (0.779, 0.800, 0.788, 0.816),
        (0.829, 0.801, 0.838, 0.818),
        (0.877, 0.799, 0.886, 0.816),
        (0.927, 0.799, 0.936, 0.819),
    )

    # 数字键盘映射.
    PAD_MAP = {
        "0": (0.223, 0.862, 0.256, 0.924),
        "1": (0.224, 0.510, 0.256, 0.571),
        "2": (0.308, 0.505, 0.344, 0.573),
        "3": (0.394, 0.506, 0.433, 0.568),
        "4": (0.232, 0.629, 0.254, 0.683),
        "5": (0.310, 0.629, 0.343, 0.687),
        "6": (0.399, 0.626, 0.432, 0.690),
        "7": (0.226, 0.744, 0.252, 0.812),
        "8": (0.313, 0.743, 0.346, 0.812),
        "9": (0.401, 0.747, 0.434, 0.805),
        "00": (0.304, 0.853, 0.356, 0.935),
        "0000": (0.383, 0.855, 0.449, 0.932),
    }

    # 表情包点击坐标 (相对坐标, 非 Box).
    EMOTE_BTN = (0.036, 0.910)
    EMOTE_FIRST = (0.164, 0.516)

    # 指定回合下拉框候选项.
    SPECIAL_ROUND_OPTIONS = [str(index) for index in range(1, 7)]

    # --- 阶段超时 (秒) ---
    # 单轮拍卖的硬上限, 防止各阶段局部超时叠加后长期卡住任务.
    ROUND_TIMEOUT = 600
    MATCH_TIMEOUT = 120
    MATCH_CLICK_TIMEOUT = 30
    BID_RESULT_TIMEOUT = 60
    RESULT_TIMEOUT = 90
    ASSET_OCR_TIMEOUT = 15

    # --- 轮询与重试 ---
    POLL_INTERVAL = 0.5
    MATCH_MAX_LOOPS = 120
    RESULT_MAX_LOOPS = 180
    MAX_FAILURES = 3
    BID_MAX_RETRIES = 3

    # 资产低于该值时领取低保金.
    WELFARE_ASSET_THRESHOLD = 100000

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.supported_languages = ["zh_CN"]
        self.name = "自动拍卖"
        self.description = "在拍卖主界面, 选择低级会场后开始"
        self.group_name = "都市闲趣"
        self.add_rounds_config()

        self.default_config.update(
            {
                self.CONF_SELL_INTERVAL: 0,
                self.CONF_KEEP_QUALITIES: ["品质红"],
                self.CONF_AUTO_RAISE: False,
                self.CONF_FIXED_PRICE: 1,
                self.CONF_RAISE_MODE: "倍数",
                self.CONF_RAISE_VALUE: "1.6",
                self.CONF_RAISE_ROUND: 2,
                self.CONF_SPECIAL_ROUND: False,
                self.CONF_SPECIAL_ROUNDS: ["5"],
                self.CONF_SPECIAL_ROUND_PRICE: "66666",
                self.CONF_USE_EMOTE: False,
                self.CONF_USE_WELFARE: False,
                self.CONF_AUTO_CLEAR_COLLECTIONS: False,
            }
        )

        # 定义下拉框和条件子配置的控件类型.
        self.config_type = {
            self.CONF_RAISE_MODE: {
                "options": ["倍数", "自定义", "百分比"],
                "sub_configs": {
                    "倍数": [self.CONF_RAISE_VALUE],
                    "自定义": [self.CONF_RAISE_VALUE],
                    "百分比": [self.CONF_RAISE_VALUE],
                },
            },
            self.CONF_KEEP_QUALITIES: {
                "type": "multi_selection",
                "options": list(self.QUALITY_KEYS),
            },
            # 仅在开关启用时显示指定回合配置.
            self.CONF_SPECIAL_ROUND: {
                "sub_configs": {
                    True: [self.CONF_SPECIAL_ROUNDS, self.CONF_SPECIAL_ROUND_PRICE],
                }
            },
            self.CONF_SPECIAL_ROUNDS: {
                "type": "multi_selection",
                "options": list(self.SPECIAL_ROUND_OPTIONS),
            },
        }

        self.config_description.update(
            {
                self.CONF_SELL_INTERVAL: "设置为0则不出售",
                self.CONF_USE_EMOTE: "收藏的第一个表情包",
                self.CONF_AUTO_CLEAR_COLLECTIONS: "启用会禁用出售间隔",
                self.CONF_AUTO_RAISE: "在自定义价格基础上自动加价",
                self.CONF_RAISE_MODE: "倍数: 基础价*(倍数^出价次数), 百分比: 基础价*(1+百分比/100*"
                "出价次数), 自定义: 基础价+自定义值*出价次数",
                self.CONF_RAISE_VALUE: "加价数值(支持小数)",
                self.CONF_RAISE_ROUND: "0为从第1次出价开始加, N为从第N次出价开始加",
                self.CONF_FIXED_PRICE: "固定出价(自定义)",
                self.CONF_USE_WELFARE: "我的资产低于10万领取",
                self.CONF_KEEP_QUALITIES: "勾选品质不会被出售",
                self.CONF_SPECIAL_ROUND: "启用指定回合单独出价",
                self.CONF_SPECIAL_ROUNDS: "勾选需要使用单独价格的回合(可多选)",
                self.CONF_SPECIAL_ROUND_PRICE: "这些回合使用的价格(整数)",
            }
        )

        self.last_bid_price = None
        self.current_bid_count = 0
        self.add_exit_after_config()

    # --- 任务入口 ---
    def run(self):
        """任务入口, 确保游戏窗口捕获和连接已就绪。"""
        super().run()
        try:
            self.do_run()
        except TaskDisabledException:
            raise
        except Exception as e:
            self.log_error("自动拍卖任务执行异常", e)
            raise

    def do_run(self):
        """主执行逻辑, 使用基类的轮次管理框架。"""
        self.start_rounds()
        boxes = self._build_boxes()
        try:
            while self.has_remaining_rounds():
                if not self.begin_round():
                    break
                try:
                    self._run_single_round(boxes)
                except TaskDisabledException:
                    raise
                except Exception as e:
                    self.add_failed("拍卖执行异常")
                    self.log_error(f"本轮拍卖失败: {type(e).__name__}: {e}")
                    self.sleep(2)
        finally:
            self.finish_rounds()

    def _build_boxes(self) -> AuctionBoxes:
        """按相对比例一次性构建本轮拍卖使用的全部 UI 区域。"""
        screen = self.box_of_screen
        return AuctionBoxes(
            match=screen(*self.BOX_MATCH),
            confirm=screen(*self.BOX_CONFIRM),
            bid=screen(*self.BOX_BID),
            skip_area=screen(*self.BOX_SKIP_AREA),
            exit=screen(*self.BOX_EXIT),
            bid_confirm=screen(*self.BOX_BID_CONFIRM),
            abandon=screen(*self.BOX_ABANDON),
            abandon_confirm=screen(*self.BOX_ABANDON_CONFIRM),
            asset_value=screen(*self.BOX_ASSET_VALUE),
            last_bid=screen(*self.BOX_LAST_BID),
            clear=screen(*self.BOX_CLEAR),
            price_result=screen(*self.BOX_PRICE_RESULT),
            exception_area=screen(*self.BOX_EXCEPTION_AREA),
            main_asset_title=screen(*self.BOX_MAIN_ASSET_TITLE),
            main_asset=screen(*self.BOX_MAIN_ASSET),
            insufficient=screen(*self.BOX_INSUFFICIENT),
            welfare_btn=screen(*self.BOX_WELFARE_BTN),
            claim=screen(*self.BOX_CLAIM),
            cancel=screen(*self.BOX_CANCEL),
            warehouse_btn=screen(*self.BOX_WAREHOUSE_BTN),
            warehouse_title=screen(*self.BOX_WAREHOUSE_TITLE),
            sell=screen(*self.BOX_SELL),
            confirm_sell=screen(*self.BOX_CONFIRM_SELL),
            blank=screen(*self.BOX_BLANK),
            close=screen(*self.BOX_CLOSE),
        )

    def _run_single_round(self, boxes: AuctionBoxes) -> None:
        """执行一轮拍卖, 记录结果并按需触发定期出售。"""
        if self._exec_auction_round(boxes):
            self.add_success()
        else:
            self.add_failed("结果阶段进入下一轮出价")

        self.log_info(f"本轮拍卖完成 ({self.current_round}/{self._round_state.total_text})")
        self._sell_collections_on_interval(boxes)

    # --- 单轮流程编排 ---
    def _exec_auction_round(self, boxes: AuctionBoxes) -> bool:
        """执行单轮拍卖, 按序调度各阶段。

        Returns:
            bool: 拍卖是否顺利进入结算(进入下一轮出价时返回 False)。
        """
        deadline = time.monotonic() + self.ROUND_TIMEOUT
        self.info_set("当前阶段", "匹配中")
        self.log_info(f"拍卖开始, 单轮最长运行 {self.ROUND_TIMEOUT} 秒")
        self.sleep(0.5)

        state = self._stage_match(boxes, deadline)
        if state is AuctionState.SKIP:
            return self._stage_result(boxes, deadline)

        state = self._ensure_confirm_stage(boxes, state, deadline)
        if state is AuctionState.SKIP:
            return self._stage_result(boxes, deadline)

        self.info_set("当前阶段", "出价中")
        if state is AuctionState.CONFIRM:
            self._wait_bid_screen(boxes, deadline)

        self._stage_bid_loop(boxes, deadline)

        self.info_set("当前阶段", "结算中")
        return self._stage_result(boxes, deadline)

    def _ensure_confirm_stage(
        self, boxes: AuctionBoxes, state: AuctionState, deadline: float
    ) -> AuctionState:
        """确保确认阶段完成, 失败时回到匹配阶段重试, 仍受同一 deadline 约束。"""
        self.info_set("当前阶段", "确认中" if state is AuctionState.CONFIRM else "出价中")
        if state is not AuctionState.CONFIRM:
            self.log_info("当前已在出价界面, 跳过确认阶段")
            return state

        if self._stage_confirm(boxes, deadline):
            return state

        self.log_warning("确认阶段未完成, 回到匹配阶段重试, 不重置单轮超时")
        state = self._stage_match(boxes, deadline)
        if state is AuctionState.SKIP:
            return state
        if state is AuctionState.CONFIRM and not self._stage_confirm(boxes, deadline):
            raise WaitFailedException("确认阶段连续失败")
        return state

    def _wait_bid_screen(self, boxes: AuctionBoxes, deadline: float) -> None:
        """等待确认后的出价界面加载完成。"""
        self.log_info("等待进入出价界面")
        ready = self.wait_until(
            lambda: self._is_bid_screen(boxes),
            time_out=self._remaining_timeout(deadline, 60),
            settle_time=0.5,
            post_action=lambda: self.sleep(0.5),
            raise_if_not_found=False,
        )
        if not ready:
            raise WaitFailedException("等待出价界面超时")

    # --- 各阶段实现 ---
    def _stage_match(self, boxes: AuctionBoxes, deadline: float) -> AuctionState:
        """匹配阶段: 等待进入确认或出价状态。

        仅在检测到"开始匹配"按钮时才尝试点击, 避免界面过渡期无谓的阻塞。
        deadline 是整轮拍卖的绝对截止时间, 不会因点击重试而重置。
        """
        self.log_info("匹配阶段开始, 等待确认或出价界面")
        stage_deadline = min(deadline, time.monotonic() + self.MATCH_TIMEOUT)
        fail_count = 0
        loop_count = 0

        while loop_count < self.MATCH_MAX_LOOPS and time.monotonic() < stage_deadline:
            loop_count += 1
            # 显式取一帧: 下面四次 ocr 共用同一帧, 避免依赖 self.sleep 清空缓存的副作用.
            self.next_frame()

            # 优先快速检测当前界面状态.
            if self._is_bid_screen(boxes):
                self.log_info("检测到已在出价界面")
                return AuctionState.BID
            if self._is_confirm_screen(boxes):
                self.log_info("检测到已在确认界面")
                return AuctionState.CONFIRM
            if self._is_skip_screen(boxes):
                self.log_info("匹配阶段检测到跳过动画, 拍卖已意外结束")
                return AuctionState.SKIP

            # 仅在"开始匹配"按钮确实存在时才尝试点击.
            if self._is_match_screen(boxes):
                try:
                    result = self._handle_match_click(boxes, stage_deadline)
                    if result is not None:
                        return result
                except TaskDisabledException:
                    raise
                except WaitFailedException:
                    fail_count += 1
                    self.log_warning(
                        f"匹配状态等待失败 ({fail_count}/{self.MAX_FAILURES}), 将继续重试"
                    )
                    if fail_count >= self.MAX_FAILURES:
                        raise WaitFailedException("匹配阶段连续失败")

            self.sleep(self.POLL_INTERVAL)

        raise WaitFailedException("匹配阶段超时, 未进入确认或出价界面")

    def _handle_match_click(self, boxes: AuctionBoxes, deadline: float) -> AuctionState | None:
        """点击开始匹配, 并等待后续界面状态变化。

        使用统一循环同时检测确认, 出价和跳过界面, 覆盖匹配对局与加载动画。
        内部等待最多 MATCH_CLICK_TIMEOUT 秒, 同时不能超过整轮 deadline。
        """
        clicked = self.wait_click_ocr(
            box=boxes.match,
            match=RE_MATCH,
            time_out=self._remaining_timeout(deadline, 5),
            raise_if_not_found=False,
        )
        if not clicked:
            return None
        self.log_info("已点击开始匹配, 等待状态变化")

        click_deadline = min(deadline, time.monotonic() + self.MATCH_CLICK_TIMEOUT)
        while time.monotonic() < click_deadline:
            self.next_frame()
            if self._is_confirm_screen(boxes):
                self.log_info("匹配成功, 进入确认阶段")
                return AuctionState.CONFIRM
            if self._is_bid_screen(boxes):
                self.log_info("匹配成功, 进入出价阶段")
                return AuctionState.BID
            if self._is_skip_screen(boxes):
                self.log_info("匹配阶段检测到跳过动画")
                return AuctionState.SKIP
            self.sleep(self.POLL_INTERVAL)

        self.log_warning(
            f"点击匹配后 {self.MATCH_CLICK_TIMEOUT} 秒内未检测到后续界面, 将重新检查匹配按钮"
        )
        return None

    def _stage_confirm(self, boxes: AuctionBoxes, deadline: float) -> bool:
        """确认阶段: 等待并点击确认按钮, 所有等待受整轮 deadline 约束。"""
        self.log_info("确认阶段开始, 等待确认按钮")
        found = self.wait_ocr(
            box=boxes.confirm,
            match=RE_CONFIRM,
            time_out=self._remaining_timeout(deadline, 15),
            raise_if_not_found=False,
            settle_time=0.5,
        )
        if not found:
            self.log_warning("确认按钮在阶段等待时间内未出现")
            return False

        self.operate_click(boxes.confirm, after_sleep=0.5)
        confirmed = self.wait_until(
            lambda: not self._is_confirm_screen(boxes),
            time_out=self._remaining_timeout(deadline, 5),
            settle_time=0.5,
            raise_if_not_found=False,
        )
        if not confirmed:
            self.log_warning("点击确认按钮后按钮仍存在, 本次确认失败")
            return False

        self.log_info("确认完成, 等待进入出价界面")
        return True

    def _stage_bid_loop(self, boxes: AuctionBoxes, deadline: float) -> None:
        """出价阶段: 循环出价直到拍卖结束, 支持多轮竞拍。

        每次出价结果最多等待 BID_RESULT_TIMEOUT 秒, 整个阶段仍受单轮 deadline 约束。
        """
        # 每轮拍卖开始前重置出价计数和上次价格.
        self.current_bid_count = 0
        self.last_bid_price = None

        retry = 0
        # 循环只通过 return(拍卖结束) 或 raise(连续失败/超时) 退出, 所以用 while True.
        # 出价失败一律抛异常, 连续失败达到 BID_MAX_RETRIES 时在 except 分支抛出.
        while True:
            self._remaining_timeout(deadline, 0.1)
            try:
                self._attempt_bid(boxes, deadline)
            except TaskDisabledException:
                raise
            except Exception as e:
                retry += 1
                self.log_warning(
                    f"出价异常 ({retry}/{self.BID_MAX_RETRIES}): {type(e).__name__}: {e}"
                )
                if retry >= self.BID_MAX_RETRIES:
                    raise
                self.sleep(1)
                continue

            # 出价成功后重置失败重试计数, 用于下一次出价.
            retry = 0
            self.current_bid_count += 1
            self.log_info(f"第 {self.current_bid_count} 次出价成功, 等待拍卖结果或加价")

            if self._wait_bid_outcome(boxes, deadline):
                return

    def _wait_bid_outcome(self, boxes: AuctionBoxes, deadline: float) -> bool:
        """等待本次出价的结果。

        Returns:
            bool: True 表示拍卖已结束; False 表示有人加价, 需要继续下一次出价。
        """
        wait_deadline = min(deadline, time.monotonic() + self.BID_RESULT_TIMEOUT)
        while time.monotonic() < wait_deadline:
            self.next_frame()
            if self._is_skip_screen(boxes):
                self.log_info("检测到跳过动画, 拍卖结束")
                return True
            if self._is_match_screen(boxes):
                self.log_info("返回匹配界面, 拍卖结束")
                return True
            if self._is_bid_screen(boxes):
                self.log_info("检测到有人加价, 准备再次出价")
                return False
            self.sleep(self.POLL_INTERVAL)

        if time.monotonic() >= deadline:
            raise WaitFailedException("单轮拍卖超时")
        self.log_info(f"本次出价结果等待 {self.BID_RESULT_TIMEOUT} 秒未变化, 按拍卖结束处理")
        return True

    def _attempt_bid(self, boxes: AuctionBoxes, deadline: float) -> None:
        """单次出价尝试: 包含资产识别, 出价面板确认和可选表情包动作。

        失败时抛出 WaitFailedException, 由调用方决定是否重试。
        """
        # 等待确认后的加载动画完成, 再判断资产值.
        asset_value = self._read_asset_value(
            boxes.asset_value, self._remaining_timeout(deadline, self.ASSET_OCR_TIMEOUT)
        )
        if asset_value is None:
            self.log_warning("资产值识别失败, 准备重试本次出价")
            raise WaitFailedException("资产值未识别")

        # 资产明确为 0 时放弃本轮出价.
        if asset_value == 0:
            self.log_info("当前资产值为 0, 放弃本轮出价")
            self.operate_click(boxes.abandon, after_sleep=0.5)
            self.sleep(0.5)
            self.operate_click(boxes.abandon_confirm, after_sleep=0.5)
            return

        self.log_debug(f"当前资产值为 {asset_value}, 不等于 0, 继续执行出价")

        # 继续执行常规出价流程.
        self.log_info("等待出价按钮")
        found = self.wait_click_ocr(
            box=boxes.bid,
            match=RE_BID,
            time_out=self._remaining_timeout(deadline, 10),
            raise_if_not_found=False,
        )
        if not found:
            self.log_warning("出价按钮未出现, 准备重试本次出价")
            raise WaitFailedException("出价按钮未出现")

        self.log_info("点击出价")
        panel_ready = self.wait_ocr(
            box=boxes.bid_confirm,
            match=RE_BID_PANEL_READY,
            time_out=self._remaining_timeout(deadline, 5),
            raise_if_not_found=False,
            settle_time=0.5,
        )
        if not panel_ready:
            self.log_warning("数字面板未出现, 准备重试本次出价")
            raise WaitFailedException("数字面板未出现")

        self.log_info("数字面板加载完成")
        self._input_fixed_price(boxes, deadline=deadline)

        bid_confirmed = self.wait_until(
            lambda: not self._is_bid_screen(boxes),
            time_out=self._remaining_timeout(deadline, 5),
            settle_time=0.5,
            raise_if_not_found=False,
        )
        if not bid_confirmed:
            raise WaitFailedException("出价确认失败: 出价按钮仍存在")

        if self.config.get(self.CONF_USE_EMOTE, False):
            self._send_emote()

    def _stage_result(self, boxes: AuctionBoxes, deadline: float) -> bool:
        """结果阶段: 等待结算, 处理跳过动画或返回匹配界面。

        结果检测最多持续 RESULT_TIMEOUT 秒, 退出和结算后的辅助操作也受单轮 deadline 约束。
        """
        self.log_info("结算阶段开始, 等待拍卖结果")
        result_deadline = min(deadline, time.monotonic() + self.RESULT_TIMEOUT)
        loop_count = 0

        while loop_count < self.RESULT_MAX_LOOPS and time.monotonic() < result_deadline:
            loop_count += 1
            self.next_frame()

            skip_results = self.ocr(box=boxes.skip_area, match=RE_SKIP)
            if skip_results:
                self._finish_auction(boxes, skip_results, result_deadline)
                return True

            if self._is_match_screen(boxes):
                self.log_info("返回匹配界面")
                return True

            if self._is_bid_screen(boxes):
                self.log_info("进入下一轮出价")
                return False

            self.sleep(self.POLL_INTERVAL)

        raise WaitFailedException("结算阶段超时, 未检测到结束状态")

    def _finish_auction(
        self, boxes: AuctionBoxes, skip_results: list[Box], deadline: float
    ) -> None:
        """拍卖结束处理: 跳过动画, 退出拍卖, 并在主界面执行结算后的辅助操作。"""
        self.log_info("检测到跳过动画")
        self.operate_click(skip_results, after_sleep=0.5)

        exit_button = self.wait_click_ocr(
            box=boxes.exit,
            match=RE_EXIT,
            time_out=self._remaining_timeout(deadline, 5),
            after_sleep=0.5,
            raise_if_not_found=False,
            settle_time=0.5,
        )
        if not exit_button:
            raise WaitFailedException("退出拍卖按钮未出现")
        self.log_info("退出拍卖")

        self.log_info("等待主界面稳定")
        main_asset_title = self.wait_ocr(
            box=boxes.main_asset_title,
            match=RE_MAIN_ASSET_TITLE,
            time_out=self._remaining_timeout(deadline, 15),
            raise_if_not_found=False,
            settle_time=0.5,
            post_action=lambda: self.sleep(0.5),
        )
        if not main_asset_title:
            self.log_warning("主界面加载标志未识别, 跳过本轮结算后处理")
            return

        self.log_info("主界面加载完成")
        self._run_post_round_actions(boxes, deadline)

    def _run_post_round_actions(self, boxes: AuctionBoxes, deadline: float) -> None:
        """结算后回到主界面时的辅助操作: 自动清理藏品与低保金领取。"""
        need_clear_collections = self._should_clear_collections(boxes, deadline)

        if self.config.get(self.CONF_USE_WELFARE, False):
            self._claim_welfare_if_needed(boxes, deadline)

        if need_clear_collections:
            self.log_info("根据之前的标记, 现在执行自动清理藏品")
            self._sell_collections(boxes, deadline)

    def _should_clear_collections(self, boxes: AuctionBoxes, deadline: float) -> bool:
        """启用自动清理时, 检测库存不足提示以决定是否需要出售藏品。"""
        if not self.config.get(self.CONF_AUTO_CLEAR_COLLECTIONS, False):
            return False

        insufficient_text = self.wait_ocr(
            box=boxes.insufficient,
            match=RE_COLLECTION_INSUFFICIENT,
            time_out=self._remaining_timeout(deadline, 1),
            settle_time=0.5,
            raise_if_not_found=False,
        )
        if insufficient_text:
            self.log_info("检测到库存不足提示, 标记需要自动清理藏品")
            return True

        self.log_info("未检测到库存不足提示, 跳过自动清理")
        return False

    def _claim_welfare_if_needed(self, boxes: AuctionBoxes, deadline: float) -> None:
        """主界面资产低于阈值时领取低保金。"""
        # 使用数字 match, 避免漏识别单字符数值 0.
        asset_value = self._read_asset_value(boxes.main_asset, self._remaining_timeout(deadline, 5))
        if asset_value is None:
            self.log_warning("资产值识别失败, 跳过本次低保金领取")
            return

        self.log_info(f"当前资产: {asset_value}")
        if asset_value < self.WELFARE_ASSET_THRESHOLD:
            self.log_info(f"资产低于{self.WELFARE_ASSET_THRESHOLD}, 执行低保金领取")
            self._try_claim_welfare(boxes, deadline)
        else:
            self.log_info(f"资产达到{self.WELFARE_ASSET_THRESHOLD}, 跳过低保金领取")

    # --- 界面状态判定 ---
    def _is_match_screen(self, boxes: AuctionBoxes) -> bool:
        return bool(self.ocr(box=boxes.match, match=RE_MATCH))

    def _is_confirm_screen(self, boxes: AuctionBoxes) -> bool:
        return bool(self.ocr(box=boxes.confirm, match=RE_CONFIRM))

    def _is_bid_screen(self, boxes: AuctionBoxes) -> bool:
        return bool(self.ocr(box=boxes.bid, match=RE_BID))

    def _is_skip_screen(self, boxes: AuctionBoxes) -> bool:
        return bool(self.ocr(box=boxes.skip_area, match=RE_SKIP))

    # --- 超时辅助 ---
    @staticmethod
    def _remaining_timeout(deadline: float, limit: float) -> float:
        """返回受单轮 deadline 限制的等待时间, deadline 到期时立即失败。"""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WaitFailedException("单轮拍卖超时")
        return min(limit, remaining)

    def _bounded_timeout(self, deadline: float | None, limit: float) -> float:
        """在兼容无 deadline 调用的同时, 限制有 deadline 调用的等待时间。"""
        return limit if deadline is None else self._remaining_timeout(deadline, limit)

    def _bounded_sleep(self, deadline: float | None, delay: float) -> None:
        """执行受 deadline 限制的短暂操作等待。"""
        self.sleep(self._bounded_timeout(deadline, delay))

    # --- 配置读取辅助 ---
    def _config_int(self, key: str, default: int = 0, *, warn: str | None = None) -> int:
        """读取整数配置, 非法时回退到默认值并可选输出告警。"""
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            if warn:
                self.log_warning(warn)
            return default

    def _config_float(self, key: str, default: float = 0.0) -> float:
        """读取浮点配置, 非法时回退到默认值。"""
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _config_int_list(self, key: str) -> list[int]:
        """读取整数列表配置, 任一项非法时返回空列表。"""
        try:
            return [int(item) for item in self.config.get(key, [])]
        except (TypeError, ValueError):
            return []

    # --- 资产解析 ---
    @staticmethod
    def _parse_asset_value(raw_text: str) -> int | None:
        """统一解析资产 OCR 文本, 返回整数或 None。

        处理流程:
        1. 全角数字转半角数字.
        2. 修正常见 OCR 错误 (O -> 0, l/I -> 1).
        3. 提取数字.
        4. 转换为 int, 失败时返回 None.
        """
        normalized = raw_text.translate(FULLWIDTH_DIGITS)
        corrected = normalized.replace("l", "1").replace("I", "1").replace("O", "0")
        digits = re.sub(r"[^\d]", "", corrected)
        if not digits:
            return None

        try:
            return int(digits)
        except ValueError:
            return None

    def _read_asset_value(self, box: Box, timeout: float) -> int | None:
        """对指定区域做 OCR 并解析资产数值, 未识别或解析失败时返回 None。"""
        boxes = self.wait_ocr(
            box=box,
            match=RE_NUMBER,
            time_out=timeout,
            raise_if_not_found=False,
            settle_time=0.5,
        )
        if not boxes:
            return None

        raw_text = "".join(text_box.name for text_box in boxes)
        value = self._parse_asset_value(raw_text)
        self.log_debug(f"资产 OCR: '{raw_text}', 解析值: {value}")
        return value

    # --- 自动加价计算 ---
    def _calculate_auction_price(self) -> int:
        """计算当前出价应该输入的价格。

        基准价格为自定义价格, 如果启用自动加价, 则根据模式计算加价结果。
        出价序号从 1 开始计数, 基于成功出价次数 + 1。
        如果启用了指定回合单独出价, 且当前序号在勾选的回合列表中, 则直接使用该价格。
        """
        base_price = self._config_int(self.CONF_FIXED_PRICE, 1)

        # 出价序号从 1 开始.
        bid_count = self.current_bid_count + 1

        special_price = self._special_round_price(bid_count)
        if special_price is not None:
            return special_price

        if not self.config.get(self.CONF_AUTO_RAISE, False):
            return base_price

        return self._raise_price(base_price, bid_count)

    def _special_round_price(self, bid_count: int) -> int | None:
        """启用指定回合单独出价时返回该回合价格, 否则返回 None。"""
        if not self.config.get(self.CONF_SPECIAL_ROUND, False):
            return None

        special_rounds = self._config_int_list(self.CONF_SPECIAL_ROUNDS)
        special_price = self._config_int(self.CONF_SPECIAL_ROUND_PRICE, 0)
        if special_price > 0 and bid_count in special_rounds:
            self.log_info(f"指定回合 {bid_count} 使用单独价格 {special_price}")
            return special_price
        return None

    def _raise_price(self, base_price: int, bid_count: int) -> int:
        """按配置的加价方式计算第 bid_count 次出价的价格。"""
        mode = self.config.get(self.CONF_RAISE_MODE, "倍数")
        value = self._config_float(self.CONF_RAISE_VALUE, 0.0)
        raise_round = self._config_int(self.CONF_RAISE_ROUND, 0)

        # 在达到配置的加价回合前使用基础价.
        if raise_round > 0 and bid_count < raise_round:
            return base_price

        # 计算加价偏移次数, 从 1 开始.
        offset = bid_count if raise_round == 0 else bid_count - raise_round + 1

        # 根据所选方式计算价格.
        if mode == "倍数":
            # 指数增长: 基础价 * (倍数 ^ offset).
            result = base_price * (value**offset)
        elif mode == "百分比":
            # 线性增长: 基础价 * (1 + 百分比 / 100 * offset).
            result = base_price * (1 + value / 100 * offset)
        else:  # 自定义
            # 线性增长: 基础价 + 自定义值 * offset.
            result = base_price + value * offset

        # 四舍五入为整数.
        final_price = int(Decimal(str(result)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

        # 确保计算结果为正整数.
        if final_price <= 0:
            self.log_warning(f"计算出的价格 {final_price} 无效, 回退到基础价 {base_price}")
            final_price = base_price

        self.log_info(
            f"自动加价计算: 基础价 {base_price}, 模式 {mode}, 数值 {value}, "
            f"出价序号 {bid_count}, 加价偏移 {offset}, 出价 {final_price}"
        )
        return final_price

    # --- 价格输入 ---
    def _input_fixed_price(
        self, boxes: AuctionBoxes, price: int | None = None, deadline: float | None = None
    ) -> None:
        """使用游戏内数字键盘输入价格, 支持上轮出价、00 和 0000 快捷按钮。

        输入失败时抛出异常, 由调用方重试本次出价。
        """
        if price is None:
            price = self._calculate_auction_price()

        price_str = str(price)
        if not price_str.isdigit() or price <= 0:
            raise ValueError(f"非法价格 '{price}'")
        if deadline is not None:
            self._remaining_timeout(deadline, 0.1)

        if self._can_reuse_last_bid(price):
            self.operate_click(boxes.last_bid, after_sleep=0.2)
            self.log_info(f"使用上轮出价快捷输入价格 {price}")
        else:
            self.operate_click(boxes.clear, after_sleep=0.3)
            self._press_price_digits(price_str, deadline)

        self._verify_input_price(boxes, price, deadline)
        self._confirm_bid_price(boxes, deadline)

        self.log_info(f"输入价格 {price}")
        self.last_bid_price = price

    def _can_reuse_last_bid(self, price: int) -> bool:
        """仅在未启用自动加价时复用上轮出价, 避免自动加价下快捷输入的不确定性。"""
        return (
            not self.config.get(self.CONF_AUTO_RAISE, False)
            and self.last_bid_price is not None
            and price == self.last_bid_price
        )

    def _press_price_digits(self, price_str: str, deadline: float | None) -> None:
        """按数字键盘逐键点击, 优先使用 0000 / 00 快捷键。"""
        for key in self._price_key_sequence(price_str):
            if deadline is not None:
                self._remaining_timeout(deadline, 0.1)
            self.operate_click(self.box_of_screen(*self.PAD_MAP[key]), after_sleep=0.2)

    @staticmethod
    def _price_key_sequence(price_str: str) -> list[str]:
        """把价格字符串切分为按键序列, 可一次输入的 0000 / 00 优先整体输入。"""
        keys: list[str] = []
        index = 0
        while index < len(price_str):
            remaining = price_str[index:]
            if remaining in PAD_SHORTCUTS:
                keys.append(remaining)
                index = len(price_str)
            else:
                keys.append(price_str[index])
                index += 1
        return keys

    def _verify_input_price(self, boxes: AuctionBoxes, price: int, deadline: float | None) -> None:
        """校验数字面板显示的价格与目标价格一致, 不一致时抛出异常。"""
        price_boxes = self.wait_ocr(
            box=boxes.price_result,
            match=RE_NUMBER,
            time_out=3 if deadline is None else self._remaining_timeout(deadline, 3),
            settle_time=0.5,
            raise_if_not_found=False,
        )
        if not price_boxes:
            self.log_warning("输入价格结果未识别, 取消确认并重试当前出价")
            raise WaitFailedException("输入价格结果未识别")

        raw_price = "".join(text_box.name for text_box in price_boxes)
        input_price = self._parse_asset_value(raw_price)
        self.log_debug(f"输入价格结果 OCR: '{raw_price}', 解析值: {input_price}")
        if input_price != price:
            self.log_warning(
                f"输入价格校验失败, 目标价格: {price}, 实际价格: {input_price}, "
                "取消确认并重试当前出价"
            )
            raise WaitFailedException("输入价格校验失败")

    def _confirm_bid_price(self, boxes: AuctionBoxes, deadline: float | None) -> None:
        """点击确认出价, 并处理可能出现的异常确认框。"""
        confirmed = self.wait_click_ocr(
            box=boxes.bid_confirm,
            match=RE_BID_CONFIRM,
            time_out=5 if deadline is None else self._remaining_timeout(deadline, 5),
            after_sleep=0.2,
            raise_if_not_found=False,
        )
        if not confirmed:
            self.log_warning("确认出价失败, 5秒内未完成点击, 准备重试当前出价")
            raise WaitFailedException("确认出价失败")

        # 检测是否出现异常确认框 (非必须等待, 用一次性 ocr 避免每次出价都白等).
        if self.ocr(box=boxes.exception_area, match=RE_CONFIRM_ANY):
            self.operate_click(boxes.exception_area, after_sleep=0.3)
            self.log_info("检测到异常确认框, 点击确认")
        else:
            self.log_info("未检测到异常确认框")

    # --- 低保金 ---
    def _try_claim_welfare(self, boxes: AuctionBoxes, deadline: float | None = None) -> bool:
        """尝试领取每日低保金, deadline 为空时保持原有独立超时行为。"""
        try:
            self.log_info("执行低保金领取流程")
            self._wait_click_or_fail(boxes.welfare_btn, RE_WELFARE, deadline, 5, "低保金按钮")
            self._bounded_sleep(deadline, 0.5)

            self._wait_click_or_fail(boxes.claim, RE_CLAIM, deadline, 5, "领取按钮")
            self._bounded_sleep(deadline, 0.5)

            self._wait_click_or_fail(boxes.cancel, RE_CANCEL, deadline, 5, "取消按钮")
            self._bounded_sleep(deadline, 0.5)

            cancel_closed = self.wait_until(
                lambda: not self.ocr(box=boxes.cancel, match=RE_CANCEL),
                time_out=self._bounded_timeout(deadline, 3),
                settle_time=0.5,
                raise_if_not_found=False,
            )
            if not cancel_closed:
                raise WaitFailedException("低保金弹窗未关闭")

            self.log_info("低保金领取完成")
            return True
        except TaskDisabledException:
            raise
        except WaitFailedException:
            raise
        except Exception as e:
            self.log_warning(f"低保金领取失败: {type(e).__name__}: {e}")
            return False

    def _wait_click_or_fail(
        self,
        box: Box,
        match: re.Pattern,
        deadline: float | None,
        timeout: float,
        desc: str,
    ) -> None:
        """等待并点击目标控件, 超时未出现时抛出 WaitFailedException。"""
        clicked = self.wait_click_ocr(
            box=box,
            match=match,
            time_out=self._bounded_timeout(deadline, timeout),
            after_sleep=0,
            settle_time=0.5,
        )
        if not clicked:
            raise WaitFailedException(f"{desc}未出现")

    # --- 藏品出售 ---
    def _sell_collections_on_interval(self, boxes: AuctionBoxes) -> None:
        """按配置的间隔轮次出售藏品, 启用自动清理时禁用该定期出售。"""
        if self.config.get(self.CONF_AUTO_CLEAR_COLLECTIONS, False):
            return

        sell_interval = self._config_int(
            self.CONF_SELL_INTERVAL, 0, warn="出售间隔次数配置无效, 按 0 处理"
        )
        if sell_interval > 0 and self.current_round % sell_interval == 0:
            self._sell_collections(boxes)

    def _sell_collections(self, boxes: AuctionBoxes, deadline: float | None = None) -> bool:
        """尝试出售藏品, deadline 为空时保持定期清理分支的原有行为。"""
        self.log_info("开始执行藏品出售流程")
        try:
            warehouse_button = self.wait_click_ocr(
                box=boxes.warehouse_btn,
                match=RE_WAREHOUSE,
                time_out=self._bounded_timeout(deadline, 10),
                after_sleep=0,
                raise_if_not_found=False,
            )
            if not warehouse_button:
                self.log_warning("未点击藏品仓库入口, 取消出售流程")
                return False
            self._bounded_sleep(deadline, 1)
            self.log_info("藏品仓库入口已点击")

            if not self.wait_ocr(
                box=boxes.warehouse_title,
                match=RE_WAREHOUSE,
                time_out=self._bounded_timeout(deadline, 10),
                raise_if_not_found=False,
                settle_time=0.5,
            ):
                self.log_warning("藏品仓库界面加载失败, 取消出售流程")
                return False
            self.log_info("藏品仓库界面加载完成")

            self.operate_click(boxes.sell, after_sleep=0)
            self._bounded_sleep(deadline, 1)
            self._select_quality_filters(deadline)

            self.operate_click(boxes.confirm_sell, after_sleep=0)
            self._bounded_sleep(deadline, 1.5)
            self.log_info("确认出售")

            self.operate_click(boxes.blank, after_sleep=0)
            self._bounded_sleep(deadline, 0.5)
            self.operate_click(boxes.close, after_sleep=0)
            self._bounded_sleep(deadline, 1)
            self.log_info("藏品出售完成")
            return True
        except TaskDisabledException:
            raise
        except WaitFailedException:
            raise
        except Exception as e:
            self.log_warning(f"藏品出售失败: {type(e).__name__}: {e}")
            return False

    def _select_quality_filters(self, deadline: float | None) -> None:
        """勾选需要出售的品质按钮, 跳过配置中保留的品质。"""
        keep_qualities = self.config.get(self.CONF_KEEP_QUALITIES, [])
        for quality_name, quality_pos in zip(self.QUALITY_KEYS, self.QUALITY_BOXES):
            if quality_name in keep_qualities:
                self.log_info(f"保留{quality_name}")
                continue
            self.operate_click(self.box_of_screen(*quality_pos), after_sleep=0)
            self._bounded_sleep(deadline, 0.5)
            self.log_info(f"选择{quality_name}")

    # --- 表情包 ---
    def _send_emote(self) -> None:
        """发送表情菜单中的第一个表情。"""
        self.log_info("发送表情包")
        self.operate_click(*self.EMOTE_BTN, after_sleep=0.8)
        self.sleep(0.8)
        self.operate_click(*self.EMOTE_FIRST, after_sleep=0.5)
        self.log_info("表情包发送完成")
