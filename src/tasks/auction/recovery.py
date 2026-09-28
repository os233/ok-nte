"""拍卖掉线回场与弹窗恢复: 大世界探测, 回场路径, 入口确认, 阻塞弹窗兜底。

模块函数的第一个参数 task 是 AutoBidAuctionTask 实例: OCR/输入/导航等框架 API
与本轮回场配额 (_recover_quota, 由 _exec_auction_round 每轮重置) 都经它访问;
配额的所有者是任务实例。回场预算等行为常量由本模块定义, 不再挂回任务类。
内部互相调用一律走 task._<方法名>, 让测试的实例级 mock 与任务侧的统一入口
保持生效。

预算规则与迁移前一致: 回场路径的每一步都按「剩余预算」取超时 (见
return_to_auction 的注释), 回场成功后重跑匹配阶段用的是调用方传入的同一份
轮次 deadline, 不另开预算。
"""

import time

from ok import TaskDisabledException, WaitFailedException

from src.tasks.auction.layout import (
    BOX_CITY_FUN_CARDS,
    BOX_CITY_FUN_TITLE,
    BOX_CURRENT_VENUE,
    POS_CITY_FUN_SCROLL,
    RE_CITY_FUN,
    RE_CURRENT_VENUE,
    RE_MAIN_TITLE,
    AuctionBoxes,
    AuctionState,
)

# --- 掉线回场 (秒/次) ---
# 网络不稳时匹配阶段会被踢回大世界, 界面状态全不命中, 只能空转到 MATCH_TIMEOUT。
# 回场是一次性的异常路径: 失败就按本轮失败处理, 交给下一轮重试。
RECOVER_TIMEOUT = 90  # 单次回场总预算
RECOVER_STEP_TIMEOUT = 12  # 回场各步骤的等待上限
RECOVER_SCROLL_STEPS = 4  # 「都市闲趣」面板最多滚动几次去找「即刻落槌」
RECOVER_SCROLL_WHEEL = -8  # 每次滚动的滚轮格数
# 单轮回场次数上限, 由 _exec_auction_round 写进 self._recover_quota 并扣减。
# 挂在轮次而不是调用参数上的原因见 AutoBidAuctionTask._exec_auction_round: 参数会在
# 「确认失败后重跑 _stage_match」的路径上被默认值恢复, 使同一轮可以反复回场, 每次都
# 重走一遍面板动画把整轮 deadline 耗光, 并且让「本轮只回场一次」这个约定形同虚设。
RECOVER_MAX_PER_ROUND = 1
# 启动时的入口回场 (见 ensure_auction_entry): 探测主界面标题的等待上限,
# 以及一次性回场预算。预算与 RECOVER_TIMEOUT 一致, 两者走的是同一条路径。
ENTRY_PROBE_TIMEOUT = 3
ENTRY_RECOVER_TIMEOUT = 90


def blocking_popup(task, boxes: AuctionBoxes | None = None) -> None:
    """整轮失败后的兜底: 按界面特征处理卡住的弹窗。

    `check_monthly_card` 只在 5 点前后 2 分钟的时间窗内生效, 任务在窗口之后
    才启动(或本机时钟与游戏刷新时刻不一致)时, 弹窗不会被时间窗命中, 每轮都会
    空转到超时。这里只在已经失败的情况下多花一次模板匹配, 正常路径没有额外开销。

    低保金弹窗同理: 领取流程异常时弹窗会留在界面上, 之后每轮都会识别不到拍卖界面。
    入场费确认/异常出价/满仓提示等弹窗共用一套模板, 也在这里兜一次 —— 整轮失败后
    才执行, 正常路径没有额外开销。
    """
    try:
        if task.find_monthly_card() is not None:
            task.log_info("本轮失败且检测到月卡弹窗, 关闭弹窗后重试")
            task.handle_monthly_card()
            return

        if boxes is not None and task._is_welfare_dialog_open(boxes):
            task.log_warning("本轮失败且检测到低保金弹窗未关闭, 尝试关闭")
            task._close_welfare_dialog(boxes, None)
            return

        if boxes is not None:
            task._dismiss_notice_popup(boxes, None, "本轮失败后")
    except TaskDisabledException:
        raise
    except Exception as e:
        task.log_warning(f"弹窗兜底处理失败: {type(e).__name__}: {e}")


