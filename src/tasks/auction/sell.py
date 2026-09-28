"""拍卖藏品出售能力: 模式判定, 间隔与满仓触发, 仓库流程, 品质筛选, 失败升级。

模块函数的第一个参数 task 是 AutoBidAuctionTask 实例: OCR/输入/日志等框架 API
与跨轮状态 (_sell_failures / _inventory_stuck) 都经它访问。出售域的行为常量
由本模块定义, 不再挂回任务类。内部互相调用一律走 task._<方法名>, 让测试的
实例级 mock 与任务侧的统一入口保持生效。

跨轮状态的所有者仍是任务实例: _sell_failures 只按「满仓失败」累积, 成功清零;
_inventory_stuck 置位会让下一轮跳过拍卖先重试清理。置位/清零条件见
run_with_escalation 的注释, 与拆分前逐字一致。
"""

from ok import TaskDisabledException, WaitFailedException

from src.tasks.auction.layout import (
    QUALITY_BOXES,
    RE_COLLECTION_INSUFFICIENT,
    RE_ONE_CLICK_SELL,
    RE_POPUP_CLOSE_HINT,
    RE_SELL_LABEL,
    RE_WAREHOUSE,
    AuctionBoxes,
    PostRoundState,
)
from src.tasks.auction.options import (
    CONF_SELL_INTERVAL,
    QUALITY_KEYS,
    SELL_MODE_INTERVAL,
    SELL_MODE_OFF,
    SELL_MODE_ONE_CLICK,
    SELL_MODES,
)

# 品质圆点每点击一次界面会重绘, 间隔太短时后续点击会落空;
# 勾选后读出售价值校验, 读到 0 或读不出时换帧重读, 最多尝试 SELL_SELECT_RETRIES 次.
# 不能靠重新勾选来重试: 勾选是无条件点击, 再点一次会把刚勾上的品质全部点掉.
SELL_QUALITY_GAP = 0.5
SELL_SELECT_RETRIES = 2

# 「出售价值」在品质圆点刚点完时会短暂变成空白(界面重绘), 只给 1 秒经常读空;
# 读不出时返回 None, 调用方必须按「未确认」处理, 不能当成出售成功.
SELL_VALUE_TIMEOUT = 3

# 库存不足提示条出现时机不定, 给 1 秒容易漏掉(漏掉就不会提前清理, 满仓会卡住).
# 满仓检测的本体是本模块的 detect_inventory_full, 低保观测用的也是这条预算.
INVENTORY_FULL_TIMEOUT = 3

# 出售连续失败到这个次数后放宽出售清单(6 个品质全卖)再试一次: 满仓卖不掉会让后续
# 出价全部失败, 这时候把仓库腾空的优先级高于按低保阶段挑选品质.
SELL_FAILURE_ESCALATE_AFTER = 2

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


def normalize_mode(raw_mode: str) -> str:
    """出售模式取值兜底: 未知值一律按「不出售」处理, 避免脏配置意外清空仓库。"""
    return raw_mode if raw_mode in SELL_MODES else SELL_MODE_OFF


def uses_collection_sell(mode: str) -> bool:
    """本轮是否需要走「藏品仓库」出售流程: 满仓检测 + 品质勾选 + 确认出售。

    「拍卖成功一键出售」用游戏自带的按钮在结算界面直接卖, 不碰仓库, 也不做满仓检测,
    因此不算这条流程 —— 见 on_settlement_screen。
    """
    return mode not in (SELL_MODE_OFF, SELL_MODE_ONE_CLICK)


def is_selection_confirmed(
    selected: int, sell_value: int | None, *, require_sale: bool = False
) -> bool | None:
    """判断品质勾选是否被出售价值证实。

    Args:
        selected: 实际点击勾选的品质数量.
        sell_value: 读到的出售价值, None 表示读不出.
        require_sale: 本次出售是否必须真的清掉藏品(满仓时无法继续出价).

    Returns:
        True: 读到正数, 勾选确实生效; 或本来就没有要出售的品质且不要求出售.
        False: 读到了 0; 或要求出售却一个品质都没勾上.
        None: 读不出(界面重绘中的空白态), 无法判断 —— 调用方必须按「未确认」处理.
    """
    if selected <= 0:
        # 没有勾选任何品质: 本来就不该清掉藏品, 但要求出售时不能算成功 ——
        # 否则会把满仓标记清掉, 仓库一件没腾却报告成功, 之后每轮出价都失败.
        return not require_sale
    if sell_value is None:
        return None
    return sell_value > 0


