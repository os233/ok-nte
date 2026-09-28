"""拍卖界面契约: OCR 正则, 界面状态类型与 UI 区域常量。

本模块只承载纯声明, 不依赖任务类。坐标一律是相对屏幕比例, 区域经
AuctionBoxes 一次性构造后传给流程方法 (见 AutoBidAuctionTask._build_boxes),
不要将 OCR 裁框混入 src/scene/PanelPosition.py 或通用 PositionMap。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from ok import Box

# --- 拍卖界面 OCR 正则 ---
# 集中定义, 避免各阶段重复编译同一规则。
RE_MATCH = re.compile(r"开始匹配|开始|匹配")
RE_CONFIRM = re.compile(r"确\s*认")
RE_BID = re.compile(r"出\s*价")
# 数字键盘弹出后的出价界面判定。
# 键盘弹窗会盖住 BOX_BID 所在区域(实测该框 all_boxes 全空), 此时界面上只剩弹窗自己的
# 元素 —— 右下角的「出价」按钮和「放弃」都在弹窗外或不可见, 用 RE_BID 判定会永远为假,
# 于是空转到 60 秒报「等待出价界面超时」(2026-09-23 18:44 实测一次)。这里收弹窗上的
# 固定文案作为补充特征; 这些字串只出现在出价键盘/面板上, 放宽不会误命中其他界面。
RE_BID_PANEL = re.compile(r"出\s*价|请输入你愿意出的价格|推荐出价参考|上轮出价|清空")
RE_SKIP = re.compile(r"跳\s*过")
RE_EXIT = re.compile(r"退\s*出")
RE_BID_CONFIRM = re.compile(r"确认出价")
RE_BID_PANEL_READY = re.compile(r"确认出价|[0-9]")
RE_NUMBER = re.compile(r"[0-9\uff10-\uff19,]+")
# 价格输入区未输入时显示 "可输入范围0~<资产>" 提示, 同样能被 RE_NUMBER 命中, 不能当作价格.
RE_PRICE_HINT = re.compile(r"[~\uff5e\u4e00-\u9fff]")
RE_MAIN_TITLE = re.compile(r"即刻落槌")
RE_COLLECTION_INSUFFICIENT = re.compile(r"少于200格")
RE_WELFARE = re.compile(r"低保金")
# 弹窗正文「今日已领取次数：N/5」。这是「今日低保是否领完」的权威读数, 直接决定
# 能不能放开出售, 因此不再依赖「弹窗里还有没有领取按钮」这类间接信号。
RE_WELFARE_COUNTER = re.compile(r"次数\s*[：:]\s*([0-9\uff10-\uff19]+)\s*/\s*([0-9\uff10-\uff19]+)")
RE_CLAIM = re.compile(r"领取")
RE_CANCEL = re.compile(r"取消")
RE_WAREHOUSE = re.compile(r"藏品仓库")
RE_SELL_LABEL = re.compile(r"出售\s*价值")
# 结算界面右下角游戏自带的「一键出售」圆钮(图标 + 文字), 拍卖成功后才出现.
# 首字「一」是单笔画, 检测模型在裁剪偏紧时会直接丢掉它(实测只读出「键出售」),
# 所以两种写法都收 —— 这个区域里只有这一个按钮, 放宽不会误命中别的东西.
RE_ONE_CLICK_SELL = re.compile(r"一键出售|键出售")
# 一键出售后弹出的「获得物品」提示条, 底部写着这句, 点提示条以外的空白区域即可关闭.
# 只匹配前半句: 整句较长, 尾部被 OCR 认坏时仍要能命中.
RE_POPUP_CLOSE_HINT = re.compile(r"点击空白")
# 掉线回场路径上的两个标志: 「都市闲趣」MENU 面板标题, 以及拍卖入口卡片「即刻落槌」。
RE_CITY_FUN = re.compile(r"都市闲趣")
# 拍卖主界面右侧的会场文字, 形如「当前：海贝场」。回场后用它核对会场有没有被重置。
RE_CURRENT_VENUE = re.compile(r"当前")

# 全角数字与全角逗号统一转半角, 用于统一资产与价格的 OCR 文本。
# 逗号必须一起转: _is_partial_number_text / _has_inconsistent_grouping 靠
# re.sub(r"[^\d,]", "") 保留半角逗号来识别「首位漏读 / 分组不自洽」, 全角逗号「，」
# 不在保留范围里会被当噪声删掉, 「，643」就此洗成「643」, 两条残缺读数防线同时失效。
FULLWIDTH_NUMERIC = str.maketrans("０１２３４５６７８９，", "0123456789,")

# 数字键盘上一次点击即可输入的快捷键, 需优先于逐位输入。
PAD_SHORTCUTS = ("0000", "00")


class AuctionState(Enum):
    """单轮拍卖过程中可检测到的界面状态。"""

    CONFIRM = "confirm"
    BID = "bid"
    SKIP = "skip"
    # 掉线被踢回大世界: 界面既不在拍卖流程里, 也不在主界面上, 需要先回场再继续。
    WORLD = "world"


@dataclass(frozen=True)
class AuctionBoxes:
    """单轮拍卖使用的 UI 区域, 在轮次开始时按相对比例一次性构建。"""

    match: Box
    confirm: Box
    bid: Box
    bid_keypad: Box
    skip_area: Box
    exit: Box
    bid_confirm: Box
    abandon: Box
    abandon_confirm: Box
    asset_value: Box
    estimate: Box
    last_bid: Box
    clear: Box
    price_result: Box
    price_result_keypad: Box
    exception_area: Box
    main_title: Box
    main_asset: Box
    insufficient: Box
    welfare_btn: Box
    welfare_dialog: Box
    welfare_counter: Box
    claim: Box
    cancel: Box
    warehouse_btn: Box
    warehouse_title: Box
    sell: Box
    confirm_sell: Box
    sell_label: Box
    sell_value: Box
    blank: Box
    close: Box
    one_click_sell: Box
    popup_close_hint: Box
    popup_blank: Box


@dataclass(frozen=True)
class PostRoundState:
    """结算后回到主界面时的观测结果, 供轮次末尾的出售决策复用。

    inventory_full 为 None 表示本轮未检测满仓状态, 需要时由调用方补测。
    observed 表示本轮是否真的执行过结算后观测: _finish_auction 在主界面标题没
    识别到时会提前返回, 此时画面状态未知, 轮次末尾不能再按「已回到主界面」去
    点仓库入口, 否则只会在错误的界面上白等超时。
    """

    inventory_full: bool | None = None
    observed: bool = False


# --- UI 坐标 (相对比例) ---
# 主界面按钮.
BOX_MATCH = (0.7427, 0.8972, 0.8360, 0.9472)  # 开始匹配
BOX_CONFIRM = (0.535, 0.633, 0.666, 0.681)  # 确认按钮
BOX_BID = (0.882, 0.913, 0.930, 0.953)  # 出价按钮
# 数字键盘弹窗的文字区。键盘弹窗整体覆盖约 0.30~0.90 / 0.44~0.80, 会盖住 BOX_BID,
# 导致「是否已进入出价界面」判定永远为假。这个区域只取弹窗右侧的文字部分
# (「请输入你愿意出的价格」等), 避开数字键, 用于键盘态下的界面判定。
BOX_BID_KEYPAD = (0.578, 0.560, 0.800, 0.720)  # 键盘弹窗文字区
BOX_SKIP_AREA = (0.703, 0.902, 0.807, 0.953)  # 跳过区域
BOX_EXIT = (0.853, 0.900, 0.961, 0.949)  # 退出拍卖
BOX_BID_CONFIRM = (0.649, 0.868, 0.726, 0.911)  # 确认出价

# 出价面板.
BOX_ABANDON = (0.7276, 0.9083, 0.7833, 0.9583)  # 放弃按钮
BOX_ABANDON_CONFIRM = (0.5474, 0.6389, 0.6714, 0.6861)  # 放弃确认
BOX_ASSET_VALUE = (0.8583, 0.0426, 0.9870, 0.0806)  # 出价面板资产
# 出价面板当前估价.
# 实测数字右端最靠右的一局是 0.9052 (price_result.png 的 "22,684"), 另一局 13,875 在
# 0.9010; 再往右的「我的资产」数值右端在 0.9594。右边界放在 0.9200, 即在估价数字右端
# 外留出约 0.015 (1080p 约 28px, 2160p 约 57px) 的余量, 同时与资产数值左端保持距离.
#   - 不能 < 0.9052: 会把估价末位数字裁掉 (2026-09-23 的 "2,643 读成 ,643").
#   - 不能 > 0.9594: 会把「我的资产」数值一起圈进来.
# 缩到 0.9200 的意义: 即使「估价」标签这一帧没读出来, 区域里也只剩估价一个数字,
# `_read_estimate_value` 按标签过滤失败时不会退化成「拼接整段数字」.
# 数字的实际筛选仍以「估价」标签右边沿为准 (见 _read_estimate_value).
BOX_ESTIMATE = (0.7780, 0.1330, 0.9200, 0.1820)  # 出价面板当前估价, 不覆盖我的资产

BOX_LAST_BID = (0.473, 0.733, 0.546, 0.807)  # 上轮出价
BOX_CLEAR = (0.488, 0.859, 0.533, 0.917)  # 清除按钮
# 键盘未弹出时的「可输入范围0~N」提示区, 用于判断价格是否尚未输入.
BOX_PRICE_RESULT = (0.588, 0.685, 0.783, 0.747)  # 输入价格结果
# 键盘弹出后价格输入框移到键盘右侧, 上面那个矩形会落在提示文案「可输入范围0~N」上,
# 导致校验永远读到提示文案而报「未输入」。这个区域是键盘态的输入框本体, 校验优先用它。
BOX_PRICE_RESULT_KEYPAD = (0.588, 0.665, 0.790, 0.700)  # 键盘弹出后的输入价格结果
BOX_EXCEPTION_AREA = (0.579, 0.641, 0.634, 0.681)  # 异常确认框

# 主界面 / 结算.
# 主界面左上角标题「即刻落槌」, 与藏品仓库标题同一个槽位(界面切换后文字才变),
# 因此坐标与 BOX_WAREHOUSE_TITLE 一致.
BOX_MAIN_TITLE = (0.058, 0.032, 0.130, 0.081)  # 主界面标题: 即刻落槌
BOX_MAIN_ASSET = (0.670, 0.025, 0.830, 0.095)  # 主界面资产数值
BOX_INSUFFICIENT = (0.240, 0.467, 0.747, 0.536)  # 库存不足提示

# --- 掉线回场 (大世界 → 拍卖主界面) ---
# 「即刻落槌」是「都市闲趣」里的一个玩法卡片, 入口路径固定:
# 大世界按 F5 打开「都市大亨」面板 → 点「都市闲趣」→ 在 MENU 面板里找「即刻落槌」卡片。
# 「都市闲趣」入口坐标走位置表 self.pos.panels.f5.hobbies (光环中心, 见 PanelPosition);
# 下面是「都市闲趣」MENU 子面板内部的区域, 属任务私有, 不进位置表。
POS_CITY_FUN_SCROLL = (0.500, 0.550)  # 子面板内容区中部, 滚动落点
# 子面板标题「MENU 都市闲趣」。它和都市大亨面板上的「都市闲趣」入口同名, 但位置不重叠
# (入口文字在 0.48~0.56/0.39~0.47), 用区域就能区分两个界面。
BOX_CITY_FUN_TITLE = (0.10, 0.09, 0.42, 0.20)
BOX_CITY_FUN_CARDS = (0.08, 0.22, 0.87, 0.88)  # 卡片区, 「即刻落槌」在最后一页
BOX_CURRENT_VENUE = (0.630, 0.550, 0.800, 0.598)  # 主界面「当前：XXX场」

# 低保金.
BOX_WELFARE_BTN = (0.8266, 0.0398, 0.8984, 0.0778)
BOX_WELFARE_DIALOG = (0.4400, 0.3050, 0.5650, 0.3620)  # 弹窗标题
# 「今日已领取次数：N/5」整行(标签 + 数值)。必须连标签一起框: 孤立的小号数字
# (如 "0/5") 检测模型读不出来, 整行框实测 1/2/3/4/6 五个缩放档都能读出 0/5。
# 上下边界夹在「当前资产」行与按钮行之间, 只有约 20px 余量, 改前先在
# ok_templates/22.png、43.png(两张真实低保金弹窗截图)上复验。
BOX_WELFARE_COUNTER = (0.4100, 0.5150, 0.5950, 0.5820)
BOX_CLAIM = (0.576, 0.636, 0.632, 0.685)
BOX_CANCEL = (0.370, 0.637, 0.421, 0.684)

# 藏品仓库.
BOX_WAREHOUSE_BTN = (0.2109, 0.8583, 0.2740, 0.9713)
BOX_WAREHOUSE_TITLE = (0.058, 0.032, 0.130, 0.081)
BOX_SELL = (0.931, 0.860, 0.949, 0.900)
BOX_CONFIRM_SELL = (0.862, 0.863, 0.886, 0.917)
BOX_BLANK = (0.442, 0.851, 0.564, 0.917)
BOX_CLOSE = (0.950, 0.045, 0.963, 0.073)

# 出售模式 (点击出售圆钮后才出现): 用「出售价值」条区分初始视图与出售模式,
# 初始视图的同一位置是空网格, OCR 不会命中.
BOX_SELL_LABEL = (0.673, 0.862, 0.722, 0.895)  # 「出售价值」标签
# 出售价值条必须整条一起识别: 检测模型看不到孤立的小号数字, 只框数值区域读不到 "0",
# 而 "0" 恰好是「一个品质都没勾上」的判据. 右边界停在圆钮之前, 避免图标被误读成数字.
BOX_SELL_VALUE = (0.650, 0.830, 0.840, 0.950)  # 「出售价值」整条 (含标签)

# 结算界面: 拍卖成功后的游戏自带「一键出售」圆钮, 以及它触发的「获得物品」提示条.
# 实测(1920x1080, 用户 09-19 截图): 按钮组 px(1678,843)-(1758,920) ——
# 圆钮 px(1682,843)-(1755,905), 文字「一键出售」px(1670,901)-(1758,929).
# ⚠️ 框必须比文字本身宽松: 贴着文字裁时, 首字「一」是单笔画, 检测模型会直接丢掉它,
# 只读出「键出售」, 正则随之失配. 实测同一张图放大 2 倍能读出、3/4 倍读不出 ——
# 这是检测的抖动, 不能靠「试到一次成功」就收工, 必须留够余量.
# 框中心 px(1717,875) 落在圆钮内(圆钮中心 px(1718,874)): 点圆钮比点文字稳.
BOX_ONE_CLICK_SELL = (0.86198, 0.75463, 0.92708, 0.86574)
# 「获得物品」提示条底部的「点击空白区域关闭」, 只用来确认提示条出现了.
# 提示条本体实测 px(353,383)-(1574,685), 这句提示在 px(853,937)-(1079,977).
BOX_POPUP_CLOSE_HINT = (0.44010, 0.79630, 0.56250, 0.91204)
# 真正要点的「空白区域」: 提示条下沿 px685 与提示文字上沿 px937 之间都是纯背景,
# 取 px(960,790). 不能直接点 OCR 命中的提示文字 —— 用户要求点的是空白区域,
# 而且 _wait_operate_click 点的正是 OCR 命中的那个文字框.
BOX_POPUP_BLANK = (0.47396, 0.69444, 0.52604, 0.76852)

# 品质圆点区域 (白, 绿, 蓝, 紫, 橙, 红), 与 options.QUALITY_KEYS 一一对应.
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


def build_boxes(screen: Callable[..., Box]) -> AuctionBoxes:
    """按相对比例一次性构建单轮拍卖使用的全部 UI 区域。

    screen 是任务侧的 `box_of_screen`: 接受相对比例元组, 返回屏幕实际 Box。
    区域数值全部来自本模块上方的 BOX_* 常量, 任务类不再保留副本。
    """
    return AuctionBoxes(
        match=screen(*BOX_MATCH),
        confirm=screen(*BOX_CONFIRM),
        bid=screen(*BOX_BID),
        bid_keypad=screen(*BOX_BID_KEYPAD),
        skip_area=screen(*BOX_SKIP_AREA),
        exit=screen(*BOX_EXIT),
        bid_confirm=screen(*BOX_BID_CONFIRM),
        abandon=screen(*BOX_ABANDON),
        abandon_confirm=screen(*BOX_ABANDON_CONFIRM),
        asset_value=screen(*BOX_ASSET_VALUE),
        estimate=screen(*BOX_ESTIMATE),
        last_bid=screen(*BOX_LAST_BID),
        clear=screen(*BOX_CLEAR),
        price_result=screen(*BOX_PRICE_RESULT),
        price_result_keypad=screen(*BOX_PRICE_RESULT_KEYPAD),
        exception_area=screen(*BOX_EXCEPTION_AREA),
        main_title=screen(*BOX_MAIN_TITLE),
        main_asset=screen(*BOX_MAIN_ASSET),
        insufficient=screen(*BOX_INSUFFICIENT),
        welfare_btn=screen(*BOX_WELFARE_BTN),
        welfare_dialog=screen(*BOX_WELFARE_DIALOG),
        welfare_counter=screen(*BOX_WELFARE_COUNTER),
        claim=screen(*BOX_CLAIM),
        cancel=screen(*BOX_CANCEL),
        warehouse_btn=screen(*BOX_WAREHOUSE_BTN),
        warehouse_title=screen(*BOX_WAREHOUSE_TITLE),
        sell=screen(*BOX_SELL),
        confirm_sell=screen(*BOX_CONFIRM_SELL),
        sell_label=screen(*BOX_SELL_LABEL),
        sell_value=screen(*BOX_SELL_VALUE),
        blank=screen(*BOX_BLANK),
        close=screen(*BOX_CLOSE),
        one_click_sell=screen(*BOX_ONE_CLICK_SELL),
        popup_close_hint=screen(*BOX_POPUP_CLOSE_HINT),
        popup_blank=screen(*BOX_POPUP_BLANK),
    )
