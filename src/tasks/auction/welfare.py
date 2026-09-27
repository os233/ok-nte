"""拍卖低保金与结算后处理: 弹窗领取, 次数读数, 跨日重置, 结算观测编排。

模块函数的第一个参数 task 是 AutoBidAuctionTask 实例: OCR/输入/日志等框架 API、
计时常量与当日领取记录 (_welfare_day / _welfare_claims_today /
_welfare_daily_limit) 都经它访问; 记录的所有者仍是任务实例, 由弹窗读数整体覆盖。
内部互相调用一律走 task._<方法名>, 让测试的实例级 mock 与任务侧的统一入口保持生效。

与出售策略的连接是显式的: 出售清单按「今日低保是否领完」切换(任务侧
_sell_qualities 读 quota_exhausted 的结论), 观测编排只负责把满仓/资产观测和
领取流程按正确顺序走完, 不替出售做决策。
"""

from datetime import datetime, timedelta

from ok import TaskDisabledException, WaitFailedException

from src.tasks.auction.layout import (
    FULLWIDTH_NUMERIC,
    RE_CANCEL,
    RE_CLAIM,
    RE_MAIN_TITLE,
    RE_WELFARE,
    RE_WELFARE_COUNTER,
    AuctionBoxes,
    PostRoundState,
)


def quota_exhausted(claims_today: int, daily_limit: int | None) -> bool:
    """今日低保次数是否已用尽(今天再也领不到了)。

    没读到过弹窗读数时一律返回 False。两个方向的代价不对称: 判成「没领完」只是
    少卖几件藏品; 判成「领完了」却会在还能领低保的时候追加出售, 把资产抬过
    10 万线, 低保就领不到了 —— 所以拿不准时往保守方向兜。
    """
    if daily_limit is None:
        return False
    return claims_today >= daily_limit


def rollover_day(task) -> None:
    """跨过游戏每日刷新时刻时清空当日低保领取记录。

    按 5 点切分而不是自然日午夜: 游戏每日刷新在 5 点(与 src/config.py 的
    「Monthly Card Time」默认值、BaseNTETask 算 next_monthly_card_start 用的
    是同一个小时)。按午夜切会让 0~5 点这段被当成新的一天, 方向是「以为还能领 →
    不追加出售」, 虽然保守, 但会让这几轮白等一次弹窗读数。
    """
    day = (datetime.now() - timedelta(hours=task.WELFARE_RESET_HOUR)).date()
    if task._welfare_day == day:
        return

    task._welfare_day = day
    task._welfare_claims_today = 0
    task._welfare_daily_limit = None


def read_counter(task, boxes: AuctionBoxes, deadline: float | None = None) -> None:
    """读弹窗正文的「今日已领取次数：N/5」, 刷新当日已领次数与上限。

    领取流程里会读两次: 打开弹窗后读一次(此时的值用于本轮出售清单决策), 点击
    领取后再读一次(刷新为领取后的权威读数, 同时纠正点击落空造成的虚计)。

    读数**整体覆盖**本地累计值(而不是取较大者): 弹窗是权威来源, 覆盖能让
    「本地多记了一次」在下次读数时自愈。读不出时保持原值 —— 「今日已领完」
    是放开出售的开关, 读不到就必须保守。

    用 `task.ocr(match=None)` 拿区域内全部文本, 而不是 `wait_ocr(match=...)`:
    后者按 match 过滤返回值。实测这两张截图上「标签 + 数值」被识别成同一个框,
    两种取法等价; 但检测模型把两者拆成两框时, 过滤会把标签丢掉, 只剩 "0/5"
    没有可解析的整行 —— 固定用 match=None 拼回整行, 不赌识别粒度。

    换帧重读一次而不是只读一帧: 弹窗淡入途中那一帧可能是空白, 而这次读数一旦
    落空, 资产涨过 10 万后弹窗就不再打开, 当天再也读不到(阶段永远停在「未领完」,
    追加出售静默失效)。多花 0.3 秒换掉这个静默失效是划算的。
    """
    found = None
    for attempt in range(1, task.WELFARE_COUNTER_READS + 1):
        texts = [box.name for box in task.ocr(box=boxes.welfare_counter, match=None)]
        found = RE_WELFARE_COUNTER.search("".join(texts).translate(FULLWIDTH_NUMERIC))
        if found is not None:
            break
        if attempt < task.WELFARE_COUNTER_READS:
            task.next_frame()
            task._bounded_sleep(deadline, task.WELFARE_COUNTER_RETRY_GAP)

    if found is None:
        task.log_debug("低保金领取次数读数失败, 保持上次记录")
        return

    task._welfare_claims_today = int(found.group(1))
    task._welfare_daily_limit = int(found.group(2))
    task.log_info(f"今日已领取低保 {task._welfare_claims_today}/{task._welfare_daily_limit} 次")