def is_world_screen(task) -> bool:
    """是否被踢回大世界, 复用基类的 in_team_and_world()。

    不能只用 in_world() 判: 小地图箭头走的是 chamfer 打分
    (`0.7*coverage + 0.3*(1 - avg_distance/max_distance)`), 没有「场景饱和」惩罚 ——
    搜索区整片偏亮时每个模板像素的最近亮像素距离都是 0, coverage 与 distance_score
    双双为 1, 纯白画面直接得满分。实测「都市大亨」面板得 1.000、「仪器组合」面板得
    0.841, 都越过 0.75 的阈值, 与真箭头(0.997)分不开; 匹配中的亮色加载帧同理。
    误判的代价很实在: 匹配阶段会被当成掉线, 白白耗掉本轮唯一的回场配额。

    加上 is_in_team() 的组队血条判定就能分开: 真大世界命中(0.939), 都市大亨/仪器组合/
    竞拍结束/黑屏全不命中。这也正是框架自己的定义(见 is_main(in_world=True)), 而
    return_to_auction 第一步的 ensure_main(in_world=True) 本来就要求 is_in_team() ——
    两边保持一致, 才不会出现「判定在大世界, 但 ensure_main 认为不在」。
    """
    try:
        return bool(task.in_team_and_world())
    except TaskDisabledException:
        raise
    except Exception as e:
        task.log_debug(f"大世界判定失败: {type(e).__name__}: {e}")
        return False


def resume_after_world_drop(task, boxes: AuctionBoxes, deadline: float) -> AuctionState:
    """掉线后的统一出口: 本轮回场配额还有就回场并重跑匹配阶段, 否则按本轮失败结束。

    配额由 `_exec_auction_round` 创建(见 `_recover_quota`), 不通过参数逐层传递 ——
    参数会在「确认失败后重新调 `_stage_match`」这条路径上被默认值重置, 使同一轮能反复
    回场。这里扣减配额, 扣完就抛「本轮放弃」。
    """
    if task._recover_quota <= 0:
        raise WaitFailedException("被踢回大世界后再次掉线, 本轮放弃")
    task._recover_quota -= 1
    return task._recover_from_world(boxes, deadline)


def recover_from_world(task, boxes: AuctionBoxes, deadline: float) -> AuctionState:
    """掉线回场: 大世界 → 拍卖主界面, 成功后重新进入匹配阶段。

    网络不稳时点「开始匹配」后会被踢回大世界, 此时拍卖界面的四种状态判定全不命中,
    原逻辑只能空转到 MATCH_TIMEOUT(120 秒) 再按本轮失败处理, 每轮白等两分钟,
    轮次很快就被耗尽。回场成功后重跑 _stage_match, 让本轮接着走完。

    只回场 `RECOVER_MAX_PER_ROUND` 次: 配额在 resume_after_world_drop 里扣减,
    配额用完再掉线就按本轮失败结束 —— 继续重试只会把整轮 deadline 耗光, 留给下一轮
    更合适(轮次之间本来就有间隔)。
    """
    task.log_warning("检测到被踢回大世界, 尝试自动回到拍卖界面")
    task.info_set("当前阶段", "回场中")
    recover_deadline = min(deadline, time.monotonic() + RECOVER_TIMEOUT)
    if not task._return_to_auction(boxes, recover_deadline):
        raise WaitFailedException("被踢回大世界后未能回到拍卖界面")

    venue = task._read_current_venue()
    task.log_info(f"已回到拍卖主界面, 当前会场: {venue or '未识别'}")
    task.info_set("当前阶段", "匹配中")
    return task._stage_match(boxes, deadline)