def detect_inventory_full(task, boxes: AuctionBoxes, timeout: float) -> bool | None:
    """检测主界面的库存不足提示, 命中表示满仓无法继续拍卖。

    这是可选的观测步骤: 没有可用时间时按「未检测」处理(返回 None), 不抛异常,
    否则单轮 deadline 用尽会让整个结算后处理崩掉。

    注意不能返回 False: False 的语义是「确定没满仓」, 写进 PostRoundState 后
    轮次末尾的 run_on_interval 会因为「不是 None」而不再补测, 满仓会被静默漏掉
    —— 之后每轮出价都失败, 却永远不触发清理。返回 None 时调用方(那里 deadline
    为空, 有完整的 INVENTORY_FULL_TIMEOUT 可用)会重新检测。
    """
    if timeout <= 0:
        task.log_debug("满仓检测没有可用时间, 跳过本次检测")
        return None

    found = task.wait_ocr(
        box=boxes.insufficient,
        match=RE_COLLECTION_INSUFFICIENT,
        time_out=timeout,
        settle_time=0.5,
        raise_if_not_found=False,
    )
    if found:
        task.log_info("检测到库存不足提示, 当前处于满仓状态")
    return bool(found)


def on_settlement_screen(task, boxes: AuctionBoxes, deadline: float) -> None:
    """结算界面「一键出售」: 点游戏自带的按钮卖掉本局藏品, 再关掉「获得物品」提示。

    按钮只在拍卖成功(拍到东西)后才出现, 找不到就跳过 —— 流拍时本来就没有可卖的
    东西, 不该让整轮失败。

    这条路径不碰藏品仓库, 也不做满仓检测: 一键出售是游戏自己的整包出售, 没有品质
    勾选, 也读不到「出售价值」, 因此不复用仓库出售流程。

    关提示条分两步: 先用「点击空白区域关闭」确认提示条出现了, 再点提示条以外的
    空白区域 —— 游戏要求点的是空白区域, 不是那句提示文字本身。

    单轮 deadline 用尽时按「没时间」跳过, 而不是抛异常: 结算已经完成, 不该因为
    时间不够把整轮判成失败。
    """
    sell_timeout = task._timeout_or_zero(deadline, ONE_CLICK_SELL_TIMEOUT)
    if sell_timeout <= 0:
        task.log_warning("单轮时间已用尽, 跳过结算界面的「一键出售」")
        return
    if not task._wait_operate_click(
        boxes.one_click_sell, RE_ONE_CLICK_SELL, sell_timeout, after_sleep=1
    ):
        task.log_info("结算界面未出现「一键出售」, 跳过(流拍时没有可出售的藏品)")
        return
    task.log_info("已点击一键出售")

    popup_timeout = task._timeout_or_zero(deadline, POPUP_CLOSE_TIMEOUT)
    if popup_timeout <= 0:
        task.log_warning("单轮时间已用尽, 「获得物品」提示未处理")
        return
    # 只检测不点击: 要关掉提示条, 得点提示条以外的空白区域, 而不是提示文字本身.
    hint = task.wait_ocr(
        box=boxes.popup_close_hint,
        match=RE_POPUP_CLOSE_HINT,
        time_out=popup_timeout,
        raise_if_not_found=False,
        settle_time=0.5,
    )
    if not hint:
        task.log_warning("「获得物品」提示未出现, 退出拍卖前请留意残留弹窗")
        return
    task.operate_click(boxes.popup_blank, after_sleep=0.5)
    task.log_info("已点击空白区域关闭「获得物品」提示")