def is_dialog_open(task, boxes: AuctionBoxes) -> bool:
    """检测低保金弹窗是否仍留在界面上。

    标题与取消按钮任一命中即认为弹窗存在, 避免只有其一被识别时误判为已关闭。
    """
    if task.ocr(box=boxes.welfare_dialog, match=RE_WELFARE):
        return True
    return bool(task.ocr(box=boxes.cancel, match=RE_CANCEL))


def close_dialog(task, boxes: AuctionBoxes, deadline: float | None) -> bool:
    """关闭低保金弹窗, 领取成功与否都必须执行。

    每日次数用尽时弹窗没有领取按钮, 只点领取的旧逻辑会把弹窗留在界面上,
    后续所有阶段的识别都会被挡住。这里以界面特征判定弹窗是否还在, 反复点击取消,
    直到弹窗消失或重试次数用尽。
    """
    for attempt in range(1, task.WELFARE_CLOSE_RETRIES + 1):
        if not task._is_welfare_dialog_open(boxes):
            task.log_info("低保金弹窗已关闭")
            return True

        task._wait_click_optional(boxes.cancel, RE_CANCEL, deadline, 3, "取消按钮")
        task._bounded_sleep(deadline, 0.5)

        if not task._is_welfare_dialog_open(boxes):
            task.log_info("低保金弹窗已关闭")
            return True

        task.log_warning(
            f"第 {attempt}/{task.WELFARE_CLOSE_RETRIES} 次点击取消后低保金弹窗仍未关闭"
        )

    task.log_warning("低保金弹窗多次尝试后仍未关闭")
    return False


def try_claim(task, boxes: AuctionBoxes, deadline: float | None = None) -> bool:
    """尝试领取每日低保金, deadline 为空时保持原有独立超时行为。

    低保金是可选的附加流程, 按钮未出现(如当日已领取)或弹窗异常时只跳过本次领取;
    只有单轮超时才向上传播, 避免拖垮已经成功的拍卖轮次。

    每日次数用尽(如 5/5)时界面仍会打开弹窗但没有领取按钮, 此时必须继续关闭弹窗,
    否则弹窗会一直盖住拍卖界面, 让后续所有阶段都识别不到。
    """
    try:
        task.log_info("执行低保金领取流程")
        if not task._wait_click_optional(boxes.welfare_btn, RE_WELFARE, deadline, 5, "低保金按钮"):
            return False
        task._bounded_sleep(deadline, 0.5)

        # 弹窗已经打开了, 顺手把「今日已领取次数：N/5」读回来: 这是「今日低保是否
        # 领完」的唯一权威读数, 也是轮次末尾要不要追加出售的依据。
        task._read_welfare_counter(boxes, deadline)

        if task._wait_click_optional(boxes.claim, RE_CLAIM, deadline, 5, "领取按钮"):
            task._bounded_sleep(deadline, 0.5)
            task.log_info("已点击领取按钮")
            # 点击已发出不代表领取已生效: 盲目 +1 会在点击落空时虚增当日次数,
            # 提前按「已领完」放开出售, 抬高资产后弹窗不再打开, 当天剩下的低保
            # 就领不到 —— 而且这次读数之后再无弹窗, 计数无法自愈。弹窗还开着,
            # 直接重读权威读数; 读不到(弹窗领取后自动关闭等)就保持领取前的值:
            # 少记一次只是少卖几件藏品, 多记一次却会让剩下的低保领不到。
            task._read_welfare_counter(boxes, deadline)
        else:
            task.log_info("未检测到领取按钮(今日次数可能已用尽), 直接关闭低保金弹窗")

        if not task._close_welfare_dialog(boxes, deadline):
            task.log_warning("低保金弹窗未关闭, 跳过本次领取的后续确认")
            return False

        task.log_info("低保金领取完成")
        return True
    except TaskDisabledException:
        raise
    except WaitFailedException:
        raise
    except Exception as e:
        task.log_warning(f"低保金领取失败: {type(e).__name__}: {e}")
        return False


def claim_if_needed(task, boxes: AuctionBoxes, deadline: float, asset_value: int | None) -> bool:
    """主界面资产低于阈值时领取低保金, 返回是否成功领取。

    asset_value 由调用方通过 observe_main_asset 读出后传入: 资产观测与低保金领取
    拆开后, 两者的可用时间互相独立, 一次 OCR 的读数也只采信一次。
    """
    if asset_value is None:
        task.log_warning("资产值识别失败, 跳过本次低保金领取")
        return False

    if asset_value >= task.WELFARE_ASSET_THRESHOLD:
        task.log_info(f"资产达到{task.WELFARE_ASSET_THRESHOLD}, 跳过低保金领取")
        return False

    task.log_info(f"资产低于{task.WELFARE_ASSET_THRESHOLD}, 执行低保金领取")
    return task._try_claim_welfare(boxes, deadline)


