import math
import re
import time
from datetime import date
from decimal import Decimal

from ok import Box, TaskDisabledException, WaitFailedException

from src.tasks.auction import layout as auction_layout
from src.tasks.auction import options as auction_options
from src.tasks.auction import price as auction_price
from src.tasks.auction import recovery as auction_recovery
from src.tasks.auction import sell as auction_sell
from src.tasks.auction import welfare as auction_welfare
from src.tasks.auction.layout import (
    RE_BID,
    RE_BID_CONFIRM,
    RE_BID_PANEL,
    RE_BID_PANEL_READY,
    RE_CONFIRM,
    RE_EXIT,
    RE_MAIN_TITLE,
    RE_MATCH,
    RE_NUMBER,
    RE_PRICE_HINT,
    RE_SKIP,
    AuctionBoxes,
    AuctionState,
    PostRoundState,
)
from src.tasks.auction.layout import (
    RE_CANCEL as RE_CANCEL,
)
from src.tasks.auction.layout import (
    RE_CLAIM as RE_CLAIM,
)
from src.tasks.auction.layout import (
    # 测试从本模块导入这五个正则; 使用方已迁至 auction 子包, 此处显式再导出。
    RE_ONE_CLICK_SELL as RE_ONE_CLICK_SELL,
)
from src.tasks.auction.layout import (
    RE_POPUP_CLOSE_HINT as RE_POPUP_CLOSE_HINT,
)
from src.tasks.auction.layout import (
    RE_WELFARE_COUNTER as RE_WELFARE_COUNTER,
)
from src.tasks.auction.options import INST
from src.tasks.BaseNTETask import BaseNTETask
from src.tasks.NTEOneTimeTask import NTEOneTimeTask