def run_on_interval(
    task,
    boxes: AuctionBoxes,
    deadline: float | None = None,
    state: PostRoundState | None = None,
) -> None:
    """按出售模式决定本轮是否出售藏品。

    「满仓时清理」只在主界面出现库存不足提示时出售; 「按间隔出售」每 N 轮出售一次,
    且间隔没到就满仓时提前出售(库存不足无法继续出价, 等间隔会卡住拍卖)。
    「不出售」与「拍卖成功一键出售」不走这里: 后者在结算界面就卖完了。

    仅应在拍卖结束回到主界面后调用, 否则仓库入口 OCR 无法命中。
    """
    if not task._uses_collection_sell():
        # 「不出售」不碰仓库; 「一键出售」在结算界面就卖完了(见 on_settlement_screen),
        # 与轮次末尾无关.
        return

    mode = task._sell_mode()
    sell_interval = 0
    if mode == SELL_MODE_INTERVAL:
        sell_interval = task._config_int(
            CONF_SELL_INTERVAL, 0, warn="出售间隔次数配置无效, 按满仓清理处理"
        )
        if sell_interval <= 0:
            # 间隔无效时退化成「满仓时清理」, 而不是直接不出售.
            task.log_warning("出售间隔次数未设置, 本次按满仓清理处理")

    state = state or PostRoundState()
    inventory_full = state.inventory_full
    if inventory_full is None:
        # 结算后观测没测出满仓结论(含当时 deadline 用尽)时在这里补测:
        # 本方法在轮次末尾调用, deadline 为空, 有完整的 INVENTORY_FULL_TIMEOUT 可用。
        inventory_full = task._detect_inventory_full(
            boxes, task._timeout_or_zero(deadline, INVENTORY_FULL_TIMEOUT)
        )

    reached_interval = sell_interval > 0 and task.current_round % sell_interval == 0
    if not (reached_interval or inventory_full):
        return

    if reached_interval:
        task.log_info(f"第 {task.current_round} 轮到达出售间隔 {sell_interval}, 执行定期出售")
    else:
        task.log_info("检测到满仓提示, 提前执行藏品出售")

    task._sell_collections_with_escalation(
        boxes,
        deadline,
        task._sell_qualities(),
        inventory_full=bool(inventory_full),
    )