def observe_main_asset(task, boxes: AuctionBoxes, deadline: float) -> int | None:
    """读取主界面资产值, 记录到本地历史, 返回数值(未读出时 None)。

    独立于低保金领取: 资产历史记录是用户要的长期观测, 不依赖「启用辅助功能」里
    是否勾选低保金。两者合在一个方法里时, 用户取消勾选低保金会让整个资产记录
    静默停摆 —— 任务运行完全正常, 只是数据一条都不写, 极难发现。

    资产读取属于可选的观测步骤, 和满仓检测一样用 _timeout_or_zero:
    单轮时间用尽时只表示这次没测到, 不该抛 WaitFailedException —— 那会把已经成功
    结算的轮次判成失败, 而且调用方写回观测结果的那一步会被跳过, 连出售也一并丢失。
    """
    timeout = task._timeout_or_zero(deadline, task.ASSET_OBSERVE_TIMEOUT)
    if timeout <= 0:
        task.log_debug("资产观测没有可用时间, 跳过本次读取")
        return None

    # 使用数字 match, 避免漏识别单字符数值 0.
    asset_value = task._read_asset_value(boxes.main_asset, timeout)
    if asset_value is None:
        task.log_warning("资产值识别失败, 跳过本次观测")
        return None

    task.log_info(f"当前资产: {asset_value}")
    return asset_value


def observe_post_round(task, boxes: AuctionBoxes, deadline: float) -> None:
    """已经回到主界面时的结算后观测。

    _stage_result 的「返回匹配界面」分支不会走 _finish_auction —— 那里要点「跳过
    动画」和「退出拍卖」, 而这两个按钮在已经回到主界面的情况下并不存在。但结算后
    观测必须照做: 少了它, _post_round_state.observed 保持 False, 轮次末尾就不出售,
    于是满仓时后续每轮都卡在「开始匹配」上(点一次弹一次「库存不足」), 而
    _inventory_stuck 永远不会置位 —— 「满仓时清理」在这条路径上完全失效。

    仍以主界面标题为准再动手: 只有确认画面是拍卖主界面才做观测, 否则会去点不存在
    的仓库入口白等超时。
    """
    title_timeout = task._timeout_or_zero(deadline, 5)
    if title_timeout <= 0:
        return
    if not task.wait_ocr(
        box=boxes.main_title,
        match=RE_MAIN_TITLE,
        time_out=title_timeout,
        raise_if_not_found=False,
        settle_time=0.5,
    ):
        task.log_warning("主界面「即刻落槌」标题未识别, 跳过本轮结算后处理")
        return

    task.log_info("主界面加载完成")
    task._run_post_round_actions(boxes, deadline)


def run_post_round_actions(task, boxes: AuctionBoxes, deadline: float) -> None:
    """结算后回到主界面时的辅助操作: 观测满仓状态并领取低保金。

    库存不足提示位于屏幕中部, 会被低保金弹窗遮挡, 因此必须在打开弹窗之前检测。

    这里只观测并把结果写入 _post_round_state, 是否出售由轮次末尾的
    _sell_collections_on_interval 统一决定。两处都动手会让同一轮卖两次: 第二次
    面对已被卖空的仓库读到「出售价值 0」, 白白累计失败次数, 最终触发「放宽保留
    品质」把用户明确要保留的藏品一起卖掉。
    """
    # 满仓等提示弹窗会盖住库存不足提示条, 先兜掉再观测, 否则满仓永远检测不到.
    task._dismiss_notice_popup(boxes, deadline, "结算后主界面")

    # 未检测到一律保持 None(而不是 False): None 表示「本轮未测出结论」, 轮次末尾的
    # _sell_collections_on_interval 会在那里(deadline 为空, 有完整超时预算)补测一次。
    # 写成 False 等于宣称「确定没满仓」, 会把满仓静默漏掉。
    inventory_full: bool | None = None
    if task._uses_collection_sell():
        # 观测步骤没有可用时间时返回 None, 不抛异常.
        inventory_full = task._detect_inventory_full(
            boxes, task._timeout_or_zero(deadline, task.INVENTORY_FULL_TIMEOUT)
        )

    # 资产观测无条件执行, 与低保金开关无关: 这是独立的长期记录功能。
    # 观测失败(未读出)只返回 None, 不影响后续低保金与出售流程。
    asset_value = task._observe_main_asset(boxes, deadline)

    # 领取结果不再参与出售决策: 追加出售看的是「今日低保是否领完」(任务级状态,
    # 由弹窗读数维护), 不是「本轮有没有领到」。这里只负责把领取流程走完。
    if task._assist_enabled(task.ASSIST_WELFARE):
        try:
            task._claim_welfare_if_needed(boxes, deadline, asset_value)
        except TaskDisabledException:
            raise
        except WaitFailedException as e:
            # 低保金领取是可选的收尾动作, 单轮时间用尽时只跳过本次领取.
            # 让它传播出去会把已经结算成功的轮次判成失败, 而且本方法写回观测结果
            # 的那一行会被跳过 —— _post_round_state.observed 保持 False, 轮次末尾
            # 连带跳过出售.
            task.log_warning(f"低保金领取超时, 跳过本次领取: {e}")

    task._post_round_state = PostRoundState(
        inventory_full=inventory_full,
        observed=True,
    )