class AutoBidAuctionTask(NTEOneTimeTask, BaseNTETask):
    """自动完成游戏内拍卖流程。

    功能包括: 匹配, 确认, 出价, 出价重试, 结算, 低保金领取, 表情包发送, 藏品出售。
    需要在拍卖主界面选择低级会场后开始执行。
    """

    # --- 拍卖配置 (兼容别名) ---
    # 常量唯一来源: src/tasks/auction/options.py; 默认配置/控件类型/说明由
    # auction_options.default_config() / config_type() / config_description() 装配。
    # 方法与测试仍按 self.<名字> / AutoBidAuctionTask.<名字> 访问, 等消费者全部
    # 迁到 auction 包后再逐个收掉。
    CONF_FIXED_PRICE = auction_options.CONF_FIXED_PRICE
    CONF_SELL_MODE = auction_options.CONF_SELL_MODE
    SELL_MODE_OFF = auction_options.SELL_MODE_OFF
    SELL_MODE_FULL = auction_options.SELL_MODE_FULL
    SELL_MODE_ONE_CLICK = auction_options.SELL_MODE_ONE_CLICK
    SELL_MODE_INTERVAL = auction_options.SELL_MODE_INTERVAL
    SELL_MODES = auction_options.SELL_MODES
    CONF_SELL_INTERVAL = auction_options.CONF_SELL_INTERVAL
    CONF_SELL_BEFORE_WELFARE = auction_options.CONF_SELL_BEFORE_WELFARE
    CONF_SELL_AFTER_WELFARE = auction_options.CONF_SELL_AFTER_WELFARE
    SELL_QUALITY_KEYS = auction_options.SELL_QUALITY_KEYS
    CONF_AUTO_RAISE = auction_options.CONF_AUTO_RAISE
    CONF_RAISE_MODE = auction_options.CONF_RAISE_MODE
    RAISE_MODE_MULTIPLE = auction_options.RAISE_MODE_MULTIPLE
    RAISE_MODE_CUSTOM = auction_options.RAISE_MODE_CUSTOM
    RAISE_MODE_PERCENT = auction_options.RAISE_MODE_PERCENT
    RAISE_MODES = auction_options.RAISE_MODES
    CONF_RAISE_VALUE = auction_options.CONF_RAISE_VALUE
    CONF_RAISE_ROUND = auction_options.CONF_RAISE_ROUND
    CONF_SPECIAL_ROUND = auction_options.CONF_SPECIAL_ROUND
    CONF_SPECIAL_ROUNDS = auction_options.CONF_SPECIAL_ROUNDS
    CONF_SPECIAL_ROUND_PRICE = auction_options.CONF_SPECIAL_ROUND_PRICE
    CONF_BID_MODE = auction_options.CONF_BID_MODE
    BID_MODE_CUSTOM = auction_options.BID_MODE_CUSTOM
    BID_MODE_LIST = auction_options.BID_MODE_LIST
    BID_MODE_ESTIMATE = auction_options.BID_MODE_ESTIMATE
    CONF_ESTIMATE_RATIO = auction_options.CONF_ESTIMATE_RATIO
    MAX_BID_ROUNDS = auction_options.MAX_BID_ROUNDS
    CONF_BID_PRICES = auction_options.CONF_BID_PRICES
    CONF_ASSIST_FEATURES = auction_options.CONF_ASSIST_FEATURES
    ASSIST_EMOTE = auction_options.ASSIST_EMOTE
    ASSIST_WELFARE = auction_options.ASSIST_WELFARE
    ASSIST_FEATURES = auction_options.ASSIST_FEATURES
    QUALITY_KEYS = auction_options.QUALITY_KEYS
    SPECIAL_ROUND_OPTIONS = auction_options.SPECIAL_ROUND_OPTIONS

    # --- UI 坐标 (兼容别名) ---
    # 区域常量唯一来源: src/tasks/auction/layout.py (含 AuctionBoxes 与 OCR 正则),
    # 数值零改动, 注释随迁。消费者全部迁移后逐个收掉。
    BOX_MATCH = auction_layout.BOX_MATCH
    BOX_CONFIRM = auction_layout.BOX_CONFIRM
    BOX_BID = auction_layout.BOX_BID
    BOX_BID_KEYPAD = auction_layout.BOX_BID_KEYPAD
    BOX_SKIP_AREA = auction_layout.BOX_SKIP_AREA
    BOX_EXIT = auction_layout.BOX_EXIT
    BOX_BID_CONFIRM = auction_layout.BOX_BID_CONFIRM
    BOX_ABANDON = auction_layout.BOX_ABANDON
    BOX_ABANDON_CONFIRM = auction_layout.BOX_ABANDON_CONFIRM
    BOX_ASSET_VALUE = auction_layout.BOX_ASSET_VALUE
    BOX_ESTIMATE = auction_layout.BOX_ESTIMATE
    BOX_LAST_BID = auction_layout.BOX_LAST_BID
    BOX_CLEAR = auction_layout.BOX_CLEAR
    BOX_PRICE_RESULT = auction_layout.BOX_PRICE_RESULT
    BOX_PRICE_RESULT_KEYPAD = auction_layout.BOX_PRICE_RESULT_KEYPAD
    BOX_EXCEPTION_AREA = auction_layout.BOX_EXCEPTION_AREA
    BOX_MAIN_TITLE = auction_layout.BOX_MAIN_TITLE
    BOX_MAIN_ASSET = auction_layout.BOX_MAIN_ASSET
    BOX_INSUFFICIENT = auction_layout.BOX_INSUFFICIENT
    POS_CITY_FUN_SCROLL = auction_layout.POS_CITY_FUN_SCROLL
    BOX_CITY_FUN_TITLE = auction_layout.BOX_CITY_FUN_TITLE
    BOX_CITY_FUN_CARDS = auction_layout.BOX_CITY_FUN_CARDS
    BOX_CURRENT_VENUE = auction_layout.BOX_CURRENT_VENUE
    BOX_WELFARE_BTN = auction_layout.BOX_WELFARE_BTN
    BOX_WELFARE_DIALOG = auction_layout.BOX_WELFARE_DIALOG
    BOX_WELFARE_COUNTER = auction_layout.BOX_WELFARE_COUNTER
    BOX_CLAIM = auction_layout.BOX_CLAIM
    BOX_CANCEL = auction_layout.BOX_CANCEL
    BOX_WAREHOUSE_BTN = auction_layout.BOX_WAREHOUSE_BTN
    BOX_WAREHOUSE_TITLE = auction_layout.BOX_WAREHOUSE_TITLE
    BOX_SELL = auction_layout.BOX_SELL
    BOX_CONFIRM_SELL = auction_layout.BOX_CONFIRM_SELL
    BOX_BLANK = auction_layout.BOX_BLANK
    BOX_CLOSE = auction_layout.BOX_CLOSE
    BOX_SELL_LABEL = auction_layout.BOX_SELL_LABEL
    BOX_SELL_VALUE = auction_layout.BOX_SELL_VALUE
    BOX_ONE_CLICK_SELL = auction_layout.BOX_ONE_CLICK_SELL
    BOX_POPUP_CLOSE_HINT = auction_layout.BOX_POPUP_CLOSE_HINT
    BOX_POPUP_BLANK = auction_layout.BOX_POPUP_BLANK
    QUALITY_BOXES = auction_layout.QUALITY_BOXES
    PAD_MAP = auction_layout.PAD_MAP
    EMOTE_BTN = auction_layout.EMOTE_BTN
    EMOTE_FIRST = auction_layout.EMOTE_FIRST

    # 品质圆点每点击一次界面会重绘, 间隔太短时后续点击会落空;
    # 勾选后读出售价值校验, 读到 0 或读不出时换帧重读, 最多尝试 SELL_SELECT_RETRIES 次.
    # 不能靠重新勾选来重试: 勾选是无条件点击, 再点一次会把刚勾上的品质全部点掉.
    SELL_QUALITY_GAP = 0.5
    SELL_SELECT_RETRIES = 2

    # 「出售价值」在品质圆点刚点完时会短暂变成空白(界面重绘), 只给 1 秒经常读空;
    # 读不出时返回 None, 调用方必须按「未确认」处理, 不能当成出售成功.
    SELL_VALUE_TIMEOUT = 3

    # 提示类弹窗(入场费确认 / 异常出价 / 满仓提示)共用一套模板: 标题「提示」在
    # 屏幕中部, 确认与取消按钮在底部同一组坐标. 因此一个区域就能兜住这一类弹窗.
    # 单帧 ocr 会漏掉刚出现的弹窗, 必须带短超时轮询.
    NOTICE_POPUP_TIMEOUT = 2
    # 每次出价都要查一遍弹窗, 预算调小: 漏掉一次弹窗要赔上整轮, 但不该固定白等 2 秒.
    BID_NOTICE_POPUP_TIMEOUT = 1.0

    # 库存不足提示条出现时机不定, 给 1 秒容易漏掉(漏掉就不会提前清理, 满仓会卡住).
    INVENTORY_FULL_TIMEOUT = 3

    # 出售连续失败到这个次数后放宽出售清单(6 个品质全卖)再试一次: 满仓卖不掉会让后续
    # 出价全部失败, 这时候把仓库腾空的优先级高于按低保阶段挑选品质.
    SELL_FAILURE_ESCALATE_AFTER = 2

    # 轮次末尾出售流程的总预算。出售是收尾动作, 不该像拍卖阶段那样吃掉整轮 600 秒:
    # 逐分支等待下限约 35 秒, 连续失败放宽再走一趟约 71 秒, 留一倍余量。
    SELL_TIMEOUT = 90

    # 关闭藏品仓库的重试次数。批量关闭失败会把「出售模式 + 已勾选品质」留给下一轮,
    # 下次进来会无条件再点一遍同一批品质(全部取反), 必须确认真的关掉了。
    WAREHOUSE_CLOSE_RETRIES = 3
    # 藏品仓库入口与界面标题的等待上限。两者是同一段 UI 就绪过程(点入口 → 界面加载),
    # 用同一个上限, 免得调一处漏一处。
    WAREHOUSE_LOAD_TIMEOUT = 10

    # 结算界面的「一键出售」: 跳过动画刚点完, 按钮本来就该在, 给短超时即可.
    ONE_CLICK_SELL_TIMEOUT = 3
    # 点完一键出售要等服务端返回才弹出「获得物品」提示条, 给足时间.
    POPUP_CLOSE_TIMEOUT = 5

    # --- 阶段超时 (秒) ---
    # 单轮拍卖的硬上限, 防止各阶段局部超时叠加后长期卡住任务.
    ROUND_TIMEOUT = 600
    MATCH_TIMEOUT = 120
    MATCH_CLICK_TIMEOUT = 30
    # 点击成功后界面切换只要 0.6 秒左右(见历史运行日志), 所以点击后短时间内按钮仍在原位
    # 就说明这次点击没有生效, 立刻重试比白等 MATCH_CLICK_TIMEOUT 划算得多.
    MATCH_PROBE_TIMEOUT = 3
    # 等待「确认出价」后的出价界面加载完成。与 BID_RESULT_TIMEOUT 数值相同但语义不同:
    # 那个是等「一次出价的结果」(见 _stage_bid_loop), 这个是等界面渲染出来, 不要合并。
    BID_SCREEN_TIMEOUT = 60
    # 等一次出价的结果: 在这段时间内看界面是变成「已结束」还是「被加价」。
    BID_RESULT_TIMEOUT = 60
    # 结算阶段实测最长 3.9 秒(207 次样本), 这个上限从未触发过, 保留用于界面卡死时兜底;
    # 注意 RESULT_MAX_LOOPS(180) x POLL_INTERVAL(0.5) 恰好也是 90 秒, 两个条件同时到点.
    RESULT_TIMEOUT = 90
    # 结算画面存在跳过动画已出现而退出按钮尚未渲染的中间态, 等待不能太短.
    EXIT_BUTTON_TIMEOUT = 10
    ASSET_OCR_TIMEOUT = 15
    # 主界面资产观测的单次超时。观测每轮都要做, 给太长会拖累单轮总预算。
    ASSET_OBSERVE_TIMEOUT = 5
    # 出价面板的当前估价在界面刚出现时会跳动几次, 第一次识别到的不是最终值;
    # 连续读到相同值才采用, 最多等 ESTIMATE_STABLE_TIMEOUT 秒.
    ESTIMATE_STABLE_READS = 3
    # 数字滚动是逐位就位的, 低位先出现、高位后到, 所以「读到相同值」只说明这一帧没变化,
    # 不代表滚动结束。线上日志: 19:16 那局的中间值 35,365 一直持续到面板打开后 2.63 秒
    # 才变成真值 37,979; 19:18 那局恰好在 2.63 秒采信了 197(同局真实估价数万)。
    # 因此从第一次读到有效数值起, 至少观察这么久才允许采用。
    ESTIMATE_MIN_OBSERVE_SECONDS = 4.0
    ESTIMATE_STABLE_TIMEOUT = 10
    # 估价数字右端距裁框右边界, 小于「屏幕宽度的这个比例」时认为末位可能已被裁掉。
    # 2026-09-23 的故障就是这个形态: 末位 "3" 只剩 4px 宽的一条竖边, OCR 直接丢弃,
    # 2,643 读成 ,643 / 1,912 读成 912。裁框宽度不是安全保证, 所以要在运行时盯住它。
    # 取 8/1920: 该故障在 1080p 下实测的临界宽度, 按分辨率等比换算, 避免 1440p/2160p
    # 下 UI 与文字同步放大而阈值不变导致的漏判 (AGENTS.md: 坐标用相对比例, 不硬编码像素)。
    ESTIMATE_EDGE_MARGIN_RATIO = 8 / 1920

    # --- 轮询与重试 ---
    POLL_INTERVAL = 0.5
    # 基类睡眠钩子的触发间隔, 用于在长流程中处理月卡弹窗等外部打断.
    SLEEP_CHECK_INTERVAL = 0.5
    # 循环次数上限与同名阶段的 *_TIMEOUT 是互补的两重保险, 不是重复:
    # 循环体每轮至少 sleep(POLL_INTERVAL), 但命中「开始匹配」时还要走一次点击探测
    # (额外 3~30 秒), 单轮耗时并不固定 —— 所以「次数先到」和「时间先到」都会发生.
    # 实测空转到超时的样本(09-14/09-19/09-20, n=261): 62~66 秒(次数先到) 与
    # 120.4/120.7 秒(时间先到) 两种都存在, 两个上限各自生效过, 不要删任何一个.
    MATCH_MAX_LOOPS = 120
    RESULT_MAX_LOOPS = 180
    BID_MAX_RETRIES = 3

    # 资产低于该值时领取低保金.
    WELFARE_ASSET_THRESHOLD = 100000

    # 低保金弹窗关闭重试次数, 每日次数用尽时弹窗没有领取按钮, 只能靠取消关闭.
    WELFARE_CLOSE_RETRIES = 3

    # 低保金每日刷新时刻(游戏每日 5 点重置, 与 src/config.py 的「Monthly Card Time」默认值一致)。
    # 只用于跨天清空当日领取记录; 具体次数与上限一律以弹窗读数「今日已领取次数：N/5」为准,
    # 所以这里不写死「每日 5 次」—— 游戏改上限时不需要跟着改代码。
    WELFARE_RESET_HOUR = 5
    # 弹窗次数读数最多读几帧、换帧间隔多少秒。只读一帧时弹窗淡入中的空白帧会让这次
    # 读数落空, 而资产涨过 10 万后弹窗不再打开, 当天就再也读不到了(追加出售静默失效)。
    WELFARE_COUNTER_READS = 2
    WELFARE_COUNTER_RETRY_GAP = 0.3

    # --- 掉线回场 (秒/次) ---
    # 网络不稳时匹配阶段会被踢回大世界, 界面状态全不命中, 只能空转到 MATCH_TIMEOUT。
    # 回场是一次性的异常路径: 失败就按本轮失败处理, 交给下一轮重试。
    RECOVER_TIMEOUT = 90  # 单次回场总预算
    RECOVER_STEP_TIMEOUT = 12  # 回场各步骤的等待上限
    RECOVER_SCROLL_STEPS = 4  # 「都市闲趣」面板最多滚动几次去找「即刻落槌」
    RECOVER_SCROLL_WHEEL = -8  # 每次滚动的滚轮格数
    # 单轮回场次数上限, 由 _exec_auction_round 写进 self._recover_quota 并扣减。
    # 挂在轮次而不是调用参数上的原因见 _exec_auction_round: 参数会在「确认失败后重跑
    # _stage_match」的路径上被默认值恢复, 使同一轮可以反复回场, 每次都重走一遍面板动画
    # 把整轮 deadline 耗光, 并且让「本轮只回场一次」这个约定形同虚设。
    RECOVER_MAX_PER_ROUND = 1
    # 启动时的入口回场(见 _ensure_auction_entry): 探测主界面标题的等待上限,
    # 以及一次性回场预算。预算与 RECOVER_TIMEOUT 一致, 两者走的是同一条路径。
    ENTRY_PROBE_TIMEOUT = 3
    ENTRY_RECOVER_TIMEOUT = 90
    # 匹配阶段每 N 次轮询探一次大世界(约 2 秒): 判定要跑一次旋转模板匹配 + 一次血条模板
    # 匹配, 比 OCR 贵, 不能每 0.5 秒调一次; 探测只在点击「开始匹配」之后、界面迟迟不变化时
    # 才开始.
    WORLD_PROBE_INTERVAL = 4

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.supported_languages = ["zh_CN"]
        self.name = "自动拍卖(目前仅支持简中)"
        self.description = "可从大世界或拍卖主界面直接启动, 会自动进入拍卖界面; 请先阅读「说明」"
        self.group_name = "都市闲趣"
        # 任务卡上的「说明」按钮只在 instructions 非空时出现, 内容是富文本 HTML.
        self.instructions = INST
        self.add_rounds_config()
        # 本轮回场配额, 由 _exec_auction_round 每轮重置。这里先给初值, 使任务实例在任何
        # 入口(包括测试直接调 _stage_match)下都有确定行为 —— 缺省视为「本轮回场机会已用完」,
        # 不会因为没有轮次上下文而无限回场。
        self._recover_quota = 0

        self.default_config.update(auction_options.default_config())

        # 定义下拉框和条件子配置的控件类型.
        self.config_type = auction_options.config_type()

        # 描述按 default_config 的顺序排列, 与面板上的控件顺序一致, 便于对照维护.
        # 每条只写「标签本身看不出来的信息」: 做什么, 硬约束, 以及读不到时的回退行为.
        self.config_description.update(auction_options.config_description())

        self.last_bid_price = None
        self.current_bid_count = 0
        self._post_round_state = PostRoundState()
        # 藏品出售连续失败计数: 满仓卖不掉时后续出价必然失败, 需要升级处理而不是每轮重试.
        self._sell_failures = 0
        self._inventory_stuck = False
        # 当日低保领取记录. 次数与上限只从弹窗「今日已领取次数：N/5」读回(领取前读
        # 一次, 领取后再重读一次), 不做本地推算: 点击领取不等于领取生效, 盲目 +1 会
        # 在点击落空时虚增次数, 提前切换出售清单抬高资产后弹窗不再打开, 计数再无
        # 读数可纠偏。
        self._welfare_day: date | None = None
        self._welfare_claims_today = 0
        self._welfare_daily_limit: int | None = None
        # 启用基类的睡眠钩子, 拍卖流程跨过每日 5 点时靠它处理月卡弹窗.
        self.sleep_check_interval = self.SLEEP_CHECK_INTERVAL
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
        try:
            # 入场校验放在 try 内: 配置非法时也要走到 finally 的 finish_rounds,
            # 否则任务抛错退出后连轮次汇总日志和结束通知都不会发出。
            self._validate_price_config()
            self._warn_if_no_sellable_quality()
            boxes = self._build_boxes()
            while self.has_remaining_rounds():
                if not self.begin_round():
                    break
                try:
                    # 残留的藏品仓库会盖住主界面标题与全部拍卖控件, 不先收起,
                    # 入口探测与匹配阶段的四种状态判定全部落空, 每轮都在
                    # MATCH_TIMEOUT(120 秒) 上空烧到异常. 下方的轮末仓库检查只在
                    # _run_single_round 正常返回时执行, 启动前残留(上次进程被杀 /
                    # 手动开着)或异常路径都轮不到它, 所以每轮开头先查:
                    # 开着先收一次, 收不掉才停止.
                    if self._is_warehouse_open(boxes):
                        self.log_warning("检测到藏品仓库仍开着, 先收起再继续本轮")
                        self._close_warehouse(boxes)
                        if self._is_warehouse_open(boxes):
                            self.log_error("藏品仓库未关闭且无法自动收起, 停止后续轮次")
                            break
                    # 每轮都重新确认一次入口, 与 AutoHeistTask._run_loop「每轮先
                    # ensure_main + 入口判断」保持一致: 上一轮掉线或异常退出时人可能已经
                    # 不在拍卖界面, 只在循环外确认一次的话, 后续每轮都要在 _stage_match
                    # 里空转到 MATCH_TIMEOUT(120 秒) 才由末尾的大世界兜底触发回场。
                    self._ensure_auction_entry(boxes)
                    self._run_single_round(boxes)
                    if self._is_warehouse_open(boxes):
                        # 关窗失败时 _close_warehouse 只告警, 整轮照样「正常返回」。此时界面
                        # 还压在藏品仓库上, 下一轮所有拍卖控件的等待只能空转, 每轮都把
                        # ROUND_TIMEOUT 烧光, 日志里看起来却只是「每轮都失败」。这里直接收尾:
                        # 关不掉的仓库不需要另做状态标记 —— _try_sell_collections 只记录出售
                        # 结果, 被中断的「满仓清理」不会把它算成失败, 于是
                        # _inventory_stuck 仍停在 False, 下次启动会照常走完整流程重试,
                        # 不会因为这次中断而跳过出价。
                        self.log_error("藏品仓库未关闭且无法自动收起, 停止后续轮次")
                        break
                except TaskDisabledException:
                    raise
                except Exception as e:
                    self.add_failed("拍卖执行异常")
                    self.log_error(f"本轮拍卖失败: {type(e).__name__}: {e}")
                    # 界面被弹窗挡住才会整轮什么都识别不到, 失败后按弹窗特征兜底一次.
                    self._recover_blocking_popup(boxes)
                    self.sleep(2)
        finally:
            self.finish_rounds()

    # --- 外部打断 ---
    def sleep_check(self):
        """睡眠钩子: 处理月卡弹窗等外部打断, 与其它任务保持同一套机制。

        拍卖流程可能跨过每日 5 点的刷新时刻, 此时月卡弹窗会盖住拍卖界面,
        所有阶段状态判定都会失败, 轮询只能一直空转到单轮超时。
        基类在每次 sleep 时按 sleep_check_interval 调用本方法(调用前会刷新帧),
        因此轮询循环、出价中间步骤和结算后处理都被覆盖。
        check_monthly_card 只在 5 点前后 2 分钟的时间窗内生效, 窗口外只是一次时间比较。
        """
        super().sleep_check()
        if self.check_monthly_card():
            self.log_info("检测到月卡弹窗, 关闭后继续当前轮次")
            self.handle_monthly_card()

    def _recover_blocking_popup(self, boxes: AuctionBoxes | None = None) -> None:
        """整轮失败后的弹窗兜底, 实现见 auction_recovery.blocking_popup。"""
        auction_recovery.blocking_popup(self, boxes)

    # --- 掉线回场 (大世界 → 拍卖主界面) ---
    def _is_world_screen(self) -> bool:
        """是否被踢回大世界, 判定与误报防线见 auction_recovery.is_world_screen。"""
        return auction_recovery.is_world_screen(self)

    def _resume_after_world_drop(self, boxes: AuctionBoxes, deadline: float) -> AuctionState:
        """掉线后的统一出口: 扣回场配额后回场, 实现见 auction_recovery.resume_after_world_drop。"""
        return auction_recovery.resume_after_world_drop(self, boxes, deadline)

    def _recover_from_world(self, boxes: AuctionBoxes, deadline: float) -> AuctionState:
        """掉线回场并重跑匹配阶段, 预算规则见 auction_recovery.recover_from_world。"""
        return auction_recovery.recover_from_world(self, boxes, deadline)

    def _return_to_auction(self, boxes: AuctionBoxes, deadline: float) -> bool:
        """「大世界 → F5 → 都市闲趣 → 即刻落槌」回场路径, 实现见 auction_recovery。"""
        return auction_recovery.return_to_auction(self, boxes, deadline)

    def _click_instant_lot(self, deadline: float) -> bool:
        """在「都市闲趣」面板里找「即刻落槌」卡片, 实现见 auction_recovery.click_instant_lot。"""
        return auction_recovery.click_instant_lot(self, deadline)

    def _read_current_venue(self) -> str:
        """读主界面「当前：XXX场」留痕, 实现见 auction_recovery.read_current_venue。"""
        return auction_recovery.read_current_venue(self)

    def _build_boxes(self) -> AuctionBoxes:
        """按相对比例一次性构建本轮拍卖使用的全部 UI 区域。"""
        screen = self.box_of_screen
        return AuctionBoxes(
            match=screen(*self.BOX_MATCH),
            confirm=screen(*self.BOX_CONFIRM),
            bid=screen(*self.BOX_BID),
            bid_keypad=screen(*self.BOX_BID_KEYPAD),
            skip_area=screen(*self.BOX_SKIP_AREA),
            exit=screen(*self.BOX_EXIT),
            bid_confirm=screen(*self.BOX_BID_CONFIRM),
            abandon=screen(*self.BOX_ABANDON),
            abandon_confirm=screen(*self.BOX_ABANDON_CONFIRM),
            asset_value=screen(*self.BOX_ASSET_VALUE),
            estimate=screen(*self.BOX_ESTIMATE),
            last_bid=screen(*self.BOX_LAST_BID),
            clear=screen(*self.BOX_CLEAR),
            price_result=screen(*self.BOX_PRICE_RESULT),
            price_result_keypad=screen(*self.BOX_PRICE_RESULT_KEYPAD),
            exception_area=screen(*self.BOX_EXCEPTION_AREA),
            main_title=screen(*self.BOX_MAIN_TITLE),
            main_asset=screen(*self.BOX_MAIN_ASSET),
            insufficient=screen(*self.BOX_INSUFFICIENT),
            welfare_btn=screen(*self.BOX_WELFARE_BTN),
            welfare_dialog=screen(*self.BOX_WELFARE_DIALOG),
            welfare_counter=screen(*self.BOX_WELFARE_COUNTER),
            claim=screen(*self.BOX_CLAIM),
            cancel=screen(*self.BOX_CANCEL),
            warehouse_btn=screen(*self.BOX_WAREHOUSE_BTN),
            warehouse_title=screen(*self.BOX_WAREHOUSE_TITLE),
            sell=screen(*self.BOX_SELL),
            confirm_sell=screen(*self.BOX_CONFIRM_SELL),
            sell_label=screen(*self.BOX_SELL_LABEL),
            sell_value=screen(*self.BOX_SELL_VALUE),
            blank=screen(*self.BOX_BLANK),
            close=screen(*self.BOX_CLOSE),
            one_click_sell=screen(*self.BOX_ONE_CLICK_SELL),
            popup_close_hint=screen(*self.BOX_POPUP_CLOSE_HINT),
            popup_blank=screen(*self.BOX_POPUP_BLANK),
        )

    # --- 任务入口回场 (大世界 → 拍卖主界面) ---
    def _ensure_auction_entry(self, boxes: AuctionBoxes) -> None:
        """每轮开始确认在拍卖主界面, 入口回场规则见 auction_recovery.ensure_auction_entry。"""
        auction_recovery.ensure_auction_entry(self, boxes)

    def _run_single_round(self, boxes: AuctionBoxes) -> None:
        """执行一轮拍卖, 记录结果并仅在确认回到主界面后触发出售。"""
        # 结算后处理会把本轮的满仓与低保金结果写回, 每轮开始前先清空上一轮的观测.
        self._post_round_state = PostRoundState()
        # 跨过每日刷新时刻(5 点)时清空当日低保领取记录, 否则昨天领满的记录会让今天
        # 一开局就按「已领完」放开出售.
        self._rollover_welfare_day()

        if self._inventory_stuck:
            # 上一轮满仓且出售未成功: 仓库腾不出空间时这一轮出价必然失败,
            # 与其把整轮 deadline 空转掉, 不如先重试一次清理藏品.
            # 满仓是上一轮已经判定过的结论, 这里直接沿用, 不再要求重新 OCR 命中:
            # 满仓提示会被弹窗遮住, 重新检测失败就什么都不做, 变成每轮空跳的死循环.
            # 前提若已过时(上一轮未确认的出售其实清空了仓库), 由清理流程自己用
            # 「放宽清单确认读数为 0」的证据裁决并复位, 见 _sell_collections_with_escalation.
            self.log_warning("满仓且上次出售未成功, 跳过本轮拍卖, 先重试清理藏品")
            self.add_failed("满仓未清理")
            self._try_sell_collections(PostRoundState(inventory_full=True), boxes)
            return

        finished = self._exec_auction_round(boxes)
        if finished:
            self.add_success()
        else:
            self.add_failed("结果阶段进入下一轮出价")

        self.log_info(f"本轮拍卖完成 ({self.current_round}/{self._round_state.total_text})")
        if finished and self._post_round_state.observed:
            # 两个前置条件都不能少:
            # - finished 为 False 时画面仍在拍卖出价界面, 出售只会在仓库入口白等超时;
            # - observed 为 False 说明 _finish_auction 没识别到主界面标题就返回了,
            #   画面状态未知, 同样不能去点仓库入口.
            self._try_sell_collections(self._post_round_state, boxes)

    def _try_sell_collections(self, state: PostRoundState, boxes: AuctionBoxes) -> None:
        """轮次末尾的出售入口: 给出售流程一个独立的、有界的预算。

        原本两处调用都不传 deadline, 于是 _bounded_timeout(deadline=None, ...) 一律
        原样返回 limit、_timeout_or_zero 也永不返回 0 —— 整条出售流程实际上没有任何
        上级预算。逐分支累加的等待下限约 35 秒(连续失败放宽时约 71 秒), 而它不消耗
        ROUND_TIMEOUT(600): 「满仓时清理」每轮都要走一遍, 仓库入口读不到时就变成
        「任务在跑但几乎不出价」, 且日志里看不出时间被谁吃掉。

        出售失败不该影响本轮已经记下的成功/失败结论, 所以这里兜住 WaitFailedException ——
        补上 deadline 后 _sell_collections 收尾的 _bounded_sleep 会在预算耗尽时抛它。
        """
        sell_deadline = time.monotonic() + self.SELL_TIMEOUT
        try:
            self._sell_collections_on_interval(boxes, sell_deadline, state=state)
        except TaskDisabledException:
            raise
        except WaitFailedException as e:
            self.log_warning(f"藏品出售超出 {self.SELL_TIMEOUT} 秒预算, 本轮放弃出售: {e}")

    # --- 单轮流程编排 ---
    def _exec_auction_round(self, boxes: AuctionBoxes) -> bool:
        """执行单轮拍卖, 按序调度各阶段。

        Returns:
            bool: 拍卖是否顺利进入结算(进入下一轮出价时返回 False)。
        """
        deadline = time.monotonic() + self.ROUND_TIMEOUT
        # 本轮回场配额, 由整轮创建、所有匹配重跑共享。原来把这个状态放在
        # `allow_recover` 参数上逐层传递, 但 `_ensure_confirm_stage` 的确认失败分支会重新
        # 调 `_stage_match(boxes, deadline)`, 默认值 `True` 把配额悄悄恢复 —— 于是一轮里可以
        # 回场多次, 每次都要重走一遍「F5 → 都市闲趣 → 即刻落槌」的动画, 把整轮 deadline
        # 耗光。配额属于「这一轮」而不是「这一次匹配调用」, 所以只能挂在轮次上。
        self._recover_quota = self.RECOVER_MAX_PER_ROUND
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
        # 整轮 deadline 确实不重置, 但 _stage_match 内部的 MATCH_TIMEOUT 是局部预算,
        # 这一次重试会重新计一次(累计上限 2 x MATCH_TIMEOUT), 由整轮 deadline 兜底。
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
            time_out=self._remaining_timeout(deadline, self.BID_SCREEN_TIMEOUT),
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

        deadline 是整轮拍卖的绝对截止时间, 不会因点击重试而重置; 但本阶段的
        MATCH_TIMEOUT 是局部预算, 每次进入本方法都重新计一次 —— _ensure_confirm_stage
        失败后会再调一次本方法, 所以匹配阶段累计上限是 2 x MATCH_TIMEOUT, 只由整轮
        deadline 兜底。另外循环退出条件有两个(MATCH_MAX_LOOPS 与 stage_deadline),
        循环体耗时不等于 POLL_INTERVAL 时两者谁先生效不确定, 都保留。

        掉线是否还能回场由轮次配额 `_recover_quota` 决定, 不再用参数传递: 参数会在
        「确认失败后重跑本方法」这条路径上被默认值恢复, 让同一轮反复回场。
        """
        self.log_info("匹配阶段开始, 等待确认或出价界面")
        stage_deadline = min(deadline, time.monotonic() + self.MATCH_TIMEOUT)
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
            # 点击后界面未变化时返回 None 继续轮询; 单轮 deadline 到期由内部抛出超时.
            if self._is_match_screen(boxes):
                result = self._handle_match_click(boxes, stage_deadline)
                if result is AuctionState.WORLD:
                    return self._resume_after_world_drop(boxes, deadline)
                if result is not None:
                    return result

            self.sleep(self.POLL_INTERVAL)

        # 空转到超时前判一次大世界: 网络抖动掉线时四种界面状态全不命中, 直接抛超时会让
        # 本轮白等两分钟。命中大世界就回场后重跑本阶段, 仍在整轮 deadline 内。
        if self._is_world_screen():
            return self._resume_after_world_drop(boxes, deadline)

        raise WaitFailedException("匹配阶段超时, 未进入确认或出价界面")

    def _handle_match_click(
        self, boxes: AuctionBoxes, stage_deadline: float
    ) -> AuctionState | None:
        """点击开始匹配, 并等待后续界面状态变化。

        使用统一循环同时检测确认, 出价和跳过界面, 覆盖匹配对局与加载动画。
        点击后 MATCH_PROBE_TIMEOUT 秒内界面毫无变化时判定点击未生效并立刻返回,
        由调用方重新点击; 界面确实在切换(处于加载动画)时最多等待 MATCH_CLICK_TIMEOUT 秒。

        stage_deadline 是匹配阶段的局部截止时间(见 _stage_match), 不是整轮 deadline;
        所以这里的超时异常要带上阶段名, 不能沿用「单轮拍卖超时」。
        """
        clicked = self._wait_operate_click(
            boxes.match,
            RE_MATCH,
            self._remaining_timeout(stage_deadline, 5, "匹配阶段超时, 未进入确认或出价界面"),
        )
        if not clicked:
            return None
        self.log_info("已点击开始匹配, 等待状态变化")

        click_deadline = min(stage_deadline, time.monotonic() + self.MATCH_CLICK_TIMEOUT)
        probe_deadline = min(click_deadline, time.monotonic() + self.MATCH_PROBE_TIMEOUT)
        loop_count = 0
        while time.monotonic() < click_deadline:
            loop_count += 1
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

            # 界面切换时按钮会消失, 这种情况继续等加载动画;
            # 按钮还在原地说明点击没生效, 交回上层立刻重新点击.
            if time.monotonic() >= probe_deadline:
                if self._is_match_screen(boxes):
                    self.log_warning(
                        f"点击开始匹配后 {self.MATCH_PROBE_TIMEOUT} 秒界面无变化, "
                        f"判定点击未生效, 立即重新点击"
                    )
                    return None
                # 按钮已消失却没进后续界面: 正常是加载动画, 也可能是掉线被踢回大世界。
                # 大世界判定比 OCR 贵, 按 WORLD_PROBE_INTERVAL 节流探测。
                if loop_count % self.WORLD_PROBE_INTERVAL == 0 and self._is_world_screen():
                    self.log_warning("匹配阶段界面长时间无变化且检测到大世界, 判定为掉线")
                    return AuctionState.WORLD

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
        资产为 0 时放弃本次出价并等待拍卖结束, 放弃不计入出价序号。
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
                bid_placed = self._attempt_bid(boxes, deadline)
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

            # 尝试完成(无论是否真正出价)后重置失败重试计数, 用于下一次尝试.
            retry = 0
            if bid_placed:
                self.current_bid_count += 1
                self.log_info(f"第 {self.current_bid_count} 次出价成功, 等待拍卖结果或加价")
            else:
                self.log_info("本次出价已放弃, 等待拍卖结果")

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

    def _attempt_bid(self, boxes: AuctionBoxes, deadline: float) -> bool:
        """单次出价尝试: 包含资产识别, 出价面板确认和可选表情包动作。

        失败时抛出 WaitFailedException, 由调用方决定是否重试。
        Returns:
            bool: True 表示已提交出价; False 表示资产为 0 已放弃本次出价。
        """
        # 等待确认后的加载动画完成, 再判断资产值.
        asset_value = self._read_asset_value(
            boxes.asset_value, self._remaining_timeout(deadline, self.ASSET_OCR_TIMEOUT)
        )
        if asset_value is None:
            self.log_warning("资产值识别失败, 准备重试本次出价")
            raise WaitFailedException("资产值未识别")

        # 资产为 0 时放弃本轮出价; 单次误读就放弃整场拍卖代价过高, 放弃前需二次确认.
        if asset_value == 0:
            confirm_value = self._read_asset_value(
                boxes.asset_value, self._remaining_timeout(deadline, self.ASSET_OCR_TIMEOUT)
            )
            if confirm_value is None:
                self.log_warning("资产值二次识别失败, 准备重试本次出价")
                raise WaitFailedException("资产值未识别")
            if confirm_value != 0:
                self.log_warning(f"资产二次识别为 {confirm_value}, 首次读数 0 判定为误读, 继续出价")
                asset_value = confirm_value
            else:
                self.log_info("当前资产值两次识别均为 0, 放弃本轮出价")
                self.operate_click(boxes.abandon, after_sleep=0.5)
                self.sleep(0.5)
                self.operate_click(boxes.abandon_confirm, after_sleep=0.5)
                abandoned = self.wait_until(
                    lambda: not self._is_bid_screen(boxes),
                    time_out=self._remaining_timeout(deadline, 5),
                    settle_time=0.5,
                    raise_if_not_found=False,
                )
                if not abandoned:
                    self.log_warning("点击放弃后仍在出价界面, 准备重试本次尝试")
                    raise WaitFailedException("放弃出价失败")
                return False

        self.log_debug(f"当前资产值为 {asset_value}, 继续执行出价")

        # 数字键盘已经弹出时 BOX_BID 被弹窗盖住(实测该框 all_boxes 全空), 在 boxes.bid 上
        # 等 RE_BID 只会等到超时, 于是 _input_fixed_price 永远走不到. 面板已就绪时直接跳过
        # 出价按钮, 否则保持原有等待与点击路径.
        keypad_open = bool(self.ocr(box=boxes.bid_keypad, match=RE_BID_PANEL))
        if keypad_open:
            self.log_info("数字面板已打开, 跳过出价按钮")
            found = True
        else:
            self.log_info("等待出价按钮")
            found = self._wait_operate_click(
                boxes.bid,
                RE_BID,
                self._remaining_timeout(deadline, 10),
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

        if self._assist_enabled(self.ASSIST_EMOTE):
            self._send_emote()

        return True

    def _stage_result(self, boxes: AuctionBoxes, deadline: float) -> bool:
        """结果阶段: 等待结算, 处理跳过动画或返回匹配界面。

        只有「等待结算」受 RESULT_TIMEOUT 约束; 检测到结束状态之后的收尾动作
        (跳过动画 / 一键出售 / 退出拍卖 / 结算后观测) 一律用整轮 deadline。
        这两者不能共用一个截止时间: RESULT_TIMEOUT(90 秒) 只是阶段预算, 把它当成
        收尾预算后, 结算阶段空转掉 80 秒就只剩 10 秒可用来退出和观测, 空转满 90 秒
        时收尾动作会在整轮还有几百秒的情况下抛「单轮拍卖超时」, 把一个已经拍成的
        轮次记成失败; 「返回匹配界面」分支更静默 —— _observe_post_round_on_main_screen
        拿不到时间就直接返回, _post_round_state.observed 保持 False, 轮次末尾不出售,
        满仓时后续每轮都会卡在「开始匹配」上。

        主界面判定必须排在「跳过动画」之前: 两个区域都落在主界面底部按钮带
        (skip_area 0.703~0.807 / 0.902~0.953 与 BOX_MATCH 0.7427~0.8360 / 0.8972~0.9472
        大面积重叠), 主界面上误读到「跳过」时, 若先走跳过分支就会点一个不存在的
        「跳过动画」、再等一个不存在的「退出拍卖」按钮, 失败时抛异常跳过整个
        _run_post_round_actions —— 而 _post_round_state.observed 保持 False 的后果
        正是上面那段描述的「满仓时清理完全失效」。
        """
        self.log_info("结算阶段开始, 等待拍卖结果")
        result_deadline = min(deadline, time.monotonic() + self.RESULT_TIMEOUT)
        loop_count = 0

        while loop_count < self.RESULT_MAX_LOOPS and time.monotonic() < result_deadline:
            loop_count += 1
            self.next_frame()

            if self._is_match_screen(boxes):
                self.log_info("返回匹配界面")
                self._observe_post_round_on_main_screen(boxes, deadline)
                return True

            skip_results = self.ocr(box=boxes.skip_area, match=RE_SKIP)
            if skip_results:
                self._finish_auction(boxes, skip_results, deadline)
                return True

            if self._is_bid_screen(boxes):
                self.log_info("进入下一轮出价")
                return False

            self.sleep(self.POLL_INTERVAL)

        raise WaitFailedException("结算阶段超时, 未检测到结束状态")

    def _finish_auction(
        self, boxes: AuctionBoxes, skip_results: list[Box], deadline: float
    ) -> None:
        """拍卖结束处理: 跳过动画, 一键出售, 退出拍卖, 并在主界面执行结算后的辅助操作。"""
        self.log_info("检测到跳过动画")
        self.operate_click(skip_results, after_sleep=0.5)

        # 「拍卖成功一键出售」必须在退出拍卖之前完成: 一键出售按钮只在结算界面存在.
        if self._sell_mode() == self.SELL_MODE_ONE_CLICK:
            self._sell_on_settlement_screen(boxes, deadline)

        exit_button = self._wait_operate_click(
            boxes.exit,
            RE_EXIT,
            self._remaining_timeout(deadline, self.EXIT_BUTTON_TIMEOUT),
            after_sleep=0.5,
        )
        if not exit_button:
            raise WaitFailedException("退出拍卖按钮未出现")
        self.log_info("退出拍卖")

        self.log_info("等待主界面稳定")
        # 「我的资产」在结算等界面也会出现, 改用只在拍卖主界面出现的「即刻落槌」判定.
        main_title = self.wait_ocr(
            box=boxes.main_title,
            match=RE_MAIN_TITLE,
            time_out=self._remaining_timeout(deadline, 15),
            raise_if_not_found=False,
            settle_time=0.5,
            post_action=lambda: self.sleep(0.5),
        )
        if not main_title:
            self.log_warning("主界面「即刻落槌」标题未识别, 跳过本轮结算后处理")
            return

        self.log_info("主界面加载完成")
        self._run_post_round_actions(boxes, deadline)

    def _observe_post_round_on_main_screen(self, boxes: AuctionBoxes, deadline: float) -> None:
        """已经回到主界面时的结算后观测, 实现见 auction_welfare.observe_post_round。"""
        auction_welfare.observe_post_round(self, boxes, deadline)

    def _sell_on_settlement_screen(self, boxes: AuctionBoxes, deadline: float) -> None:
        """结算界面「一键出售」, 实现见 auction_sell.on_settlement_screen。"""
        auction_sell.on_settlement_screen(self, boxes, deadline)

    def _run_post_round_actions(self, boxes: AuctionBoxes, deadline: float) -> None:
        """结算后的观测与低保领取编排, 执行顺序见 auction_welfare.run_post_round_actions。"""
        auction_welfare.run_post_round_actions(self, boxes, deadline)

    def _sell_mode(self) -> str:
        """读取出售模式, 未知值按「不出售」处理, 规则见 auction_sell.normalize_mode。"""
        return auction_sell.normalize_mode(self.config.get(self.CONF_SELL_MODE, self.SELL_MODE_OFF))

    def _assist_enabled(self, feature: str) -> bool:
        """判断「启用辅助功能」多选框里是否勾选了某个功能。

        多选框存的是勾选项列表, 未勾选时为空列表。值不是列表(用户手工改成字符串或
        配置仍为旧 bool)时一律按未勾选处理: 少发一个表情、少领一次低保金都是可
        恢复的, 不该因为脏配置去点不存在的按钮。
        """
        selected = self.config.get(self.CONF_ASSIST_FEATURES, ())
        if not isinstance(selected, list):
            return False
        return feature in selected

    def _uses_collection_sell(self) -> bool:
        """本轮是否需要走「藏品仓库」出售流程, 判定见 auction_sell.uses_collection_sell。"""
        return auction_sell.uses_collection_sell(self._sell_mode())

    def _detect_inventory_full(self, boxes: AuctionBoxes, timeout: float) -> bool | None:
        """检测主界面的库存不足提示, 实现见 auction_sell.detect_inventory_full。"""
        return auction_sell.detect_inventory_full(self, boxes, timeout)

    def _observe_main_asset(self, boxes: AuctionBoxes, deadline: float) -> int | None:
        """读取主界面资产值, 实现见 auction_welfare.observe_main_asset。"""
        return auction_welfare.observe_main_asset(self, boxes, deadline)

    def _claim_welfare_if_needed(
        self, boxes: AuctionBoxes, deadline: float, asset_value: int | None
    ) -> bool:
        """资产低于阈值时领取低保金, 阈值判定见 auction_welfare.claim_if_needed。"""
        return auction_welfare.claim_if_needed(self, boxes, deadline, asset_value)

    # --- 界面状态判定 ---
    def _is_match_screen(self, boxes: AuctionBoxes) -> bool:
        return bool(self.ocr(box=boxes.match, match=RE_MATCH))

    def _is_confirm_screen(self, boxes: AuctionBoxes) -> bool:
        return bool(self.ocr(box=boxes.confirm, match=RE_CONFIRM))

    def _is_bid_screen(self, boxes: AuctionBoxes) -> bool:
        """判断是否已在出价界面(含数字键盘已弹出的状态)。

        键盘弹窗会盖住 BOX_BID, 那时只能靠弹窗上的文案认出界面, 否则会空等到超时。
        键盘态优先判定: 它是更具体的形态, 命中就不必再读 BOX_BID。
        """
        if self.ocr(box=boxes.bid_keypad, match=RE_BID_PANEL):
            return True
        return bool(self.ocr(box=boxes.bid, match=RE_BID))

    def _is_skip_screen(self, boxes: AuctionBoxes) -> bool:
        return bool(self.ocr(box=boxes.skip_area, match=RE_SKIP))

    def _dismiss_notice_popup(
        self,
        boxes: AuctionBoxes,
        deadline: float | None,
        reason: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        """点掉挡在流程前面的「提示」类弹窗, 命中返回 True。

        入场费确认、异常出价、满仓提示等弹窗共用一套模板, 确认按钮都在同一组坐标上,
        因此复用 exception_area 即可, 不需要为每个弹窗单独量框。
        单帧 ocr 会漏掉刚出现的弹窗(之后整条流程卡在弹窗上), 所以带短超时轮询;
        每次出价都要走一遍的热路径用 timeout 调小预算, 避免固定白等。
        """
        budget = self.NOTICE_POPUP_TIMEOUT if timeout is None else timeout
        budget = self._timeout_or_zero(deadline, budget)
        if budget <= 0:
            return False
        if not self.wait_ocr(
            box=boxes.exception_area,
            match=RE_CONFIRM,
            time_out=budget,
            raise_if_not_found=False,
            settle_time=0.3,
        ):
            return False

        self.log_info(f"检测到提示弹窗({reason}), 点击确认")
        self.operate_click(boxes.exception_area, after_sleep=0.3)
        return True

    # --- 超时辅助 ---
    @staticmethod
    def _remaining_timeout(deadline: float, limit: float, message: str = "单轮拍卖超时") -> float:
        """返回受 deadline 限制的等待时间, deadline 到期时立即失败。

        message 供「传进来的不是整轮 deadline 而是阶段 deadline」的调用点区分日志:
        匹配阶段的局部预算(MATCH_TIMEOUT)用尽时整轮往往还剩几百秒, 沿用「单轮拍卖
        超时」会让排查的人以为 600 秒跑满了。
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WaitFailedException(message)
        return min(limit, remaining)

    def _bounded_timeout(self, deadline: float | None, limit: float) -> float:
        """在兼容无 deadline 调用的同时, 限制有 deadline 调用的等待时间。"""
        return limit if deadline is None else self._remaining_timeout(deadline, limit)

    @staticmethod
    def _timeout_or_zero(deadline: float | None, limit: float) -> float:
        """和 _bounded_timeout 类似, 但 deadline 用尽时返回 0 而不是抛异常。

        用于可选的观测步骤(满仓检测、弹窗兜底): deadline 用尽只意味着这次没测到,
        不应该让整个结算后处理崩掉。
        """
        if deadline is None:
            return limit
        return max(min(limit, deadline - time.monotonic()), 0.0)

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

    def _config_decimal(self, key: str, default: str = "0") -> Decimal:
        """读取十进制配置, 非法时回退到默认值。

        用于参与 Decimal 运算的配置(加价数值): 配置原值本身就是文本框里的字符串,
        直接解析能保留用户填的全部精度, 而先经 float 中转再 str() 只剩 17 位有效数字。
        非法值一律回退默认值, 与 _config_float 一样不抛异常 —— 调用方另有兜底。
        """
        try:
            return Decimal(str(self.config.get(key, default)))
        except (ArithmeticError, ValueError):
            return Decimal(default)

    def _config_int_list(self, key: str) -> list[int]:
        """读取整数列表配置, 任一项非法时返回空列表。"""
        try:
            return [int(item) for item in self.config.get(key, [])]
        except (TypeError, ValueError):
            return []

    def _quality_list(self, key: str) -> list[str]:
        """读取某个出售品质清单, 值不是列表时按空清单处理。"""
        raw = self.config.get(key, [])
        if not isinstance(raw, (list, tuple)):
            return []
        return [name for name in self.QUALITY_KEYS if name in raw]

    def _sell_qualities(self) -> list[str]:
        """按当日低保阶段返回本轮要出售的品质清单。

        两个清单都是「勾选即出售」: 勾了才卖, 没勾的一律保留。低保金的领取前提是资产
        低于 10 万, 而卖藏品会抬高资产 —— 所以「还没领满」阶段只卖低价值品质, 领满
        当日次数后才放开高价值品质(领满之后当天再也领不到低保, 这时多卖才不亏)。

        ⚠️ 判定依据是「今日低保**领满**」而不是「今天领到过」: 旧实现每次领取成功就
        追加出售, 于是「领 1 次 → 卖一次 → 资产过线 → 之后再也领不到」自我阻断, 用户
        要花更多场次把资产花下去才能领下一次。读不到弹窗读数时按「还没领满」处理
        (资产高于阈值弹窗不再打开, 或未勾选「低保金」辅助 —— 弹窗只在领取流程里打开):
        少卖一次只是少赚, 卖错了却会让当天剩下的低保领不到。

        ⚠️ 满仓**不**切换清单: 满仓只说明必须腾空间, 不代表低保已无望, 按低保阶段保守
        地卖才符合「领低保优先」; 真腾不出空间时有既有的放宽机制兜底(连续满仓失败后
        6 个品质全卖, 见 _sell_collections_with_escalation)。
        """
        key = (
            self.CONF_SELL_AFTER_WELFARE
            if self._welfare_quota_exhausted()
            else self.CONF_SELL_BEFORE_WELFARE
        )
        return self._quality_list(key)

    def _validate_price_config(self) -> None:
        """任务开始前校验价格相关配置, 非法时直接终止任务。

        出价面板打开后才发现非法配置, 会以每轮 3 次重试的方式空转, 必须在入口拦截。
        校验范围随出价模式变化, 未使用的价格配置不参与校验。
        """
        mode = self.config.get(self.CONF_BID_MODE, self.BID_MODE_CUSTOM)

        if mode == self.BID_MODE_LIST:
            auction_price.validate_bid_prices(
                [self._config_int(key, 0) for key in self.CONF_BID_PRICES]
            )
            return

        if mode == self.BID_MODE_ESTIMATE:
            ratio = self._config_float(self.CONF_ESTIMATE_RATIO, 0.0)
            if not math.isfinite(ratio) or ratio <= 0:
                raise ValueError(
                    f"估价倍率必须为正数, 当前: {self.config.get(self.CONF_ESTIMATE_RATIO)!r}"
                )
            # 估价读不出时回退「基础价」出价, 非正整数会让那次出价因「非法价格」
            # 连续失败 3 次丢掉整轮。正常配置下不拦任务(估价可读时它根本用不到),
            # 只提前把后果说清楚。
            try:
                fallback = int(self.config.get(self.CONF_FIXED_PRICE))
            except (TypeError, ValueError):
                fallback = 0
            if fallback <= 0:
                self.log_warning(
                    f"「{self.CONF_FIXED_PRICE}」不是正整数"
                    f"({self.config.get(self.CONF_FIXED_PRICE)!r}), "
                    "估价读不出时回退的出价将无法输入, 该次出价会失败"
                )
            return

        base_raw = self.config.get(self.CONF_FIXED_PRICE)
        try:
            base_price = int(base_raw)
        except (TypeError, ValueError):
            raise ValueError(f"基础价配置非法: {base_raw!r}") from None
        if base_price <= 0:
            raise ValueError(f"基础价必须为正整数, 当前: {base_raw!r}")

        if self.config.get(self.CONF_AUTO_RAISE, False):
            # 校验必须与 _raise_price 同口径(_config_decimal 的 Decimal 解析): 若用
            # _config_float, 非法字符串会回退成 0.0 顺利通过, 运行时自定义/百分比
            # 模式拿着 0 静默按基础价出价, 用户完全无感.
            try:
                raise_value = Decimal(str(self.config.get(self.CONF_RAISE_VALUE)))
            except (ArithmeticError, ValueError):
                raise ValueError(
                    f"加价数值配置非法: {self.config.get(self.CONF_RAISE_VALUE)!r}"
                ) from None
            if not raise_value.is_finite():
                raise ValueError(f"加价数值配置非法: {self.config.get(self.CONF_RAISE_VALUE)!r}")
            # 0 或负数会通过 is_finite, 运行时每口出价都触发回退告警(倍率模式偶数次
            # 偏移还会先爆出天文数字); 加价的语义就是往上加, 在入口一并拦下。
            if raise_value <= 0:
                raise ValueError(
                    f"加价数值必须为正数, 当前: {self.config.get(self.CONF_RAISE_VALUE)!r}"
                )

    def _warn_if_no_sellable_quality(self) -> None:
        """两个出售品质清单都为空时给出告警: 开了出售模式却没有可出售的品质。

        配置本身不非法(用户可以随时改), 所以只告警不拦截。但满仓时这种配置会让
        出售一直报「成功」却清不出空间, 之后每轮出价都失败, 提前说清楚更好排查。
        """
        if not self._uses_collection_sell():
            return

        if any(self._quality_list(key) for key in self.SELL_QUALITY_KEYS):
            return
        self.log_warning(
            f"「{self.CONF_SELL_BEFORE_WELFARE}」与「{self.CONF_SELL_AFTER_WELFARE}」"
            "都没有勾选品质, 本次运行不会清掉任何藏品"
        )

    # --- 资产解析 (兼容别名) ---
    # 纯函数唯一来源: src/tasks/auction/price.py; OCR 副作用与告警仍在任务侧协调,
    # 测试继续按 AutoBidAuctionTask.<名字> 访问。
    _parse_asset_value = staticmethod(auction_price.parse_asset_value)
    _is_partial_number_text = staticmethod(auction_price.is_partial_number_text)
    _has_inconsistent_grouping = staticmethod(auction_price.has_inconsistent_grouping)

    def _read_estimate_texts(self, box: Box, timeout: float) -> list:
        """读区域内**全部**文本, 不做 match 过滤。

        必须用 `self.ocr(match=None)` 而不是 `wait_ocr(match=RE_NUMBER)`:
        框架 `OCR.wait_ocr` 的 `match` 不只是「找到了没有」的判定条件 —— `onnx_ocr` 里
        `detected_boxes = find_boxes_by_name(detected_boxes, match)` 会把返回值**过滤成
        只含命中的框**, 非命中文本(如「当前估价：」标签)在返回列表里根本不存在。

        2026-09-23 21:49 线上的故障正是这个: 日志里 `all_boxes: [当前估价：_0.99,
        20,930_1.00]` 说明区域内两个框都在, 但 `wait_ocr(match=RE_NUMBER)` 只返回
        `[20,930]`, 标签被过滤掉 -> `label_right is None` -> 每一帧都判「标签未读到」,
        估价永远读不出, 出价一直回退基础价 1。

        这里改用 `ocr()`(不是 wait)并传 `match=None` 拿全量结果, 标签和数字都在里面。
        取一帧即可: 调用方的稳定判定循环本来就会反复重读。
        """
        deadline = time.monotonic() + timeout
        while True:
            boxes = self.ocr(box=box, match=None, log=False)
            texts = [b for b in boxes if b.name] if boxes else []
            if texts or time.monotonic() >= deadline:
                return texts
            self.sleep(0.2)

    def _read_estimate_value(
        self, box: Box, timeout: float, label: str = "当前估价"
    ) -> tuple[int | None, bool]:
        """读估价数字: 先按「估价」标签定位, 再取标签右侧的数字。

        必须按标签过滤而不能只靠裁框宽度。拿到的框里有多个文本时, 只把「估价」标签
        右侧的接起来 —— 区域内混进第二个数字栏(如「我的资产」数值)时, 直接 `"".join()`
        会把两串数字粘成一个, 表现为「估价少了一位」(2026-09-23 截图: 2,643 读成 ,643、
        1,912 读成 912)。`BOX_ESTIMATE` 的右边界现在收到 0.9200, 已经把「我的资产」数值
        排除在区域外(见该常量注释), 但标签过滤仍是最后一道防线: 界面过渡帧里数字位置会
        偏移, 也可能混进别的小字。

        实测「估价」标签右边沿在 0.8438~0.8464, 数字在 0.8484~0.9052 (另一局 13,875 在
        0.8542~0.9010), 两者之间有明显空隙, 所以按 `x0 >= label_right` 过滤是稳定的。

        Returns:
            (值, 是否贴边). 贴边表示数字右端距裁框右边界不足 ESTIMATE_EDGE_MARGIN_RATIO
            对应的像素数, 该读数可能已被裁掉末位, 调用方应按不可信处理。
        """
        all_boxes = self._read_estimate_texts(box, timeout)
        if not all_boxes:
            return None, False

        label_right = max(
            (b.x + b.width for b in all_boxes if "估价" in b.name.replace("：", "")),
            default=None,
        )
        if label_right is None:
            # 标签没读出来: 不猜, 交给调用方重读一帧.
            self.log_debug(f"{label} 标签未读到, 本帧数字不可靠")
            return None, False

        candidates = [b for b in all_boxes if b.x >= label_right and RE_NUMBER.search(b.name)]
        if not candidates:
            return None, False

        digit_boxes = sorted(candidates, key=lambda b: b.x)
        raw_text = "".join(b.name for b in digit_boxes)
        if self._is_partial_number_text(raw_text):
            self.log_debug(f"{label} OCR: '{raw_text}', 千位分隔符前缺数字, 视为残缺读数")
            return None, False
        if self._has_inconsistent_grouping(raw_text):
            self.log_debug(f"{label} OCR: '{raw_text}', 千位分组不自洽, 视为残缺读数")
            return None, False

        value = self._parse_asset_value(raw_text)
        right_edge = max(b.x + b.width for b in digit_boxes)
        # 阈值随分辨率等比放大, 至少 1px: 高 DPI 下框宽不变(本项目 resize_image 为默认 0,
        # 截图不重采样)时小于 1px 的判定没有意义.
        margin = max(1, round(self.width * self.ESTIMATE_EDGE_MARGIN_RATIO))
        tight = (box.x + box.width - right_edge) < margin
        self.log_debug(f"{label} OCR: '{raw_text}', 解析值: {value}, 贴边: {tight}")
        return value, tight

    def _read_asset_value(
        self, box: Box, timeout: float, label: str = "资产", *, reject_partial: bool = False
    ) -> int | None:
        """对指定区域做 OCR 并解析资产数值, 未识别或解析失败时返回 None。

        reject_partial 用于估价这类会跳动、首位可能被漏读的区域: 千位分隔符前面空着的
        残缺读数按未读出处理, 交给调用方重读; 资产、出售价值这类稳定的数字保持原行为。
        """
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
        if reject_partial and self._is_partial_number_text(raw_text):
            self.log_debug(f"{label} OCR: '{raw_text}', 千位分隔符前缺数字, 视为残缺读数")
            return None

        value = self._parse_asset_value(raw_text)
        self.log_debug(f"{label} OCR: '{raw_text}', 解析值: {value}")
        return value

    # --- 自动加价计算 ---
    def _calculate_auction_price(
        self, boxes: AuctionBoxes | None = None, deadline: float | None = None
    ) -> int:
        """计算当前出价应该输入的价格。

        价格来源由出价模式决定:
        - 自定义价格: 基准价格为自定义价格, 启用自动加价时按模式计算加价结果;
          启用指定回合单独出价且当前序号在勾选列表中时直接使用该价格。
        - 每轮指定价格: 按出价序号从配置列表依次取用。
        - 按系统估价: 读取出价面板上的当前估价并乘以倍率。

        出价序号从 1 开始计数, 基于成功出价次数 + 1。
        """
        bid_count = self.current_bid_count + 1
        mode = self.config.get(self.CONF_BID_MODE, self.BID_MODE_CUSTOM)

        if mode == self.BID_MODE_LIST:
            return self._listed_bid_price(bid_count)
        if mode == self.BID_MODE_ESTIMATE:
            return self._estimate_bid_price(boxes, deadline, bid_count)

        base_price = self._config_int(self.CONF_FIXED_PRICE, 1)

        special_price = self._special_round_price(bid_count)
        if special_price is not None:
            return special_price

        if not self.config.get(self.CONF_AUTO_RAISE, False):
            return base_price

        return self._raise_price(base_price, bid_count)

    def _resolve_bid_prices(self) -> list[int]:
        """读取 6 个每轮指定价格并解析成可按出价序号直接取用的列表。

        解析规则见 auction_price.resolve_bid_prices: 未设置(0)的回合沿用上一次
        已设置的价格, 第 1 次出价必须有价格, 否则返回空列表由调用方按配置错误处理。
        """
        raw_prices = [self._config_int(key, 0) for key in self.CONF_BID_PRICES]
        return auction_price.resolve_bid_prices(raw_prices)

    def _listed_bid_price(self, bid_count: int) -> int:
        """按出价序号取每轮指定价格, 出价次数超出配置项时沿用最后一次的价格。"""
        prices = self._resolve_bid_prices()
        if not prices:
            raise ValueError(f"每轮指定价格未配置: {self.CONF_BID_PRICES[0]} 必须大于 0")

        index = min(max(bid_count, 1), len(prices)) - 1
        price = prices[index]
        if bid_count > len(prices):
            # 游戏里一轮最多 6 回合出价, 走到这里说明出价次数与预期不符, 值得告警。
            self.log_warning(
                f"出价序号 {bid_count} 超出每轮指定价格的 {len(prices)} 次出价, "
                f"沿用最后一次价格 {price}"
            )
        else:
            self.log_info(f"第 {bid_count} 次出价使用指定价格 {price}")
        return price

    def _read_stable_asset_value(
        self,
        box: Box,
        timeout: float,
        label: str,
        *,
        skip_zero: bool = False,
    ) -> int | None:
        """连续读到相同数值才认为读数稳定, 避免取到跳动中的中间值。

        出价面板的当前估价在界面刚出现时会跳动几次, 第一次识别到的往往不是最终值,
        所以按 POLL_INTERVAL 换帧重读, 连续 ESTIMATE_STABLE_READS 次没有出现更完整的
        读数才采用。
        skip_zero 用于把 0 当作「面板还没滚出数值」的占位读数: 估价面板在数字滚动前会先
        显示 0, 把它当结果会算出 0 元出价, 所以这类读数不计入稳定判定, 继续等真值。

        读数走 `_read_estimate_value`, 即「按估价标签右边沿取数字」。这解决的是本函数
        原来修不掉的那类残缺: 末位数字被裁框切掉时位数不变 (`2,643` 读成 `,643`,
        连逗号一起丢就成 `643`), 「位数不减少」这条防线对它完全无效。附带地, 按标签
        过滤也杜绝了「我的资产」数值被 `RE_NUMBER` 拼进来的污染。

        原有的「位数不减少」判定保留: 它对「数字滚动中途读到更短的值」仍然有效。
        贴边的读数按「可能被裁掉末位」处理 —— 不采信, 并**重置**已攒的连续计数,
        因为末位丢失后位数可能不变, 只有贴边这个几何信号能发现它; 连续观察到的贴边
        不能反过来抬高更早那次读数的可信度。

        `same` 统计的是「连续几帧没有带来新信息」, 其中可能**一帧有效读数都没有**(画面
        静止时 OCR 一直读不出), 所以它只能用来确认「画面不再变化」, 不能确认「读到的是
        完整数值」。返回值另加 `valid_reads >= required` 一道门槛: `valid_reads` 是
        「连续读到同一个完整数值」的最长连续帧数, 任何一个 `value is None` 的缺失帧
        都会把它归零。少了这道门槛, 单次有效读数后再来两帧 OCR 失败就能凑满 `same >= 3`,
        在 10 秒超时之前把那次未必是终值的读数当「稳定值」采信, 并直接拿去算出价。

        数字滚动本身也要时间: 实测中间值可以稳定停留到面板打开后 2.6 秒 (19:16 那局),
        所以「连续相同」还要叠加 ESTIMATE_MIN_OBSERVE_SECONDS 的最短观察窗口,
        否则会在滚动结束前就采信 (19:18 那局 2.63 秒返回, 拿到了残缺的 197)。
        超时仍未稳定时返回最后一次有效读数并告警, 让调用方拿到比回退值更接近真实的值。
        """
        required = max(self.ESTIMATE_STABLE_READS, 1)
        observe = self.ESTIMATE_MIN_OBSERVE_SECONDS
        deadline = time.monotonic() + timeout
        last: int | None = None
        same = 0
        first_seen: float | None = None
        zero_seen = False
        valid_reads = 0
        tight_seen = False

        while time.monotonic() < deadline:
            value, tight = self._read_estimate_value(
                box,
                min(deadline - time.monotonic(), self.ASSET_OCR_TIMEOUT),
                label,
            )
            now = time.monotonic()
            if tight:
                # 数字右端贴住裁框边界: 末位可能已被切掉且位数不变, 无法与正常读数区分,
                # 只能整帧判为不可信. 记下来, 超时时用它解释为什么没读到.
                #
                # 这类帧**不能**按「未读出」处理: 未读出只是没拿到新信息, 之前那次读数
                # 仍然成立, 所以可以继续累积 same; 而贴边是「当前帧的裁框已经不够用」的
                # 证据, 之前那个值是在旧帧上读的, 它的可信度不会因为又观察到几次贴边而上升.
                # 若按未读出累积 same, 连续贴边反倒会把旧值攒成「稳定值」返回 —— 正是本
                # 次要防的末位被裁故障, 而且 tight_seen 的告警分支(只在 last is None 时
                # 才走)永远不会触发, 贴边信号被静默吞掉.
                tight_seen = True
                same = 0
                first_seen = None
                # last / valid_reads 必须一起作废. 只清 same 和 first_seen 会留下两个漏洞:
                # last 仍在 -> 上面的提前稳判条件里 `last is not None` 恒为真, 而
                # `first_seen is not None` 因为被清空而恒为假, 于是提前稳判**永远不会**触发;
                # 循环只能走到超时兜底, 把贴边**之前**在旧帧上读到的值当稳定值返回.
                # 更糟的是超时分支的 tight_seen 告警要求 `last is not None` 之外的路径,
                # 一旦 last 非空就只报「未稳定, 使用最后一次读数」, 贴边这个关键信号被吞掉,
                # 排查时看不出这个价格其实来自一帧已被裁掉末位的旧读数.
                # valid_reads 同理: 它统计的是 last 那次读数的有效性, last 作废后计数也必须归零.
                last = None
                valid_reads = 0
            elif value is None:
                # 未读出或残缺读数, 没有带来更完整的信息. 画面算「没变化」(same 继续累积),
                # 但它证明不了 last 是终值, 所以「连续有效读数」的计数到此为止.
                same += 1
                valid_reads = 0
            elif skip_zero and value == 0:
                # 面板加载中的占位读数, 不计入稳定判定.
                zero_seen = True
                valid_reads = 0
            elif last is None or (len(str(value)) >= len(str(last)) and value != last):
                # 位数变多或数值更新: 之前攒的连续次数作废, 以这次为准.
                if first_seen is None:
                    first_seen = now
                last = value
                same = 1
                valid_reads = 1
            else:
                # 与当前读数相同, 或位数更少(数字滚动中途读到更短的值).
                same += 1
                if value == last:
                    # 同一个完整数值被再次读到, 才是真正意义上的「有效读数」.
                    valid_reads += 1
                else:
                    # 位数更少的残缺值: 同样是一次「没读全」, 连续有效读数的计数归零.
                    valid_reads = 0

            if (
                last is not None
                and same >= required
                and valid_reads >= required
                and first_seen is not None
                and now - first_seen >= observe
            ):
                self.log_info(f"{label}读数稳定: {last} (连续 {same} 次)")
                return last

            # 必须换一帧再读, 否则两次读取会落在同一帧上, 读到同样的中间值.
            self.next_frame()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self.sleep(min(self.POLL_INTERVAL, remaining))

        if last is not None:
            self.log_warning(f"{label}在 {timeout} 秒内未稳定, 使用最后一次读数 {last}")
            if tight_seen:
                # 中途出现过贴边帧, 说明这段读数是在「有帧被裁掉末位」的干扰下得到的:
                # 贴边帧本身已被丢弃, 但同一屏的其它帧也可能同样不完整, 这个值只作参考.
                self.log_warning(
                    f"{label}观测期间出现过贴边读数, 该值可能不完整; "
                    f"若与实际不符, 请检查 "
                    f"{self.__class__.__name__}.BOX_ESTIMATE 右边界"
                )
        elif tight_seen:
            # last 为空只有两种成因: 从头到尾没读到, 或读到之后又被贴边帧作废.
            # 后者才是要提示用户去调裁框的情形, 文案要能同时覆盖.
            self.log_warning(
                f"{label}读数贴住识别区域边界(或其后读数不可信), 末位可能被裁掉, "
                f"视为未读出; 请检查 {self.__class__.__name__}.BOX_ESTIMATE 右边界"
            )
        elif zero_seen:
            self.log_warning(f"{label}在 {timeout} 秒内只读到 0, 视为未读出")
        return last

    def _estimate_bid_price(
        self, boxes: AuctionBoxes | None, deadline: float | None, bid_count: int
    ) -> int:
        """按出价面板上的当前估价乘倍率出价, 估价读不出时回退到基础价。

        估价区域被弹窗或动画遮挡时不应中断整场拍卖, 因此只告警并回退。
        估价数字会先跳动几次才稳定, 所以走稳定读取而不是单次 OCR。
        面板在滚出数字前会先显示 0, 所以用 skip_zero 把 0 当占位读数继续等,
        否则 0 会被 `is None` 之外的假值判断当成「识别失败」, 直接回退到基础价。
        """
        base_price = self._config_int(self.CONF_FIXED_PRICE, 1)
        estimate = None
        if boxes is not None:
            estimate = self._read_stable_asset_value(
                boxes.estimate,
                self._bounded_timeout(deadline, self.ESTIMATE_STABLE_TIMEOUT),
                "当前估价",
                skip_zero=True,
            )
        if estimate is None:
            self.log_warning(f"当前估价识别失败, 第 {bid_count} 次出价回退到基础价 {base_price}")
            return base_price

        ratio = self._config_float(self.CONF_ESTIMATE_RATIO, 1.0)
        final_price = auction_price.estimate_price(estimate, ratio)
        if final_price <= 0:
            self.log_warning(
                f"按估价 {estimate} 与倍率 {ratio} 计算出的价格 {final_price} 无效, "
                f"回退到基础价 {base_price}"
            )
            return base_price

        self.log_info(f"按系统估价出价: 当前估价 {estimate}, 倍率 {ratio}, 出价 {final_price}")
        return final_price

    def _special_round_price(self, bid_count: int) -> int | None:
        """启用指定回合单独出价时返回该回合价格, 否则返回 None。"""
        if not self.config.get(self.CONF_SPECIAL_ROUND, False):
            return None

        special_rounds = self._config_int_list(self.CONF_SPECIAL_ROUNDS)
        special_price = self._config_int(self.CONF_SPECIAL_ROUND_PRICE, 0)
        price = auction_price.special_round_price(bid_count, special_rounds, special_price)
        if price is not None:
            self.log_info(f"指定回合 {bid_count} 使用单独价格 {price}")
        return price

    def _raise_mode(self) -> str:
        """读取加价方式, 无效值按默认「倍率」处理。

        配置迁移已全部删除。旧配置或手工编辑留下的无效取值不能静默落到「自定义」分支,
        否则同一份价格配置会从指数增长变成线性增长, 所以直接回退到默认方式。
        """
        mode = self.config.get(self.CONF_RAISE_MODE, self.RAISE_MODE_MULTIPLE)
        return mode if mode in self.RAISE_MODES else self.RAISE_MODE_MULTIPLE

    def _raise_price(self, base_price: int, bid_count: int) -> int:
        """按配置的加价方式计算第 bid_count 次出价的价格。

        加价回合与三种方式的计算规则、Decimal 溢出防护见 auction_price;
        这里负责读取配置、告警与回退基础价。
        """
        mode = self._raise_mode()
        value = self._config_decimal(self.CONF_RAISE_VALUE, "0")
        raise_round = self._config_int(self.CONF_RAISE_ROUND, 0)

        # 未到配置的加价回合, 直接使用基础价.
        offset = auction_price.raise_offset(bid_count, raise_round)
        if offset is None:
            return base_price

        final_price = auction_price.raise_price(base_price, offset, mode=mode, raise_value=value)
        if final_price is None:
            self.log_warning(
                f"加价计算结果超出可表示范围, 回退到基础价 {base_price} "
                f"(模式 {mode}, 数值 {value}, 加价偏移 {offset})"
            )
            return base_price

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
            price = self._calculate_auction_price(boxes, deadline)

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

    # 按键序列纯函数唯一来源: src/tasks/auction/price.py。
    _price_key_sequence = staticmethod(auction_price.price_key_sequence)

    def _verify_input_price(self, boxes: AuctionBoxes, price: int, deadline: float | None) -> None:
        """校验数字面板显示的价格与目标价格一致, 不一致时抛出异常。

        价格区未输入时显示 "可输入范围0~<资产>" 提示文本, 视为未识别处理。

        键盘弹出后价格输入框移到了键盘右侧, BOX_PRICE_RESULT 那个矩形会落在提示文案
        「可输入范围0~<资产>」上, 于是键盘态下**永远**读到提示文案、永远判「未输入」
        (2026-09-23 实测)。所以两个区域都读: 命中提示文案或读不出时, 换用键盘态的
        输入框区域再试一次, 两个位置都没得到数字才算未输入。
        """
        raw_price = self._read_price_text(boxes.price_result, deadline)
        if not raw_price or RE_PRICE_HINT.search(raw_price):
            # 只有一种情况需要换区域重读: 读到的是提示文案。此时键盘态的实际输入框
            # 在别处。若两个区域都读到提示文案, 说明价格确实还没输入。
            keypad_text = self._read_price_text(boxes.price_result_keypad, deadline)
            if keypad_text and not RE_PRICE_HINT.search(keypad_text):
                raw_price = keypad_text

        if not raw_price:
            self.log_warning("输入价格结果未识别, 取消确认并重试当前出价")
            raise WaitFailedException("输入价格结果未识别")
        if RE_PRICE_HINT.search(raw_price):
            self.log_warning("价格区仍显示可输入范围提示, 视为未输入, 取消确认并重试当前出价")
            raise WaitFailedException("输入价格结果未识别")

        input_price = self._parse_asset_value(raw_price)
        self.log_debug(f"输入价格结果 OCR: '{raw_price}', 解析值: {input_price}")
        if input_price != price:
            self.log_warning(
                f"输入价格校验失败, 目标价格: {price}, 实际价格: {input_price}, "
                "取消确认并重试当前出价"
            )
            raise WaitFailedException("输入价格校验失败")

    def _read_price_text(self, box: Box, deadline: float | None) -> str:
        """读取价格区文本, 未识别时返回空串。"""
        price_boxes = self.wait_ocr(
            box=box,
            match=RE_NUMBER,
            time_out=3 if deadline is None else self._remaining_timeout(deadline, 3),
            settle_time=0.5,
            raise_if_not_found=False,
        )
        return "".join(text_box.name for text_box in price_boxes) if price_boxes else ""

    def _confirm_bid_price(self, boxes: AuctionBoxes, deadline: float | None) -> None:
        """点击确认出价, 并处理可能出现的异常确认框。"""
        confirmed = self._wait_operate_click(
            boxes.bid_confirm,
            RE_BID_CONFIRM,
            5 if deadline is None else self._remaining_timeout(deadline, 5),
            after_sleep=0.2,
        )
        if not confirmed:
            self.log_warning("确认出价点击超时, 准备重试当前出价")
            raise WaitFailedException("确认出价失败")

        # 检测是否出现异常确认框. 原来用单帧 ocr, 弹窗晚出现一点就漏掉, 之后整条流程卡住;
        # 改成带短超时轮询, 预算见 BID_NOTICE_POPUP_TIMEOUT.
        self._dismiss_notice_popup(
            boxes, deadline, "确认出价后", timeout=self.BID_NOTICE_POPUP_TIMEOUT
        )

    # --- 低保金 ---
    def _try_claim_welfare(self, boxes: AuctionBoxes, deadline: float | None = None) -> bool:
        """尝试领取每日低保金, 流程与异常语义见 auction_welfare.try_claim。"""
        return auction_welfare.try_claim(self, boxes, deadline)

    def _is_welfare_dialog_open(self, boxes: AuctionBoxes) -> bool:
        """检测低保金弹窗是否仍在, 实现见 auction_welfare.is_dialog_open。"""
        return auction_welfare.is_dialog_open(self, boxes)

    def _close_welfare_dialog(self, boxes: AuctionBoxes, deadline: float | None) -> bool:
        """关闭低保金弹窗, 重试语义见 auction_welfare.close_dialog。"""
        return auction_welfare.close_dialog(self, boxes, deadline)

    # --- 低保金领取记录 (决定追加出售是否放开) ---
    def _read_welfare_counter(self, boxes: AuctionBoxes, deadline: float | None = None) -> None:
        """读弹窗的「今日已领取次数：N/5」刷新当日记录, 实现见 auction_welfare.read_counter。"""
        auction_welfare.read_counter(self, boxes, deadline)

    def _welfare_quota_exhausted(self) -> bool:
        """今日低保次数是否已用尽, 保守方向判定见 auction_welfare.quota_exhausted。"""
        return auction_welfare.quota_exhausted(
            self._welfare_claims_today, self._welfare_daily_limit
        )

    def _rollover_welfare_day(self) -> None:
        """跨过每日刷新时刻清空当日领取记录, 切日规则见 auction_welfare.rollover_day。"""
        auction_welfare.rollover_day(self)

    def _wait_click_optional(
        self,
        box: Box,
        match: re.Pattern,
        deadline: float | None,
        timeout: float,
        desc: str,
    ) -> bool:
        """等待并点击目标控件, 超时未出现时返回 False; deadline 到期仍会抛出单轮超时。"""
        clicked = self._wait_operate_click(box, match, self._bounded_timeout(deadline, timeout))
        if not clicked:
            self.log_warning(f"{desc}未出现, 跳过本次操作")
        return clicked

    def _wait_operate_click(
        self,
        box: Box,
        match: re.Pattern,
        timeout: float,
        *,
        after_sleep: float = 0,
        settle_time: float = 0.5,
    ) -> bool:
        """等待目标控件出现后点击, 并使用带光标还原的点击路径。

        框架的 wait_click_ocr 直接走 click_box, 不会保存和还原鼠标位置;
        后台执行时会把用户的鼠标留在游戏窗口内, 因此统一改用 operate_click。
        """
        found = self.wait_ocr(
            box=box,
            match=match,
            time_out=timeout,
            raise_if_not_found=False,
            settle_time=settle_time,
        )
        if not found:
            return False
        self.operate_click(found, after_sleep=after_sleep)
        return True

    # --- 藏品出售 ---
    def _sell_collections_on_interval(
        self,
        boxes: AuctionBoxes,
        deadline: float | None = None,
        state: PostRoundState | None = None,
    ) -> None:
        """按出售模式决定本轮是否出售, 实现见 auction_sell.run_on_interval。"""
        auction_sell.run_on_interval(self, boxes, deadline, state=state)

    def _sell_collections(
        self,
        boxes: AuctionBoxes,
        deadline: float | None = None,
        sell_qualities: list[str] | tuple[str, ...] = (),
        *,
        require_sale: bool = False,
    ) -> bool | None:
        """尝试出售藏品, 流程与返回值语义(True/False/None)见 auction_sell.run_collections。"""
        return auction_sell.run_collections(
            self, boxes, deadline, sell_qualities, require_sale=require_sale
        )

    def _close_warehouse(self, boxes: AuctionBoxes) -> None:
        """关掉藏品仓库界面复位出售模式与勾选态, 实现见 auction_sell.close_warehouse。"""
        auction_sell.close_warehouse(self, boxes)

    def _is_warehouse_open(self, boxes: AuctionBoxes) -> bool:
        """检测藏品仓库界面是否还在, 实现见 auction_sell.is_warehouse_open。"""
        return auction_sell.is_warehouse_open(self, boxes)

    # 勾选是否被出售价值证实的纯判定, 唯一来源: src/tasks/auction/sell.py。
    _is_selection_confirmed = staticmethod(auction_sell.is_selection_confirmed)

    def _sell_collections_with_escalation(
        self,
        boxes: AuctionBoxes,
        deadline: float | None,
        sell_qualities: list[str] | tuple[str, ...],
        *,
        inventory_full: bool,
    ) -> bool:
        """执行出售并在连续满仓失败后放宽清单, 状态机见 auction_sell.run_with_escalation。"""
        return auction_sell.run_with_escalation(
            self, boxes, deadline, sell_qualities, inventory_full=inventory_full
        )

    def _select_quality_filters(
        self,
        deadline: float | None,
        sell_qualities: list[str] | tuple[str, ...] = (),
    ) -> int:
        """勾选要出售的品质按钮, 实现见 auction_sell.select_quality_filters。"""
        return auction_sell.select_quality_filters(self, deadline, sell_qualities)

    def _is_sell_mode(self, boxes: AuctionBoxes, timeout: float) -> bool:
        """检测藏品仓库是否已处于出售模式, 实现见 auction_sell.is_sell_mode。"""
        return auction_sell.is_sell_mode(self, boxes, timeout)

    def _ensure_sell_value(
        self,
        boxes: AuctionBoxes,
        deadline: float | None,
        selected: int,
        sell_qualities: list[str] | tuple[str, ...] = (),
    ) -> int | None:
        """校验品质勾选是否生效, 读数分歧处理见 auction_sell.ensure_sell_value。"""
        return auction_sell.ensure_sell_value(self, boxes, deadline, selected, sell_qualities)

    def _read_sell_value(self, boxes: AuctionBoxes, timeout: float) -> int | None:
        """读取「出售价值」数值, 实现见 auction_sell.read_sell_value。"""
        return auction_sell.read_sell_value(self, boxes, timeout)

    # --- 表情包 ---
    def _send_emote(self) -> None:
        """发送表情菜单中的第一个表情。"""
        self.log_info("发送表情包")
        self.operate_click(*self.EMOTE_BTN, after_sleep=0.8)
        self.operate_click(*self.EMOTE_FIRST, after_sleep=0.5)
        self.log_info("表情包发送完成")