def return_to_auction(task, boxes: AuctionBoxes, deadline: float) -> bool:
    """按「大世界 → F5 都市大亨 → 都市闲趣 → 即刻落槌」的顺序打开拍卖界面。

    整条路径重试一次: 网络抖动时 F5 或入口点击都可能落空, 重试一次比直接判失败划算,
    但不再多试 —— 每次失败都要重新走一遍面板动画, 会把整轮 deadline 耗光。
    注意 retry_on_action 的 attempt 是「额外重试次数」, 实际执行 attempt + 1 次,
    所以这里传 1 才是「总共两次」。

    掉线时不会有「网络异常」之类的提示弹窗(已确认), 所以不在这里兜弹窗;
    真要是有弹窗, ensure_main 里的月卡/登录处理也覆盖不到, 由整轮失败后的
    blocking_popup 兜底。

    每一步都按「剩余预算」而不是各处写死的常量取超时: ensure_main 在登录态丢失时
    会把 time_out 抬到 600 秒(见 BaseNTETask.ensure_main), 而 RECOVER_TIMEOUT 只有
    90 秒 —— 不把剩余时间传进去, 这一步就能把整个回场预算连同本轮 deadline 一起耗光,
    后面的 F5/入口/落槌根本轮不到执行. 剩余时间耗尽时直接判失败, 交给下一轮重来.
    """

    def action():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            task.log_warning("回场预算已耗尽, 放弃本次回场")
            return False
        try:
            task.ensure_main(in_world=True, time_out=remaining)
            task.openF5panel()
        except TaskDisabledException:
            raise
        except Exception as e:
            task.log_warning(f"打开都市大亨面板失败: {type(e).__name__}: {e}")
            return False

        task.operate_click(*task.pos.panels.f5.hobbies)
        if not task.wait_ocr(
            box=task.box_of_screen(*BOX_CITY_FUN_TITLE),
            match=RE_CITY_FUN,
            time_out=task._timeout_or_zero(deadline, RECOVER_STEP_TIMEOUT),
            raise_if_not_found=False,
            settle_time=0.5,
        ):
            task.log_warning("未检测到「都市闲趣」面板")
            return False
        if not task._click_instant_lot(deadline):
            return False
        return bool(
            task.wait_ocr(
                box=boxes.main_title,
                match=RE_MAIN_TITLE,
                time_out=task._timeout_or_zero(deadline, RECOVER_STEP_TIMEOUT),
                raise_if_not_found=False,
                settle_time=0.5,
            )
        )

    def reset():
        """重试之间的状态复位, 同样受剩余预算约束。

        原来是直接传 self.ensure_main, 即用默认 time_out(登录态丢失时被抬到 600 秒),
        与 action 是同一个漏洞 —— 第二次重试前的复位就能把预算吃干净.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        task.ensure_main(in_world=True, time_out=remaining)

    try:
        return bool(task.retry_on_action(action, reset, attempt=1))
    except TaskDisabledException:
        raise
    except Exception as e:
        task.log_warning(f"回场流程异常: {type(e).__name__}: {e}")
        return False


def click_instant_lot(task, deadline: float) -> bool:
    """在「都市闲趣」面板里找「即刻落槌」卡片并点击。

    「即刻落槌」在面板最后一页, 刚打开时看不到, 所以边滚边找。
    命中后直接点 OCR 框中心 —— 卡片是「上图下标题」, 标题本身就在卡片的点击热区内。
    复用 RE_MAIN_TITLE 是因为卡片名与拍卖主界面标题是同一个词「即刻落槌」。
    """
    cards = task.box_of_screen(*BOX_CITY_FUN_CARDS)
    for _ in range(RECOVER_SCROLL_STEPS):
        if task._wait_operate_click(
            cards,
            RE_MAIN_TITLE,
            task._timeout_or_zero(deadline, 3),
            after_sleep=1,
        ):
            return True
        task.scroll(*POS_CITY_FUN_SCROLL, RECOVER_SCROLL_WHEEL)
        task.sleep(0.5)
    task.log_warning("都市闲趣面板里未找到「即刻落槌」入口")
    return False


def read_current_venue(task) -> str:
    """读拍卖主界面右侧的「当前：XXX场」, 只用于在日志里留痕, 读不出返回空串。"""
    try:
        results = task.ocr(box=task.box_of_screen(*BOX_CURRENT_VENUE), match=RE_CURRENT_VENUE)
    except TaskDisabledException:
        raise
    except Exception as e:
        task.log_debug(f"会场文字读取失败: {type(e).__name__}: {e}")
        return ""
    return "".join(box.name for box in results or []).strip()


def ensure_auction_entry(task, boxes: AuctionBoxes) -> None:
    """每轮开始时确认人已站在拍卖主界面, 不在就按「大世界 → 即刻落槌」补上入口。

    复用掉线回场的同一条路径(return_to_auction), 不新增识别或导航代码:
    用户从大世界直接启动时, 原来的第一轮只能在 _stage_match 里空转到
    MATCH_TIMEOUT(120 秒) 由末尾的大世界兜底触发回场, 首轮白等约一分钟.

    判定顺序: 先读拍卖主界面标题, 命中即已在拍卖界面, 直接开跑;
    未命中再判大世界 —— 不在大世界就不接管, 交给原有流程按界面异常报错,
    避免在登录页/加载页等未知界面上误触发 F5 流程.

    挂在每轮而不是只在启动时调一次, 与 AutoHeistTask._run_loop 的
    「每轮先 ensure_main + 入口判断」一致: 上一轮掉线或异常退出时人可能已经不在
    拍卖界面, 那时后续每轮都要白等 MATCH_TIMEOUT 才由 _stage_match 末尾兜底.
    已在拍卖界面时本方法只多花一次标题 OCR(命中即返回), 正常路径开销可忽略.

    执行位置在 begin_round 之后、_run_single_round 之前, 不消耗轮次内的
    `_recover_quota`(该配额每轮由 _exec_auction_round 重置), 因此不影响
    「每轮掉线可回场一次」的既有约定.
    """
    if task.wait_ocr(
        box=boxes.main_title,
        match=RE_MAIN_TITLE,
        time_out=ENTRY_PROBE_TIMEOUT,
        raise_if_not_found=False,
    ):
        return
    if not task._is_world_screen():
        return
    task.log_info("启动时检测到大世界, 自动进入「即刻落槌」")
    task.info_set("当前阶段", "入场中")
    if not task._return_to_auction(boxes, time.monotonic() + ENTRY_RECOVER_TIMEOUT):
        task.log_warning("启动回场未成功, 交给第一轮按界面异常处理")