def run_collections(
    task,
    boxes: AuctionBoxes,
    deadline: float | None = None,
    sell_qualities: list[str] | tuple[str, ...] = (),
    *,
    require_sale: bool = False,
) -> bool | None:
    """尝试出售藏品, deadline 为空时保持定期清理分支的原有行为。

    sell_qualities 是本次要卖掉的品质清单(由调用方按低保阶段算好), 勾选即出售。

    require_sale 表示本次出售必须真的清掉藏品(满仓时无法继续出价)。此时「一个品质
    都没勾上」不能再算成功 —— 那会把满仓标记清掉, 之后每轮出价都失败却不再重试清理。

    Returns:
        True: 出售确认生效(读到正数), 或没有勾选品质且本次不要求出售。
        False: 确认失败: 已勾选品质但出售价值确认为 0(含重勾后仍为 0),
            或要求出售却一个品质都没勾上 —— 这类读数是「没清掉藏品」的直接证据。
        None: 本次尝试没有产生可信证据: 出售价值读不出(确认出售已点击, 结果未知),
            或流程根本没走通(仓库入口/标题未就绪, 流程异常)。出售是否生效未知,
            调用方必须与超时同等对待, 不能当成明确失败。
    """
    task.log_info("开始执行藏品出售流程")
    try:
        # 满仓等提示弹窗会盖住仓库入口, 先兜掉再找入口.
        task._dismiss_notice_popup(boxes, deadline, "出售流程开始前")

        warehouse_button = task._wait_operate_click(
            boxes.warehouse_btn,
            RE_WAREHOUSE,
            task._bounded_timeout(deadline, WAREHOUSE_LOAD_TIMEOUT),
        )
        if not warehouse_button:
            task.log_warning("藏品仓库入口未出现, 取消出售流程")
            return None
        task._bounded_sleep(deadline, 1)
        task.log_debug("藏品仓库入口已点击")

        if not task.wait_ocr(
            box=boxes.warehouse_title,
            match=RE_WAREHOUSE,
            time_out=task._bounded_timeout(deadline, WAREHOUSE_LOAD_TIMEOUT),
            raise_if_not_found=False,
            settle_time=0.5,
        ):
            task.log_warning("藏品仓库界面加载失败, 取消出售流程")
            return None
        task.log_debug("藏品仓库界面加载完成")

        # 上一次出售中途失败会把仓库留在出售模式, 此时「出售」圆钮的位置是「取消」,
        # 再点一次会退出出售模式, 后续品质勾选与确认出售全部落空却不报错.
        if task._is_sell_mode(boxes, task._bounded_timeout(deadline, 1)):
            task.log_warning("藏品仓库已处于出售模式(上次出售未走完), 跳过点击出售")
        else:
            task.operate_click(boxes.sell, after_sleep=0)
            task._bounded_sleep(deadline, 1)

        selected = task._select_quality_filters(deadline, sell_qualities)
        # 残留勾选会让这一遍无条件点击全部取反, _ensure_sell_value 读到 0 时会重勾一次兜住.
        sell_value = task._ensure_sell_value(boxes, deadline, selected, sell_qualities)
        # 只有读到正数才算勾选生效: 读到 0 或读不出(界面重绘中的空白态)都不能算成功,
        # 否则会在毫无证据的情况下打印「藏品出售完成」, 掩盖「一个品质都没勾上」.
        selection_ok = is_selection_confirmed(selected, sell_value, require_sale=require_sale)

        task.operate_click(boxes.confirm_sell, after_sleep=0)
        task._bounded_sleep(deadline, 1.5)
        task.log_debug("已点击确认出售")

        task.operate_click(boxes.blank, after_sleep=0)
        task._bounded_sleep(deadline, 0.5)
        task._close_warehouse(boxes)
        task._bounded_sleep(deadline, 1)

        if selection_ok is None:
            task.log_warning("出售价值未读出, 本次出售是否清掉藏品无法确认")
            return None
        if not selection_ok:
            if selected <= 0:
                task.log_warning("没有勾选任何品质, 本次出售没有清掉任何藏品")
            else:
                task.log_warning("品质勾选未生效, 本次出售没有清掉任何藏品")
            return False
        if selected <= 0:
            task.log_info("没有需要出售的品质, 本次出售未清掉藏品")
            return True
        task.log_info("藏品出售完成")
        return True
    except TaskDisabledException:
        raise
    except WaitFailedException:
        # 单轮超时要向上传播, 但界面得收拾干净再走: 把仓库连同「出售模式 + 已勾选
        # 的品质」留给下一轮, 下次进来会检测到「已在出售模式」而跳过点「出售」,
        # 然后无条件再点一遍同一批品质 —— 全部取反成未勾选, 满仓放宽时还会连带
        # 卖掉用户明确要保留的品质。关掉仓库是唯一不需要勾选态素材就能复位的动作.
        task._close_warehouse(boxes)
        raise
    except Exception as e:
        task.log_warning(f"藏品出售失败: {type(e).__name__}: {e}")
        task._close_warehouse(boxes)
        # 异常点可能在「确认出售」之后, 出售是否已生效未知, 与超时同理返回 None.
        return None


def close_warehouse(task, boxes: AuctionBoxes) -> None:
    """关掉藏品仓库界面, 让出售模式和里面的勾选状态一起复位。

    出售流程无论成功还是异常退出都要走到这一步。关闭按钮是无文字图标(OCR 在所有
    截图上都读到空), 只能按调用点确认含义; 在主界面点它是空操作, 所以异常发生在
    「还没打开仓库」的阶段时也安全。

    点完要确认仓库真的关了(标题消失)再重试一次: 仓库连同「出售模式 + 已勾选的品质」
    留给下一轮时, 下次进来会检测到「已在出售模式」而跳过点「出售」, 然后无条件再点
    一遍同一批品质 —— 全部取反成未勾选, 满仓放宽时还会连带卖掉用户明确保留的品质。
    关不掉时只告警不抛: 这里是异常收尾路径, 再抛异常会盖掉真正的失败原因。
    """
    for attempt in range(1, WAREHOUSE_CLOSE_RETRIES + 1):
        task.operate_click(boxes.close, after_sleep=0.5)
        if not task._is_warehouse_open(boxes):
            return
        task.log_warning(f"第 {attempt}/{WAREHOUSE_CLOSE_RETRIES} 次点击关闭后藏品仓库仍未收起")
    task.log_warning("藏品仓库界面多次尝试后仍未关闭, 下一轮可能受残留勾选影响")


def is_warehouse_open(task, boxes: AuctionBoxes) -> bool:
    """检测藏品仓库界面是否还在, 复用标题区域的 OCR。"""
    return bool(task.ocr(box=boxes.warehouse_title, match=RE_WAREHOUSE, log=False))


def is_sell_mode(task, boxes: AuctionBoxes, timeout: float) -> bool:
    """检测藏品仓库是否已经处于出售模式。

    出售模式下才会出现「出售价值」条, 用它区分初始视图和出售模式;
    初始视图的同一位置是空网格, OCR 不会命中, 因此读不到就按未进入处理。
    """
    return bool(
        task.wait_ocr(
            box=boxes.sell_label,
            match=RE_SELL_LABEL,
            time_out=timeout,
            raise_if_not_found=False,
            settle_time=0.5,
        )
    )


def select_quality_filters(
    task,
    deadline: float | None,
    sell_qualities: list[str] | tuple[str, ...] = (),
) -> int:
    """勾选要出售的品质按钮, 返回实际点击次数。

    勾选即出售: sell_qualities 里的品质点选, 其余一律保留。清单由调用方按当日低保
    阶段算好(见任务侧 _sell_qualities), 这里不再读配置。

    本函数是「无条件点击」: 对同一个品质调用两次会把刚勾上的状态点掉。所以它只在
    进入出售模式后调用一次, 校验读数时不能靠再调一次来重试(那是双重取反)。
    """
    sell = set(sell_qualities)
    clicked = 0
    # 逐项「保留/选择」只留在 DEBUG; INFO 记一次汇总, 避免品质多时每轮刷屏.
    task.log_info(
        f"勾选出售品质: {', '.join(sell_qualities) if sell_qualities else '无'}"
    )

    for quality_name, quality_pos in zip(QUALITY_KEYS, QUALITY_BOXES):
        if quality_name not in sell:
            task.log_debug(f"保留{quality_name}")
            continue
        task.log_debug(f"选择{quality_name}")
        task.operate_click(task.box_of_screen(*quality_pos), after_sleep=0)
        task._bounded_sleep(deadline, SELL_QUALITY_GAP)
        clicked += 1

    return clicked


def ensure_sell_value(
    task,
    boxes: AuctionBoxes,
    deadline: float | None,
    selected: int,
    sell_qualities: list[str] | tuple[str, ...] = (),
) -> int | None:
    """校验品质勾选是否真的生效: 读数没到位时先换帧重读, 仍是 0 才重勾一次。

    sell_qualities 与首次勾选用的是同一批品质, 重勾必须原样传回去。

    品质圆点每点一次界面都会重绘, 间隔太短时后续点击会落空, 表现为只卖掉一种品质,
    而流程依旧会点确认出售并报告成功。这里读「出售价值」来验证: 读到正数说明生效。

    读到 0 和读不出要分开处理, 两者的成因不同:

    - **读不出**(界面重绘期间的空白态, 线上 4 次出售有 3 次是这样)只换帧重读,
      绝不能重勾 —— 重勾会把刚勾上的品质点掉(双重取反), 让「重试」必然失败;
    - **换帧后仍是 0**, 说明这一遍点击之后真的一个品质都没勾上。干净的初始视图点完
      目标集合 T 之后价值必然为正, 所以这种情况多半是仓库里本来就有残留勾选: 上一次
      出售在点「确认出售」之前异常退出, 品质圆点还留在勾选态, 这一遍无条件点击恰好
      把它们全部点掉。此时再点一遍 T 能把状态拉回来 —— 设点击前集合为 S, 则
      「点完 T 后价值为 0」等价于 S 与 T 的对称差里没有一件有藏品的品质, 于是重勾后
      留下的 S 中「有藏品」的部分全部落在 T 内, 只会卖掉本该出售的品质。

    Returns:
        读到正数时返回该数值; 其余情况返回最后一次读数(0 或 None), 由调用方判定。
    """
    if selected <= 0:
        # 没有任何品质需要出售, 不必校验, 也不必花时间读数值.
        return None

    retries = max(SELL_SELECT_RETRIES, 1)
    value: int | None = None
    for attempt in range(retries):
        if attempt > 0:
            # 必须换帧: 两次读取落在同一帧上会读到同样的空白值.
            task.next_frame()
            task._bounded_sleep(deadline, SELL_QUALITY_GAP)
        value = task._read_sell_value(boxes, task._bounded_timeout(deadline, SELL_VALUE_TIMEOUT))
        if value is None:
            # 界面重绘中的空白态, 重读一次再放弃.
            task.log_warning("出售价值未读出, 无法确认品质勾选是否生效")
            continue
        if value > 0:
            task.log_info(f"品质勾选生效, 出售价值 {value}")
            return value
        task.log_warning(f"已勾选 {selected} 个品质但出售价值为 {value}, 换帧后重读")

    if value == 0:
        # 换帧重读后仍是 0, 才按「残留勾选被这一遍点掉」处理. 只重勾一次:
        # 若本来就是干净的初始视图(目标品质都没藏品), 重勾得到空集, 与不重勾
        # 的结果一样(都卖不掉), 不会更糟; 无限重试没有意义.
        task.log_warning("换帧重读后出售价值仍为 0, 按残留勾选被点掉处理, 重新勾选一次")
        task._select_quality_filters(deadline, sell_qualities)
        value = task._read_sell_value(boxes, task._bounded_timeout(deadline, SELL_VALUE_TIMEOUT))
        if value is None:
            task.log_warning("重新勾选后出售价值未读出, 本次出售是否生效无法确认")
        elif value <= 0:
            task.log_warning(f"重新勾选后出售价值仍为 {value}, 本次出售不会清掉任何藏品")
        else:
            task.log_info(f"重新勾选后品质勾选生效, 出售价值 {value}")
    return value


def read_sell_value(task, boxes: AuctionBoxes, timeout: float) -> int | None:
    """读取出售模式下的「出售价值」数值, 未识别或解析失败时返回 None。"""
    return task._read_asset_value(boxes.sell_value, timeout, "出售价值")


def run_with_escalation(
    task,
    boxes: AuctionBoxes,
    deadline: float | None,
    sell_qualities: list[str] | tuple[str, ...],
    *,
    inventory_full: bool,
) -> bool:
    """执行藏品出售, 连续失败且满仓时放宽出售清单再试一次。

    满仓卖不掉会让后续出价全部失败, 所以连续失败后优先把仓库腾空 —— 6 个品质
    全部出售, 不再按低保阶段挑选。成功一次就清零计数。

    非满仓的失败多半是界面重绘导致的读数抖动, 此时放宽会白白卖掉用户明确要
    保留的品质, 而收益为零 —— 所以只在满仓时放宽, **也只在满仓失败时累积计数**。

    计数必须按「满仓失败」累积, 不能让非满仓失败把它填满: 阈值是「满仓连续失败
    几次后放宽」, 若非满仓的抖动也计入, 阈值会被历史抖动提前填满, 之后满仓的
    第一次失败就立刻放宽, 把用户明确保留的品质一起卖掉(实测: 非满仓失败 5 次后,
    紧接一次满仓失败即触发, escalated 集合是 6 个品质全卖)。

    出售超时(预算耗尽抛 WaitFailedException)与「没有可信证据」的返回(None)同等
    对待: 两者的确认出售都可能已经生效, 结果未知。把这种结果当成满仓失败会连累
    两处: 放宽品质(可能卖掉用户明确保留的品质)被无证据的失败提前触发; 且
    _inventory_stuck 一旦被误置, 下一轮会直接跳过拍卖并记一次失败(见
    _run_single_round), 仓库其实已空时还会反复触发 —— 2026-09-23 线上 4 次出售
    有 3 次读不出「出售价值」, 这条路径不是小概率。但满仓时 None 仍要置
    _inventory_stuck: 仓库若真的还满, 不置位的话下一轮会去空烧匹配阶段,
    永远轮不到清理 —— 置位才有重试清理的机会。

    「满仓」前提的失效出口: 以放宽集合(6 个品质全勾)出售且要求出售时, 出售价值
    被**确认**读到 0(重勾后仍为 0), 说明仓库里已经没有任何可出售藏品 —— 满仓
    结论必然过时(最常见: 上一轮出售结果未确认, 实际已清空仓库)。此时清掉
    _inventory_stuck 恢复正常拍卖, 不再无限重试「满仓清理」。若判定有误(品质
    点击全部落空才会如此), 下一轮出价会再次触发满仓提示并重新走清理, 不会更糟。

    计数达到阈值后以放宽集合开局(use_escalated): 否则第一次调用就超时的话,
    放宽分支永远走不到。
    """
    escalated = sorted(set(sell_qualities) | set(QUALITY_KEYS))
    # 已经达到放宽阈值时直接用放宽集合开局: 满仓耗尽 SELL_TIMEOUT 会让第一次调用就抛
    # 异常, 永远走不到下面的放宽分支, 计数累到阈值也没有用.
    use_escalated = inventory_full and task._sell_failures >= SELL_FAILURE_ESCALATE_AFTER
    if use_escalated:
        task.log_warning(f"藏品出售已连续 {task._sell_failures} 次未完成, 直接放宽出售清单")

    try:
        sold = task._sell_collections(
            boxes,
            deadline,
            escalated if use_escalated else sell_qualities,
            require_sale=inventory_full,
        )
    except WaitFailedException as e:
        # 结果未知, 不动计数也不置 _inventory_stuck: 收尾的 _bounded_sleep 在点完
        # confirm_sell 之后也会抛, 那次出售可能已经生效. 误置 _inventory_stuck 会让
        # 下一轮跳过拍卖并记一次失败, 而仓库其实已空时还会反复触发.
        task.log_warning(f"藏品出售超出预算, 结果未知, 不计入放宽计数: {e}")
        raise
    if sold:
        task._sell_failures = 0
        task._inventory_stuck = False
        return True

    if sold is None:
        # 确认出售可能已点击(读不出)或流程未走通, 是否清掉藏品未知: 不计入放宽
        # 计数, 避免无证据的失败驱动放宽(会把要保留的品质一起卖掉); 满仓时置位
        # 让下一轮先重试清理, 而不是对着满仓空烧匹配阶段.
        task.log_warning("藏品出售结果未知, 不计入放宽计数")
        task._inventory_stuck = inventory_full
        return False

    if not inventory_full:
        # 非满仓失败只是读数抖动, 不为将来的满仓放宽积攒「信用」.
        task.log_warning("藏品出售未完成 (非满仓, 不计入放宽计数)")
        task._inventory_stuck = False
        return False

    if use_escalated:
        # 放宽集合(6 个品质全勾)确认读数为 0: 仓库没有任何可出售藏品, 满仓前提
        # 已失效, 恢复正常拍卖. 不计失败: 没有东西可卖不是清理失败.
        task.log_warning("放宽出售清单确认读数为 0, 仓库已无可出售藏品, 满仓前提失效")
        task._inventory_stuck = False
        return False

    task._sell_failures += 1
    if task._sell_failures < SELL_FAILURE_ESCALATE_AFTER:
        # 本次失败用的是未放宽清单, 还轮不到放宽.
        task.log_warning(f"藏品出售未完成 (满仓连续 {task._sell_failures} 次)")
        task._inventory_stuck = inventory_full
        return False

    task.log_warning(f"藏品出售连续 {task._sell_failures} 次未完成, 放宽出售清单重试一次")
    try:
        escalated_sold = task._sell_collections(boxes, deadline, escalated, require_sale=True)
    except WaitFailedException as e:
        # 同上一处: 放宽后的这次出售是否生效同样无法确认, 保持计数与 _inventory_stuck
        # 不变, 交给下一轮的实测结论决定.
        task.log_warning(f"放宽出售清单后的出售超出预算, 结果未知: {e}")
        raise
    if escalated_sold:
        task.log_info("放宽出售清单后出售成功")
        task._sell_failures = 0
        task._inventory_stuck = False
        return True

    if escalated_sold is None:
        # 放宽后的结果未知: 首次失败已有明确证据(计数保留), 置位让下一轮继续清理;
        # 那时以放宽集合开局, 读到确认读数后走成功或上面的失效出口.
        task.log_warning("放宽出售清单后的出售结果未知, 不计入放宽计数")
        task._inventory_stuck = inventory_full
        return False

    task.log_warning("放宽出售清单确认读数为 0, 仓库已无可出售藏品, 满仓前提失效")
    task._inventory_stuck = False
    return False
